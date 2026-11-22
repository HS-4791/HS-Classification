"""The live service: a description in, a code and its reasoning out.

What is served is the whole architecture's claim in one request. Stage 2
proposes candidates from precedent - the rulings a CBP officer has already
decided - and stage 3 either settles them under the GRI or **refuses**, and
the response says which happened and why. A refusal is a first-class answer
here, not an error: it carries the shortlist a human classifier would work
from and the rule the walk reached before it stopped.

**No model and no vectors.** Precedent retrieval is BM25 over 18,253 ruling
descriptions, which builds from text in about a second, so the image carries
no embeddings, downloads nothing at start-up and needs no API key. That is
also why it is the deployable path: the schedule's vector index is 44 MB of
floats and the gains measured on it were a tenth of what precedent gives.

Every response states the schedule revision it was answered against, because
a classification is only true against a version of the nomenclature.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from pydantic import BaseModel

from hsclass.pipeline.explain import Explainer
from hsclass.pipeline.gri import GRIEngine, extract_facts
from hsclass.retrieval.precedent import Precedent, PrecedentIndex
from hsclass.schedule.exclusions import build_exclusion_graph
from hsclass.schedule.notes import Note

#: How many candidates stage 3 is handed. Five, because that is what every
#: measurement in this project used: precedent's top five holds the right
#: six-digit subheading 73% of the time against 22.5% from the schedule.
SHORTLIST = 5

#: Where ``scripts/build_deploy_bundle.py`` writes, and what the image copies.
BUNDLE = Path("data/deploy")

#: What the precedent tier is, and how often it is right.
#:
#: Measured on *this* configuration, end to end through HTTP against the built
#: container, by ``scripts/measure_served.py`` - not borrowed from the
#: adjudicated pipeline's ablations, because the two are not the same system.
#: The pipeline proposes candidates from a 44 MB vector index and settles 4 of
#: the 200 test cases; the container has no vector index and settles 3.
#:
#: The precedent tier, though, reproduces the library measurement exactly -
#: 105/200 and 92/200, the counts behind the 0.525 and 0.46 in
#: reports/precedent-bm25-test-200.json - so the bundle build is faithful to
#: the index it was made from.
SUGGESTED_BASIS = (
    "nearest CBP ruling by BM25 over 18,253 rulings; on the 200-case test "
    "split it is right 52.5% of the time at six digits (105/200) and 46.0% at "
    "ten (92/200). Excluding the 40 test descriptions that appear verbatim in "
    "the training data: 46.3% at six digits (74/160) and 40.0% at ten (64/160)"
)


class ClassifyRequest(BaseModel):
    """One description to classify.

    Declared at module level rather than inside ``create_app``, which is
    where it was first written. ``from __future__ import annotations`` turns
    annotations into strings and FastAPI resolves them against the function's
    *module* globals, so a model defined in a closure is invisible: the route
    quietly degraded to expecting a query parameter named "request" and every
    POST came back 422. Nothing about the code looked wrong.
    """

    description: str


#: Where an HTS code can legitimately be truncated, deepest first: statistical
#: suffix, US tariff line, international subheading, heading, chapter. Cutting
#: anywhere else produces a string that is not a provision.
#:
#: The 2-digit rung is legally real but unreachable from this bundle, which
#: carries 961 headings, 2,093 subheadings, 6,858 tariff lines and 19,949
#: statistical suffixes and no chapter rows at all. It stays because the list
#: describes the nomenclature, not this parse of it.
LEVELS = (10, 8, 6, 4, 2)


class Service:
    """Everything loaded once, so a request is retrieval plus arithmetic."""

    def __init__(self, bundle: Path = BUNDLE) -> None:
        meta = json.loads((bundle / "bundle.json").read_text(encoding="utf-8"))
        self.revision = meta["revision"]

        # The Dockerfile bakes HTS_REVISION in and says "an image that
        # classifies against a different revision than it claims is the bug
        # this prevents" - but nothing read it, so
        # ``--build-arg HTS_REVISION=2026HTSRev22`` over a Rev21 bundle built
        # an image whose environment said one thing and whose every response
        # said another, silently. Now it refuses to start.
        declared = os.environ.get("HTS_REVISION")
        if declared and declared != self.revision:
            raise ValueError(
                f"HTS_REVISION is {declared!r} but the bundle at {bundle} was "
                f"built from {self.revision!r}. A classification is only valid "
                f"against a schedule version, so this will not serve. Rebuild "
                f"the bundle with scripts/build_deploy_bundle.py, or correct "
                f"HTS_REVISION."
            )

        rulings = json.loads((bundle / "precedents.json").read_text(encoding="utf-8"))
        self.precedents = PrecedentIndex(
            [Precedent(r["d"], r["c"]) for r in rulings]
        )

        provisions = json.loads((bundle / "provisions.json").read_text(encoding="utf-8"))
        self.texts = {p["c"]: p["t"] for p in provisions}

        notes = [
            Note(**{k: v for k, v in n.items() if k in Note.__dataclass_fields__})
            for n in json.loads((bundle / "notes.json").read_text(encoding="utf-8"))
        ]
        self.engine = GRIEngine(build_exclusion_graph(notes))
        self.explainer = Explainer(texts=self.texts)

    def resolve(self, code: str | None) -> dict:
        """A code's provision text, or the nearest ancestor's, said out loud.

        Found by running the service, not by reading it. CBP rulings are
        historical and the schedule is not: **2,106 of the 18,253 indexed
        rulings (11.5%) cite a code that does not exist in the pinned
        revision**, and 665 of the 3,696 distinct codes (18.0%). Nearly all
        are ten-digit statistical suffixes that were renumbered - a ruling
        says 7306.30.50.32 and 2026HTSRev21 goes ...31 then ...35.

        The page used to print those as bare digits with nothing beside them,
        which reads as a broken lookup. Worse, it offered a code as an answer
        without saying that the revision it names in the same breath does not
        contain it.

        So: walk back to the deepest level that does exist, show that text,
        and report ``in_revision`` false so the caller can say which it got.
        This does not touch accuracy - the ATLAS gold labels are historical
        too, so a renumbered suffix can still be scored right - it is about
        not presenting a code as current when it is not.

        ``reason`` names *why*, because the first version of this asserted one
        cause for all of them - "the revision renumbered the statistical
        suffix" - and a cold read measured that true for only 22 of the 35
        cases it fired on. When the eight-digit tariff line is gone too,
        something other than the suffix changed, and 3 of the indexed rulings
        cite a code that is not a legal length at all.
        """
        digits = "".join(c for c in (code or "") if c.isdigit())
        if not digits:
            return {
                "text": None,
                "text_of": None,
                "in_revision": False,
                "reason": "no code",
            }

        exact = self.texts.get(digits)
        if exact is not None:
            return {
                "text": exact,
                "text_of": digits,
                "in_revision": True,
                "reason": "in revision",
            }

        # A code whose own length is not a level of the nomenclature is
        # malformed, not merely withdrawn. The guard here used to be
        # ``level >= len(digits)``, which skipped level 10 for a nine-digit
        # code and dropped it four levels to a bare heading: 390410000 landed
        # on 3904 although 3904100000 is in the revision. Guessing which line
        # was meant is not this function's business, but saying "renumbered
        # suffix" about it was wrong, so it says "malformed" instead.
        malformed = len(digits) not in LEVELS

        for level in LEVELS:
            if level >= len(digits):
                continue
            text = self.texts.get(digits[:level])
            if text is None:
                continue
            if malformed:
                reason = "malformed"
            elif len(digits) == 10 and level == 8:
                reason = "suffix renumbered"
            else:
                reason = "line withdrawn"
            return {
                "text": text,
                "text_of": digits[:level],
                "in_revision": False,
                "reason": reason,
            }
        return {
            "text": None,
            "text_of": None,
            "in_revision": False,
            "reason": "malformed" if malformed else "unresolved",
        }

    def classify(self, description: str) -> dict:
        started = time.perf_counter()
        candidates = self.precedents.codes(description, digits=10)[:SHORTLIST]
        facts = extract_facts(description)
        decision = self.engine.adjudicate(facts, candidates, self.texts)
        explanation = self.explainer.explain(decision, description)

        # Two tiers, labelled, because they carry different weight and a
        # reviewer deserves to know which is which. In this configuration the
        # rules settle 3 of the 200 test cases and precedent answers all 200 at
        # 52.5% top-1 at six digits. Showing only the deterministic tier would
        # mean answering "not classified" to 99% of queries while holding a
        # better-than-even guess back - which reads as broken rather than
        # careful. Showing only precedent would throw away what this is for.
        suggested = candidates[0] if candidates else None
        decided_at = self.resolve(decision.code)
        suggested_at = self.resolve(suggested)
        shortlist = []
        for code in (decision.shortlist or candidates)[:SHORTLIST]:
            at = self.resolve(code)
            shortlist.append(
                {
                    "code": code,
                    "text": at["text"] or "",
                    "in_revision": at["in_revision"],
                }
            )

        return {
            "description": description,
            "schedule_revision": self.revision,
            "decided": decision.code is not None,
            "code": decision.code,
            "code_text": decided_at["text"],
            "suggested": suggested,
            "suggested_text": suggested_at["text"],
            # False when the ruling cites a code the pinned revision no longer
            # carries - 11.5% of the index. ``suggested_text_of`` then names
            # the ancestor the text actually belongs to.
            "suggested_in_revision": suggested_at["in_revision"] if suggested else None,
            "suggested_text_of": suggested_at["text_of"],
            # Why it is not in the revision, rather than one asserted cause
            # for every case: "suffix renumbered", "line withdrawn",
            # "malformed" or "unresolved".
            "suggested_reason": suggested_at["reason"] if suggested else None,
            # None when there is nothing to justify. It used to be sent
            # unconditionally, so a description matching no ruling came back
            # with no suggestion and an accuracy claim for it anyway - the page
            # hid it, but an API consumer saw a basis for an answer that did
            # not exist.
            "suggested_basis": SUGGESTED_BASIS if suggested else None,
            "rule_path": decision.rule_path,
            "notes_cited": decision.notes_cited,
            "conditions_applied": decision.conditions_applied,
            "escalated": decision.escalated,
            "shortlist": shortlist,
            "explanation": explanation.text,
            # The reason precedent retrieval belongs in a system whose output
            # is a citation: these are checkable rulings, not a similarity.
            "precedents": [
                {"description": p.description, "code": p.code}
                for p in self.precedents.citations(description)
            ],
            "attributes": {
                "materials": facts.materials,
                "named_materials": facts.named_materials,
            },
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        }


def create_app(bundle: Path = BUNDLE):
    """Build the FastAPI app. Imported lazily so the module stays testable."""
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse

    service = Service(bundle)

    app = FastAPI(
        title="HS tariff classification",
        description=(
            "A description in, a customs code and its legal reasoning out - "
            "or a refusal with the shortlist a human would work from."
        ),
        version=service.revision,
    )

    @app.get("/health")
    def health() -> dict:
        return {
            "ok": True,
            "schedule_revision": service.revision,
            "rulings_indexed": len(service.precedents.precedents),
            "provisions": len(service.texts),
        }

    @app.post("/classify")
    def classify(request: ClassifyRequest) -> dict:
        return service.classify(request.description)

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return PAGE.replace("{revision}", service.revision).replace(
            "{rulings}", f"{len(service.precedents.precedents):,}"
        )

    return app


#: Deliberately one file with no build step and no CDN: a reviewer opens the
#: URL, types a description and sees the rule path. Anything heavier is a
#: second thing that can be broken at deploy time.
PAGE = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>HS tariff classification</title>
<style>
 :root { --ink:#1a1a1a; --dim:#666; --line:#e2e2e2; --bg:#fff; --accent:#0b5;
         --warn:#b26b00; --warnbg:#fdf6ec; }
 @media (prefers-color-scheme: dark) {
   :root { --ink:#e8e8e8; --dim:#999; --line:#333; --bg:#151515;
           --warn:#d08a2c; --warnbg:#241d12; }
 }
 body { background:var(--bg); color:var(--ink); max-width:46rem; margin:0 auto;
        padding:2rem 1rem 4rem; font:16px/1.55 ui-sans-serif,system-ui,sans-serif; }
 h1 { font-size:1.3rem; margin:0 0 .2rem; }
 .sub { color:var(--dim); font-size:.85rem; margin-bottom:1.5rem; }
 textarea { width:100%; box-sizing:border-box; padding:.7rem; font:inherit;
            border:1px solid var(--line); border-radius:6px; background:var(--bg);
            color:var(--ink); }
 button { margin-top:.6rem; padding:.5rem 1.1rem; font:inherit; cursor:pointer;
          border:1px solid var(--line); border-radius:6px; background:var(--bg);
          color:var(--ink); }
 button:hover { border-color:var(--accent); }
 .card { border:1px solid var(--line); border-radius:8px; padding:1rem;
         margin-top:1.5rem; }
 .code { font:600 1.4rem ui-monospace,monospace; }
 .code.dim { color:var(--dim); }
 .refused { color:var(--dim); font-weight:600; }
 .warn { border-left:3px solid var(--warn); padding:.4rem .7rem; margin:.5rem 0;
         font-size:.85rem; color:var(--ink); background:var(--warnbg); }
 .tier { color:var(--accent); font-size:.7rem; font-weight:700;
         text-transform:uppercase; letter-spacing:.06em; margin:.8rem 0 .2rem; }
 .label { color:var(--dim); font-size:.75rem; text-transform:uppercase;
          letter-spacing:.05em; margin:1rem 0 .3rem; }
 ul { margin:.2rem 0; padding-left:1.1rem; }
 li { margin:.2rem 0; }
 code { font:.85rem ui-monospace,monospace; }
 .ex { color:var(--dim); font-size:.85rem; }
 .ex a { color:inherit; }
</style>
<h1>HS tariff classification</h1>
<div class="sub">
  Schedule revision <strong>{revision}</strong> &middot; {rulings} CBP rulings indexed
  &middot; the rules decide or refuse &mdash; they never guess
</div>

<textarea id="q" rows="3"
  placeholder="a men's knit pullover made of 60% cotton and 40% modal"></textarea>
<button onclick="go()">Classify</button>
<div class="ex">Try:
  <a href="#" onclick="set(this);return false"
    >a women's knit cardigan of 90% acrylic and 10% spandex</a> &middot;
  <a href="#" onclick="set(this);return false"
    >cast carbon steel pipe fittings made to ASTM A216</a> &middot;
  <a href="#" onclick="set(this);return false"
    >a cast iron fry pan, 11 inches, with a wooden handle</a>
</div>
<div id="out"></div>

<script>
function set(a){ document.getElementById('q').value = a.textContent.trim(); go(); }
// Quotes are escaped too. Every interpolation below lands in element text, so
// &<> would be enough today - but 225 provision texts in the bundle contain a
// double quote and 2,818 an apostrophe, and the first time one of these values
// is put in a title= or data- attribute, &<> alone is an injection. Covering
// it costs one character class.
function esc(s){ return (s==null?'':String(s)).replace(/[&<>"']/g,
  c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

async function go(){
  const q = document.getElementById('q').value.trim();
  const out = document.getElementById('out');
  if(!q){ out.innerHTML=''; return; }
  out.innerHTML = '<div class="card">Classifying&hellip;</div>';
  try {
    const r = await fetch('/classify', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({description:q})
    });
    if(!r.ok) throw new Error('HTTP ' + r.status);
    render(await r.json(), out);
  } catch(e) {
    out.innerHTML = '<div class="card">Could not classify: ' + esc(e.message) + '</div>';
  }
}

// What resolve() measured about a code the revision does not carry. Measured
// over the 200-case test split: 22 renumbered suffixes, 11 withdrawn lines,
// 2 that resolve to nothing.
const CAUSE = {
  'suffix renumbered':
    'The ruling predates this revision, which renumbered the ten-digit '
    + 'statistical suffix under the same eight-digit tariff line.',
  'line withdrawn':
    'The eight-digit tariff line it sat under is not in this revision either, '
    + 'so more than the statistical suffix changed.',
  'malformed':
    'It is not a valid length for an HTS code, so which line the ruling meant '
    + 'cannot be determined from the code alone.',
  'unresolved':
    'No level of it matches a provision in this revision.'
};

// A provision's inherited chain is long, and the segments that distinguish it
// from its siblings are at the end. So trim from the front - but by whole
// segments, never mid-word: a chain cut at 90 characters renders as
// "rts, knitted or crocheted" and "23 percent or more by weight of wool",
// which reads as a bug rather than as an abbreviation.
function tail(text, budget){
  const ell = String.fromCharCode(8230);
  const sep = ' ' + String.fromCharCode(8212) + ' ';
  const parts = String(text == null ? '' : text).split(sep);
  const kept = [];
  let n = 0;
  let dropped = false;
  for(let i = parts.length - 1; i >= 0; i--){
    const len = parts[i].length + (kept.length ? sep.length : 0);
    if(kept.length && n + len > budget){ dropped = true; break; }
    kept.unshift(parts[i]);
    n += len;
  }
  // The guard above only fires once something is kept, so a single segment
  // longer than the budget passes through whole: subheading 2933.91's chain
  // ends in a 521-character enumeration of benzodiazepine INNs and rendered
  // at 523 characters where 90 was asked for. So cap on the way out - and
  // cap on EVERY path, because the first attempt at this fix put the cap
  // after the loop while the loop still returned early, which left the
  // overflow exactly as it was.
  const lead = dropped ? ell + ' ' : '';
  return lead + head(kept.join(sep), Math.max(1, budget - lead.length));
}

// Ruling descriptions are prose, not chains, so here the front is what matters.
function head(text, budget){
  // No regex here on purpose: PAGE is not a raw Python string, so a backslash
  // character class is an invalid escape sequence that the suite warns about
  // today and Python will reject outright later. Keep this page backslash-free.
  const ell = String.fromCharCode(8230);
  const s = String(text == null ? '' : text);
  if(s.length <= budget) return s;
  if(budget <= 1) return ell;
  // Reserve a character for the ellipsis, so the result honours the budget
  // rather than exceeding it by one.
  const cut = s.slice(0, budget - 1);
  const at = cut.lastIndexOf(' ');
  // Cut at a word boundary when the budget holds one. A long unbroken token -
  // a part number, a CAS name, a URL - has none, and then a mid-token cut is
  // the only thing left; trimEnd keeps a trailing space out of the join.
  return (at > 0 ? cut.slice(0, at) : cut).trimEnd() + ell;
}

function render(d, out){
  let h = '<div class="card">';
  if(d.decided){
    h += '<div class="tier">Settled by the rules</div>';
    h += '<div class="code">' + esc(d.code) + '</div>';
    if(d.code_text) h += '<div class="sub">' + esc(d.code_text) + '</div>';
  } else {
    h += '<div class="refused">The rules did not settle this &mdash; '
       + 'referred to a classifier</div>';
    if(d.suggested){
      h += '<div class="tier">Precedent suggests, not confirmed by the rules</div>';
      h += '<div class="code dim">' + esc(d.suggested) + '</div>';
      if(d.suggested_text)
        h += '<div class="sub">' + esc(d.suggested_text) + '</div>';
      // A ruling can cite a code the pinned revision no longer carries: 11.5%
      // of the index does. Saying so matters more than the suggestion itself,
      // because the page names the revision two lines above. The cause is
      // whatever resolve() measured, not one asserted for all of them - the
      // first version of this said "renumbered statistical suffix" every
      // time, which was true for 22 of the 35 cases it fired on.
      if(d.suggested && d.suggested_in_revision === false)
        h += '<div class="warn">Not a line in revision '
           + esc(d.schedule_revision) + '. ' + esc(CAUSE[d.suggested_reason]
               || 'The ruling that cites it predates this revision.')
           + (d.suggested_text_of
               ? ' The text above is the nearest provision that does exist, '
                 + esc(d.suggested_text_of) + '.'
               : d.suggested_reason === 'unresolved'
                 ? ''  // CAUSE already says exactly this; do not say it twice
                 : ' No provision text could be resolved for it at all.')
           + '</div>';
      h += '<div class="sub">' + esc(d.suggested_basis) + '</div>';
    }
  }
  h += '<div class="label">Reasoning</div><div>' + esc(d.explanation) + '</div>';
  if(d.rule_path && d.rule_path.length)
    h += '<div class="label">Rules applied</div><div><code>'
       + d.rule_path.map(esc).join(' &rarr; ') + '</code></div>';
  if(d.notes_cited && d.notes_cited.length)
    h += '<div class="label">Notes consulted</div><ul>'
       + d.notes_cited.map(n=>'<li>'+esc(n)+'</li>').join('') + '</ul>';
  if(!d.decided && d.shortlist && d.shortlist.length)
    h += '<div class="label">Shortlist for a human</div><ul>'
       + d.shortlist.map(s=>'<li><code>'+esc(s.code)+'</code> '
         + (s.in_revision === false ? '<em>(not in this revision)</em> ' : '')
         // An unresolvable code used to render as the digits, the marker and
         // then empty space, which looks like a failed lookup rather than a
         // statement. Six of the 200 test cases did this.
         + (s.text ? esc(tail(s.text, 90))
                   : '<em>no provision text in this revision</em>')
         + '</li>').join('') + '</ul>';
  if(d.precedents && d.precedents.length)
    h += '<div class="label">Rulings this drew on</div><ul>'
       + d.precedents.map(p=>'<li><code>'+esc(p.code)+'</code> '
         + esc(head(p.description, 110))+'</li>').join('') + '</ul>';
  h += '<div class="label">Answered against</div><div class="sub">revision '
     + esc(d.schedule_revision) + ' &middot; ' + esc(d.latency_ms) + ' ms</div>';
  out.innerHTML = h + '</div>';
}
</script>
"""
