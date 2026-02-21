"""Camera and sound card structure parsing for RED, ARRI, and audio recorders.

Understands the on-disk layout of:
- RED DSMC2 (Monstro, Helium, Gemini) — .RDC folder structure
- RED Komodo / Komodo-X — flat or minimal .R3D structure
- ARRI ALEXA Mini / Mini LF — ARRIRAW (.ari) and ProRes (.mxf)
- ARRI ALEXA 35 — updated ARRI structure
- ARRI AMIRA — ProRes workflow
- Sound Devices (MixPre, 7xx, 8xx) — WAV/BWF with iXML
- Zoom (F6, F8, H6) — ZOOM#### folder structure
- Tascam (DR-series) — MUSIC folder or flat WAV
- Generic sound recorders — WAV/AIFF/FLAC volumes
"""

import os
from pathlib import Path
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from .volumes import VolumeType


class ContentType(Enum):
    """High-level content classification for routing files."""
    VIDEO = "video"
    SOUND = "sound"
    SIDECAR = "sidecar"
    METADATA = "metadata"


class MediaFormat(Enum):
    """Camera and audio media file formats."""
    R3D = "R3D"           # REDCODE RAW
    ARI = "ari"           # ARRIRAW
    MXF = "mxf"           # ProRes in MXF container
    MOV = "mov"           # ProRes in QuickTime
    WAV = "wav"           # WAV / Broadcast WAV audio
    BWF = "bwf"           # Broadcast Wave Format
    AIFF = "aiff"         # AIFF audio
    MP3 = "mp3"           # MP3 audio
    FLAC = "flac"         # FLAC lossless audio
    OGG = "ogg"           # Ogg Vorbis audio
    M4A = "m4a"           # AAC audio
    UNKNOWN = "unknown"


# Map file extensions to MediaFormat
EXTENSION_TO_FORMAT = {
    ".r3d": MediaFormat.R3D,
    ".ari": MediaFormat.ARI,
    ".mxf": MediaFormat.MXF,
    ".mov": MediaFormat.MOV,
    ".wav": MediaFormat.WAV,
    ".bwf": MediaFormat.BWF,
    ".aiff": MediaFormat.AIFF,
    ".aif": MediaFormat.AIFF,
    ".mp3": MediaFormat.MP3,
    ".flac": MediaFormat.FLAC,
    ".ogg": MediaFormat.OGG,
    ".m4a": MediaFormat.M4A,
    ".aac": MediaFormat.M4A,
}

# Which formats are audio
AUDIO_FORMATS = {
    MediaFormat.WAV, MediaFormat.BWF, MediaFormat.AIFF,
    MediaFormat.MP3, MediaFormat.FLAC, MediaFormat.OGG, MediaFormat.M4A,
}

# Which formats are video
VIDEO_FORMATS = {
    MediaFormat.R3D, MediaFormat.ARI, MediaFormat.MXF, MediaFormat.MOV,
}


@dataclass
class MediaFile:
    """A single media file from a camera or sound card."""
    path: Path
    format: MediaFormat
    size_bytes: int
    clip_name: str = ""
    reel: str = ""
    content_type: ContentType = ContentType.VIDEO

    @property
    def size_gb(self) -> float:
        return self.size_bytes / (1024 ** 3)

    @property
    def filename(self) -> str:
        return self.path.name

    @property
    def is_audio(self) -> bool:
        return self.format in AUDIO_FORMATS

    @property
    def captured_at(self) -> Optional[float]:
        """Return the file's modification time as a proxy for capture time."""
        try:
            return self.path.stat().st_mtime
        except OSError:
            return None


@dataclass
class CameraRoll:
    """A camera roll / magazine containing one or more clips."""
    name: str
    source_path: Path
    files: list[MediaFile] = field(default_factory=list)
    sidecar_files: list[Path] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(f.size_bytes for f in self.files)

    @property
    def total_gb(self) -> float:
        return self.total_bytes / (1024 ** 3)

    @property
    def file_count(self) -> int:
        return len(self.files)


