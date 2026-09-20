FROM python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /srv/app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY app ./app
RUN uv sync --frozen --no-dev

# tree-sitter grammars are fetched on first use. Bake them into the image so
# indexing never depends on network access at runtime, and so the versions in
# production are the ones this image was built and tested with.
RUN uv run --no-dev python -c "\
from tree_sitter_language_pack import prefetch; \
from app.knowledge_graph.extract.languages import spec_for, SUPPORTED_LANGUAGES; \
prefetch([spec_for(language).grammar for language in SUPPORTED_LANGUAGES])"

# git is required to check out repositories for indexing.
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

EXPOSE 8000
CMD ["uv", "run", "--no-dev", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
