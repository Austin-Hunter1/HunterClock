"""Turn a video into a round 240x240 frame loop for a display node.

    python tools/make_loop.py evan.mp4 firmware/esp32/data/loop.wcl
    python tools/make_loop.py evan.mp4 out.wcl --seconds 6 --fps 10 --budget 1.5

ffmpeg does the work: it crops the video to a centred square, scales it to
240x240, masks it to a circle (the GC9A01 is round, so the corners are never
seen and encoding them wastes space), samples it at about 10 fps and writes one
JPEG per frame.  Those are packed into a single ``.wcl`` container.

The budget matters, and it is not a round number.  PlatformIO's ``default.csv``
partition table on a 4 MB ESP32 gives:

======== ========= ==========
app0     0x140000  1 310 720 B
app1     0x140000  1 310 720 B  (the OTA slot)
littlefs 0x160000  1 441 792 B
======== ========= ==========

So the loop has **1 441 792 bytes** to live in, minus ``config.json`` and
LittleFS's own block overhead.  The default budget here is 1.3 MB, which leaves
about 130 KB of margin.  Asking for 1.5 MB, as a round number suggests, produces
a file that builds happily and then fails to upload.

This re-encodes at progressively lower JPEG quality until the result fits, and
says exactly what it did rather than silently producing something unflashable.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    from loopfile import pack
except ImportError:  # imported as tools.make_loop rather than run as a script
    from tools.loopfile import pack

#: Quality ladder, tried in order until the loop fits the budget.
#: ffmpeg's -q:v runs 2 (best) to 31 (worst); below about 12 a face on a 240px
#: circle starts to look like a mistake rather than a stylistic choice.
QUALITY_LADDER = (3, 5, 7, 9, 12)

DEFAULT_SIZE = 240
DEFAULT_FPS = 10
DEFAULT_SECONDS = 6.0
DEFAULT_BUDGET_MB = 1.3


class FfmpegMissing(RuntimeError):
    """ffmpeg is not installed."""


def require_ffmpeg() -> str:
    """Return the ffmpeg binary path, or explain how to get it."""
    found = shutil.which("ffmpeg")
    if not found:
        raise FfmpegMissing(
            "ffmpeg is not installed.\n"
            "  Debian/Ubuntu/Raspberry Pi OS:  sudo apt install ffmpeg\n"
            "  macOS:                          brew install ffmpeg\n"
            "  Windows:                        winget install ffmpeg"
        )
    return found


def build_filter(size: int, fps: int, *, circle: bool) -> str:
    """Build the ffmpeg filter chain.

    Centre-crops to a square, scales, then (optionally) masks to a circle by
    zeroing every pixel outside it.  The mask is applied directly to the colour
    channels rather than through an alpha channel, because JPEG has no alpha and
    a filter chain that builds one only has to throw it away again.  Flat black
    corners cost almost nothing to encode, and the GC9A01 is round, so nobody
    ever sees them.
    """
    chain = [
        f"fps={fps}",
        "crop='min(iw,ih)':'min(iw,ih)'",
        f"scale={size}:{size}:flags=lanczos",
    ]
    if circle:
        # ``:g`` so an even size renders as 120 rather than 120.0; ffmpeg
        # accepts either, but the filter string ends up in logs and error
        # messages and is easier to read without the noise.
        radius = f"{size / 2:g}"
        inside = f"lte(hypot(X-{radius},Y-{radius}),{radius})"
        chain.append("format=rgb24")
        chain.append(
            f"geq=r='if({inside},r(X,Y),0)'"
            f":g='if({inside},g(X,Y),0)'"
            f":b='if({inside},b(X,Y),0)'"
        )
    return ",".join(chain)


def extract_frames(
    source: Path,
    workdir: Path,
    *,
    size: int,
    fps: int,
    seconds: float,
    quality: int,
    start: float,
    circle: bool,
    ffmpeg: str,
) -> list[bytes]:
    """Run ffmpeg once and read the JPEGs it produced.

    Returns:
        The frames, in order.

    Raises:
        RuntimeError: if ffmpeg fails or produces nothing.
    """
    for stale in workdir.glob("frame-*.jpg"):
        stale.unlink()
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel", "error",
        "-ss", str(start),
        "-t", str(seconds),
        "-i", str(source),
        "-vf", build_filter(size, fps, circle=circle),
        "-q:v", str(quality),
        "-an",
        str(workdir / "frame-%04d.jpg"),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed:\n{result.stderr.strip()}")
    frames = sorted(workdir.glob("frame-*.jpg"))
    if not frames:
        raise RuntimeError(
            "ffmpeg produced no frames. Is the clip shorter than --start, or not a video?"
        )
    return [path.read_bytes() for path in frames]


def make_loop(
    source: Path,
    destination: Path,
    *,
    size: int = DEFAULT_SIZE,
    fps: int = DEFAULT_FPS,
    seconds: float = DEFAULT_SECONDS,
    start: float = 0.0,
    budget_mb: float = DEFAULT_BUDGET_MB,
    circle: bool = True,
    report=print,
) -> dict:
    """Build a loop file, dropping quality until it fits the budget.

    Args:
        source: The input video.
        destination: Where to write the ``.wcl`` file.
        size: Output square size in pixels.
        fps: Playback rate.
        seconds: How much of the clip to use.
        start: Seconds into the clip to start.
        budget_mb: Size limit for the finished file.
        circle: Mask the corners to black for the round display.
        report: Where to print progress.

    Returns:
        A dict describing what was produced.

    Raises:
        FfmpegMissing: ffmpeg is not installed.
        RuntimeError: ffmpeg failed, or the budget cannot be met.
    """
    ffmpeg = require_ffmpeg()
    if not source.is_file():
        raise RuntimeError(f"no such file: {source}")
    budget_bytes = int(budget_mb * 1_000_000)

    with tempfile.TemporaryDirectory(prefix="weasley-loop-") as tmp:
        workdir = Path(tmp)
        attempts = []
        for quality in QUALITY_LADDER:
            frames = extract_frames(
                source, workdir,
                size=size, fps=fps, seconds=seconds,
                quality=quality, start=start, circle=circle, ffmpeg=ffmpeg,
            )
            blob = pack(frames, width=size, height=size, fps=fps)
            attempts.append((quality, len(blob), len(frames)))
            report(
                f"  quality {quality:>2}: {len(frames):>3} frames, "
                f"{len(blob) / 1_000_000:.2f} MB "
                f"({'fits' if len(blob) <= budget_bytes else 'over budget'})"
            )
            if len(blob) <= budget_bytes:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(blob)
                return {
                    "path": str(destination),
                    "bytes": len(blob),
                    "frames": len(frames),
                    "quality": quality,
                    "fps": fps,
                    "size": size,
                    "seconds": len(frames) / fps,
                    "budget_bytes": budget_bytes,
                    "attempts": attempts,
                }

    smallest = min(attempts, key=lambda item: item[1])
    raise RuntimeError(
        f"cannot fit {budget_mb} MB. The smallest attempt was "
        f"{smallest[1] / 1_000_000:.2f} MB at quality {smallest[0]}.\n"
        f"Try --seconds {max(1, int(seconds / 2))}, or --fps {max(4, fps - 3)}."
    )


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(
        description="Make a round 240x240 frame loop for a Weasley Clock display node.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Example:\n"
            "  python tools/make_loop.py evan.mp4 firmware/esp32/data/loop.wcl \\\n"
            "      --seconds 6 --fps 10 --budget 1.5\n"
        ),
    )
    parser.add_argument("source", type=Path, help="input video")
    parser.add_argument("destination", type=Path, help="output .wcl file")
    parser.add_argument("--size", type=int, default=DEFAULT_SIZE)
    parser.add_argument("--fps", type=int, default=DEFAULT_FPS)
    parser.add_argument("--seconds", type=float, default=DEFAULT_SECONDS)
    parser.add_argument("--start", type=float, default=0.0, help="seconds into the clip")
    parser.add_argument("--budget", type=float, default=DEFAULT_BUDGET_MB, help="size limit in MB")
    parser.add_argument("--square", action="store_true", help="do not mask to a circle")
    args = parser.parse_args(argv)

    print(f"Packing {args.source} -> {args.destination}")
    try:
        result = make_loop(
            args.source, args.destination,
            size=args.size, fps=args.fps, seconds=args.seconds,
            start=args.start, budget_mb=args.budget, circle=not args.square,
        )
    except (FfmpegMissing, RuntimeError) as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 1

    print(
        f"\nWrote {result['path']}\n"
        f"  {result['frames']} frames at {result['fps']} fps "
        f"({result['seconds']:.1f} s per loop)\n"
        f"  {result['bytes'] / 1_000_000:.2f} MB of a "
        f"{result['budget_bytes'] / 1_000_000:.2f} MB budget, JPEG quality {result['quality']}\n"
        f"\nUpload it with:  pio run -t uploadfs"
    )
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main())
