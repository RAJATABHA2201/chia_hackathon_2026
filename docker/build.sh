#!/usr/bin/env bash
# Build the SparseCraft synthesis image.
#
# This host has podman, not docker, and no passwordless sudo, so everything
# runs rootless. TMPDIR must point at /home -- podman's default (/var/tmp) is
# on the 70 GB root volume and a 31 GB base image blows through it.
set -euo pipefail

# The build context is THIS checkout's root: the Dockerfile COPYs docker/...
# paths relative to it. It used to be the hardcoded V1 tree
# (/home/chia-sparsecraft/sparsecraft), so building from V2 silently built V1's
# Dockerfile and helper scripts.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

export PATH="$HOME/bin:$PATH"          # the docker -> podman shim
# NOT $ROOT/podman-tmp: that path's parent is root:chia-loop drwxrwx---, and
# buildah cannot make a bind mount private underneath it ("make ... private:
# Permission denied"). $HOME works -- it is where podman's graphroot already
# lives.
export TMPDIR="${TMPDIR:-$HOME/podman-tmp}"
mkdir -p "$TMPDIR"

IMAGE="${SPARSECRAFT_SYNTH_IMAGE:-localhost/sparsecraft-synth:latest}"
WITH_SKY130="${WITH_SKY130:-1}"

echo "building $IMAGE (WITH_SKY130=$WITH_SKY130, TMPDIR=$TMPDIR)"
# --format docker, not the OCI default: the base image carries
# SHELL ["/bin/bash", "-cl"], which the OCI format silently drops.
podman build \
    --format docker \
    -f docker/SparseCraftSynthDockerfile \
    --build-arg "WITH_SKY130=${WITH_SKY130}" \
    -t "$IMAGE" \
    .

echo
echo "built:"
podman images --format '  {{.Repository}}:{{.Tag}}  {{.Size}}' "$IMAGE"
