from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

from hsclass.schedule.models import ScheduleTree, digits

#: A leaf may legitimately stop at 8 digits when a tariff line carries no
#: statistical breakout, so both lengths are destinations.
TERMINAL_DIGIT_LENGTHS = (8, 10)


@dataclass
class Issue:
    check: str
    severity: str  # "error" | "warn"
    message: str
    node: str | None = None


@dataclass
class ValidationReport:
    revision: str
    stats: dict
    issues: list[Issue] = field(default_factory=list)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "warn"]

    @property
    def passed(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict:
        return {
            "revision": self.revision,
            "passed": self.passed,
            "stats": self.stats,
            "counts": {
                "errors": len(self.errors),
                "warnings": len(self.warnings),
                "by_check": dict(Counter(i.check for i in self.issues)),
                "by_severity_and_check": {
                    severity: dict(Counter(i.check for i in self.issues if i.severity == severity))
                    for severity in ("error", "warn")
                },
            },
            "issues": [asdict(i) for i in self.issues[:200]],
        }

    def write(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        return path


def _severity(node, base: str = "error") -> str:
    """Structural rules are binding on the nomenclature, not on chapters 98-99.

    Those are US special classification provisions: they re-describe goods
    that are classified elsewhere, so they do not nest by digit prefix, they
    carry effective-date columns instead of the ordinary rate grid, and their
    grouping lines routinely stand alone. Holding them to the same structural
    rules reports hundreds of failures that say nothing about the tree the
    classifier actually walks, so they are recorded as warnings and counted
    separately rather than quietly skipped.
    """
    return "warn" if node.is_special_provision else base


def _nearest_coded_ancestor(tree: ScheduleTree, key: str):
    for anc in reversed(tree.ancestors(key)):
        if anc.htsno and not anc.synthetic:
            return anc
    return None


def validate_tree(tree: ScheduleTree, manifest: dict | None = None) -> ValidationReport:
    """The four week-1 assertions, plus the two that catch silent corruption.

    A corrupt tree does not raise - it answers confidently and wrongly. These
    checks are the deliverable, not a formality.
    """
    issues: list[Issue] = []
    add = issues.append

    # 1. Exactly one parent, and parent/child links agree in both directions.
    seen_as_child: dict[str, str] = {}
    for node in tree:
        for child in node.children:
            if child in seen_as_child:
                add(Issue("single_parent", "error",
                          f"{child} is a child of both {seen_as_child[child]} "
                          f"and {node.key}", child))
            seen_as_child[child] = node.key
        if node.parent is not None and node.key not in tree.get(node.parent).children:
            add(Issue("link_symmetry", "error",
                      f"{node.key} names {node.parent} as parent but is not "
                      f"among its children", node.key))
        if node.parent is None and not node.synthetic:
            add(Issue("orphan", "error",
                      f"{node.key} has no parent and is not a synthetic root", node.key))

    # 2. Every leaf is a destination: it carries a code of terminal length.
    #    A subchapter divider is an exception - the export emits it as a label
    #    beside the headings it scopes, not as their parent, so it is childless
    #    by construction rather than by corruption.
    for leaf in tree.leaves():
        if leaf.synthetic:
            add(Issue("empty_chapter", "warn", f"chapter {leaf.htsno} has no lines", leaf.key))
        elif leaf.kind == "subchapter":
            continue
        elif leaf.is_grouping:
            add(Issue("grouping_leaf", _severity(leaf),
                      "grouping line is a leaf, its children were lost: "
                      f"{leaf.description!r}", leaf.key))
        elif len(digits(leaf.htsno)) not in TERMINAL_DIGIT_LENGTHS:
            add(Issue("short_leaf", _severity(leaf),
                      f"leaf {leaf.htsno} is not a full-length code", leaf.key))

    # 3. A coded node must sit under its own code prefix. This is the real
    #    parentage test - stronger than indent, because the digits cannot lie.
    #    Chapters 98 and 99 are exempt: they are US special provisions that
    #    reference goods of other chapters, so they do not nest by prefix.
    prefix_ok: set[str] = set()
    for node in tree:
        if not node.htsno or node.synthetic:
            continue
        anc = _nearest_coded_ancestor(tree, node.key)
        if anc is None:
            continue
        if digits(node.htsno).startswith(digits(anc.htsno)):
            prefix_ok.add(node.key)
        elif node.is_special_provision:
            add(Issue("special_provision_nesting", "warn",
                      f"{node.htsno} nests under {anc.htsno} (chapter 98/99 cross-reference)",
                      node.key))
        else:
            add(Issue("prefix_break", "error",
                      f"{node.htsno} is nested under {anc.htsno}", node.key))

    # 4. No level jumps greater than one. The published indent is not
    #    contiguous everywhere - stat suffixes sometimes sit two or three
    #    levels below their tariff line with nothing in between - so a jump is
    #    only an error when the code prefix does not vouch for the parentage.
    for node in tree:
        if node.parent is None:
            continue
        parent = tree.get(node.parent)
        if parent.synthetic or node.indent - parent.indent <= 1:
            continue
        severity = "warn" if node.key in prefix_ok else _severity(node)
        add(Issue("level_jump", severity,
                  f"{node.htsno or node.description!r} jumps from indent "
                  f"{parent.indent} to {node.indent}", node.key))

    # 5. A line ending in a colon introduces children - unless it already
    #    carries a terminal code, which happens in the published data.
    for node in tree:
        if not node.description.endswith(":") or node.children or node.kind == "subchapter":
            continue
        terminal = len(digits(node.htsno)) in TERMINAL_DIGIT_LENGTHS
        add(Issue("childless_branch", "warn" if terminal else _severity(node),
                  f"{node.description!r} ends in a colon but has no children", node.key))

    # 6. Counts reconcile against the published schedule.
    stats = tree.stats()
    if manifest:
        expected = manifest.get("expected_tree", {})
        real = stats["nodes"] - stats["synthetic"]
        if "total_rows" in expected and real != expected["total_rows"]:
            add(Issue("count_reconcile", "error",
                      f"tree holds {real} published rows, manifest expects "
                      f"{expected['total_rows']}"))
        for length, want in (expected.get("by_digit_length") or {}).items():
            got = stats["by_digit_length"].get(int(length), 0)
            if got != want:
                add(Issue("count_reconcile", "error",
                          f"{length}-digit lines: tree has {got}, manifest expects {want}"))
        if "chapters_present" in expected and stats["chapters"] != expected["chapters_present"]:
            add(Issue("count_reconcile", "error",
                      f"tree has {stats['chapters']} chapters, manifest expects "
                      f"{expected['chapters_present']}"))

    # 7. Section is not in the digits; until the PDF parse fills it in, say so.
    if not any(n.section for n in tree):
        add(Issue("sections_unresolved", "warn",
                  "no node carries a Section; Section notes cannot be applied yet"))

    return ValidationReport(revision=tree.revision, stats=stats, issues=issues)
