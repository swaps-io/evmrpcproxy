# FROM python:3.11-slim-bookworm AS base
FROM python@sha256:cfd7ed5c11a88ce533d69a1da2fd932d647f9eb6791c5b4ddce081aedf7f7876 AS python-base

# COPY --from=ghcr.io/astral-sh/uv:0.6.16 /uv /uvx /bin/
COPY --from=ghcr.io/astral-sh/uv@sha256:db305ce8edc1c2df4988b9d23471465d90d599cc55571e6501421c173a33bb0b /uv /uvx /bin/

# See also:
# https://docs.astral.sh/uv/configuration/environment/#uv_http_timeout
ENV \
    DEBIAN_FRONTEND=noninteractive \
    PYTHONFAULTHANDLER=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_HTTP_TIMEOUT=100


# working directory and Python path
WORKDIR /opt/app

################################
# BUILDER-BASE
# Used to build deps + create our virtual environment
################################
FROM python-base as builder-base

RUN apt-get -y update && \
    apt-get -y upgrade && \
    apt-get -y install --no-install-recommends \
        curl git build-essential tini libgmp3-dev libmpfr-dev \
    && \
    apt-get -y purge --auto-remove -o APT::AutoRemove::RecommendsImportant=false && \
    apt-get -y clean && rm -rf /var/lib/apt/lists/*

# used to init dependencies
WORKDIR /opt/app
COPY uv.lock pyproject.toml ./

# https://docs.astral.sh/uv/guides/integration/docker/#intermediate-layers
RUN --mount=type=cache,target=/root/.cache/uv \
    touch README.md && \
    ls -la && \
    uv sync --locked --no-dev --no-install-project


################################
# PRODUCTION
# Final image used for runtime
################################
FROM python-base as app

RUN DEBIAN_FRONTEND=noninteractive apt-get update && \
    apt-get install -y --no-install-recommends \
    ca-certificates libgmp10 libmpfr6 && \
    apt-get -y purge --auto-remove -o APT::AutoRemove::RecommendsImportant=false && \
    apt-get -y clean && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/app

# copy in our built venv
COPY --from=builder-base /opt/app/.venv ./.venv

COPY uv.lock pyproject.toml ./
COPY ./src ./src

RUN touch README.md && \
    uv sync --locked --no-dev

RUN mkdir --parents /opt/app && \
    groupadd --gid=1000 app && \
    useradd --uid=1000 --home-dir=/opt/app --gid=app app && \
    chown app:app -R /opt/app
USER app

RUN uv run --no-dev which python && uv run --no-dev python --version
# Ensure no further uv processing is done
ENV UV_OFFLINE=1 \
    UV_NO_SYNC=1

ENTRYPOINT ["uv", "run", "evmrpcproxy"]
CMD ["api"]
