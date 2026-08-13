FROM node:25-bookworm AS frontend-build

WORKDIR /workspace/frontend

ARG VITE_APP_BASE_PATH=/seedpilot/
ARG VITE_API_BASE_URL=/seedpilot
ENV VITE_APP_BASE_PATH=${VITE_APP_BASE_PATH} \
    VITE_API_BASE_URL=${VITE_API_BASE_URL}

COPY frontend/package*.json ./
RUN npm ci

COPY frontend/ ./
RUN npm run build


FROM rust:1.95-bookworm AS runtime-sidecar-build

WORKDIR /workspace/native

COPY native/ ./

RUN cargo build --locked --release -p maf_runtime_sidecar --bin maf-runtime-sidecar


FROM debian:bookworm-slim AS runtime-sidecar

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libgcc-s1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=runtime-sidecar-build \
    /workspace/native/target/release/maf-runtime-sidecar \
    /usr/local/bin/maf-runtime-sidecar

RUN mkdir -p /run/maf-runtime-sidecar /var/lib/maf-runtime-sidecar

HEALTHCHECK --interval=10s --timeout=5s --start-period=5s --retries=6 \
    CMD ["/usr/local/bin/maf-runtime-sidecar", "--probe", "unix:///run/maf-runtime-sidecar/runtime.sock"]

ENTRYPOINT ["/usr/local/bin/maf-runtime-sidecar"]
CMD ["--serve", "unix:///run/maf-runtime-sidecar/runtime.sock", "--sqlite", "/var/lib/maf-runtime-sidecar/runtime.sqlite3"]


FROM ubuntu:22.04 AS backend

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    MAF_RUST_CORE_MODE=off \
    MAF_RUST_LIFECYCLE_MODE=off \
    MAF_RUST_ARTIFACT_STORE_MODE=off \
    MAF_RUST_AUTH_CORE_MODE=off \
    MAF_RUST_DATA_ACCESS_MODE=off \
    MAF_RUST_AUDIT_SANITIZER_MODE=off \
    MAF_RUST_SKILL_RUNTIME_MODE=off

# R-backed Skill bundles require UTF-8 source parsing and jsonlite JSON output.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        curl \
        libgomp1 \
        libstdc++6 \
        locales \
        r-base-core \
        r-cran-jsonlite \
        tini \
    && rm -rf /var/lib/apt/lists/*

ARG CONDA_INSTALLER_URL=https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-x86_64.sh
ARG CONDA_ENV_NAME=multi_agent
ARG PYTHON_VERSION=3.13.13

ENV CONDA_DIR=/opt/conda \
    CONDA_ENV_NAME=${CONDA_ENV_NAME} \
    PATH=/opt/conda/envs/${CONDA_ENV_NAME}/bin:/opt/conda/bin:$PATH

RUN curl -fsSL "${CONDA_INSTALLER_URL}" -o /tmp/miniconda.sh \
    && bash /tmp/miniconda.sh -b -p "${CONDA_DIR}" \
    && rm /tmp/miniconda.sh \
    && "${CONDA_DIR}/bin/conda" config --system --set channel_priority strict \
    && "${CONDA_DIR}/bin/conda" create -y -n "${CONDA_ENV_NAME}" "python=${PYTHON_VERSION}" pip \
    && "${CONDA_DIR}/bin/conda" clean -afy

WORKDIR /app

ENV PYTHONPATH=/app

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY docs/api ./docs/api
COPY config.yaml ./config.yaml

RUN mkdir -p runtime skill \
    && chmod 755 runtime skill

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/api-doc >/dev/null || exit 1

CMD ["tini", "--", "python", "-m", "uvicorn", "src.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]


FROM ubuntu:22.04 AS frontend

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl nginx \
    && rm -rf /var/lib/apt/lists/* \
    && rm -f /etc/nginx/sites-enabled/default

COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=frontend-build /workspace/frontend/dist/ /usr/share/nginx/html/seedpilot/

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1/seedpilot/ >/dev/null || exit 1

CMD ["nginx", "-g", "daemon off;"]
