FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir -e .

# The schedule version is baked in: an image that classifies against a
# different revision than it claims is the bug this prevents.
ARG HTS_REVISION=2026HTSRev21
ENV HTS_REVISION=${HTS_REVISION}

COPY data/manifests ./data/manifests
COPY scripts ./scripts

ENTRYPOINT ["hsclass"]
CMD ["--help"]
