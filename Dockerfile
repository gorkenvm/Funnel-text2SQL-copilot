# Phonak Funnel Copilot — M2 web application image.
#
# Data is NOT baked into the image: mount ./data as a volume at /app/data
# (see docker-compose.yml). Build context is the repo root.
FROM python:3.11-slim

WORKDIR /app

# System deps: none beyond what pip needs; duckdb/pyarrow ship manylinux wheels.
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY src/ /app/src/
COPY app/ /app/app/

ENV PYTHONUNBUFFERED=1 \
    AGENT_DB=duckdb \
    AGENT_DATA_DIR=/app/data

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
