# ---- stage 1: build the React SPA -------------------------------------------
FROM node:22-slim AS frontend
WORKDIR /app
COPY app/package.json app/package-lock.json ./
RUN npm ci
COPY app/ ./
RUN npm run build

# ---- stage 2: the tool, with no data of its own -----------------------------
# Build this one to run Upstrace against your own dbt project:
#   docker build --target runtime -t upstrace .
#   docker run -v /path/to/your/project:/project -w /project \
#     -p 7860:7860 upstrace uvicorn upstrace.api:app --host 0.0.0.0 --port 7860
# upstrace.yml is discovered by walking up from the working directory, and every
# path inside it resolves relative to that file - so the mounted project's
# warehouse and dbt project are found, while the package stays at /srv.
FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UPSTRACE_LLM_PROVIDER=mock

WORKDIR /srv

COPY pyproject.toml README.md ./
COPY src/ src/

# Editable install, deliberately. It keeps the package rooted at /srv, so
# PROJECT_ROOT still resolves to the repo - app/static where the API expects it.
# A normal install would move the package to site-packages and break that.
RUN pip install -e . "dbt-core==1.10.*" "dbt-duckdb==1.9.*"

COPY --from=frontend /app/static app/static

RUN useradd -m -u 1000 user && chown -R user:user /srv
USER user

EXPOSE 7860
CMD ["uvicorn", "upstrace.api:app", "--host", "0.0.0.0", "--port", "7860"]

# ---- stage 3: the public demo -----------------------------------------------
# Same tool, plus a 400k-row sample of the NYC taxi data and a warehouse built
# at image build time. This is what Hugging Face Spaces runs: it starts in under
# a second and needs no external data source.
FROM runtime AS demo

USER root

COPY upstrace.yml ./
COPY transform/ transform/
COPY scripts/ scripts/
COPY cache/ cache/
COPY data/demo/yellow_tripdata_demo.parquet data/

RUN python scripts/build_demo_warehouse.py

RUN chown -R user:user /srv
USER user

EXPOSE 7860
CMD ["uvicorn", "upstrace.api:app", "--host", "0.0.0.0", "--port", "7860"]