@dataclass
class CameraCard:
    """Represents a parsed camera card with all its rolls and media."""
    volume_name: str
    mount_point: Path
    camera_type: VolumeType
    rolls: list[CameraRoll] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(r.total_bytes for r in self.rolls)

    @property
    def total_gb(self) -> float:
        return self.total_bytes / (1024 ** 3)

    @property
    def total_files(self) -> int:
        return sum(r.file_count for r in self.rolls)

    @property
    def all_files(self) -> list[MediaFile]:
        files = []
        for roll in self.rolls:
            files.extend(roll.files)
        return files


# File extensions we care about per camera system
RED_EXTENSIONS = {".r3d"}
RED_SIDECAR_EXTENSIONS = {".rmd", ".rsx", ".r3d.rmd"}
ARRI_EXTENSIONS = {".ari", ".mxf", ".mov"}
ARRI_SIDECAR_EXTENSIONS = {".xml", ".ale", ".cdl"}
AUDIO_EXTENSIONS = {".wav", ".bwf"}


def parse_camera_card(mount_point: Path, volume_type: VolumeType) -> CameraCard:
    """Parse a camera card and return structured representation of its contents."""
    card = CameraCard(
        volume_name=mount_point.name,
        mount_point=mount_point,
        camera_type=volume_type,
    )

    if volume_type == VolumeType.RED_DSMC2:
        card.rolls = _parse_red_dsmc2(mount_point)
    elif volume_type == VolumeType.RED_KOMODO:
        card.rolls = _parse_red_komodo(mount_point)
    elif volume_type in (VolumeType.ARRI_ALEXA_MINI, VolumeType.ARRI_ALEXA35, VolumeType.ARRI_AMIRA):
        card.rolls = _parse_arri(mount_point, volume_type)

    return card


def _parse_red_dsmc2(mount_point: Path) -> list[CameraRoll]:
    """Parse RED DSMC2 card structure.

    Typical structure:
        VOLUME/
        ├── A001_1234AB/          (or just at root)
        │   ├── A001_C001_1234AB.RDC/
        │   │   ├── A001_C001_1234AB_001.R3D
        │   │   ├── A001_C001_1234AB_002.R3D
        │   │   └── A001_C001_1234AB.RMD
        │   └── A001_C002_1234AB.RDC/
        │       └── ...

    Uses recursive search to handle varied nesting depths across firmware versions.
    """
    rolls = []

    # Find all .RDC directories at any depth
    rdc_dirs = sorted(
        d for d in mount_point.rglob("*.RDC") if d.is_dir()
    )

    for rdc_dir in rdc_dirs:
        roll = CameraRoll(
            name=rdc_dir.stem,
            source_path=rdc_dir,
        )

        # Search recursively within each .RDC directory for R3D and sidecar files
        for f in sorted(rdc_dir.rglob("*")):
            if not f.is_file():
                continue
            if f.suffix.lower() in RED_EXTENSIONS:
                roll.files.append(_make_media_file(f, MediaFormat.R3D))
            elif f.suffix.lower() in RED_SIDECAR_EXTENSIONS:
                roll.sidecar_files.append(f)

        if roll.files:
            rolls.append(roll)

    # Fallback: if no RDC directories found, look for loose .R3D files anywhere
    if not rolls:
        r3d_files = sorted(mount_point.rglob("*.R3D"))
        if r3d_files:
            # Group by parent directory
            dir_groups: dict[Path, list[Path]] = {}
            for f in r3d_files:
                dir_groups.setdefault(f.parent, []).append(f)

            for dir_path, files in sorted(dir_groups.items()):
                roll_name = dir_path.name if dir_path != mount_point else mount_point.name
                roll = CameraRoll(name=roll_name, source_path=dir_path)
                for f in sorted(files):
                    roll.files.append(_make_media_file(f, MediaFormat.R3D))
                # Gather sidecar files in the same directory
                for f in dir_path.iterdir():
                    if f.is_file() and f.suffix.lower() in RED_SIDECAR_EXTENSIONS:
                        roll.sidecar_files.append(f)
                if roll.files:
                    rolls.append(roll)

    return rolls


