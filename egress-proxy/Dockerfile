# Egress proxy image (design 04 §3.4, P6b).
#
# Reachable from untrusted sandboxes, so the image carries as little as possible:
# no compiler, no shell utilities beyond the base, no platform configuration
# files. It holds ONE secret (the task-token signing key) and that arrives through
# the environment, never baked in.

FROM python:3.13-slim AS builder

WORKDIR /build
COPY shared/python /build/shared/python
COPY egress-proxy/pyproject.toml /build/egress-proxy/pyproject.toml
COPY egress-proxy/src /build/egress-proxy/src

RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
 && /opt/venv/bin/pip install --no-cache-dir /build/shared/python \
 && /opt/venv/bin/pip install --no-cache-dir /build/egress-proxy


FROM python:3.13-slim

# Unprivileged, with no home directory to write into: this process needs no
# filesystem state at all.
RUN useradd --system --uid 65533 --no-create-home --shell /usr/sbin/nologin egress

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /build/egress-proxy/src /app/src

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=UTC

WORKDIR /app
USER 65533

EXPOSE 3128

# Liveness is "the port accepts a connection"; the deployment uses a tcpSocket
# probe rather than an HTTP endpoint, so none is exposed here.
ENTRYPOINT ["python", "-m", "src.main"]
