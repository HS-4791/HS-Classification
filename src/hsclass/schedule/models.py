from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

#: The nomenclature levels, in legal order. "section" is not encoded in the
#: digits anywhere - it has to be carried separately, and Section notes are
#: what decide cases like 6109.10 vs 6109.90.
LEVELS = ("section", "chapter", "heading", "subheading", "tariff_line", "statistical")

_DIGITS_TO_LEVEL = {
    2: "chapter", 4: "heading", 6: "subheading", 8: "tariff_line", 10: "statistical",
}

#: Chapters 28, 29, 39 and others are divided into Roman-numeral subchapters
#: ("I. CHEMICAL ELEMENTS"). The export emits them as uncoded lines that scope
#: the headings that follow, but its indent does not make them parents.
SUBCHAPTER_RE = re.compile(r"^(?P<num>[IVXL]+)\.\s+(?P<title>.+?):?$")

#: Chapters 98 and 99 are US special classification provisions. They reference
#: goods of other chapters, so their nesting does not follow digit prefixes.
SPECIAL_CHAPTERS = {"98", "99"}


def digits(code: str | None) -> str:
    return "".join(ch for ch in (code or "") if ch.isdigit())


@dataclass
class TariffNode:
    key: str
    description: str
    indent: int
    htsno: str | None = None
    superior: bool = False
    units: list[str] = field(default_factory=list)
    general: str = ""
    special: str = ""
    other: str = ""
    footnotes: list = field(default_factory=list)
    parent: str | None = None
    children: list[str] = field(default_factory=list)
    chapter: str | None = None
    subchapter: str | None = None
    section: str | None = None
    synthetic: bool = False

    @property
    def is_grouping(self) -> bool:
        """A line with no code of its own. It still binds everything beneath it."""
        return not self.htsno

    @property
    def level(self) -> str | None:
        return _DIGITS_TO_LEVEL.get(len(digits(self.htsno)))

    @property
    def kind(self) -> str:
        if self.synthetic:
            return "chapter"
        if self.is_grouping:
            return "subchapter" if SUBCHAPTER_RE.match(self.description) else "grouping"
        return "line"

    @property
    def is_special_provision(self) -> bool:
        return (self.chapter or "") in SPECIAL_CHAPTERS

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["level"] = self.level
        return d


class ScheduleTree:
    """The nomenclature as a tree. Not chunked - the hierarchy is the law."""

    def __init__(self, revision: str) -> None:
        self.revision = revision
        self.nodes: dict[str, TariffNode] = {}
        self.roots: list[str] = []
        self._by_htsno: dict[str, str] = {}

    def add(self, node: TariffNode) -> None:
        self.nodes[node.key] = node
        if node.parent is None:
            self.roots.append(node.key)
        else:
            self.nodes[node.parent].children.append(node.key)
        if node.htsno:
            self._by_htsno.setdefault(node.htsno, node.key)

    def __len__(self) -> int:
        return len(self.nodes)

    def __iter__(self) -> Iterator[TariffNode]:
        return iter(self.nodes.values())

    def get(self, key: str) -> TariffNode:
        return self.nodes[key]

    def by_code(self, htsno: str) -> TariffNode | None:
        key = self._by_htsno.get(htsno)
        return self.nodes[key] if key else None

    def ancestors(self, key: str) -> list[TariffNode]:
        out: list[TariffNode] = []
        node = self.nodes[key]
        while node.parent is not None:
            node = self.nodes[node.parent]
            out.append(node)
        return list(reversed(out))

    def inherited_chain(self, key: str) -> list[str]:
        """Every description from the root down to this node.

        "6109.10" reads as just "Of cotton". Embedded on its own it is
        meaningless; the embeddable unit is the whole chain.
        """
        chain = [a.description for a in self.ancestors(key) if a.description]
        node = self.nodes[key]
        if node.description:
            chain.append(node.description)
        return chain

    def full_description(self, key: str, sep: str = " \u2014 ") -> str:
        return sep.join(part.rstrip(":") for part in self.inherited_chain(key))

    def leaves(self) -> list[TariffNode]:
        return [n for n in self.nodes.values() if not n.children]

    def stats(self) -> dict:
        coded = [n for n in self.nodes.values() if n.htsno]
        by_len: dict[int, int] = {}
        for n in coded:
            by_len[len(digits(n.htsno))] = by_len.get(len(digits(n.htsno)), 0) + 1
        return {
            "revision": self.revision,
            "nodes": len(self.nodes),
            "coded": len(coded),
            "grouping": len(self.nodes) - len(coded),
            "synthetic": sum(1 for n in self.nodes.values() if n.synthetic),
            "roots": len(self.roots),
            "leaves": len(self.leaves()),
            "by_digit_length": dict(sorted(by_len.items())),
            "chapters": len({n.chapter for n in self.nodes.values() if n.chapter}),
        }


