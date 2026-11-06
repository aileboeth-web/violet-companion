# Linux install acceptance (automated)

**English** | [简体中文](README.zh-CN.md)

Before a release, the one-line install must pass this set of checks on real Linux. Use only throwaway machines / VMs / containers. **Do not run it on a server with a real Violet on it.**

| Way | Command | Covers |
|---|---|---|
| A fresh Linux VM (root) | `bash "violet/deploy/linux-e2e/install_e2e.sh"` | Everything: one-command install, systemd (start on boot, automatic restart after kill -9), the initial password locking the panel, repair keeping the port and password, backup verification, automatic rollback of a bad upgrade, uninstall keeping data, purge cleanup |
| Docker (several distributions, no systemd) | `bash "violet/deploy/linux-e2e/run_in_docker.sh" debian:12 ubuntu:24.04 rockylinux:9` | Same as above, with the service run by Violet's own supervisor |
| GitHub Actions | `.github/workflows/install-linux.yml` (triggered by hand, or automatically when the installer changes) | systemd on Ubuntu 22.04 / 24.04 VMs + containers for three distributions |

The script builds the release package itself (`tools/make_release.py --worktree`), serves it for download on 127.0.0.1, and then runs exactly the command users run: `curl -fsSL <address>/install.sh | bash`. It needs access to PyPI (to install dependencies). Each check prints PASS / FAIL, and it stops at the first failure.

Not checked automatically here and worth a look by hand: `--access public` on the real internet (cloud security groups, DNS), accepting the self-signed certificate in a browser, and logging in to real chat platforms.
