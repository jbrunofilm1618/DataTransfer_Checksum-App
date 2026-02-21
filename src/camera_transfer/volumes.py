"""Volume detection and monitoring for macOS.

Detects mounted volumes, identifies camera cards (RED / ARRI),
and watches for new mounts/unmounts.
"""

import os
import re
import subprocess
import json
import time
from pathlib import Path
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# RED camera magazine volume name pattern: letter + 3 digits + underscore + camera ID
# Examples: A005_02208H, B001_1234AB, C002_5678CD
RED_VOLUME_NAME_RE = re.compile(r'^[A-Z]\d{3}_[A-Za-z0-9]+$')


class VolumeType(Enum):
    """Classification of a mounted volume."""
    RED_KOMODO = "red_komodo"
    RED_DSMC2 = "red_dsmc2"
    ARRI_ALEXA_MINI = "arri_alexa_mini"
    ARRI_ALEXA35 = "arri_alexa35"
    ARRI_AMIRA = "arri_amira"
    SOUND_DEVICES = "sound_devices"
    ZOOM_RECORDER = "zoom_recorder"
    TASCAM_RECORDER = "tascam_recorder"
    SOUND_RECORDER = "sound_recorder"
    GENERIC_STORAGE = "generic_storage"
    UNKNOWN = "unknown"


# Audio file extensions recognized as production sound
SOUND_EXTENSIONS = {".wav", ".bwf", ".aiff", ".aif", ".mp3", ".flac", ".ogg", ".m4a", ".aac"}

# Video file extensions (used to distinguish sound-only volumes)
VIDEO_EXTENSIONS = {".r3d", ".ari", ".mxf", ".mov", ".mp4", ".m4v"}


@dataclass
class Volume:
    """Represents a mounted volume."""
    name: str
    mount_point: Path
    volume_type: VolumeType
    total_bytes: int = 0
    free_bytes: int = 0
    used_bytes: int = 0
    filesystem: str = ""
    is_camera_card: bool = False
    is_sound_device: bool = False
    camera_rolls: list[str] = field(default_factory=list)

    @property
    def total_gb(self) -> float:
        return self.total_bytes / (1024 ** 3)

    @property
    def used_gb(self) -> float:
        return self.used_bytes / (1024 ** 3)

    @property
    def free_gb(self) -> float:
        return self.free_bytes / (1024 ** 3)


def get_mounted_volumes() -> list[Volume]:
    """Get all currently mounted volumes under /Volumes."""
    volumes = []
    volumes_root = Path("/Volumes")

    if not volumes_root.exists():
        return volumes

    for entry in volumes_root.iterdir():
        if not entry.is_dir() or entry.is_symlink():
            continue

        vol = _inspect_volume(entry)
        if vol:
            volumes.append(vol)

    return volumes


def _inspect_volume(mount_point: Path) -> Optional[Volume]:
    """Inspect a single mount point and classify it."""
    try:
        stat = os.statvfs(mount_point)
        total = stat.f_frsize * stat.f_blocks
        free = stat.f_frsize * stat.f_bavail
        used = total - free
    except OSError:
        return None

    name = mount_point.name
    vol_type = _classify_volume(mount_point)
    is_camera = vol_type not in (
        VolumeType.GENERIC_STORAGE, VolumeType.UNKNOWN,
        VolumeType.SOUND_DEVICES, VolumeType.ZOOM_RECORDER,
        VolumeType.TASCAM_RECORDER, VolumeType.SOUND_RECORDER,
    )
    is_sound = vol_type in (
        VolumeType.SOUND_DEVICES, VolumeType.ZOOM_RECORDER,
        VolumeType.TASCAM_RECORDER, VolumeType.SOUND_RECORDER,
    )

    vol = Volume(
        name=name,
        mount_point=mount_point,
        volume_type=vol_type,
        total_bytes=total,
        free_bytes=free,
        used_bytes=used,
        is_camera_card=is_camera,
        is_sound_device=is_sound,
    )

    if is_camera:
        vol.camera_rolls = _find_camera_rolls(mount_point, vol_type)

    return vol