def build_from_export(rows: Iterable[dict], revision: str) -> ScheduleTree:
    """Build the tree from the USITC JSON export.

    The export is flat: each row carries an ``indent`` and the reader is
    expected to infer parentage from it. Three things it does not give you:

    * no Section or Chapter rows at all - Chapter nodes are synthesised here,
      and Section is left for the PDF parse to fill in
    * ``indent`` is zero-padded on a handful of rows ("03"), so it is coerced
      to int rather than compared as a string
    * a Roman-numeral subchapter divider is emitted *before* the chapter it
      belongs to begins, so it is held back and applied to the next chapter
    """
    rows = list(rows)
    tree = ScheduleTree(revision)
    stack: list[tuple[int, str]] = []  # (indent, key)
    current_chapter: str | None = None
    current_subchapter: str | None = None
    pending_subchapter: str | None = None
    chapter_key: str | None = None
    divider_keys: list[tuple[str, int]] = []

    for i, row in enumerate(rows):
        htsno = (row.get("htsno") or "").strip() or None
        indent = int(str(row.get("indent") or "0"))
        description = (row.get("description") or "").strip()
        code_digits = digits(htsno)

        if code_digits:
            chapter = code_digits[:2]
            if chapter != current_chapter:
                current_chapter = chapter
                # A divider seen just before this chapter opened belongs to it.
                current_subchapter = pending_subchapter
                pending_subchapter = None
                chapter_key = f"ch{chapter}"
                tree.add(
                    TariffNode(
                        key=chapter_key,
                        description=f"Chapter {int(chapter)}",
                        indent=-1,
                        htsno=chapter,
                        chapter=chapter,
                        synthetic=True,
                    )
                )
                stack = []
        elif SUBCHAPTER_RE.match(description):
            pending_subchapter = description.rstrip(":")
            divider_keys.append((f"n{i:06d}", i))

        while stack and stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1] if stack else chapter_key

        key = f"n{i:06d}"
        tree.add(
            TariffNode(
                key=key,
                description=description,
                indent=indent,
                htsno=htsno,
                superior=str(row.get("superior")).lower() == "true",
                units=list(row.get("units") or []),
                general=row.get("general") or "",
                special=row.get("special") or "",
                other=row.get("other") or "",
                footnotes=list(row.get("footnotes") or []),
                parent=parent,
                chapter=current_chapter,
                subchapter=pending_subchapter or current_subchapter,
            )
        )
        stack.append((indent, key))

    # A divider was filed under whatever was still open when it was read -
    # the previous chapter, or nothing at all when it opens the file. Re-file
    # it under the chapter it actually introduces.
    for key, i in divider_keys:
        node = tree.get(key)
        nxt = next((r for r in rows[i + 1:] if (r.get("htsno") or "").strip()), None)
        node.subchapter = node.description.rstrip(":")
        if nxt is None:
            continue
        chapter = digits(nxt["htsno"])[:2]
        node.chapter = chapter
        parent_key = f"ch{chapter}"
        if parent_key not in tree.nodes or node.parent == parent_key:
            continue
        if node.parent is None:
            tree.roots.remove(key)
        else:
            tree.get(node.parent).children.remove(key)
        node.parent = parent_key
        tree.get(parent_key).children.append(key)

    return tree


def load_export(path: Path) -> list[dict]:
    return json.loads(Path(path).read_text(encoding="utf-8"))
