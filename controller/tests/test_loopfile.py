"""The .wcl frame-loop container and the packer's budget logic.

The container is read by C++ on the ESP32, so the layout is checked byte by
byte here: a mismatch between this and `firmware/esp32/src/loopfile.h` would
show up as a display node that boots and then shows nothing, which is a
miserable thing to debug six boards at a time.
"""

from __future__ import annotations

import struct

import pytest
from loopfile import HEADER, MAGIC, VERSION, Loop, pack, unpack


def jpeg(index: int, size: int = 32) -> bytes:
    """A payload shaped like a JPEG: SOI, filler, EOI."""
    body = bytes([index % 256]) * size
    return b"\xff\xd8" + body + b"\xff\xd9"


def test_round_trip():
    frames = [jpeg(i) for i in range(5)]
    loop = unpack(pack(frames, width=240, height=240, fps=10))
    assert loop.frames == frames
    assert loop.width == 240
    assert loop.height == 240
    assert loop.fps == 10
    assert loop.count == 5
    assert loop.duration_s == pytest.approx(0.5)


def test_header_layout_is_exactly_what_the_firmware_expects():
    blob = pack([jpeg(0)], width=240, height=240, fps=10)
    magic, version, count, width, height, fps, reserved = HEADER.unpack(blob[:16])
    assert magic == MAGIC == b"WCLP"
    assert version == VERSION == 1
    assert (count, width, height, fps, reserved) == (1, 240, 240, 10, 0)
    assert HEADER.size == 16


def test_index_offsets_point_at_the_frames():
    frames = [jpeg(0, 10), jpeg(1, 20), jpeg(2, 30)]
    blob = pack(frames, width=240, height=240, fps=10)
    count = len(frames)
    offsets = struct.unpack_from(f"<{count}I", blob, 16)
    lengths = struct.unpack_from(f"<{count}I", blob, 16 + 4 * count)
    for index, frame in enumerate(frames):
        assert blob[offsets[index] : offsets[index] + lengths[index]] == frame
    assert offsets[0] == 16 + 8 * count


def test_overhead_is_sixteen_bytes_plus_eight_per_frame():
    frames = [jpeg(i, 100) for i in range(100)]
    blob = pack(frames, width=240, height=240, fps=10)
    assert len(blob) == 16 + 8 * 100 + sum(len(f) for f in frames)
    assert len(blob) - sum(len(f) for f in frames) == 816


def test_nbytes_matches_the_packed_size():
    frames = [jpeg(i) for i in range(7)]
    loop = Loop(width=240, height=240, fps=10, frames=frames)
    assert loop.nbytes == len(pack(frames, width=240, height=240, fps=10))


def test_a_single_frame_is_legal():
    """A still portrait is a perfectly good 'loop'."""
    loop = unpack(pack([jpeg(0)], width=240, height=240, fps=1))
    assert loop.count == 1
    assert loop.duration_s == pytest.approx(1.0)


def test_empty_frame_list_is_rejected():
    with pytest.raises(ValueError, match="at least one frame"):
        pack([], width=240, height=240, fps=10)


@pytest.mark.parametrize(
    ("kwargs", "word"),
    [
        ({"width": 0, "height": 240, "fps": 10}, "width"),
        ({"width": 240, "height": 0, "fps": 10}, "height"),
        ({"width": 240, "height": 240, "fps": 0}, "fps"),
        ({"width": 70000, "height": 240, "fps": 10}, "width"),
    ],
)
def test_out_of_range_header_values_are_rejected(kwargs, word):
    with pytest.raises(ValueError, match=word):
        pack([jpeg(0)], **kwargs)


def test_too_many_frames_is_rejected():
    with pytest.raises(ValueError, match="frame count"):
        pack([b"x"] * 70000, width=240, height=240, fps=10)


def test_bad_magic_is_rejected():
    blob = bytearray(pack([jpeg(0)], width=240, height=240, fps=10))
    blob[:4] = b"NOPE"
    with pytest.raises(ValueError, match="not a .wcl file"):
        unpack(bytes(blob))


def test_a_future_version_is_rejected_rather_than_misread():
    blob = bytearray(pack([jpeg(0)], width=240, height=240, fps=10))
    blob[4:6] = struct.pack("<H", 99)
    with pytest.raises(ValueError, match="version 99"):
        unpack(bytes(blob))


def test_a_short_file_is_rejected():
    with pytest.raises(ValueError, match="too short"):
        unpack(b"WCLP")


def test_a_truncated_index_is_rejected():
    blob = pack([jpeg(i) for i in range(5)], width=240, height=240, fps=10)
    with pytest.raises(ValueError, match="truncated index"):
        unpack(blob[:20])


def test_a_truncated_payload_is_rejected():
    blob = pack([jpeg(i) for i in range(5)], width=240, height=240, fps=10)
    with pytest.raises(ValueError, match="past the end"):
        unpack(blob[:-10])


def test_zero_frames_in_the_header_is_rejected():
    blob = bytearray(pack([jpeg(0)], width=240, height=240, fps=10))
    blob[6:8] = struct.pack("<H", 0)
    with pytest.raises(ValueError, match="zero frames"):
        unpack(bytes(blob))


# -- the ffmpeg filter chain -------------------------------------------------


def test_the_filter_chain_crops_scales_and_masks():
    from make_loop import build_filter

    chain = build_filter(240, 10, circle=True)
    assert "fps=10" in chain
    assert "crop='min(iw,ih)':'min(iw,ih)'" in chain
    assert "scale=240:240" in chain
    # The mask zeroes the colour channels directly: JPEG has no alpha, so a
    # chain that builds one would only have to throw it away.
    assert "format=rgb24" in chain
    assert "hypot(X-120,Y-120)" in chain
    assert "a=" not in chain.replace("scale=", "")


def test_the_square_option_skips_the_mask():
    from make_loop import build_filter

    chain = build_filter(240, 10, circle=False)
    assert "hypot" not in chain
    assert "scale=240:240" in chain


def test_the_quality_ladder_only_gets_worse():
    from make_loop import QUALITY_LADDER

    assert list(QUALITY_LADDER) == sorted(QUALITY_LADDER)
    assert QUALITY_LADDER[0] >= 2  # ffmpeg's best
    assert QUALITY_LADDER[-1] <= 31  # ffmpeg's worst


def test_missing_ffmpeg_explains_how_to_install_it(monkeypatch):
    import make_loop

    monkeypatch.setattr(make_loop.shutil, "which", lambda _name: None)
    with pytest.raises(make_loop.FfmpegMissing) as excinfo:
        make_loop.require_ffmpeg()
    assert "apt install ffmpeg" in str(excinfo.value)
