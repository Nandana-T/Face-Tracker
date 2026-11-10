"""Pinned face-detector model descriptors and an explicit, checksum-verified fetch step.

Models are NEVER downloaded implicitly (not at API start-up, not at worker start-up). Run
``python -m app.vision.detection.model_registry fetch`` (``scripts\\download-model.ps1``) once.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from app.core.paths import resolve_path


@dataclass(frozen=True)
class ModelSpec:
    key: str
    filename: str
    url: str
    sha256: str
    size_bytes: int
    source: str
    revision: str
    license: str
    notes: str
    input_size: int = 640


YOLOV11N_FACE = ModelSpec(
    key="yolov11n-face",
    filename="yolov11n-face.onnx",
    url=(
        "https://huggingface.co/AdamCodd/YOLOv11n-face-detection/resolve/"
        "0e97fea5eacb1460b35725c929d813c7095841b5/model.onnx"
    ),
    sha256="2dfe14171f5b76a05f9bcf0dac7f94b7bff4416b1f29eff7c9ef5830f51c5719",
    size_bytes=10_583_429,
    source="https://huggingface.co/AdamCodd/YOLOv11n-face-detection",
    revision="0e97fea5eacb1460b35725c929d813c7095841b5",
    license="apache-2.0 (model card); base architecture Ultralytics YOLO11 is AGPL-3.0 - see docs",
    notes="YOLO11-nano fine-tuned on WIDERFACE, single class 'face'.",
)

MODELS: dict[str, ModelSpec] = {YOLOV11N_FACE.key: YOLOV11N_FACE}


class ModelFetchError(Exception):
    pass


def model_path(models_dir: Path, spec: ModelSpec) -> Path:
    return models_dir / "face-detector" / spec.filename


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_model_file(path: Path, spec: ModelSpec) -> None:
    """Raise ModelFetchError with an actionable message if the file is missing or corrupted."""
    if not path.is_file():
        raise ModelFetchError(
            f"Model file not found: {path}. Download it with scripts\\download-model.ps1 "
            "(python -m app.vision.detection.model_registry fetch)."
        )
    if sha256_of(path) != spec.sha256:
        raise ModelFetchError(
            f"Model file {path} does not match the pinned checksum; delete it and re-run the fetch."
        )


def fetch_model(models_dir: Path, spec: ModelSpec, force: bool = False) -> Path:
    target = model_path(models_dir, spec)
    if target.is_file() and not force:
        verify_model_file(target, spec)
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".part")
    try:
        with urllib.request.urlopen(spec.url, timeout=60) as response, partial.open("wb") as out:  # noqa: S310
            while chunk := response.read(1 << 20):
                out.write(chunk)
    except OSError as exc:
        partial.unlink(missing_ok=True)
        raise ModelFetchError(f"Could not download {spec.url}: {exc}") from exc
    if sha256_of(partial) != spec.sha256:
        partial.unlink(missing_ok=True)
        raise ModelFetchError("Downloaded file failed checksum verification; nothing was saved.")
    partial.replace(target)
    manifest = {
        "key": spec.key,
        "file": spec.filename,
        "sha256": spec.sha256,
        "size_bytes": spec.size_bytes,
        "source": spec.source,
        "revision": spec.revision,
        "license": spec.license,
    }
    (target.parent / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Face detector model management")
    parser.add_argument("command", choices=["fetch", "verify"])
    parser.add_argument("--model", default=YOLOV11N_FACE.key, choices=sorted(MODELS))
    parser.add_argument("--models-dir", type=Path, default=None)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    from app.core.settings import get_settings

    models_dir = args.models_dir or get_settings().models_path
    models_dir = resolve_path(models_dir)
    spec = MODELS[args.model]
    try:
        if args.command == "fetch":
            path = fetch_model(models_dir, spec, force=args.force)
            print(f"OK {path} ({path.stat().st_size} bytes, sha256 verified)")
        else:
            verify_model_file(model_path(models_dir, spec), spec)
            print("OK checksum matches")
    except ModelFetchError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
