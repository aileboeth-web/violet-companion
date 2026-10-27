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
    # a normal user on a machine without sudo (Debian minimal): told to switch to root, nothing installed
    if ! command -v sudo >/dev/null && command -v useradd >/dev/null && command -v su >/dev/null; then
      useradd -m violet-e2e-user
      if su violet-e2e-user -s /bin/bash -c "bash /tmp/project/install.sh doctor" >/tmp/nonroot.out 2>&1; then
        cat /tmp/nonroot.out; echo "FAIL a normal user without sudo was not stopped"; exit 1; fi
      grep -q "su -" /tmp/nonroot.out || { cat /tmp/nonroot.out; echo "FAIL no hint to switch to root"; exit 1; }
      echo "PASS normal user without sudo: told to switch to root (su -)"
    fi
    # root without sudo (the one-line command below runs as root, no sudo involved)
    VIOLET_E2E_SERVICE=supervisor bash deploy/linux-e2e/install_e2e.sh'
done