def _classify_volume(mount_point: Path) -> VolumeType:
    """Classify a volume based on its directory structure and file contents.

    Uses limited-depth searches (not rglob) so large destination drives
    with previously-transferred footage aren't misclassified as camera cards.
    """
    # RED DSMC2: .RDC directories at root or up to 2 levels deep
    rdc_dirs = _glob_limited(mount_point, "*.RDC", max_depth=2, dirs_only=True)
    if rdc_dirs:
        return VolumeType.RED_DSMC2

    # RED: .R3D files at root or up to 2 levels deep
    r3d_files = _glob_limited(mount_point, "*.R3D", max_depth=2)
    if r3d_files:
        return VolumeType.RED_KOMODO

    # RED ProRes: volume name matches RED magazine naming AND has video files
    # RED mags are named like A005_02208H (letter + 3 digits + _ + camera ID)
    if RED_VOLUME_NAME_RE.match(mount_point.name):
        video_files = _glob_limited_multi(
            mount_point, ["*.mov", "*.MOV", "*.mp4", "*.MP4"], max_depth=2
        )
        if video_files:
            return VolumeType.RED_DSMC2
        # Even without video, a RED-named volume with any non-system files is a RED card
        for entry in mount_point.iterdir():
            if entry.is_file() and not entry.name.startswith("."):
                return VolumeType.RED_DSMC2

    # ARRI: look for ARRI-specific folder structures
    # ALEXA 35 uses a structure with ARRIRAW or ProRes folders
    if (mount_point / "ARRIRAW").is_dir() or (mount_point / "ARRI").is_dir():
        # Check for ALEXA 35 markers
        alexa35_markers = list(mount_point.glob("**/ALEXA35*")) + \
                          list(mount_point.glob("**/*.mxf"))
        if alexa35_markers:
            return VolumeType.ARRI_ALEXA35
        return VolumeType.ARRI_ALEXA_MINI

    # ARRI ALEXA Mini / Mini LF: .mxf or .ari files in specific structure
    mxf_files = list(mount_point.glob("**/*.mxf"))
    ari_files = list(mount_point.glob("**/*.ari"))
    if mxf_files or ari_files:
        # Check folder structure for ARRI patterns
        for parent in set(f.parent for f in mxf_files + ari_files):
            if any(p in str(parent) for p in ["ARRI", "A0", "B0", "C0"]):
                return VolumeType.ARRI_ALEXA_MINI

    # ARRI Amira: similar to Mini but may have AMIRA in metadata
    if list(mount_point.glob("**/AMIRA*")):
        return VolumeType.ARRI_AMIRA

    # --- Sound recorder detection ---
    sound_type = _classify_sound_device(mount_point)
    if sound_type is not None:
        return sound_type

    # Check if it's a usable storage volume (has significant space)
    try:
        stat = os.statvfs(mount_point)
        total = stat.f_frsize * stat.f_blocks
        if total > 1_000_000_000:  # > 1 GB
            return VolumeType.GENERIC_STORAGE
    except OSError:
        pass

    return VolumeType.UNKNOWN


def _glob_limited(
    root: Path, pattern: str, max_depth: int = 2, dirs_only: bool = False
) -> list[Path]:
    """Glob for a pattern at limited depth (0 to max_depth levels).

    Avoids rglob which can be slow on large volumes and may find
    previously-transferred files deep in project folder structures.
    """
    results = []
    for depth in range(max_depth + 1):
        prefix = "/".join(["*"] * depth) + "/" if depth > 0 else ""
        for p in root.glob(f"{prefix}{pattern}"):
            if dirs_only and not p.is_dir():
                continue
            results.append(p)
    return results


def _glob_limited_multi(
    root: Path, patterns: list[str], max_depth: int = 2
) -> list[Path]:
    """Glob for multiple patterns at limited depth."""
    results = []
    for pattern in patterns:
        results.extend(_glob_limited(root, pattern, max_depth))
    return results


