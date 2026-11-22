FROM python:3.11-slim

WORKDIR /app

# The service needs no embeddings, no ONNX runtime and no model download:
# precedent retrieval is BM25 over ruling text. That is why the image is this
# small and starts cold in about a second.
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir -e ".[serve]"

# The schedule version is baked in: an image that classifies against a
# different revision than it claims is the bug this prevents.
ARG HTS_REVISION=2026HTSRev21
ENV HTS_REVISION=${HTS_REVISION}

COPY data/manifests ./data/manifests
COPY scripts ./scripts

# Built by scripts/build_deploy_bundle.py, and deliberately NOT committed:
# data/ stays gitignored and reproducible from scripts. Build the bundle, then
# build the image. 14.7 MB of rulings, provision text and notes.
COPY data/deploy ./data/deploy

EXPOSE 8000

# Honest about failure: if the bundle is missing the container should stop,
# not serve a page that cannot classify anything.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

CMD ["uvicorn", "--factory", "hsclass.serve.app:create_app", \
     "--host", "0.0.0.0", "--port", "8000"]
