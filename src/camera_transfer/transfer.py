"""File transfer engine with checksum verification.

Handles copying files from camera cards to destination volumes with:
- Configurable destination folder structure
- Real-time progress tracking
- Post-transfer checksum verification
- Transfer logs and reports
"""

import json
import shutil
import time
from pathlib import Path
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from .camera_cards import (
    CameraCard, CameraRoll, MediaFile, SoundCard,
    get_all_transferable_files, ContentType,
)
from .checksum import (
    ChecksumResult,
    HashAlgorithm,
    compute_checksum,
    verify_transfer,
)


class TransferStatus(Enum):
    """Status of a file transfer."""
    PENDING = "pending"
    COPYING = "copying"
    VERIFYING = "verifying"
    VERIFIED = "verified"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class FileTransferRecord:
    """Record of a single file transfer."""
    source_path: Path
    dest_path: Path
    relative_path: str
    size_bytes: int
    content_type: str = "video"      # "video", "sound", "sidecar", "metadata"
    captured_at: Optional[float] = None  # source file mtime (capture timestamp)
    transferred_at: Optional[float] = None  # when the copy completed
    status: TransferStatus = TransferStatus.PENDING
    source_checksum: Optional[str] = None
    dest_checksum: Optional[str] = None
    checksums_match: bool = False
    error: Optional[str] = None
    start_time: float = 0.0
    end_time: float = 0.0

    @property
    def duration(self) -> float:
        if self.start_time and self.end_time:
            return self.end_time - self.start_time
        return 0.0

    @property
    def speed_mbps(self) -> float:
        if self.duration > 0:
            return (self.size_bytes / (1024 * 1024)) / self.duration
        return 0.0


@dataclass
class TransferJob:
    """A complete transfer job (one camera card to one destination)."""
    card: CameraCard
    destination_root: Path
    volume_title: str
    records: list[FileTransferRecord] = field(default_factory=list)
    algorithm: HashAlgorithm = HashAlgorithm.XXHASH
    status: TransferStatus = TransferStatus.PENDING
    start_time: float = 0.0
    end_time: float = 0.0

    @property
    def total_bytes(self) -> int:
        return sum(r.size_bytes for r in self.records)

    @property
    def transferred_bytes(self) -> int:
        return sum(
            r.size_bytes for r in self.records
            if r.status in (TransferStatus.VERIFIED, TransferStatus.VERIFYING)
        )

    @property
    def progress(self) -> float:
        if self.total_bytes == 0:
            return 0.0
        return self.transferred_bytes / self.total_bytes

    @property
    def verified_count(self) -> int:
        return sum(1 for r in self.records if r.status == TransferStatus.VERIFIED)

    @property
    def failed_count(self) -> int:
        return sum(1 for r in self.records if r.status == TransferStatus.FAILED)

    @property
    def total_files(self) -> int:
        return len(self.records)


def build_destination_path(
    dest_root: Path,
    volume_title: str,
    source_mount: Path,
    file_path: Path,
    camera_roll_name: str = "",
) -> Path:
    """Build the destination path for a file, preserving camera card structure.

    Destination structure:
        dest_root/
        └── <volume_title>/
            └── <camera_roll_name>/
                └── (original file structure within the roll)

    If no roll name, files go directly under volume_title.
    """
    # Get relative path from mount point
    try:
        rel_path = file_path.relative_to(source_mount)
    except ValueError:
        rel_path = Path(file_path.name)

    if camera_roll_name:
        return dest_root / volume_title / camera_roll_name / rel_path
    else:
        return dest_root / volume_title / rel_path


def prepare_transfer_job(
    card: CameraCard,
    destination_root: Path,
    volume_title: str,
    algorithm: HashAlgorithm = HashAlgorithm.XXHASH,
) -> TransferJob:
    """Prepare a transfer job by mapping all source files to destination paths.

    Args:
        card: Parsed camera card.
        destination_root: Root of destination volume.
        volume_title: Human-readable title for the folder name.
        algorithm: Checksum algorithm to use.

    Returns:
        A TransferJob ready to execute.
    """
    job = TransferJob(
        card=card,
        destination_root=destination_root,
        volume_title=volume_title,
        algorithm=algorithm,
    )

    for roll in card.rolls:
        # Media files
        for mf in roll.files:
            dest = build_destination_path(
                destination_root, volume_title, card.mount_point, mf.path, roll.name
            )
            record = FileTransferRecord(
                source_path=mf.path,
                dest_path=dest,
                relative_path=str(mf.path.relative_to(card.mount_point)),
                size_bytes=mf.size_bytes,
                content_type=mf.content_type.value,
                captured_at=_get_capture_time(mf.path),
            )
            job.records.append(record)

        # Sidecar files
        for sf in roll.sidecar_files:
            try:
                size = sf.stat().st_size
            except OSError:
                size = 0
            dest = build_destination_path(
                destination_root, volume_title, card.mount_point, sf, roll.name
            )
            record = FileTransferRecord(
                source_path=sf,
                dest_path=dest,
                relative_path=str(sf.relative_to(card.mount_point)),
                size_bytes=size,
                content_type="sidecar",
                captured_at=_get_capture_time(sf),
            )
            job.records.append(record)

    # Also include top-level metadata directories
    meta_dirs = ["ARRI", "Metadata", "Proxy"]
    for dirname in meta_dirs:
        meta_path = card.mount_point / dirname
        if meta_path.is_dir():
            for f in meta_path.rglob("*"):
                if f.is_file():
                    try:
                        size = f.stat().st_size
                    except OSError:
                        size = 0
                    dest = build_destination_path(
                        destination_root, volume_title, card.mount_point, f
                    )
                    record = FileTransferRecord(
                        source_path=f,
                        dest_path=dest,
                        relative_path=str(f.relative_to(card.mount_point)),
                        size_bytes=size,
                        content_type="metadata",
                        captured_at=_get_capture_time(f),
                    )
                    job.records.append(record)

    return job


