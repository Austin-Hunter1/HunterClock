"""Render an STL to a PNG, so a printed part can be looked at before it is printed.

    python tools/preview3d.py part.stl out.png
    python tools/preview3d.py a.stl b.stl c.stl --grid sheet.png --labels one two three

A small software rasteriser: orthographic projection, z-buffer, two-point
Lambert shading. No OpenGL, which matters because this runs headless.

It exists because a concentric moulding is invisible from directly above and
indistinguishable in a cross-section table. The only way to judge whether a
bezel profile looks like turned brass or like a 3D print is to look at it.
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path

import numpy as np

#: Where the light comes from, and how much ambient fill there is.
KEY = np.array([-0.4, -0.5, 0.78])
FILL = np.array([0.6, 0.3, 0.4])
AMBIENT = 0.22

#: Brass, lit and shadowed, as RGB 0-1.
BRASS = np.array([0.80, 0.62, 0.28])
BACKGROUND = np.array([0.97, 0.955, 0.92])


def look_at(vertices: np.ndarray, elevation: float, azimuth: float) -> np.ndarray:
    """Rotate vertices into a view frame: azimuth about z, then elevation."""
    az, el = math.radians(azimuth), math.radians(elevation)
    rz = np.array([[math.cos(az), -math.sin(az), 0],
                   [math.sin(az), math.cos(az), 0],
                   [0, 0, 1.0]])
    rx = np.array([[1.0, 0, 0],
                   [0, math.cos(el), -math.sin(el)],
                   [0, math.sin(el), math.cos(el)]])
    return vertices @ rz.T @ rx.T


def render(
    mesh,
    size: int = 640,
    elevation: float = 32.0,
    azimuth: float = -28.0,
    colour: np.ndarray = BRASS,
    margin: float = 0.1,
) -> np.ndarray:
    """Rasterise a mesh into an RGB image array.

    Args:
        mesh: A trimesh object.
        size: Output edge length in pixels.
        elevation: Camera angle above the part, degrees.
        azimuth: Camera angle around the part, degrees.
        colour: Base RGB, 0-1.
        margin: Fraction of the frame left empty around the part.

    Returns:
        An ``(size, size, 3)`` float array in 0-1.
    """
    view = look_at(np.asarray(mesh.vertices, dtype=float), elevation, azimuth)
    normals = look_at(np.asarray(mesh.face_normals, dtype=float), elevation, azimuth)

    low, high = view.min(axis=0), view.max(axis=0)
    span = max((high - low)[:2].max(), 1e-6) * (1 + 2 * margin)
    centre = (high + low) / 2
    scale = size / span

    screen = (view[:, :2] - centre[:2]) * scale + size / 2
    screen[:, 1] = size - screen[:, 1]  # y down
    depth = view[:, 2]

    image = np.repeat(BACKGROUND[None, None, :], size, 0).repeat(size, 1).astype(float)
    zbuf = np.full((size, size), -np.inf)

    key = KEY / np.linalg.norm(KEY)
    fill = FILL / np.linalg.norm(FILL)

    tris = mesh.faces
    # Draw far to near so the z-buffer has less to do, and skip back faces.
    order = np.argsort(view[tris].mean(axis=1)[:, 2])
    for index in order:
        normal = normals[index]
        if normal[2] <= 0:
            continue
        shade = (AMBIENT
                 + 0.68 * max(float(normal @ key), 0.0)
                 + 0.22 * max(float(normal @ fill), 0.0))
        rgb = np.clip(colour * shade, 0, 1)

        tri = tris[index]
        pts = screen[tri]
        zs = depth[tri]
        min_x, min_y = np.floor(pts.min(axis=0)).astype(int)
        max_x, max_y = np.ceil(pts.max(axis=0)).astype(int)
        min_x, min_y = max(min_x, 0), max(min_y, 0)
        max_x, max_y = min(max_x, size - 1), min(max_y, size - 1)
        if min_x > max_x or min_y > max_y:
            continue

        xs = np.arange(min_x, max_x + 1)
        ys = np.arange(min_y, max_y + 1)
        gx, gy = np.meshgrid(xs, ys)
        (x0, y0), (x1, y1), (x2, y2) = pts
        denom = (y1 - y2) * (x0 - x2) + (x2 - x1) * (y0 - y2)
        if abs(denom) < 1e-12:
            continue
        a = ((y1 - y2) * (gx - x2) + (x2 - x1) * (gy - y2)) / denom
        b = ((y2 - y0) * (gx - x2) + (x0 - x2) * (gy - y2)) / denom
        c = 1.0 - a - b
        inside = (a >= -1e-6) & (b >= -1e-6) & (c >= -1e-6)
        if not inside.any():
            continue
        z = a * zs[0] + b * zs[1] + c * zs[2]
        nearer = inside & (z > zbuf[min_y:max_y + 1, min_x:max_x + 1])
        if not nearer.any():
            continue
        region = zbuf[min_y:max_y + 1, min_x:max_x + 1]
        region[nearer] = z[nearer]
        patch = image[min_y:max_y + 1, min_x:max_x + 1]
        patch[nearer] = rgb
    return image


def render_scene(
    parts,
    size: int = 900,
    elevation: float = 0.0,
    azimuth: float = 0.0,
    margin: float = 0.04,
    background: np.ndarray = BACKGROUND,
    bounds=None,
) -> np.ndarray:
    """Rasterise several meshes into one image, each with its own colour.

    :func:`render` draws one part against the frame. An assembly needs the
    opposite: one camera, one z-buffer and one bounding box shared by every
    part, so that a hand in front of a dial actually occludes it.

    Args:
        parts: ``(mesh, rgb)`` pairs. Colour is RGB 0-1.
        size: Output edge length in pixels.
        elevation: Camera angle above the scene, degrees. 0 looks straight
            down the z axis, which for this clock is square on to the front.
        azimuth: Camera angle around the scene, degrees.
        margin: Fraction of the frame left empty around the assembly.
        background: Background RGB 0-1.
        bounds: ``(low, high)`` in view space to frame on, instead of the
            scene's own extent. Pass the same value for every panel of a
            series and the parts stay the same size from frame to frame.

    Returns:
        An ``(size, size, 3)`` float array in 0-1.
    """
    views, normal_sets = [], []
    for mesh, _rgb in parts:
        views.append(look_at(np.asarray(mesh.vertices, dtype=float), elevation, azimuth))
        normal_sets.append(look_at(np.asarray(mesh.face_normals, dtype=float),
                                   elevation, azimuth))

    if bounds is None:
        every = np.vstack(views)
        low, high = every.min(axis=0), every.max(axis=0)
    else:
        low, high = np.asarray(bounds[0], float), np.asarray(bounds[1], float)
    span = max((high - low)[:2].max(), 1e-6) * (1 + 2 * margin)
    centre = (high + low) / 2
    scale = size / span

    image = np.repeat(background[None, None, :], size, 0).repeat(size, 1).astype(float)
    zbuf = np.full((size, size), -np.inf)
    key = KEY / np.linalg.norm(KEY)
    fill = FILL / np.linalg.norm(FILL)

    for (mesh, rgb), view, normals in zip(parts, views, normal_sets, strict=True):
        colour = np.asarray(rgb, dtype=float)
        screen = (view[:, :2] - centre[:2]) * scale + size / 2
        screen[:, 1] = size - screen[:, 1]
        depth = view[:, 2]
        tris = mesh.faces
        for index in range(len(tris)):
            normal = normals[index]
            if normal[2] <= 0:
                continue
            shade = (AMBIENT
                     + 0.68 * max(float(normal @ key), 0.0)
                     + 0.22 * max(float(normal @ fill), 0.0))
            face_rgb = np.clip(colour * shade, 0, 1)
            _triangle(image, zbuf, screen[tris[index]], depth[tris[index]], face_rgb, size)
    return image


def _triangle(image, zbuf, pts, zs, rgb, size) -> None:
    """Z-buffer one triangle into the image."""
    min_x, min_y = np.floor(pts.min(axis=0)).astype(int)
    max_x, max_y = np.ceil(pts.max(axis=0)).astype(int)
    min_x, min_y = max(min_x, 0), max(min_y, 0)
    max_x, max_y = min(max_x, size - 1), min(max_y, size - 1)
    if min_x > max_x or min_y > max_y:
        return
    gx, gy = np.meshgrid(np.arange(min_x, max_x + 1), np.arange(min_y, max_y + 1))
    (x0, y0), (x1, y1), (x2, y2) = pts
    denom = (y1 - y2) * (x0 - x2) + (x2 - x1) * (y0 - y2)
    if abs(denom) < 1e-12:
        return
    a = ((y1 - y2) * (gx - x2) + (x2 - x1) * (gy - y2)) / denom
    b = ((y2 - y0) * (gx - x2) + (x0 - x2) * (gy - y2)) / denom
    c = 1.0 - a - b
    inside = (a >= -1e-6) & (b >= -1e-6) & (c >= -1e-6)
    if not inside.any():
        return
    z = a * zs[0] + b * zs[1] + c * zs[2]
    window = zbuf[min_y:max_y + 1, min_x:max_x + 1]
    nearer = inside & (z > window)
    if not nearer.any():
        return
    window[nearer] = z[nearer]
    image[min_y:max_y + 1, min_x:max_x + 1][nearer] = rgb


def project(points, bounds, size: int, elevation: float = 0.0,
            azimuth: float = 0.0, margin: float = 0.04):
    """Where world points land in a rendered image, for labelling it afterwards.

    Uses the same camera as :func:`render_scene`, so passing that call's
    ``size``, ``bounds`` and angles puts a caption exactly on the feature it
    names.
    """
    view = look_at(np.asarray(points, dtype=float), elevation, azimuth)
    low, high = np.asarray(bounds[0], float), np.asarray(bounds[1], float)
    span = max((high - low)[:2].max(), 1e-6) * (1 + 2 * margin)
    centre = (high + low) / 2
    screen = (view[:, :2] - centre[:2]) * (size / span) + size / 2
    screen[:, 1] = size - screen[:, 1]
    return screen


def scene_bounds(parts, elevation: float = 0.0, azimuth: float = 0.0):
    """The view-space extent of an assembly, to frame a series of panels alike."""
    every = np.vstack([
        look_at(np.asarray(mesh.vertices, dtype=float), elevation, azimuth)
        for mesh, _rgb in parts
    ])
    return every.min(axis=0), every.max(axis=0)


def save(image: np.ndarray, path: Path) -> Path:
    """Write an RGB float array to a PNG."""
    from PIL import Image

    Image.fromarray((np.clip(image, 0, 1) * 255).astype(np.uint8)).save(path)
    return path


def main(argv: list[str] | None = None) -> int:
    """Render one or more STLs."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("models", nargs="+", type=Path)
    parser.add_argument("--out", type=Path, default=None, help="output PNG for a single model")
    parser.add_argument("--grid", type=Path, default=None, help="tile every model into one PNG")
    parser.add_argument("--labels", nargs="*", default=None)
    parser.add_argument("--size", type=int, default=560)
    parser.add_argument("--elevation", type=float, default=32.0)
    parser.add_argument("--azimuth", type=float, default=-28.0)
    args = parser.parse_args(argv)

    import trimesh
    from PIL import Image, ImageDraw

    images = []
    for path in args.models:
        mesh = trimesh.load(str(path), force="mesh")
        images.append(render(mesh, size=args.size, elevation=args.elevation,
                             azimuth=args.azimuth))
        print(f"  rendered {path.name}")

    if args.grid:
        labels = args.labels or [p.stem for p in args.models]
        columns = min(len(images), 3)
        rows = -(-len(images) // columns)
        sheet = Image.new("RGB", (columns * args.size, rows * (args.size + 34)),
                          tuple((BACKGROUND * 255).astype(int)))
        draw = ImageDraw.Draw(sheet)
        for index, image in enumerate(images):
            col, row = index % columns, index // columns
            tile = Image.fromarray((np.clip(image, 0, 1) * 255).astype(np.uint8))
            sheet.paste(tile, (col * args.size, row * (args.size + 34)))
            name = labels[index] if index < len(labels) else ""
            draw.text((col * args.size + 14, row * (args.size + 34) + args.size + 9),
                      name, fill=(60, 50, 35))
        sheet.save(args.grid)
        print(f"  wrote {args.grid}")
    elif args.out:
        save(images[0], args.out)
        print(f"  wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