def _find_camera_rolls(mount_point: Path, vol_type: VolumeType) -> list[str]:
    """Find individual camera rolls / clips on a volume."""
    rolls = []

    if vol_type in (VolumeType.RED_DSMC2, VolumeType.RED_KOMODO):
        # RED: each .RDC folder is a roll
        rdc_dirs = _glob_limited(mount_point, "*.RDC", max_depth=2, dirs_only=True)
        if rdc_dirs:
            rolls = [d.stem for d in sorted(rdc_dirs)]
        else:
            # Group video files by RED clip prefix (A005_C001)
            video_files = _glob_limited_multi(
                mount_point,
                ["*.R3D", "*.mov", "*.MOV"],
                max_depth=2,
            )
            prefixes = set()
            for f in video_files:
                parts = f.stem.split("_")
                if len(parts) >= 2:
                    prefixes.add(f"{parts[0]}_{parts[1]}")
            rolls = sorted(prefixes)

    elif vol_type in (VolumeType.ARRI_ALEXA_MINI, VolumeType.ARRI_ALEXA35, VolumeType.ARRI_AMIRA):
        # ARRI: look for clip directories (A001C001_*, etc.)
        for pattern in ["**/*.mxf", "**/*.ari"]:
            for f in mount_point.glob(pattern):
                parts = f.stem.split("_")
                if parts:
                    rolls.append(parts[0])
        rolls = sorted(set(rolls))

    return rolls


def _classify_sound_device(mount_point: Path) -> Optional[VolumeType]:
    """Classify a volume as a sound recording device if applicable.

    Detects:
    - Sound Devices (MixPre, 7-series, 8-series): WAV/BWF files, often with
      iXML metadata. May have a flat structure or scene/take folders.
    - Zoom (F6, F8, H6, etc.): Creates ZOOM0001/ folders with WAV files.
      Root may contain a ZOOM_* marker folder.
    - Tascam (DR-series): Creates MUSIC/ folder or numbered folders with WAV files.
    - Generic sound: Any volume with audio files and no video files.

    Returns VolumeType or None if not a sound device.
    """
    # Collect audio and video files (limit depth to avoid slow scans)
    audio_files = []
    for ext in SOUND_EXTENSIONS:
        audio_files.extend(mount_point.glob(f"*{ext}"))
        audio_files.extend(mount_point.glob(f"*/*{ext}"))
        audio_files.extend(mount_point.glob(f"*/*/*{ext}"))

    if not audio_files:
        return None

    # Check for video files — if present, this is not a sound-only device
    has_video = False
    for ext in VIDEO_EXTENSIONS:
        if list(mount_point.glob(f"*{ext}")) or list(mount_point.glob(f"*/*{ext}")):
            has_video = True
            break
    if has_video:
        return None

    # Sound Devices: look for iXML metadata in WAV or "SoundDevices" markers
    sd_markers = list(mount_point.glob("**/SoundDevices*")) + \
                 list(mount_point.glob("**/Sound Devices*")) + \
                 list(mount_point.glob("**/*.SD2"))
    if sd_markers:
        return VolumeType.SOUND_DEVICES

    # Sound Devices MixPre: typically names files with take/scene patterns
    # and creates a flat structure with .wav files containing BWF/iXML
    mixpre_markers = list(mount_point.glob("**/MixPre*"))
    if mixpre_markers:
        return VolumeType.SOUND_DEVICES

    # Zoom recorders: characteristic ZOOM#### folders
    zoom_folders = list(mount_point.glob("ZOOM[0-9][0-9][0-9][0-9]"))
    zoom_markers = list(mount_point.glob("ZOOM_*")) + list(mount_point.glob("**/ZOOM*"))
    if zoom_folders or zoom_markers:
        return VolumeType.ZOOM_RECORDER

    # Tascam recorders: MUSIC folder or TASCAM markers
    tascam_markers = list(mount_point.glob("MUSIC")) + \
                     list(mount_point.glob("**/TASCAM*")) + \
                     list(mount_point.glob("**/DR-*"))
    if tascam_markers:
        return VolumeType.TASCAM_RECORDER

    # Generic: volume has audio files but no video — treat as sound recorder
    return VolumeType.SOUND_RECORDER


def get_destination_volumes() -> list[Volume]:
    """Get volumes suitable as transfer destinations (non-camera storage)."""
    all_vols = get_mounted_volumes()
    return [v for v in all_vols if v.volume_type == VolumeType.GENERIC_STORAGE]


def get_camera_volumes() -> list[Volume]:
    """Get volumes identified as camera cards."""
    all_vols = get_mounted_volumes()
    return [v for v in all_vols if v.is_camera_card]