def _parse_red_komodo(mount_point: Path) -> list[CameraRoll]:
    """Parse RED Komodo card structure.

    Komodo may have a flatter structure:
        VOLUME/
        ├── A001_C001_0101AB_001.R3D
        ├── A001_C001_0101AB_002.R3D
        ├── A001_C002_0101AB_001.R3D
        └── ...
    Or sometimes with .RDC-like groupings in folders.
    """
    rolls = []

    # First check for .RDC style folders (Komodo can use these too)
    rdc_dirs = [d for d in mount_point.rglob("*.RDC") if d.is_dir()]
    if rdc_dirs:
        return _parse_red_dsmc2(mount_point)

    # Flat structure: group R3D files by clip name prefix
    r3d_files = sorted(mount_point.rglob("*.R3D"))

    clip_groups: dict[str, list[Path]] = {}
    for f in r3d_files:
        # RED naming: A001_C001_0101AB_001.R3D
        # Clip identifier is everything except the segment number
        parts = f.stem.rsplit("_", 1)
        clip_id = parts[0] if len(parts) >= 2 else f.stem
        clip_groups.setdefault(clip_id, []).append(f)

    for clip_id, files in sorted(clip_groups.items()):
        roll = CameraRoll(
            name=clip_id,
            source_path=files[0].parent,
        )
        for f in sorted(files):
            roll.files.append(_make_media_file(f, MediaFormat.R3D))

        # Find matching sidecar files
        for f in files[0].parent.iterdir():
            if f.stem.startswith(clip_id) and f.suffix.lower() in RED_SIDECAR_EXTENSIONS:
                roll.sidecar_files.append(f)

        rolls.append(roll)

    return rolls


def _parse_arri(mount_point: Path, volume_type: VolumeType) -> list[CameraRoll]:
    """Parse ARRI camera card structure.

    ALEXA Mini / Mini LF typical structure:
        VOLUME/
        ├── ARRIRAW/
        │   ├── A001C001_230101_R1AB.ari
        │   ├── A001C001_230101_R1AB.ari
        │   └── ...
        └── (or ProRes in .mxf)

    ALEXA 35 structure:
        VOLUME/
        ├── A001R1AB/
        │   ├── A001C001_230101_R1AB.mxf
        │   └── ...
        └── ARRI/
            └── metadata files

    Amira:
        Similar to Mini but may use .mov containers
    """
    rolls = []

    # Collect all media files
    media_files: list[tuple[Path, MediaFormat]] = []

    for f in mount_point.rglob("*"):
        if not f.is_file():
            continue
        suffix = f.suffix.lower()
        if suffix == ".ari":
            media_files.append((f, MediaFormat.ARI))
        elif suffix == ".mxf":
            media_files.append((f, MediaFormat.MXF))
        elif suffix == ".mov":
            media_files.append((f, MediaFormat.MOV))

    # Group by clip name
    # ARRI naming: A001C001_230101_R1AB.mxf -> clip is A001C001
    clip_groups: dict[str, list[tuple[Path, MediaFormat]]] = {}
    for f, fmt in media_files:
        parts = f.stem.split("_")
        clip_id = parts[0] if parts else f.stem
        clip_groups.setdefault(clip_id, []).append((f, fmt))

    for clip_id, file_list in sorted(clip_groups.items()):
        source_path = file_list[0][0].parent
        roll = CameraRoll(
            name=clip_id,
            source_path=source_path,
        )

        for f, fmt in sorted(file_list, key=lambda x: x[0].name):
            roll.files.append(_make_media_file(f, fmt))

        # Gather sidecar files (XML, ALE, CDL)
        for f in source_path.iterdir():
            if f.suffix.lower() in ARRI_SIDECAR_EXTENSIONS and clip_id in f.stem:
                roll.sidecar_files.append(f)

        rolls.append(roll)

    # Also gather top-level sidecar/metadata directories
    # ARRI cards often have an ARRI/ metadata folder — we want to preserve it
    return rolls


