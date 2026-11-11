"""ONNX Runtime face embedder (SFace, 128-d) with validated, L2-normalised output.

Preprocessing (verified against OpenCV's reference ``FaceRecognizerSF`` in the tests: cosine 1.0):
the aligned 112x112 BGR crop is converted to **RGB**, pixel values stay 0-255 as float32, layout
NCHW, no mean/std scaling. (Feeding BGR gives a different, worse embedding: cosine ~0.95.)
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import numpy as np
import onnxruntime as ort

from app.vision.recognition.registry import RecognitionModelSpec

logger = logging.getLogger("katomaran.recognition")


class EmbeddingError(Exception):
    code = "embedding_error"


def normalize(vector: np.ndarray) -> np.ndarray:
    v = np.asarray(vector, dtype=np.float32).reshape(-1)
    if not np.isfinite(v).all():
        raise EmbeddingError("embedding contains non-finite values")
    norm = float(np.linalg.norm(v))
    if norm < 1e-6:
        raise EmbeddingError("embedding has (near) zero norm")
    out: np.ndarray = v / norm
    return out


def validate_embedding(vector: np.ndarray, dim: int) -> np.ndarray:
    v = np.asarray(vector, dtype=np.float32).reshape(-1)
    if v.shape[0] != dim:
        raise EmbeddingError(f"expected a {dim}-d embedding, got {v.shape[0]}")
    return normalize(v)


class OrtEmbedder:
    def __init__(self, model_path: Path, spec: RecognitionModelSpec, threads: int = 2) -> None:
        if not model_path.is_file():
            raise EmbeddingError(f"embedding model not found: {model_path}")
        self.spec = spec
        opts = ort.SessionOptions()
        opts.log_severity_level = 3
        opts.intra_op_num_threads = threads
        opts.inter_op_num_threads = 1
        opts.add_session_config_entry("session.intra_op.allow_spinning", "0")
        started = time.perf_counter()
        try:
            self._session = ort.InferenceSession(
                str(model_path), opts, providers=["CPUExecutionProvider"]
            )
        except Exception as exc:  # noqa: BLE001
            raise EmbeddingError(f"could not load the embedding model: {exc}") from exc
        self.load_seconds = time.perf_counter() - started
        inp = self._session.get_inputs()[0]
        self._input = inp.name
        if list(inp.shape[1:]) != [3, spec.input_size, spec.input_size]:
            raise EmbeddingError(f"unexpected model input shape {inp.shape}")
        out_shape = self._session.get_outputs()[0].shape
        if out_shape[-1] != spec.embedding_dim:
            raise EmbeddingError(f"unexpected model output shape {out_shape}")
        self.model_version = spec.key
        self.dim = spec.embedding_dim
        self.last_ms = 0.0
        logger.info(
            "Recognition model loaded",
            extra={"model": spec.key, "dim": self.dim, "load_s": round(self.load_seconds, 2)},
        )

    def embed(self, aligned_bgr: np.ndarray) -> np.ndarray:
        size = self.spec.input_size
        if aligned_bgr.shape != (size, size, 3) or aligned_bgr.dtype != np.uint8:
            raise EmbeddingError(f"expected an aligned {size}x{size} BGR uint8 crop")
        rgb = aligned_bgr[:, :, ::-1]
        blob = np.ascontiguousarray(rgb.transpose(2, 0, 1)[None].astype(np.float32))
        started = time.perf_counter()
        try:
            out = self._session.run(None, {self._input: blob})[0]
        except Exception as exc:  # noqa: BLE001
            raise EmbeddingError(f"embedding inference failed: {exc}") from exc
        self.last_ms = (time.perf_counter() - started) * 1000
        return validate_embedding(out, self.dim)
