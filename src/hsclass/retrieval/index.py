"""An exact vector index over the tariff lines.

Exact search, not approximate. The whole schedule is about 24,000 terminal
lines, which is a 24,000 x 384 matrix - a single matmul per query batch, in
milliseconds. An approximate index would add a recall variable to every
later ablation for no measurable speed-up at this size, and would make the
determinism tests harder to argue.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from hsclass.retrieval.embedder import Embedder

#: How many texts to embed between progress lines.
PROGRESS_CHUNK = 2000

#: Separators inside the fingerprint, so that a code ending where the next
#: text begins cannot hash the same as a different split of the same bytes.
_FIELD_END = b"\x00"
_RECORD_END = b"\x01"


class StaleIndex(RuntimeError):
    """A cached index does not match the candidates it is being used for."""


def fingerprint(candidates: list[Candidate]) -> str:
    """A hash of exactly what was embedded: every code and every text.

    A saved index already records its embedder and its schedule revision,
    and both can match while the index is stale anyway - a parser fix
    changes the *contents* of a revision without changing its name. That
    happened: the cached levelled index held 30,195 provisions while the
    current parser produces 29,861, a 342-provision gap with 494 further
    texts differing, and nothing in the repository noticed. Two arms of the
    chunking ablation were compared across that gap.

    ``make check`` validates the tree and reconciles it against the USITC
    export; neither looks at the index. This is the check that does.
    """
    digest = hashlib.blake2b(digest_size=16)
    for candidate in candidates:
        digest.update(candidate.code.encode("utf-8"))
        digest.update(_FIELD_END)
        digest.update(candidate.text.encode("utf-8"))
        digest.update(_RECORD_END)
    return digest.hexdigest()


@dataclass
class Candidate:
    """One searchable tariff line."""

    code: str
    text: str
    chapter: str | None = None
    section: str | None = None


@dataclass
class Hit:
    code: str
    text: str
    score: float


class VectorIndex:
    def __init__(self, candidates: list[Candidate], vectors: np.ndarray, embedder_name: str,
                 revision: str) -> None:
        if len(candidates) != vectors.shape[0]:
            raise ValueError("one vector per candidate is required")
        self.candidates = candidates
        self.vectors = np.ascontiguousarray(vectors, dtype=np.float32)
        self.embedder_name = embedder_name
        self.revision = revision

    def __len__(self) -> int:
        return len(self.candidates)

    @classmethod
    def build(cls, candidates: list[Candidate], embedder: Embedder, revision: str,
              progress: bool = False) -> VectorIndex:
        texts = [c.text for c in candidates]
        if not progress:
            return cls(candidates, embedder.encode(texts), embedder.name, revision)

        # Embed in chunks so a long build reports where it is rather than
        # going silent for several minutes.
        started = time.time()
        chunks: list[np.ndarray] = []
        done = 0
        for start in range(0, len(texts), PROGRESS_CHUNK):
            chunk = texts[start:start + PROGRESS_CHUNK]
            chunks.append(embedder.encode(chunk))
            done += len(chunk)
            rate = done / max(time.time() - started, 1e-9)
            remaining = (len(texts) - done) / max(rate, 1e-9)
            print(f"  embedded {done}/{len(texts)} ({rate:.0f}/s, ~{remaining:.0f}s left)",
                  flush=True)
        return cls(candidates, np.vstack(chunks), embedder.name, revision)

    def search(self, queries: np.ndarray, k: int = 20) -> list[list[Hit]]:
        """Cosine similarity, because both sides are already normalised."""
        scores = np.asarray(queries, dtype=np.float32) @ self.vectors.T
        k = min(k, len(self.candidates))
        # argpartition finds the top k without sorting all 24,000
        top = np.argpartition(-scores, kth=k - 1, axis=1)[:, :k]
        results: list[list[Hit]] = []
        for row, columns in enumerate(top):
            ordered = columns[np.argsort(-scores[row, columns])]
            results.append(
                [Hit(self.candidates[i].code, self.candidates[i].text, float(scores[row, i]))
                 for i in ordered]
            )
        return results

    def save(self, directory: Path | str) -> Path:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        np.save(directory / "vectors.npy", self.vectors)
        (directory / "candidates.json").write_text(
            json.dumps({
                "embedder": self.embedder_name,
                "revision": self.revision,
                "fingerprint": fingerprint(self.candidates),
                "candidates": [c.__dict__ for c in self.candidates],
            }),
            encoding="utf-8",
        )
        return directory

    @classmethod
    def load(
        cls, directory: Path | str, expecting: list[Candidate] | None = None
    ) -> VectorIndex:
        """Load a saved index, optionally checking that it is still current.

        ``expecting`` is the candidate list the caller is about to use this
        index for. Passing it turns a silent staleness into an error, which
        is the only way a stale index gets noticed - the revision and the
        embedder both still match across a parser change.
        """
        directory = Path(directory)
        meta = json.loads((directory / "candidates.json").read_text(encoding="utf-8"))
        candidates = [Candidate(**c) for c in meta["candidates"]]
        if expecting is not None:
            wanted = fingerprint(expecting)
            if meta.get("fingerprint") != wanted:
                raise StaleIndex(
                    f"{directory} holds {len(candidates)} provisions "
                    f"({meta.get('fingerprint', 'unfingerprinted')}); the caller "
                    f"expects {len(expecting)} ({wanted}). Rebuild it."
                )
        return cls(
            candidates,
            np.load(directory / "vectors.npy"),
            meta["embedder"],
            meta["revision"],
        )
