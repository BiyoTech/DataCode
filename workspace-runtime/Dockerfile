# Data-plane workspace-runtime image (design 02 §2.5, design 06 P3/P4a).
#
# Build context is the datacode/ directory (the shared package is a local path
# dependency and must be present at install time):
#   docker build -f workspace-runtime/Dockerfile -t datacode/runtime:<ver> .
#
# The sole owner of working copies: SQLMesh engine + Ontology + Dagster code
# locations + Git + file IO + the L3-proc executor, in ONE process.

FROM python:3.13-slim

WORKDIR /app

# git is required for working-copy materialization (clone --bare -> worktree);
# build tools keep C-extension wheels installable when no prebuilt wheel
# matches the platform.
RUN apt-get update && apt-get install -y --no-install-recommends \
    git build-essential \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 app && useradd --uid 10001 --gid 10001 --create-home app

# Shared package first (layer caching: it changes less often than the service).
COPY shared/python/ shared/python/
COPY workspace-runtime/pyproject.toml workspace-runtime/
# Extras baked into this image on top of `[usercode]`, comma-separated (see
# workspace-runtime/pyproject.toml). This is how a package that user code needs
# but PyPI ships without a wheel enters the PLATFORM BASELINE — `pyspark` is the
# case that forced the knob: it publishes one ~300 MB source distribution per
# release, and the dependency installer only installs wheels (no compiler in the
# sandbox, no build script ever executed). A package in this image is provided by
# the platform, so a working copy declaring it installs nothing at all.
#
#   docker build --build-arg RUNTIME_EXTRAS=spark ...
#
# Empty by default: every extra is paid for by every replica, and Spark alone is
# well over half a gigabyte plus a JRE.
ARG RUNTIME_EXTRAS=""

# The Kubernetes task backend imports its cluster client at CONSTRUCTION time
# (src/tasks/backends/kubernetes.py refuses to start without it), so this one is
# NOT opt-in: one image serves every deployment, and a cluster rollout that
# forgot a build-arg would fail at startup with nothing but the extra's name to
# go on. It is a few megabytes of pure Python.

# Install shared and the service in ONE pip command: the resolver then
# satisfies the service's `datacode-common` requirement from the local
# directory candidate (never published to any index). The PostgreSQL driver
# (asyncpg) is a core dependency — same install as the dev venvs, so the two
# paths cannot drift apart again.
#
# `[usercode]` is the set of packages user code may import from a code
# location, which runs on this interpreter. It is NOT an optional group here:
# an asset that imports pandas is not a special deployment, it is the ordinary
# case, and a promise that is only kept in the dev venv is not a promise.
RUN pip install --no-cache-dir ./shared/python \
    -e "./workspace-runtime[usercode,k8s${RUNTIME_EXTRAS:+,${RUNTIME_EXTRAS}}]"

# Fail the build when an extra was asked for but did not land. pip treats an
# unknown extra as a WARNING and installs everything else, so a malformed value
# — a misspelled group, or a whole shell fragment pasted into the build-arg —
# yields an image that looks correct, silently omits the packages, and surfaces
# much later as `deps_no_wheel` for the very package it was meant to provide.
RUN python -m datacode_common.image_extras \
    workspace-runtime/pyproject.toml RUNTIME_EXTRAS "${RUNTIME_EXTRAS}"

# A JVM, and only when Spark was asked for. pyspark bundles the Spark jars but
# not a runtime to execute them, so without this the import succeeds and the
# first action fails with "JAVA_HOME is not set" — the quietest possible way to
# ship a broken image.
RUN case ",${RUNTIME_EXTRAS}," in \
      *,spark,*) apt-get update \
        && apt-get install -y --no-install-recommends default-jre-headless \
        && rm -rf /var/lib/apt/lists/* ;; \
    esac

# Service source. conf/ is NOT baked in: container deployments mount a
# config directory via CONFIG_PATH (see deploy/) so one image serves every
# environment.
COPY workspace-runtime/src/ workspace-runtime/src/

# The task-runner package, for the LOCAL task backend (`task.backend: local`,
# the single-node default). That vehicle spawns `python -m src.main` with this
# package as the child's cwd, on THIS image's interpreter — there is no sibling
# checkout inside a container to fall back to, so without this copy every
# sandboxed task (deps install, agent shell) is refused at submit time.
#
# The Kubernetes backend does not read it: it runs the separate task-runner
# image. Sharing the checkout here is deliberate — a version skew between the
# two fails every deps.sync (see task-runner/src/deps_ops.py).
COPY task-runner/src/ task-runner/src/

WORKDIR /app/workspace-runtime

# Working copies are materialized into the volume mounted at /data/workspaces
# (compose) or the per-replica PVC (K8s). Pre-create it so the runtime never
# needs to write to the image's filesystem.
RUN mkdir -p /data/workspaces && chown app:app /data/workspaces
USER app

# REST API (8002) is the only published surface: user traffic arrives via the
# gateway, lease placement via /internal/* from peers. Executor UDS sockets
# and code-location gRPC ports are pod-internal.
EXPOSE 8002

HEALTHCHECK --interval=15s --timeout=3s --start-period=30s --retries=6 \
  CMD python -c "import urllib.request as u; u.urlopen('http://localhost:8002/health', timeout=2)" || exit 1

CMD ["python", "-m", "src.main"]
