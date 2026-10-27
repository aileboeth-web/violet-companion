#!/usr/bin/env bash
# Run install_e2e.sh in throw-away containers of several distributions (no systemd in a container: the installer
# uses its own supervisor there; systemd is covered by the CI workflow on a real VM, see README.md).
#   bash "violet/deploy/linux-e2e/run_in_docker.sh" [debian:12 ubuntu:24.04 rockylinux:9 ...]
set -euo pipefail
PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
IMAGES=("$@")
[ ${#IMAGES[@]} -gt 0 ] || IMAGES=(debian:12 ubuntu:24.04 ubuntu:22.04 rockylinux:9)
for image in "${IMAGES[@]}"; do
  echo "=== $image"
  docker run --rm -v "$PROJECT:/src:ro" "$image" bash -c '
    set -e
    if command -v apt-get >/dev/null; then apt-get update -qq && apt-get install -y -qq curl python3 >/dev/null;
    elif command -v dnf >/dev/null; then dnf install -y -q curl-minimal python3 >/dev/null 2>&1 || dnf install -y -q python3 >/dev/null; fi
    cp -r /src /tmp/project && cd /tmp/project
    VIOLET_E2E_SERVICE=supervisor bash deploy/linux-e2e/install_e2e.sh'
done