def get_all_transferable_files(card: CameraCard) -> list[Path]:
    """Get a flat list of every file that should be transferred from a card.

    Includes media files, sidecars, and preserves any metadata directories.
    """
    files = []

    for roll in card.rolls:
        for mf in roll.files:
            files.append(mf.path)
        files.extend(roll.sidecar_files)

    # Include top-level metadata directories (ARRI/, etc.)
    meta_dirs = ["ARRI", "Metadata", "Proxy"]
    for dirname in meta_dirs:
        meta_path = card.mount_point / dirname
        if meta_path.is_dir():
            for f in meta_path.rglob("*"):
                if f.is_file():
                    files.append(f)

    return files


# ---------- Sound card parsing ----------

@dataclass
class SoundCard:
    """Represents a parsed sound recorder card with all its tracks/takes."""
    volume_name: str
    mount_point: Path
    recorder_type: VolumeType
    rolls: list[CameraRoll] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(r.total_bytes for r in self.rolls)

    @property
    def total_gb(self) -> float:
        return self.total_bytes / (1024 ** 3)

    @property
    def total_files(self) -> int:
        return sum(r.file_count for r in self.rolls)

    @property
    def all_files(self) -> list[MediaFile]:
        files = []
        for roll in self.rolls:
            files.extend(roll.files)
        return files


# Audio file extensions for scanning
SOUND_SCAN_EXTENSIONS = {".wav", ".bwf", ".aiff", ".aif", ".mp3", ".flac", ".ogg", ".m4a", ".aac"}


def parse_sound_card(mount_point: Path, volume_type: VolumeType) -> SoundCard:
    """Parse a sound recorder card and return structured representation."""
    card = SoundCard(
        volume_name=mount_point.name,
        mount_point=mount_point,
        recorder_type=volume_type,
    )

    if volume_type == VolumeType.ZOOM_RECORDER:
        card.rolls = _parse_zoom(mount_point)
    elif volume_type == VolumeType.TASCAM_RECORDER:
        card.rolls = _parse_tascam(mount_point)
    elif volume_type == VolumeType.SOUND_DEVICES:
        card.rolls = _parse_sound_devices(mount_point)
    else:
        card.rolls = _parse_generic_sound(mount_point)

    return card


def _parse_zoom(mount_point: Path) -> list[CameraRoll]:
    """Parse Zoom recorder card structure.

    Typical structure:
        VOLUME/
        ├── ZOOM0001/
        │   ├── ZOOM0001_Tr1.WAV
        │   ├── ZOOM0001_Tr2.WAV
        │   └── ZOOM0001_LR.WAV
        ├── ZOOM0002/
        │   └── ...
    """
    rolls = []

    # Look for ZOOM#### folders
    zoom_dirs = sorted(mount_point.glob("ZOOM[0-9][0-9][0-9][0-9]"))

    if zoom_dirs:
        for zoom_dir in zoom_dirs:
            roll = CameraRoll(name=zoom_dir.name, source_path=zoom_dir)
            for f in sorted(zoom_dir.iterdir()):
                if f.is_file() and f.suffix.lower() in SOUND_SCAN_EXTENSIONS:
                    roll.files.append(_make_sound_file(f))
            if roll.files:
                rolls.append(roll)
    else:
        # Fallback to generic parsing
        rolls = _parse_generic_sound(mount_point)

    return rolls


