#!/usr/bin/env bash
# Build every service image with one versioned tag.
#
# Usage:
#   scripts/build-images.sh                     # tag: <VERSION>-g<git-sha>
#   scripts/build-images.sh -t mytag            # explicit tag
#   IMAGE_REGISTRY=registry.example.com/team scripts/build-images.sh
#
# The tag is derived from VERSION (the file) plus the git short SHA, so every
# build is uniquely addressable AND carries its semantic version. Kubernetes
# manifests and compose files take the same tag through the IMAGE_TAG
# variable — no image is ever referenced as :latest, because a mutable tag
# makes "what is running" unanswerable after the second deploy.
#
# GO_BUILD_PARALLEL: parallel package compiles are memory-bound; on small
# builder VMs (2 GiB colima default) even -p 2 OOMs. 1 is slow but safe
# everywhere; raise on hosts that can afford it.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

usage() {
  cat <<'EOF'
Usage: build-images.sh [-t TAG]

Options:
  -t TAG    Explicit image tag (default: contents of VERSION + "-g<short-sha>")
  -h        This help

Environment:
  IMAGE_REGISTRY   Registry prefix (default: "datacode" — local-only names)
  GO_BUILD_PARALLEL  Concurrent Go package compiles (default: 1, safe on small VMs)
EOF
}

TAG=""
while getopts "t:h" opt; do
  case "$opt" in
    t) TAG="$OPTARG" ;;
    h) usage; exit 0 ;;
    *) usage >&2; exit 1 ;;
  esac
done

# A tag is validated before the FIRST build, not discovered by the docker CLI
# on image 1 of 8. Docker's tag grammar is narrower than SemVer's: '+' is the
# build-metadata separator in SemVer and is not in the allowed set at all, so
# `0.2.0+g74ad387` is a perfectly valid semantic version and an invalid image
# tag. '-' is the conventional stand-in (the tag is an image address for
# Kubernetes and compose; nothing parses it as a version).
validate_tag() {
  local tag="$1"
  if ! printf '%s' "$tag" | grep -Eq '^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$'; then
    echo "error: invalid image tag: '$tag'" >&2
    echo >&2
    echo "  Allowed: letters, digits, '_', '.' and '-';" >&2
    echo "  must not start with '.' or '-'; 128 characters maximum." >&2
    echo "  '+' is NOT allowed even though SemVer uses it for build" >&2
    echo "  metadata. Pass -t TAG to override the derived one." >&2
    exit 1
  fi
}

if [ -z "$TAG" ]; then
  VERSION="$(tr -d ' \n\t\r' < VERSION)"
  SHA="$(git rev-parse --short HEAD 2>/dev/null || echo nogit)"
  TAG="${VERSION}-g${SHA}"
fi
validate_tag "$TAG"
REGISTRY="${IMAGE_REGISTRY:-datacode}"

echo "==> building 8 images as ${REGISTRY}/<service>:${TAG}"

# Only the two Go services declare this build-arg: it bounds concurrent package
# compiles, which is what OOMs on small builder VMs (see the note at the top).
# Handing it to the Python and Node images too made docker print "One or more
# build-args were not consumed" for six of the eight builds — noise that buries
# the warnings an operator actually needs to read. Passed per image instead.
GO_BUILD_ARGS=(--build-arg "GO_BUILD_PARALLEL=${GO_BUILD_PARALLEL:-1}")

# One build context (the repository root) for every image: the shared packages
# are local path dependencies and the Dockerfiles COPY them from the context.
# Anything after the dockerfile is forwarded to `docker build` verbatim.
build() {
  local name="$1" dockerfile="$2"
  shift 2
  echo "--> ${REGISTRY}/${name}:${TAG}"
  # `"$@"` with no remaining args expands to nothing, which is safe under
  # `set -u` ($@ is exempt) and on the bash 3.2 macOS ships.
  docker build -f "$dockerfile" "$@" \
    -t "${REGISTRY}/${name}:${TAG}" \
    . 1>&2
}

build gateway          gateway/Dockerfile          "${GO_BUILD_ARGS[@]}"
build identity-service identity-service/Dockerfile "${GO_BUILD_ARGS[@]}"
build control-service  control-service/Dockerfile
build agent-service    agent-service/Dockerfile
build workspace-runtime workspace-runtime/Dockerfile
build task-runner      task-runner/Dockerfile
build egress-proxy     egress-proxy/Dockerfile
build frontend         frontend/Dockerfile

cat <<EOF

==> done. Images:
EOF
docker images --format '    {{.Repository}}:{{.Tag}}' | grep ":${TAG}" | sort || true

cat <<EOF

    Use them with:
      IMAGE_TAG=${TAG} make up            # compose
      IMAGE_TAG=${TAG} make k8s-apply OVERLAY=cell   # kubernetes
EOF
