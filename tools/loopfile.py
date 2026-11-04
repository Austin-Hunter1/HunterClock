"""The ``.wcl`` frame-loop container, shared by the packer and the firmware.

A display node plays a short looping animation behind its overlay.  Storing one
JPEG per file on LittleFS works but is slow to open 100 times a second and wastes
a filesystem entry each; one container with a flat index is simpler for an ESP32
to read and is what ``firmware/esp32`` expects.

Layout, little-endian throughout::

    offset  size  field
    0       4     magic, b"WCLP"
    4       2     format version (currently 1)
    6       2     frame count N
    8       2     width in pixels
    10      2     height in pixels
    12      2     frames per second
    14      2     reserved, zero
    16      4*N   byte offset of each frame, from the start of the file
    16+4N   4*N   byte length of each frame
    ...           the JPEG payloads, in order

The header is 16 bytes and the index is 8 bytes per frame, so a 100-frame loop
costs 816 bytes of overhead.  The firmware reads the header and the index once
at boot and then seeks straight to each frame.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

MAGIC = b"WCLP"
VERSION = 1
HEADER = struct.Struct("<4sHHHHHH")
assert HEADER.size == 16


@dataclass
class Loop:
    """A decoded frame loop."""

    width: int
    height: int
    fps: int
    frames: list[bytes]

    @property
    def count(self) -> int:
        """How many frames."""
        return len(self.frames)

    @property
    def duration_s(self) -> float:
        """How long one pass through the loop takes."""
        return self.count / self.fps if self.fps else 0.0

    @property
    def nbytes(self) -> int:
        """Total size of the packed file."""
        return HEADER.size + 8 * self.count + sum(len(f) for f in self.frames)


def pack(frames: list[bytes], *, width: int, height: int, fps: int) -> bytes:
    """Pack JPEG frames into a ``.wcl`` container.

    Args:
        frames: JPEG payloads, in display order.
        width: Frame width in pixels.
        height: Frame height in pixels.
        fps: Playback rate.

    Returns:
        The container bytes.

    Raises:
        ValueError: for an empty list, or sizes that do not fit the header.
    """
    if not frames:
        raise ValueError("a loop needs at least one frame")
    for value, name in ((len(frames), "frame count"), (width, "width"), (height, "height"),
                        (fps, "fps")):
        if not 0 < value <= 0xFFFF:
            raise ValueError(f"{name} must be between 1 and 65535, got {value}")

    header = HEADER.pack(MAGIC, VERSION, len(frames), width, height, fps, 0)
    index_size = 8 * len(frames)
    offset = HEADER.size + index_size
    offsets, lengths = [], []
    for frame in frames:
        offsets.append(offset)
        lengths.append(len(frame))
        offset += len(frame)

    parts = [header]
    parts.extend(struct.pack("<I", value) for value in offsets)
    parts.extend(struct.pack("<I", value) for value in lengths)
    parts.extend(frames)
    return b"".join(parts)


def unpack(data: bytes) -> Loop:
    """Read a ``.wcl`` container.

    Args:
        data: The whole file.

    Returns:
        The decoded :class:`Loop`.

    Raises:
        ValueError: if it is not a loop file, is a version this code does not
            understand, or is truncated.
    """
    if len(data) < HEADER.size:
        raise ValueError("too short to be a loop file")
    magic, version, count, width, height, fps, _reserved = HEADER.unpack(data[: HEADER.size])
    if magic != MAGIC:
        raise ValueError(f"bad magic {magic!r}; this is not a .wcl file")
    if version != VERSION:
        raise ValueError(f"loop file version {version}, this code understands {VERSION}")
    if count == 0:
        raise ValueError("loop file claims zero frames")

    index_end = HEADER.size + 8 * count
    if len(data) < index_end:
        raise ValueError("truncated index")
    offsets = struct.unpack_from(f"<{count}I", data, HEADER.size)
    lengths = struct.unpack_from(f"<{count}I", data, HEADER.size + 4 * count)

    frames = []
    for index, (offset, length) in enumerate(zip(offsets, lengths, strict=True)):
        end = offset + length
        if end > len(data):
            raise ValueError(f"frame {index} runs past the end of the file")
        frames.append(data[offset:end])
    return Loop(width=width, height=height, fps=fps, frames=frames)