def get_sound_volumes() -> list[Volume]:
    """Get volumes identified as sound recording devices."""
    all_vols = get_mounted_volumes()
    return [v for v in all_vols if v.is_sound_device]


def get_media_volumes() -> list[Volume]:
    """Get all media source volumes (camera cards and sound devices)."""
    all_vols = get_mounted_volumes()
    return [v for v in all_vols if v.is_camera_card or v.is_sound_device]


# ---------- Project folder management ----------

PROJECT_SUBFOLDERS = ["ASSETS", "FOOTAGE", "DOCUMENTATION", "SOUND", "EXPORTS"]


@dataclass
class Project:
    """Represents a project folder on a destination volume."""
    name: str
    path: Path
    has_footage: bool = False
    has_assets: bool = False
    has_documentation: bool = False
    has_sound: bool = False
    has_exports: bool = False

    @property
    def is_complete(self) -> bool:
        """Check if the project has all standard subfolders."""
        return all([
            self.has_footage, self.has_assets, self.has_documentation,
            self.has_sound, self.has_exports,
        ])

    @property
    def existing_subfolders(self) -> list[str]:
        """List which standard subfolders exist."""
        folders = []
        if self.has_assets:
            folders.append("ASSETS")
        if self.has_footage:
            folders.append("FOOTAGE")
        if self.has_documentation:
            folders.append("DOCUMENTATION")
        if self.has_sound:
            folders.append("SOUND")
        if self.has_exports:
            folders.append("EXPORTS")
        return folders


def scan_projects(volume_path: Path) -> list[Project]:
    """Scan a volume for existing project folders.

    A project folder is identified by containing at least one of the
    standard subfolders (ASSETS, FOOTAGE, DOCUMENTATION, SOUND, EXPORTS).
    """
    projects = []

    if not volume_path.exists():
        return projects

    for entry in sorted(volume_path.iterdir()):
        if not entry.is_dir() or entry.name.startswith(".") or entry.name.startswith("_"):
            continue

        # Check if this directory contains any standard project subfolders
        has_footage = (entry / "FOOTAGE").is_dir()
        has_assets = (entry / "ASSETS").is_dir()
        has_documentation = (entry / "DOCUMENTATION").is_dir()
        has_sound = (entry / "SOUND").is_dir()
        has_exports = (entry / "EXPORTS").is_dir()

        if any([has_footage, has_assets, has_documentation, has_sound, has_exports]):
            projects.append(Project(
                name=entry.name,
                path=entry,
                has_footage=has_footage,
                has_assets=has_assets,
                has_documentation=has_documentation,
                has_sound=has_sound,
                has_exports=has_exports,
            ))

    return projects


def create_project(volume_path: Path, project_name: str) -> Project:
    """Create a new project folder with the standard subfolder structure.

    Creates:
        <volume_path>/<project_name>/
            ASSETS/
            FOOTAGE/
            DOCUMENTATION/
            SOUND/
            EXPORTS/
    """
    project_path = volume_path / project_name

    for subfolder in PROJECT_SUBFOLDERS:
        (project_path / subfolder).mkdir(parents=True, exist_ok=True)

    return Project(
        name=project_name,
        path=project_path,
        has_footage=True,
        has_assets=True,
        has_documentation=True,
        has_sound=True,
        has_exports=True,
    )


class VolumeWatcher:
    """Watches for volume mount/unmount events by polling /Volumes."""

    def __init__(self, poll_interval: float = 2.0):
        self.poll_interval = poll_interval
        self._known_mounts: set[str] = set()
        self._running = False

    def start(self, on_mount=None, on_unmount=None):
        """Start watching for volume changes. Blocks until stop() is called."""
        self._running = True
        self._known_mounts = {str(v.mount_point) for v in get_mounted_volumes()}

        while self._running:
            time.sleep(self.poll_interval)
            current = {str(v.mount_point) for v in get_mounted_volumes()}

            # New mounts
            new_mounts = current - self._known_mounts
            for mp in new_mounts:
                vol = _inspect_volume(Path(mp))
                if vol and on_mount:
                    on_mount(vol)

            # Unmounts
            removed = self._known_mounts - current
            for mp in removed:
                if on_unmount:
                    on_unmount(Path(mp))

            self._known_mounts = current

    def stop(self):
        """Stop the watcher loop."""
        self._running = False
