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

**Verified, on 2026-11-22:** 456 MB image, 2 minutes cold, 116 MB resident once serving.
`docker inspect` reports the HEALTHCHECK `healthy` with a zero failing streak, `/health` answers
within half a second of the container starting, and `scripts/measure_served.py` scored all 200
test cases through it over HTTP — about 6 ms at the median observed by the client, p95 about
31 ms. The server's own `latency_ms` reported 1.6 ms and 3.4 ms, because it excludes request
parsing and response serialisation; quoting only the second would overstate the service by
roughly ten times at the tail.

**Not re-verified since the tie-break fix of 2026-11-25.** That fix changed which ruling wins a
tied BM25 score, so the accuracy figures from the 2026-11-22 container run are superseded and
`reports/served-test-200.json` now holds a venv uvicorn run instead — Docker was not running on
the machine that day. The image properties above (size, resident memory, healthcheck, exit code
on a missing bundle) are untouched by a sort argument, but untouched-in-principle is not a
measurement, so they are dated rather than restated as current. The deployed Cloud Run revision
*is* a container built from this Dockerfile and it reproduces the venv's accuracy exactly, which
is the stronger check anyway.

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

Measure the deployed service the same way, and keep both — `--out` exists so that one does not
silently overwrite the other:

```powershell
python -u scripts/measure_served.py https://hs-classification-257016385834.asia-south1.run.app --out=served-cloudrun-test-200.json
```

Running exactly this against two machines is what caught the tie-break bug. Comparing the two
reports is now the cheapest determinism check in the project: same bundle, same counts, or
something is wrong.

## 4. Deploy

Every target below works from the same Dockerfile. All need an account, so that part cannot be
done from here. One of the three is no longer free; read the first section before choosing.

### Hugging Face Spaces — no longer free, as of 2026-11-25

**This route needs a paid plan now.** `create_repo` returns `402 Payment Required`:

> Static Spaces are free for everyone, but hosting Gradio and Docker Spaces on free cpu-basic
> requires a PRO subscription.

Static Spaces serve files and cannot run a server, so they are no use here. PRO is $9/month and
the route then works unchanged. This was found by running it, not by reading the docs — several
2026 pricing pages still describe CPU Basic as free for Docker Spaces, and the
[Spaces overview](https://huggingface.co/docs/hub/en/spaces-overview) is the page that agrees
with the error.

The script is kept, because it is correct and runs the moment a subscription exists. Log in once
— **this step is yours, so the token stays yours**:

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

### Google Cloud Run — what is actually deployed

**Live: <https://hs-classification-257016385834.asia-south1.run.app>**

Deployed 2026-11-25 to `asia-south1` (Mumbai), public, scaling to zero between visits. Cloud
Run's free allowance is 2M requests, 180,000 vCPU-seconds and 360,000 GiB-seconds a month,
applied as a spending discount at Tier 1 rates rather than being restricted to US regions, and
Mumbai is a Tier 1 region — so the nearest region is also the free one.

```powershell
gcloud run deploy hs-classification --source . --region asia-south1 --allow-unauthenticated --port 8000 --memory 512Mi --cpu 1 --min-instances 0 --max-instances 3
```

`--source .` builds in Cloud Build, so no local Docker daemon is needed — which also sidesteps
the buildkit DNS failure noted above.

Three things had to be true first, and two of them were not obvious:

1. **`.gcloudignore` must exist.** With no such file gcloud generates one that *inherits
   `.gitignore`*, and `data/deploy` is gitignored on purpose. The upload would carry no bundle,
   the image would build cleanly, and `create_app()` would exit 1 on a container that never
   served a request. The file in the repository root is an allow-list for the same reason
   `deploy_space.py` uses one. Verified with `gcloud meta list-files-for-upload`, which prints
   the exact set: **69 files, 15.2 MB, no `.env`** — the same count the Hugging Face script
   computes from its own independent allow-list.
2. **Billing must be enabled**, even though nothing is charged at this scale.
3. **The default compute service account needs `roles/cloudbuild.builds.builder`.** On projects
   created recently it starts without it and the first deploy fails with `PERMISSION_DENIED`
   naming `serviceusage.serviceUsageConsumer`:

   ```powershell
   gcloud projects add-iam-policy-binding PROJECT_ID --member="serviceAccount:PROJECT_NUMBER-compute@developer.gserviceaccount.com" --role="roles/cloudbuild.builds.builder"
   ```

Re-run the deploy command to ship a new revision, including for a new schedule revision —
rebuild the bundle first. The service refuses to start if `HTS_REVISION` disagrees with what the
bundle was built from.

**Deploying is what found the tie-break bug.** The same bundle scored 105/200 at six digits in
the local container and 108/200 here; `measure_served.py`'s reproduction check is what noticed.
Both now return 107/200. The root README's "What deploying it found" has the measurement.

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
  accuracy printed next to it: **53.5% at six digits (107/200)** and 47.5% at ten (95/200);
  excluding the 40 test descriptions that appear verbatim in the training data, **46.3%
  (74/160)** and 40.0% (64/160). These reproduce `reports/precedent-bm25-test-200.json` count
  for count, and the measurement script asserts that rather than claiming it — which is how the
  tie-break bug below was caught.

A third thing the page says, where it applies: **the suggested code is not a line in the pinned
revision at all.** This fires on 36 of the 200 test cases, because CBP rulings are historical and
the schedule is not — 2,106 of the 18,253 indexed rulings cite a code 2026HTSRev21 does not
carry. The cause is measured per case rather than asserted: 23 renumbered statistical suffixes,
11 whose eight-digit tariff line is gone too, 2 that resolve to nothing. **19 of the 36 are still
right at six digits**, which is the plainest evidence in this project for why six- and ten-digit
accuracy are reported as separate claims: the international subheading is stable, the US
statistical suffix is not.

Since the page shows the decided answer where it exists and the suggestion otherwise, the
user-visible accuracy over all 200 cases is **53.5% at six digits and 47.5% at ten** — the
decided tier is too small to move it either way.

Keeping them apart is the point. Collapsing them would either answer "not classified" to almost
every query while withholding a better-than-even guess, or pass a similarity match off as an
adjudicated one.
