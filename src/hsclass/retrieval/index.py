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


def _top_k(scores: np.ndarray, k: int) -> np.ndarray:
    """The k best rows, highest score first, ties broken by lowest row.

    Why this is not just ``argpartition`` followed by a stable sort, which is
    what it used to be. ``argpartition`` chooses WHICH k rows it returns, and
    when the k-th and (k+1)-th scores are equal it picks among the tied rows
    arbitrarily. The stable sort that followed was stable with respect to
    *that arbitrary output order*, not with respect to row number, so neither
    the membership nor the order was a function of the data alone.

    Say plainly what this is worth, because the first version of this comment
    overstated it. ``search`` has one caller in the repository -
    ``VectorOnlyClassifier``, which is ablation 1 - and it runs on the flat
    ``index``. The hierarchical path never calls it: ``LevelIndex.rank``
    multiplies ``vectors[rows] @ query`` itself. Measured over the 200 test
    queries against the old selection:

      index                  set differs   order differs   (k = 5 / 20 / 50)
      index                    0 / 0 / 0       0 / 0 / 0
      index-levels             0 / 0 / 0       0 / 0 / 1
      index-levels-own-text   42 / 46 / 60    85 / 163 / 188

    So on the index ``search`` is actually used against, the old code was
    already giving the deterministic answer, and this fix corrects nothing
    that any report measures. The own-text column is large because stripping
    the inherited chain leaves 6,370 provisions reading "Other" - 18,372 of
    its 29,861 rows sit in a byte-identical vector group - but nothing calls
    ``search`` on that index either. It is fixed because a function should not
    return build-dependent results, not because a number moved.

    The cost of being right is small, which is why the whole array is still
    not sorted: argpartition finds the cut, then the boundary is resolved
    explicitly. At 29,861 candidates and k=50 this costs about 1.4x
    argpartition alone, against about 30x for a full deterministic sort. The
    absolute figures move with the machine, so the ratios are what is quoted.
    """
    if k <= 0:
        return np.empty(0, dtype=np.intp)
    part = np.argpartition(-scores, kth=k - 1)[:k]
    cut = scores[part].min()
    # Everything strictly better than the cut is in, no question. The rows
    # sitting exactly on it are the contested ones, and flatnonzero returns
    # them in ascending row order, so taking a prefix takes the lowest rows.
    above = np.flatnonzero(scores > cut)
    tied = np.flatnonzero(scores == cut)
    chosen = np.concatenate((above, tied[: k - len(above)]))
    if len(chosen) < k:
        # Only reachable if a score is NaN: the cut is then NaN, both
        # comparisons above are False for every row, and this would hand back
        # an empty list that the caller has no way to tell from "nothing
        # matched". Not reachable today - _normalise clamps the norm, so no
        # text produces a NaN - which is why this is a cheap length check
        # rather than an isnan scan over 30,000 scores on every query. The
        # fallback is the slow, obviously-correct ordering, and numpy sorts
        # NaN last, so k rows still come back.
        return np.lexsort((np.arange(len(scores)), -scores))[:k]
    return chosen[np.argsort(-scores[chosen], kind="stable")]


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
        results: list[list[Hit]] = []
        for row in range(scores.shape[0]):
            ordered = _top_k(scores[row], k)
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