def _parse_tascam(mount_point: Path) -> list[CameraRoll]:
    """Parse Tascam recorder card structure.

    Typical structure:
        VOLUME/
        ├── MUSIC/
        │   ├── TASCAM_0001.WAV
        │   ├── TASCAM_0002.WAV
        │   └── ...
    """
    rolls = []

    music_dir = mount_point / "MUSIC"
    if music_dir.is_dir():
        roll = CameraRoll(name="MUSIC", source_path=music_dir)
        for f in sorted(music_dir.rglob("*")):
            if f.is_file() and f.suffix.lower() in SOUND_SCAN_EXTENSIONS:
                roll.files.append(_make_sound_file(f))
        if roll.files:
            rolls.append(roll)
    else:
        rolls = _parse_generic_sound(mount_point)

    return rolls


def _parse_sound_devices(mount_point: Path) -> list[CameraRoll]:
    """Parse Sound Devices recorder card structure.

    Sound Devices recorders (MixPre, 7-series, 8-series) typically write
    WAV/BWF files with embedded iXML metadata. Files are often organized
    by scene/take or in a flat structure.

    Common naming patterns:
        Scene1_Take1.wav, 001_240101_1234.wav
    """
    rolls = []

    # Group audio files by parent directory
    dir_groups: dict[Path, list[Path]] = {}
    for f in sorted(mount_point.rglob("*")):
        if f.is_file() and f.suffix.lower() in SOUND_SCAN_EXTENSIONS:
            dir_groups.setdefault(f.parent, []).append(f)

    for dir_path, files in sorted(dir_groups.items()):
        # Use directory name as the roll/session name
        if dir_path == mount_point:
            roll_name = mount_point.name
        else:
            try:
                roll_name = str(dir_path.relative_to(mount_point))
            except ValueError:
                roll_name = dir_path.name

        roll = CameraRoll(name=roll_name, source_path=dir_path)
        for f in sorted(files):
            roll.files.append(_make_sound_file(f))

        # Gather sidecar metadata files in the same directory
        for f in dir_path.iterdir():
            if f.is_file() and f.suffix.lower() in {".xml", ".csv", ".txt", ".pdf"}:
                roll.sidecar_files.append(f)

        if roll.files:
            rolls.append(roll)

    return rolls


def _parse_generic_sound(mount_point: Path) -> list[CameraRoll]:
    """Parse a generic sound card — any volume with audio files."""
    rolls = []

    dir_groups: dict[Path, list[Path]] = {}
    for f in sorted(mount_point.rglob("*")):
        if f.is_file() and f.suffix.lower() in SOUND_SCAN_EXTENSIONS:
            dir_groups.setdefault(f.parent, []).append(f)

    for dir_path, files in sorted(dir_groups.items()):
        if dir_path == mount_point:
            roll_name = mount_point.name
        else:
            try:
                roll_name = str(dir_path.relative_to(mount_point))
            except ValueError:
                roll_name = dir_path.name

        roll = CameraRoll(name=roll_name, source_path=dir_path)
        for f in sorted(files):
            roll.files.append(_make_sound_file(f))
        if roll.files:
            rolls.append(roll)

    return rolls


def _make_sound_file(path: Path) -> MediaFile:
    """Create a MediaFile for an audio file."""
    try:
        size = path.stat().st_size
    except OSError:
        size = 0

    fmt = EXTENSION_TO_FORMAT.get(path.suffix.lower(), MediaFormat.WAV)

    return MediaFile(
        path=path,
        format=fmt,
        size_bytes=size,
        clip_name=path.stem,
        reel="",
        content_type=ContentType.SOUND,
    )


def _make_media_file(path: Path, fmt: MediaFormat) -> MediaFile:
    """Create a MediaFile from a path."""
    try:
        size = path.stat().st_size
    except OSError:
        size = 0

    # Extract clip name and reel from filename
    parts = path.stem.split("_")
    clip_name = parts[0] if parts else path.stem
    reel = ""
    if len(parts) >= 2:
        reel = parts[0]  # A001 is typically the reel

    return MediaFile(
        path=path,
        format=fmt,
        size_bytes=size,
        clip_name=clip_name,
        reel=reel,
    )
