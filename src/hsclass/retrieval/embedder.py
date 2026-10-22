"""Text embedding, behind an interface.

The backend is swappable on purpose. Week 4 has to show that the same input
returns the same code across two providers, which is only a meaningful claim
if nothing above this layer knows which one is in use.

The default runs locally on CPU: no API key, no per-request cost, and the
same vector every time, which matters because the determinism tests later
compare runs rather than tolerances.
"""

from __future__ import annotations

import hashlib
from typing import Protocol

import numpy as np


class Embedder(Protocol):
    """Returns L2-normalised vectors, so a dot product is a cosine."""

    name: str
    dimensions: int

    def encode(self, texts: list[str]) -> np.ndarray: ...


def _normalise(vectors: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    return vectors / np.maximum(norms, 1e-12)


class LocalEmbedder:
    """BGE-small through ONNX Runtime, on CPU.

    sentence-transformers would be the obvious choice and runs the same model,
    but it pulls in PyTorch, and PyTorch's DLLs are refused by the Windows
    Application Control policy on this machine::

        OSError: [WinError 4551] An Application Control policy has blocked
        this file. Error loading "...torch\\lib\\torch_python.dll"

    ONNX Runtime is permitted, so the model is run through that instead. The
    weights are identical, and the artefact is a few hundred megabytes rather
    than a few gigabytes - which also makes it a more honest thing to put in
    a container later.
    """

    def __init__(self, model_name: str = "BAAI/bge-small-en-v1.5", batch_size: int = 256) -> None:
        from fastembed import TextEmbedding

        self.name = model_name
        self.batch_size = batch_size
        self._model = TextEmbedding(model_name)
        self.dimensions = 384

    def encode(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dimensions), dtype=np.float32)
        vectors = np.asarray(
            list(self._model.embed(texts, batch_size=self.batch_size)), dtype=np.float32
        )
        # fastembed normalises already; this keeps the contract explicit.
        return _normalise(vectors)


class HashingEmbedder:
    """A deterministic bag-of-words embedder with no model behind it.

    Not a serious retriever - it exists so the index, the classifier and the
    eval harness can be tested without downloading a model, and so a machine
    with no model cache can still run the suite.
    """

    def __init__(self, dimensions: int = 256) -> None:
        self.name = f"hashing-{dimensions}"
        self.dimensions = dimensions

    def encode(self, texts: list[str]) -> np.ndarray:
        vectors = np.zeros((len(texts), self.dimensions), dtype=np.float32)
        for row, text in enumerate(texts):
            for token in text.lower().split():
                digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
                vectors[row, int.from_bytes(digest, "big") % self.dimensions] += 1.0
        return _normalise(vectors)


def get_embedder(backend: str = "local", model: str = "BAAI/bge-small-en-v1.5") -> Embedder:
    if backend == "local":
        return LocalEmbedder(model)
    if backend == "hashing":
        return HashingEmbedder()
    raise ValueError(f"unknown embedding backend {backend!r}")
