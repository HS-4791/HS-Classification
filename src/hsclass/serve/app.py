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


class Service:
    """Everything loaded once, so a request is retrieval plus arithmetic."""

    def __init__(self, bundle: Path = BUNDLE) -> None:
        meta = json.loads((bundle / "bundle.json").read_text(encoding="utf-8"))
        self.revision = meta["revision"]

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

    def classify(self, description: str) -> dict:
        started = time.perf_counter()
        candidates = self.precedents.codes(description, digits=10)[:SHORTLIST]
        facts = extract_facts(description)
        decision = self.engine.adjudicate(facts, candidates, self.texts)
        explanation = self.explainer.explain(decision, description)

        # Two tiers, labelled, because they carry different weight and a
        # reviewer deserves to know which is which. The rules answer about 2%
        # of cases and are right 42% of the time on those; precedent answers
        # everything and is right 52.5% at six digits. Showing only the
        # deterministic tier would mean answering "not classified" to almost
        # every query while holding a better-than-even guess back - which
        # reads as broken rather than careful. Showing only precedent would
        # throw away the thing this project is for.
        suggested = candidates[0] if candidates else None

        return {
            "description": description,
            "schedule_revision": self.revision,
            "decided": decision.code is not None,
            "code": decision.code,
            "code_text": self.texts.get(decision.code or "", None),
            "suggested": suggested,
            "suggested_text": self.texts.get(suggested or "", None),
            "suggested_basis": (
                "nearest CBP ruling by BM25 over 18,253 rulings; 52.5% correct "
                "at six digits on the 200-case test split, 46.3% excluding "
                "descriptions duplicated in the training data"
            ),
            "rule_path": decision.rule_path,
            "notes_cited": decision.notes_cited,
            "conditions_applied": decision.conditions_applied,
            "escalated": decision.escalated,
            "shortlist": [
                {"code": code, "text": self.texts.get(code, "")}
                for code in (decision.shortlist or candidates)[:SHORTLIST]
            ],
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
 :root { --ink:#1a1a1a; --dim:#666; --line:#e2e2e2; --bg:#fff; --accent:#0b5; }
 @media (prefers-color-scheme: dark) {
   :root { --ink:#e8e8e8; --dim:#999; --line:#333; --bg:#151515; }
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
function esc(s){ return (s==null?'':String(s)).replace(/[&<>]/g,
  c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }

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
         + esc((s.text||'').slice(-90))+'</li>').join('') + '</ul>';
  if(d.precedents && d.precedents.length)
    h += '<div class="label">Rulings this drew on</div><ul>'
       + d.precedents.map(p=>'<li><code>'+esc(p.code)+'</code> '
         + esc(p.description.slice(0,110))+'</li>').join('') + '</ul>';
  h += '<div class="label">Answered against</div><div class="sub">revision '
     + esc(d.schedule_revision) + ' &middot; ' + esc(d.latency_ms) + ' ms</div>';
  out.innerHTML = h + '</div>';
}
</script>
"""
