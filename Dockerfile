# Pure-Python image: no bash/curl/jq/kubectl. Arbitrary-UID-safe for OpenShift.
FROM python:3.12-slim AS build
WORKDIR /app
RUN pip install --no-cache-dir uv
COPY pyproject.toml ./
COPY src ./src
COPY README.md ./
# Vendor deps + the package into a self-contained venv.
RUN uv venv /opt/venv \
    && VIRTUAL_ENV=/opt/venv uv pip install --no-cache .

FROM python:3.12-slim
ENV PATH="/opt/venv/bin:$PATH" \
    SERVICE_INSTANCES_DIR=/etc/service/instances \
    SERVICE_PORT=8080
COPY --from=build /opt/venv /opt/venv

# OpenShift's restricted-v2 SCC assigns a random non-root UID in the namespace's
# range; that UID is not in /etc/passwd and always belongs to GID 0. Make the
# runtime dirs group-owned by root (GID 0) and group-writable so the image works
# under BOTH a pinned UID 10001 (kind/generic k8s) and an arbitrary injected UID
# (OpenShift). Rossoctl's own UI/backend pods pin no UID and rely on this SCC
# injection; we additionally pin 10001 as the default for non-OpenShift targets.
RUN mkdir -p /etc/service/instances \
    && chgrp -R 0 /opt/venv /etc/service \
    && chmod -R g=u /opt/venv /etc/service

USER 10001
EXPOSE 8080
ENTRYPOINT ["benchmarking-service"]