def _get_capture_time(file_path: Path) -> Optional[float]:
    """Get the capture/creation time of a file (mtime as best proxy)."""
    try:
        stat = file_path.stat()
        # Prefer birth time (st_birthtime) on macOS, fall back to mtime
        return getattr(stat, "st_birthtime", stat.st_mtime)
    except OSError:
        return None


def prepare_sound_transfer_job(
    card: SoundCard,
    destination_root: Path,
    volume_title: str,
    algorithm: HashAlgorithm = HashAlgorithm.XXHASH,
) -> TransferJob:
    """Prepare a transfer job for a sound recorder card.

    Routes all audio files to the destination, preserving folder structure.

    Args:
        card: Parsed sound card.
        destination_root: Root of destination (typically <project>/SOUND).
        volume_title: Human-readable title for the folder name.
        algorithm: Checksum algorithm to use.
    """
    # Create a lightweight CameraCard wrapper so TransferJob works
    camera_card = CameraCard(
        volume_name=card.volume_name,
        mount_point=card.mount_point,
        camera_type=card.recorder_type,
        rolls=card.rolls,
    )

    job = TransferJob(
        card=camera_card,
        destination_root=destination_root,
        volume_title=volume_title,
        algorithm=algorithm,
    )

    for roll in card.rolls:
        for mf in roll.files:
            dest = build_destination_path(
                destination_root, volume_title, card.mount_point, mf.path, roll.name
            )
            record = FileTransferRecord(
                source_path=mf.path,
                dest_path=dest,
                relative_path=str(mf.path.relative_to(card.mount_point)),
                size_bytes=mf.size_bytes,
                content_type="sound",
                captured_at=_get_capture_time(mf.path),
            )
            job.records.append(record)

        for sf in roll.sidecar_files:
            try:
                size = sf.stat().st_size
            except OSError:
                size = 0
            dest = build_destination_path(
                destination_root, volume_title, card.mount_point, sf, roll.name
            )
            record = FileTransferRecord(
                source_path=sf,
                dest_path=dest,
                relative_path=str(sf.relative_to(card.mount_point)),
                size_bytes=size,
                content_type="sidecar",
                captured_at=_get_capture_time(sf),
            )
            job.records.append(record)

    return job


def execute_transfer(
    job: TransferJob,
    on_file_start=None,
    on_file_complete=None,
    on_progress=None,
    on_verify_start=None,
    on_verify_complete=None,
) -> TransferJob:
    """Execute a prepared transfer job.

    Copies all files, then verifies checksums.

    Callbacks:
        on_file_start(record, file_index, total_files)
        on_file_complete(record, file_index, total_files)
        on_progress(bytes_copied, total_bytes)
        on_verify_start(record, file_index, total_files)
        on_verify_complete(record, file_index, total_files, match)
    """
    job.start_time = time.time()
    job.status = TransferStatus.COPYING
    total = len(job.records)

    # Phase 1: Copy all files
    for i, record in enumerate(job.records):
        if on_file_start:
            on_file_start(record, i, total)

        record.start_time = time.time()
        record.status = TransferStatus.COPYING

        try:
            # Create destination directory
            record.dest_path.parent.mkdir(parents=True, exist_ok=True)

            # Copy file preserving metadata
            shutil.copy2(str(record.source_path), str(record.dest_path))

            record.transferred_at = time.time()
            record.status = TransferStatus.VERIFYING
        except (OSError, shutil.Error) as e:
            record.status = TransferStatus.FAILED
            record.error = str(e)
            record.end_time = time.time()

        if on_file_complete:
            on_file_complete(record, i, total)

    # Phase 2: Verify checksums
    job.status = TransferStatus.VERIFYING
    for i, record in enumerate(job.records):
        if record.status == TransferStatus.FAILED:
            continue

        if on_verify_start:
            on_verify_start(record, i, total)

        src_result, dst_result, match = verify_transfer(
            record.source_path, record.dest_path, job.algorithm
        )

        record.source_checksum = src_result.hex_digest
        record.dest_checksum = dst_result.hex_digest
        record.checksums_match = match
        record.end_time = time.time()

        if match:
            record.status = TransferStatus.VERIFIED
        else:
            record.status = TransferStatus.FAILED
            if src_result.error:
                record.error = f"Source checksum error: {src_result.error}"
            elif dst_result.error:
                record.error = f"Dest checksum error: {dst_result.error}"
            else:
                record.error = "Checksum mismatch"

        if on_verify_complete:
            on_verify_complete(record, i, total, match)

    # Final status
    job.end_time = time.time()
    if job.failed_count > 0:
        job.status = TransferStatus.FAILED
    else:
        job.status = TransferStatus.VERIFIED

    return job


