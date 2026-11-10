"""Deterministic test-video generator built from a licensed still image.

The video is SYNTHETIC: a public-domain portrait is panned/zoomed over a plain background.
It exercises the pipeline with a real face but is not a substitute for real footage; accuracy
claims must not be drawn from it. Usage:

    python -m app.vision.sample_video --out datasets/samples/astronaut-demo.mp4
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np

DEFAULT_IMAGE = (
    Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "astronaut_nasa_public_domain.png"
)


def portrait_rects(
    index: int,
    total: int,
    size: tuple[int, int] = (960, 540),
    faces: int = 1,
) -> list[tuple[int, int, int]]:
    """(x0, y0, side) of every portrait tile in frame ``index`` (deterministic)."""
    width, height = size
    t = index / max(1, total - 1)
    side = int(300 + 90 * t)  # slow zoom-in
    slot_w = width // faces
    rects = []
    for k in range(faces):
        cx = int(slot_w * k + slot_w * (0.3 + 0.4 * t))  # slow pan
        x0 = int(np.clip(cx - side // 2, 0, width - side))
        rects.append((x0, (height - side) // 2, side))
    return rects


def build_sample_video(
    out_path: Path,
    image_path: Path = DEFAULT_IMAGE,
    seconds: float = 6.0,
    fps: int = 25,
    size: tuple[int, int] = (960, 540),
    faces: int = 1,
    face_free_tail_seconds: float = 0.0,
) -> int:
    """Write an mp4 and return the number of frames written.

    ``faces`` is the number of portrait copies (the second is mirrored). The last
    ``face_free_tail_seconds`` contain no portrait at all (a genuine no-face segment).
    """
    img = cv2.imread(str(image_path))
    if img is None:
        raise FileNotFoundError(f"Cannot read image {image_path}")
    width, height = size
    out_path.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(str(out_path), cv2.VideoWriter.fourcc(*"mp4v"), fps, size)
    if not writer.isOpened():
        raise RuntimeError("OpenCV could not open an mp4 writer")
    total = int(seconds * fps)
    tail_start = total - int(face_free_tail_seconds * fps)
    yy, xx = np.mgrid[0:height, 0:width]
    background = np.stack([(40 + 25 * xx / width), (36 + 20 * yy / height), (46 + 0 * xx)], axis=-1)
    background = background.astype(np.uint8)
    try:
        for i in range(total):
            frame = background.copy()
            if i < tail_start:
                for k, (x0, y0, side) in enumerate(portrait_rects(i, total, size, faces)):
                    tile = cv2.resize(img, (side, side), interpolation=cv2.INTER_AREA)
                    frame[y0 : y0 + side, x0 : x0 + side] = tile if k == 0 else cv2.flip(tile, 1)
            writer.write(frame)
    finally:
        writer.release()
    return total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--image", type=Path, default=DEFAULT_IMAGE)
    parser.add_argument("--seconds", type=float, default=8.0)
    parser.add_argument("--fps", type=int, default=25)
    parser.add_argument("--faces", type=int, default=1, choices=[1, 2])
    parser.add_argument("--no-face-tail", type=float, default=0.0)
    args = parser.parse_args(argv)
    n = build_sample_video(
        args.out,
        args.image,
        args.seconds,
        args.fps,
        faces=args.faces,
        face_free_tail_seconds=args.no_face_tail,
    )
    print(f"wrote {args.out} ({n} frames)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
