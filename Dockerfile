FROM debian:buster-slim AS build

ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update && \
    apt-get install --no-install-suggests --no-install-recommends --yes python3-venv=3.7.3-1 gcc=4:8.3.0-1 libpython3-dev=3.7.3-1 && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* && \
    python3 -m venv /venv && \
    /venv/bin/pip install --upgrade pip

FROM build as build-venv

WORKDIR /
COPY requirements.txt .
RUN /venv/bin/pip install --no-cache-dir -r requirements.txt

FROM gcr.io/distroless/python3-debian10

COPY --from=build-venv /venv /venv
WORKDIR /app
COPY . .
CMD /venv/bin/kopf run prometheus_shard_autoscaler/app.py --verbose
