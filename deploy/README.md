# Deploying the service

The served path is deliberately the cheap one. Precedent retrieval is BM25 over 18,253 ruling
descriptions, so the container carries **no embeddings, no ONNX runtime and no model download**:
a 14.7 MB data bundle, a cold start of about a second, and no API key. The schedule's vector
index is 44 MB of floats and was never needed here.

## 1. Build the data bundle

`data/` is gitignored and reproducible from scripts, so the bundle is a **build artefact that is
not committed**. Make it first:

```bash
python -u scripts/build_deploy_bundle.py
```

That writes `data/deploy/` — `precedents.json` (18,253 rulings), `provisions.json` (29,861
provisions and their inherited chains), `notes.json`, and `bundle.json` stamping the revision.
It needs `data/raw/atlas/train.jsonl` and a parsed schedule, so run `scripts/fetch_atlas.py` and
`scripts/parse_full_schedule.py` first on a clean checkout.

## 2. Run it locally

```bash
uvicorn --factory hsclass.serve.app:create_app --port 8000
```

`GET /` is the page, `POST /classify` takes `{"description": "..."}`, `GET /health` reports the
revision and how much is indexed. `GET /docs` is the generated OpenAPI page.

## 3. Build the image

```bash
docker build -t hsclass:latest .
```

**Not yet verified.** Docker Desktop was not running when this was written, so the image has
never been built. The pieces it depends on *are* verified — the `serve` extra installs, the app
builds, and the service answers over HTTP — but the Dockerfile itself is untested, which this
project's own rule says to state rather than imply.

## 4. Deploy

Either target works from the same Dockerfile. Both need an account, so this is the step that
cannot be done from here.

### Hugging Face Spaces

Free, public URL, Docker-native, and the bundle can live in the Space repo without putting data
in the main one. Create a Space with **SDK: Docker**, then give its `README.md` this frontmatter:

```yaml
---
title: HS Tariff Classification
emoji: 📦
colorFrom: gray
colorTo: green
sdk: docker
app_port: 8000
---
```

Then push this repository plus `data/deploy/` to the Space remote:

```bash
git remote add space https://huggingface.co/spaces/<user>/<space>
git push space main
```

`data/deploy/` is gitignored here, so force-add it **on the Space remote only**:

```bash
git add -f data/deploy && git commit -m "bundle" && git push space main
```

### Fly.io

```bash
fly launch --no-deploy --name hsclass
fly deploy
```

`fly.toml` in this directory sets the internal port to 8000 and a 1 GB machine, which is more
than the service needs — it holds the rulings, the provision text and an inverted index, and
measured about 250 MB resident locally.

## What a reviewer sees

The page states the schedule revision, how many rulings are indexed, and that the rules decide
or refuse. Typing a description returns one of two clearly labelled tiers:

* **Settled by the rules** — the deterministic answer, with its GRI path and the notes consulted.
  Rare: about 2% of cases, right 41.7% of the time on the held-out set.
* **Precedent suggests, not confirmed by the rules** — the nearest CBP ruling's code, with the
  accuracy printed next to it (52.5% at six digits, 46.3% excluding leaked duplicates).

Keeping them apart is the point. Collapsing them would either answer "not classified" to almost
every query while withholding a better-than-even guess, or pass a similarity match off as an
adjudicated one.