def generate_transfer_report(job: TransferJob) -> dict:
    """Generate a JSON-serializable report of a completed transfer job."""
    report = {
        "volume_title": job.volume_title,
        "source_volume": job.card.volume_name,
        "camera_type": job.card.camera_type.value,
        "destination": str(job.destination_root),
        "algorithm": job.algorithm.value,
        "status": job.status.value,
        "start_time": job.start_time,
        "end_time": job.end_time,
        "duration_seconds": job.end_time - job.start_time if job.end_time else 0,
        "total_files": job.total_files,
        "verified_files": job.verified_count,
        "failed_files": job.failed_count,
        "total_bytes": job.total_bytes,
        "files": [],
    }

    for record in job.records:
        file_entry = {
            "source": str(record.source_path),
            "destination": str(record.dest_path),
            "relative_path": record.relative_path,
            "size_bytes": record.size_bytes,
            "content_type": record.content_type,
            "status": record.status.value,
            "source_checksum": record.source_checksum,
            "dest_checksum": record.dest_checksum,
            "checksums_match": record.checksums_match,
            "speed_mbps": round(record.speed_mbps, 2),
        }
        if record.captured_at:
            file_entry["captured_at"] = record.captured_at
            file_entry["captured_at_human"] = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(record.captured_at)
            )
        if record.transferred_at:
            file_entry["transferred_at"] = record.transferred_at
            file_entry["transferred_at_human"] = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(record.transferred_at)
            )
        if record.error:
            file_entry["error"] = record.error
        report["files"].append(file_entry)

    return report


def save_transfer_report(job: TransferJob, report_dir: Optional[Path] = None) -> Path:
    """Save transfer report as JSON file.

    Saves to the destination volume under a _transfer_reports/ directory.
    """
    if report_dir is None:
        report_dir = job.destination_root / job.volume_title / "_transfer_reports"

    report_dir.mkdir(parents=True, exist_ok=True)

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    filename = f"transfer_{job.card.volume_name}_{timestamp}.json"
    report_path = report_dir / filename

    report = generate_transfer_report(job)

    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)

    # Also write per-folder metadata tags
    save_folder_metadata(job)

    return report_path


def save_folder_metadata(job: TransferJob):
    """Write _transfer_metadata.json into each destination folder.

    Each metadata file records:
    - When files were transferred
    - When files were originally captured (if available)
    - Whether content is video footage or sound
    - Source device information
    """
    # Group records by destination folder
    folder_records: dict[Path, list[FileTransferRecord]] = {}
    for record in job.records:
        folder = record.dest_path.parent
        folder_records.setdefault(folder, []).append(record)

    for folder, records in folder_records.items():
        # Determine the dominant content type for this folder
        content_types = set(r.content_type for r in records)
        if "video" in content_types:
            folder_type = "video"
        elif "sound" in content_types:
            folder_type = "sound"
        else:
            folder_type = list(content_types)[0] if content_types else "unknown"

        # Collect capture time range
        capture_times = [r.captured_at for r in records if r.captured_at]
        transfer_times = [r.transferred_at for r in records if r.transferred_at]

        metadata = {
            "content_type": folder_type,
            "source_device": job.card.volume_name,
            "device_type": job.card.camera_type.value,
            "file_count": len(records),
            "total_bytes": sum(r.size_bytes for r in records),
        }

        if transfer_times:
            earliest = min(transfer_times)
            latest = max(transfer_times)
            metadata["transferred_at"] = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(earliest)
            )
            if earliest != latest:
                metadata["transfer_completed_at"] = time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(latest)
                )

        if capture_times:
            earliest = min(capture_times)
            latest = max(capture_times)
            metadata["captured_earliest"] = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(earliest)
            )
            metadata["captured_latest"] = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(latest)
            )

        metadata["files"] = []
        for r in records:
            entry = {
                "filename": r.dest_path.name,
                "content_type": r.content_type,
                "size_bytes": r.size_bytes,
            }
            if r.captured_at:
                entry["captured_at"] = time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(r.captured_at)
                )
            if r.transferred_at:
                entry["transferred_at"] = time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(r.transferred_at)
                )
            if r.source_checksum:
                entry["checksum"] = r.source_checksum
            metadata["files"].append(entry)

        meta_path = folder / "_transfer_metadata.json"
        folder.mkdir(parents=True, exist_ok=True)
        with open(meta_path, "w") as f:
            json.dump(metadata, f, indent=2)
