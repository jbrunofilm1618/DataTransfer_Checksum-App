"""Checksum computation and verification using xxHash and MD5.

xxHash (xxh3_128) is the primary algorithm — extremely fast, ideal for
large media files. MD5 is available as a fallback for compatibility with
existing post-production workflows.
"""

import hashlib
import os
from pathlib import Path
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import xxhash


class HashAlgorithm(Enum):
    """Supported hash algorithms."""
    XXHASH = "xxh3_128"
    MD5 = "md5"


# Default buffer size for reading files: 4 MB
DEFAULT_BUFFER_SIZE = 4 * 1024 * 1024


@dataclass
class ChecksumResult:
    """Result of checksumming a single file."""
    file_path: Path
    algorithm: HashAlgorithm
    hex_digest: str
    file_size: int
    verified: bool = False
    error: Optional[str] = None


def compute_checksum(
    file_path: Path,
    algorithm: HashAlgorithm = HashAlgorithm.XXHASH,
    buffer_size: int = DEFAULT_BUFFER_SIZE,
    progress_callback=None,
) -> ChecksumResult:
    """Compute checksum of a file.

    Args:
        file_path: Path to file.
        algorithm: Hash algorithm to use.
        buffer_size: Read buffer size in bytes.
        progress_callback: Optional callable(bytes_read, total_bytes) for progress.

    Returns:
        ChecksumResult with the computed digest.
    """
    try:
        file_size = file_path.stat().st_size
    except OSError as e:
        return ChecksumResult(
            file_path=file_path,
            algorithm=algorithm,
            hex_digest="",
            file_size=0,
            error=str(e),
        )

    if algorithm == HashAlgorithm.XXHASH:
        hasher = xxhash.xxh3_128()
    else:
        hasher = hashlib.md5()

    bytes_read = 0
    try:
        with open(file_path, "rb") as f:
            while True:
                chunk = f.read(buffer_size)
                if not chunk:
                    break
                hasher.update(chunk)
                bytes_read += len(chunk)
                if progress_callback:
                    progress_callback(bytes_read, file_size)
    except OSError as e:
        return ChecksumResult(
            file_path=file_path,
            algorithm=algorithm,
            hex_digest="",
            file_size=file_size,
            error=str(e),
        )

    return ChecksumResult(
        file_path=file_path,
        algorithm=algorithm,
        hex_digest=hasher.hexdigest(),
        file_size=file_size,
    )


def verify_transfer(
    source_path: Path,
    dest_path: Path,
    algorithm: HashAlgorithm = HashAlgorithm.XXHASH,
    progress_callback=None,
) -> tuple[ChecksumResult, ChecksumResult, bool]:
    """Verify a file transfer by comparing checksums of source and destination.

    Returns:
        Tuple of (source_result, dest_result, match).
    """
    source_result = compute_checksum(source_path, algorithm, progress_callback=progress_callback)
    dest_result = compute_checksum(dest_path, algorithm, progress_callback=progress_callback)

    if source_result.error or dest_result.error:
        return source_result, dest_result, False

    match = source_result.hex_digest == dest_result.hex_digest
    source_result.verified = match
    dest_result.verified = match

    return source_result, dest_result, match


def verify_file_list(
    file_pairs: list[tuple[Path, Path]],
    algorithm: HashAlgorithm = HashAlgorithm.XXHASH,
    progress_callback=None,
) -> list[tuple[ChecksumResult, ChecksumResult, bool]]:
    """Verify a list of source/destination file pairs.

    Args:
        file_pairs: List of (source_path, dest_path) tuples.
        algorithm: Hash algorithm to use.
        progress_callback: Optional callable(file_index, total_files, current_file, match).

    Returns:
        List of (source_result, dest_result, match) tuples.
    """
    results = []
    total = len(file_pairs)

    for i, (src, dst) in enumerate(file_pairs):
        src_result, dst_result, match = verify_transfer(src, dst, algorithm)
        results.append((src_result, dst_result, match))

        if progress_callback:
            progress_callback(i + 1, total, src.name, match)

    return results
