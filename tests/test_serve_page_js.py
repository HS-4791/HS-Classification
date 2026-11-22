"""Run the page's own trimming functions, in node, against real provision text.

Why this file exists. ``tail()`` and ``head()`` are JavaScript inside a Python
string, so the Python suite could only ever assert that the source *looks*
right - and it did: a test asserting ``"esc(tail(s.text, 90))" in PAGE``
passed while ``tail`` was returning 523 characters for a budget of 90, and
passed again after a first fix that put the cap after a loop which still
returned early. Reading the code is what missed it twice.

So this extracts the two functions from ``PAGE`` and executes them, on the
longest inherited chains the real bundle contains. It skips when node is not
installed or the bundle has not been built, and says which.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from hsclass.serve.app import BUNDLE, PAGE

#: The budget the page asks for, and the one the shortlist actually uses.
BUDGET = 90

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


def _function(name: str) -> str:
    """Lift one top-level ``function name(...){...}`` out of PAGE by brace depth."""
    start = PAGE.index(f"function {name}(")
    depth = 0
    for i in range(PAGE.index("{", start), len(PAGE)):
        if PAGE[i] == "{":
            depth += 1
        elif PAGE[i] == "}":
            depth -= 1
            if depth == 0:
                return PAGE[start : i + 1]
    raise AssertionError(f"function {name} is not brace-balanced in PAGE")


def run_js(script: str) -> object:
    """Execute the page's trimming helpers plus `script`, and return its JSON."""
    source = "\n".join(
        [_function("tail"), _function("head"), f"console.log(JSON.stringify({script}))"]
    )
    # Written to a file rather than passed with -e: 300 real provision chains
    # is about 40 kB of argument, and Windows refuses the command line with
    # "The filename or extension is too long" well before that.
    with tempfile.TemporaryDirectory() as workspace:
        entry = Path(workspace) / "trim.mjs"
        entry.write_text(source, encoding="utf-8")
        done = subprocess.run(
            [NODE, str(entry)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_the_two_functions_are_extractable_and_run():
    """If this fails the rest are vacuous, so it is asserted separately."""
    assert run_js("tail('a — b — c', 90)") == "a — b — c"
    assert run_js("head('hello world', 90)") == "hello world"


@pytest.mark.parametrize(
    "script,limit",
    [
        # The case that was shipping at 523 characters: one segment, no
        # separator anywhere in it, far longer than the budget.
        ("tail('x'.repeat(400), 90)", BUDGET),
        ("tail('one — ' + 'x'.repeat(400), 90)", BUDGET),
        ("head('x'.repeat(400), 110)", 110),
        ("tail('short', 90)", BUDGET),
    ],
)
def test_trimming_never_exceeds_its_budget(script, limit):
    assert len(run_js(script)) <= limit


def test_empty_and_degenerate_input_does_not_throw():
    assert run_js("tail('', 90)") == ""
    assert run_js("tail(null, 90)") == ""
    assert run_js("head(null, 90)") == ""
    # A budget of zero cannot hold text and an ellipsis both; it must still
    # return a string rather than throw or return the whole input.
    assert len(run_js("tail('one', 0)")) <= 2
    assert len(run_js("head('one', 0)")) <= 2


def test_a_segment_boundary_cut_does_not_land_mid_word():
    """The defect this function was written for, checked on its own output."""
    chain = (
        "Section XI — Chapter 61 — Shirts, knitted or crocheted "
        "— Of other textile materials — Of wool or fine animal hair (438)"
    )
    out = run_js(f"tail({json.dumps(chain)}, 90)")
    assert len(out) <= BUDGET
    # Whatever survived must begin at a segment boundary, marked by the
    # ellipsis, and must not begin part-way through a word like "rts,".
    assert out.startswith("… ") or out == chain
    assert "rts," not in out


@pytest.mark.skipif(
    not (BUNDLE / "provisions.json").exists(),
    reason="the deploy bundle has not been built",
)
def test_every_real_provision_chain_trims_within_budget():
    """Measured against the bundle, not against invented strings.

    One in nine of these chains is longer than the budget, and the longest is
    several hundred characters, so this is the population the function is for.
    """
    provisions = json.loads(
        (BUNDLE / "provisions.json").read_text(encoding="utf-8")
    )
    chains = [p["t"] for p in provisions if p.get("t")]
    assert len(chains) > 1000, "the bundle looks truncated"

    # The longest few hundred are where the function can misbehave; running
    # all 29,861 through node would make the suite slow for no extra signal.
    longest = sorted(chains, key=len, reverse=True)[:300]
    out = run_js(f"{json.dumps(longest)}.map(t => tail(t, {BUDGET}))")
    over = [(len(t), t[:60]) for t in out if len(t) > BUDGET]
    assert not over, f"{len(over)} chains exceeded {BUDGET}: {over[:3]}"

    # And the cut must not leave a dangling word fragment at the front: every
    # trimmed line starts with the ellipsis.
    trimmed = [t for t, original in zip(out, longest, strict=True) if t != original]
    assert trimmed, "nothing was trimmed, so this test proved nothing"
    assert all(t.startswith("… ") for t in trimmed)


def test_the_page_has_no_character_count_slice_left():
    """The original defect, pinned at the source level as well."""
    assert not re.search(r"\.slice\(-\d+\)", PAGE)
