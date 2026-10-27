#!/usr/bin/env bash
# Violet one-command installer (phase 10.4 / 10.7).
#
# On a fresh server -- nothing to download first, no git, no Python setup:
#     curl -fsSL __VIOLET_RELEASE_BASE__/install.sh | bash
# The same command everywhere: as root it installs directly (Debian minimal has no sudo); as a normal user it runs
# itself again through sudo (asks for the password); a normal user without sudo is told to switch to root (su -).
# --user-install keeps it in the current user's home instead (~/.local/share/violet, no root needed).
# Options go after "-s --":
#     curl -fsSL __VIOLET_RELEASE_BASE__/install.sh | bash -s -- --access public
#     curl -fsSL __VIOLET_RELEASE_BASE__/install.sh | bash -s -- upgrade
# From an unpacked release or a source checkout the same script works locally:
#     bash install.sh [install|upgrade|status|backup|uninstall ...] [options]
#
# What this script does itself: find Python 3.11+ with venv (as root, install it with the system package manager --
# set VIOLET_NO_SYSTEM_PACKAGES=1 to forbid that), and -- when it is not next to the program -- download the release
# archive from the same place it came from, check its SHA-256 and unpack it to a temporary folder. Everything else is
# tools/violet_installer.py (see INSTALL.md).
#
# The body is one function called on the LAST line: when the script arrives through a pipe, bash has read all of it
# before anything runs, and no command below can swallow the rest of the script from stdin.
set -euo pipefail

violet_main() {
  local release_base="${VIOLET_RELEASE_BASE:-__VIOLET_RELEASE_BASE__}"
  local command="install"
  case "${1:-}" in
    install|upgrade|rollback|status|backup|restore|uninstall|doctor|start|stop|restart) command="$1"; shift ;;
  esac

  say() { printf '%s\n' "$*" >&2; }
  die() { say "失败：$*"; exit 1; }
  is_root() { [ "$(id -u 2>/dev/null || echo 1)" = "0" ]; }

  # --user-install: stay the current user on purpose (no root, no sudo)
  local user_install=0 arg
  local -a rest=()
  for arg in "$@"; do
    if [ "$arg" = "--user-install" ]; then user_install=1; else rest+=("$arg"); fi
  done
  set -- ${rest[@]+"${rest[@]}"}

  if ! is_root && [ "$user_install" = "0" ] && [ "${VIOLET_NO_ELEVATE:-0}" != "1" ]; then
    local script=""
    if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then script="${BASH_SOURCE[0]}"; fi
    if command -v sudo >/dev/null 2>&1; then
      say "== 需要管理员权限：用 sudo 重新运行（可能要输入你的密码）"
      local code=0 tmp_script=""
      if [ -z "$script" ]; then
        # arrived through a pipe: fetch this same script again to hand it to sudo
        case "$release_base" in
          __VIOLET_RELEASE_BASE*) die "请以 root 运行，或在前面加 sudo" ;;
        esac
        tmp_script="$(mktemp)"
        if command -v curl >/dev/null 2>&1; then curl -fsSL --retry 3 "$release_base/install.sh" -o "$tmp_script"
        elif command -v wget >/dev/null 2>&1; then wget -q -O "$tmp_script" "$release_base/install.sh"
        else die "需要 curl 或 wget"; fi
        script="$tmp_script"
      fi
      sudo bash "$script" "$command" "$@" || code=$?
      [ -z "$tmp_script" ] || rm -f "$tmp_script"
      exit "$code"
    fi
    die "需要 root 权限，但这台机器没有 sudo。请先切换到 root（执行 su - 并输入 root 密码）后重新运行这条命令；
