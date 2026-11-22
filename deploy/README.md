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

**Verified.** Built on 2026-11-22: 456 MB image, 2 minutes cold, 116 MB resident once serving.
`docker inspect` reports the HEALTHCHECK `healthy` with a zero failing streak, `/health` answers
within half a second of the container starting, and `scripts/measure_served.py` scored all 200
test cases through it over HTTP — about 6 ms at the median observed by the client, p95 about
31 ms. The server's own `latency_ms` reports 1.6 ms and 3.4 ms, because it excludes request
parsing and response serialisation; both are in the report, and quoting only the second would
overstate the service by roughly ten times at the tail.

One environment note, since an unexecuted workflow is not a workflow: on this machine buildkit's
build sandbox stopped resolving DNS part-way through the day (`Name or service not known` for
files.pythonhosted.org) while ordinary containers resolved fine. `DOCKER_BUILDKIT=0 docker build`
succeeds. Hugging Face and Fly build with their own builders, so this is local only.

Mounting an empty directory over `data/deploy` exits the container with code 1 and serves
nothing, which is the intended behaviour and was worth checking rather than assuming: it is
`create_app()` reading the bundle that enforces it, not the HEALTHCHECK, which never gets a
first probe in.

Running it is what found seven defects the tests and the linter did not — the root README's
"What running it found that reading it did not" lists them. Two of the seven were defects in the
fix for an earlier one.

## 3a. Measure what you are about to deploy

```bash
python -u scripts/measure_served.py http://127.0.0.1:8100
```

The ablation numbers in the root README were measured on the **adjudicated pipeline**, which
proposes candidates from a 44 MB vector index. The container has none. That makes it a different
configuration, so it gets its own measurement rather than borrowing one — the pipeline settles 4
of the 200 test cases and the container settles 3, which is proof enough that they differ.

Results are in [`reports/served-test-200.json`](../reports/served-test-200.json).

## 4. Deploy

Either target works from the same Dockerfile. Both need an account, so this is the step that
cannot be done from here.

### Hugging Face Spaces

Free, public URL, Docker-native, and the bundle can live in the Space without going anywhere
near this repository's history. Log in once — **this step is yours, so the token stays yours**:

```powershell
.\.venv\Scripts\python.exe -c "from huggingface_hub.cli.hf import main; main()" auth login
```

Then:

```powershell
python -u scripts/deploy_space.py <user>/<space-name> --dry-run
```

```powershell
python -u scripts/deploy_space.py <user>/<space-name>
```

The script creates the Space with `sdk: docker`, uploads `deploy/space-README.md` as the
Space's `README.md` — its YAML frontmatter is what tells the Hub to build the Dockerfile and
which port to expose — and uploads the 69 files the image needs, 15.2 MB. It uploads from an
**allow-list**, not an ignore-list, so a new top-level directory here cannot reach a public
Space because nobody remembered to exclude it, and it refuses outright if anything matching
`.env` appears in the set. It never touches the token: `huggingface_hub` reads the credential
stored by `auth login`, so the value is never put on a command line or printed.

Re-run it to update, including for a new schedule revision — rebuild the bundle first. The
service refuses to start if `HTS_REVISION` disagrees with what the bundle was built from.

#### Why not the git route

Pushing a branch to a Space remote also works, and the obvious spelling of it is wrong three
ways. **A commit belongs to a branch, not to a remote**, so `git add -f data/deploy && git
commit` on `main` puts the 14.7 MB artefact into this repository's history and the next
`git push origin main` carries it upstream — exactly what `.gitignore` exists to prevent. Doing
it on a throwaway branch avoids that, but then **checking out `main` again deletes
`data/deploy` from the working tree**, because the files were tracked on the branch and are
ignored on `main`; that happened here, and the bundle had to be rebuilt. And the Space wants
its own short `README.md`, which on a branch means overwriting this repository's.

The Hub API has none of those properties: nothing is committed locally, `main` never carries the
bundle, and the working tree is untouched.

### Fly.io

```bash
fly launch --no-deploy --name hsclass
fly deploy
```

`fly.toml` in this directory sets the internal port to 8000 and a 1 GB machine, which is more
than the service needs — it holds the rulings, the provision text and an inverted index, and
measured **116 MB resident in the container** (`docker stats`, after serving 200 requests). An
earlier draft of this file said about 250 MB; that was a uvicorn process in the Windows venv, not
the image, and the image is the thing being sized.

## What a reviewer sees

The page states the schedule revision, how many rulings are indexed, and that the rules decide
or refuse. Typing a description returns one of two clearly labelled tiers. Both figures below
were measured **on this configuration**, through HTTP against the built container:

* **Settled by the rules** — the deterministic answer, with its GRI path and the notes consulted.
  Rare: **3 of 200** test cases (1.5%), and **1 of those 3** was right. Three cases is not an
  accuracy and this file will not print one; the measurement script prints the cases instead.
  One of the two misses is instructive — "a chisel" resolved at GRI 1 to 8209.00.0030, cermet
  tool plates, because no candidate in the shortlist was 8205.30, chisels. Stage 3 can only
  adjudicate the pool stage 2 hands it, and a deterministic engine over a bad pool is
  confidently wrong rather than unsure.
* **Precedent suggests, not confirmed by the rules** — the nearest CBP ruling's code, with the
  accuracy printed next to it: **52.5% at six digits (105/200)** and 46.0% at ten (92/200);
  excluding the 40 test descriptions that appear verbatim in the training data, **46.3%
  (74/160)** and 40.0% (64/160). These reproduce `reports/precedent-bm25-test-200.json` count
  for count, and the measurement script now asserts that rather than claiming it.

A third thing the page says, where it applies: **the suggested code is not a line in the pinned
revision at all.** This fires on 35 of the 200 test cases, because CBP rulings are historical and
the schedule is not — 2,106 of the 18,253 indexed rulings cite a code 2026HTSRev21 does not
carry. The cause is measured per case rather than asserted: 22 renumbered statistical suffixes,
11 whose eight-digit tariff line is gone too, 2 that resolve to nothing. **19 of the 35 are still
right at six digits**, which is the plainest evidence in this project for why six- and ten-digit
accuracy are reported as separate claims: the international subheading is stable, the US
statistical suffix is not.

Since the page shows the decided answer where it exists and the suggestion otherwise, the
user-visible accuracy over all 200 cases is **52.5% at six digits and 46.0% at ten** — the
decided tier is too small to move it either way.

Keeping them apart is the point. Collapsing them would either answer "not classified" to almost
every query while withholding a better-than-even guess, or pass a similarity match off as an
adjudicated one.
