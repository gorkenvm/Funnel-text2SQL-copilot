# Funnel Copilot — production web application image.
#
# Data is NOT baked into the image: mount ./data as a volume at /app/data
# (see docker-compose.yml; ../data is generated separately via
# `docker compose run --rm datagen`, see that file's comment). Build
# context is the repo root.
#
# What gets copied, and why (audited against every open()/read_text()/
# Path(__file__)-relative read in src/agent and app — see
# docs/deploy_runbook_vps.md's "Runtime dosya haritası" table for the
# file-by-file mapping):
#   src/               agent.py, agentic.py, db.py, llm.py, dashboard.py,
#                       medallion.py, sentinel_core.py, knowledge.py,
#                       metrics.yaml (lives inside src/agent/ itself), ...
#   app/               main.py + the static/ frontend it serves
#   sql/               medallion.sql (agent.medallion) and sql/sentinel/*
#                       (agent.sentinel_core)
#   config/            model_tiers.json (agent.llm), dashboard_kpis.json
#                       (agent.dashboard), sentinel_registry.json
#                       (agent.sentinel_core)
#   docs/knowledge/    the RAG markdown corpus (agent.knowledge) — ONLY
#                       this subfolder, never the rest of docs/
# Deliberately NOT copied: tests/, reports/, notebooks/, scripts/,
# docs/ beyond knowledge/, README.md — nothing under those paths is ever
# opened by the running app (verified by grepping every open()/read_text()/
# Path(__file__).resolve().parents[N] call site in src/agent and app).
FROM python:3.11-slim

WORKDIR /app

# System deps: none beyond what pip needs; duckdb/pyarrow ship manylinux wheels.
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY src/ /app/src/
COPY app/ /app/app/
COPY sql/ /app/sql/
COPY config/ /app/config/
COPY docs/knowledge/ /app/docs/knowledge/

ENV PYTHONUNBUFFERED=1 \
    AGENT_DATA_DIR=/app/data

# Non-root: cheap (one extra RUN, no build-time cost) and worth it for a
# public-facing container. docs/knowledge/ stays writable by this user —
# agent.knowledge.KnowledgeBase caches OpenAI embeddings there as
# .embedding_cache.json when OPENAI_API_KEY is set (BM25 fallback needs no
# write access at all). /app/data is a separate, usually read-only, mount
# — this chown does not affect it.
RUN useradd --system --create-home --home-dir /home/appuser --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