或者加 --user-install 只装到当前用户（不注册系统服务，没有自动更新）"
  fi

  find_python() {
    local candidate
    # 3.11+: the version Violet is tested with (3.10 lacks builtins the program uses, e.g. BaseExceptionGroup)
    for candidate in "${PYTHON:-}" python3.13 python3.12 python3.11 python3; do
      [ -n "$candidate" ] || continue
      command -v "$candidate" >/dev/null 2>&1 || continue
      if "$candidate" - >/dev/null 2>&1 <<'PY'
import sys, venv, ensurepip
sys.exit(0 if sys.version_info >= (3, 11) else 1)
PY
      then
        command -v "$candidate"
        return 0
      fi
    done
    return 1
  }

  install_python() {
    [ "${VIOLET_NO_SYSTEM_PACKAGES:-0}" = "1" ] && return 1
    is_root || return 1
    local pkg=""
    if command -v apt-get >/dev/null 2>&1; then pkg="apt"
    elif command -v dnf >/dev/null 2>&1; then pkg="dnf"
    elif command -v yum >/dev/null 2>&1; then pkg="yum"
    elif command -v apk >/dev/null 2>&1; then pkg="apk"
    elif command -v zypper >/dev/null 2>&1; then pkg="zypper"
    else return 1; fi
    say "== 安装 Python 3 和 venv（系统包管理器：$pkg）"
    case "$pkg" in
      apt) DEBIAN_FRONTEND=noninteractive apt-get update -y </dev/null >&2 && \
           DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-venv ca-certificates openssl </dev/null >&2 ;;
      dnf) dnf install -y python3 openssl </dev/null >&2 ;;
      yum) yum install -y python3 openssl </dev/null >&2 ;;
      apk) apk add --no-cache python3 py3-pip openssl </dev/null >&2 ;;
      zypper) zypper --non-interactive install python3 openssl </dev/null >&2 ;;
    esac
    find_python >/dev/null && return 0
    # the system's default python3 is older than 3.11 (Ubuntu 22.04 ships 3.10): its versioned package
    say "== 系统自带的 Python 版本太旧，安装 Python 3.11"
    case "$pkg" in
      apt) DEBIAN_FRONTEND=noninteractive apt-get install -y python3.11 python3.11-venv </dev/null >&2 ;;
      dnf) dnf install -y python3.11 python3.11-pip </dev/null >&2 ;;
      yum) yum install -y python3.11 python3.11-pip </dev/null >&2 ;;
      zypper) zypper --non-interactive install python311 </dev/null >&2 ;;
      *) return 1 ;;
    esac
  }

  fetch() {
    if command -v curl >/dev/null 2>&1; then curl -fsSL --retry 3 "$1" -o "$2"
    elif command -v wget >/dev/null 2>&1; then wget -q -O "$2" "$1"
    else die "需要 curl 或 wget 来下载程序"; fi
  }

  sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1
    elif command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | cut -d' ' -f1
    else "$PY" -c 'import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$1"; fi
  }

  PY="$(find_python || true)"
  if [ -z "$PY" ]; then
    install_python || true
    PY="$(find_python || true)"
  fi
  [ -n "$PY" ] || die "没有找到 Python 3.11 或更新版本（带 venv）。请用 root 运行（会自动安装），或先安装：apt install python3.11 python3.11-venv"

  # openssl: the self-signed HTTPS certificate of a public install (Debian minimal may not have it)
  if is_root && ! command -v openssl >/dev/null 2>&1 && [ "${VIOLET_NO_SYSTEM_PACKAGES:-0}" != "1" ]; then
    say "== 安装 openssl（生成 HTTPS 证书用）"
    if command -v apt-get >/dev/null 2>&1; then
      DEBIAN_FRONTEND=noninteractive apt-get install -y openssl </dev/null >&2 || \
        { DEBIAN_FRONTEND=noninteractive apt-get update -y </dev/null >&2 && \
          DEBIAN_FRONTEND=noninteractive apt-get install -y openssl </dev/null >&2; } || true
    elif command -v dnf >/dev/null 2>&1; then dnf install -y openssl </dev/null >&2 || true
    elif command -v yum >/dev/null 2>&1; then yum install -y openssl </dev/null >&2 || true
    elif command -v apk >/dev/null 2>&1; then apk add --no-cache openssl </dev/null >&2 || true
    elif command -v zypper >/dev/null 2>&1; then zypper --non-interactive install openssl </dev/null >&2 || true
    fi
  fi

  # the program: next to this script, else the release archive from the same address
  local here=""
  if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
    here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  fi
  if [ -z "$here" ] || [ ! -f "$here/tools/violet_installer.py" ]; then
    case "$release_base" in
      __VIOLET_RELEASE_BASE*) die "这个 install.sh 不是发布版本（没有下载地址）。请在程序目录里运行：sudo bash install.sh" ;;
    esac
    VIOLET_TMP="$(mktemp -d)"
    trap 'rm -rf "$VIOLET_TMP"' EXIT
    local tmp="$VIOLET_TMP"
    say "== 下载 Violet：$release_base"
    fetch "$release_base/violet.tar.gz" "$tmp/violet.tar.gz"
    fetch "$release_base/violet.tar.gz.sha256" "$tmp/violet.tar.gz.sha256"
    local want got
    want="$(tr -d ' \r\n' < "$tmp/violet.tar.gz.sha256" | cut -c1-64)"
    got="$(sha256_of "$tmp/violet.tar.gz")"
    [ -n "$want" ] && [ "$want" = "$got" ] || die "下载的文件校验失败（SHA-256 不一致），没有安装任何东西"
    tar -xzf "$tmp/violet.tar.gz" -C "$tmp"
    here="$tmp/violet"
    [ -f "$here/tools/violet_installer.py" ] || die "下载的文件里没有安装程序"
  fi

  local code=0
  "$PY" "$here/tools/violet_installer.py" "$command" "$@" </dev/null || code=$?
  return "$code"
}

violet_main "$@"
