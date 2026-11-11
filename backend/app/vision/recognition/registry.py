"""Pinned recognition-side model descriptors (embedder + landmark detector) with explicit fetch.

Nothing is downloaded implicitly. ``python -m app.vision.recognition.registry fetch`` downloads
the files into ``<models dir>/face-recognizer/`` and verifies the pinned SHA-256; every model is
documented with its licence in ``docs/decisions/0013-recognition-model-and-licensing.md``.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from app.core.paths import resolve_path
from app.vision.detection.model_registry import ModelFetchError, sha256_of


@dataclass(frozen=True)
class RecognitionModelSpec:
    key: str
    filename: str
    url: str
    sha256: str
    size_bytes: int
    source: str
    revision: str
    license: str
    notes: str
    role: str  # "embedder" | "landmarks"
    embedding_dim: int = 0
    input_size: int = 112


SFACE = RecognitionModelSpec(
    key="sface-2021dec",
    filename="face_recognition_sface_2021dec.onnx",
    url=(
        "https://huggingface.co/opencv/face_recognition_sface/resolve/main/"
        "face_recognition_sface_2021dec.onnx"
    ),
    sha256="0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79",
    size_bytes=38_696_353,
    source="https://github.com/opencv/opencv_zoo/tree/main/models/face_recognition_sface",
    revision="OpenCV Zoo, file face_recognition_sface_2021dec.onnx (fp32)",
    license=(
        "Apache-2.0 (declared by the OpenCV Zoo for all files in the directory); the zoo does NOT "
        "state the training data. The SFace paper reports experiments on CASIA-WebFace, VGGFace2 "
        "and MS-Celeb-1M, whose own terms are research-oriented - treat as non-commercial until clarified"
    ),
    notes="MobileFaceNet trained with the SFace loss; 112x112 aligned input, 128-d embedding.",
    role="embedder",
    embedding_dim=128,
)

YUNET = RecognitionModelSpec(
    key="yunet-2023mar",
    filename="face_detection_yunet_2023mar.onnx",
    url=(
        "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/"
        "face_detection_yunet_2023mar.onnx"
    ),
    sha256="8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
    size_bytes=232_589,
    source="https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet",
    revision="OpenCV Zoo, face_detection_yunet_2023mar.onnx",
    license="MIT (OpenCV Zoo / ShiqiYu libfacedetection.train)",
    notes="Used only to obtain the 5 facial landmarks needed for alignment (not as the detector).",
    role="landmarks",
)

RECOGNITION_MODELS: dict[str, RecognitionModelSpec] = {SFACE.key: SFACE, YUNET.key: YUNET}


def model_path(models_dir: Path, spec: RecognitionModelSpec) -> Path:
    return models_dir / "face-recognizer" / spec.filename


def verify_model_file(path: Path, spec: RecognitionModelSpec) -> None:
    if not path.is_file():
        raise ModelFetchError(
            f"Recognition model not found: {path}. Run scripts\\download-model.ps1 -Recognition "
            "(python -m app.vision.recognition.registry fetch)."
        )
    if sha256_of(path) != spec.sha256:
        raise ModelFetchError(f"{path} does not match the pinned checksum; delete it and re-fetch.")


def fetch_model(models_dir: Path, spec: RecognitionModelSpec, force: bool = False) -> Path:
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
    manifest_path = target.parent / "manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8")) if manifest_path.is_file() else {}
    manifest[spec.key] = {
        "file": spec.filename,
        "sha256": spec.sha256,
        "size_bytes": spec.size_bytes,
        "source": spec.source,
        "revision": spec.revision,
        "license": spec.license,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return target


def recognition_status(models_dir: Path) -> tuple[bool, str | None]:
    """(available, actionable detail) for both required files; cheap size check + checksum."""
    for spec in (SFACE, YUNET):
        try:
            verify_model_file(model_path(models_dir, spec), spec)
        except ModelFetchError as exc:
            return False, str(exc)
    return True, None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Face recognition model management")
    parser.add_argument("command", choices=["fetch", "verify"])
    parser.add_argument("--models-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    from app.core.settings import get_settings

    models_dir = resolve_path(args.models_dir or get_settings().models_path)
    try:
        for spec in (SFACE, YUNET):
            if args.command == "fetch":
                path = fetch_model(models_dir, spec)
                print(f"OK {path} ({path.stat().st_size} bytes, sha256 verified)")
            else:
                verify_model_file(model_path(models_dir, spec), spec)
                print(f"OK {spec.key} checksum matches")
    except ModelFetchError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
