# task-runner image — the sandboxed vehicle for ONE task (design 04 §3, P6a).
#
# Build context is the datacode/ directory (the shared package is a local path
# dependency and must be present at install time):
#   docker build -f task-runner/Dockerfile -t datacode/task-runner:<ver> .
#
# This image runs UNTRUSTED input. Everything below follows from that:
#
#   * No platform packages. It cannot import workspace-runtime or
#     control-service, so there is no code path from user input into the
#     platform's own logic.
#   * No shell entry point, no supervisor, no server. One process, one task,
#     then exit — the Pod's lifetime IS the task's lifetime.
#   * Nothing baked in that a task could read for credentials. conf/ is NOT
#     copied; the runner needs no platform configuration at all, only the spec
#     file the vehicle mounts.
#   * Runs as a non-root user matching the profile's `run_as_user: 65534`, so a
#     misconfigured Pod spec cannot silently promote it to root.

FROM python:3.13-slim

WORKDIR /app

# git only: `git.clone` / `git.push` are L2 task kinds. No build-essential —
# a compiler in a sandbox running untrusted code is an unnecessary capability,
# and every dependency here ships a manylinux wheel.
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

# Shared package first (layer caching: it changes less often than the runner).
COPY shared/python/ shared/python/
COPY task-runner/pyproject.toml task-runner/
# One pip command so the resolver satisfies `datacode-common` from the local
# directory candidate (never published to any index).
#
# RUNNER_EXTRAS mirrors workspace-runtime's build-arg of the same shape: it
# bakes optional groups (see task-runner/pyproject.toml) into the image, which
# is the only route for a package that publishes no wheel. Empty by default —
# this image is created once per task and every megabyte is paid on every cold
# start, so nothing is added to it without being asked for.
#
#   docker build --build-arg RUNNER_EXTRAS=spark ...
#
# Note it is APPENDED, never substituted: the base install is unconditional and
# has no extras of its own, so the brackets appear only when the arg is set.
ARG RUNNER_EXTRAS=""
RUN pip install --no-cache-dir ./shared/python \
    -e "./task-runner${RUNNER_EXTRAS:+[${RUNNER_EXTRAS}]}"

# Same assertion as the runtime image, for the same reason: pip downgrades an
# unknown extra to a warning, so without this a malformed build-arg ships an
# image that omits the packages and says nothing.
RUN python -m datacode_common.image_extras \
    task-runner/pyproject.toml RUNNER_EXTRAS "${RUNNER_EXTRAS}"

# A JVM, and only when Spark was asked for — same reason as the identical step
# in the workspace-runtime image: pyspark bundles the Spark jars but not a
# runtime to execute them, so without this `import pyspark` succeeds and the
# first action dies with "JAVA_HOME is not set".
RUN case ",${RUNNER_EXTRAS}," in \
      *,spark,*) apt-get update \
        && apt-get install -y --no-install-recommends default-jre-headless \
        && rm -rf /var/lib/apt/lists/* ;; \
    esac

COPY task-runner/src/ task-runner/src/

# Governance guards (design 04 §3.6). Generated at BUILD time from the shared
# verb declaration, so the image holds no hand-written copy of the list.
#
# They refuse the verbs the platform serves itself (`sqlmesh apply`, `dc run`,
# ...), which the command router cannot route when they arrive inside a COMPOUND
# command — a string with shell operators cannot be split into argv without a
# shell, so it reaches this sandbox whole. Everything else is forwarded to the
# real binary, so `sqlmesh test` / `audit` / `dag` are unaffected.
#
# Root-owned, outside every writable mount, and PREPENDED to PATH: here the real
# binaries live in /usr/local/bin, so a guard appended after it would never win
# the lookup. Prepending is safe because the directory holds only these scripts
# and the task's uid cannot write to it.
RUN python -m datacode_common.command_guards /opt/datacode \
    && chmod -R a+rX /opt/datacode
ENV PATH="/opt/datacode/command-guards:${PATH}"

WORKDIR /app/task-runner

# 65534 (nobody) matches `run_as_user` in both container profiles. Declared in
# the image as well as the Pod spec so a direct `docker run` is equally
# unprivileged.
USER 65534:65534

# The spec path is injected by the submitter; no default, because a runner that
# started without a spec must fail loudly rather than fall back to something.
ENV DATACODE_TASK_SPEC_PATH=""

# No EXPOSE: the runner accepts no inbound connections (the Pod's NetworkPolicy
# denies ingress outright).
ENTRYPOINT ["python", "-m", "src.main"]
