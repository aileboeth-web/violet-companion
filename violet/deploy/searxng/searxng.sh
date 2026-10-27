#!/usr/bin/env bash
# Violet -- manage the private SearXNG (deploy/searxng). See README.md.
#
#   ./searxng.sh init       create .env (random SEARXNG_SECRET) and the cache / state dirs; nothing else
#   ./searxng.sh check      Docker present, .env, settings, compose syntax, port / name conflicts -- no pull, no start
#   ./searxng.sh up         the deployment: check, ask "yes", pull the official image, start, health, one test search
#   ./searxng.sh upgrade    ask "yes", record the current image, pull the newer one, recreate (config / cache kept)
#   ./searxng.sh down | restart | status | logs [-f] | test
#   add --yes to skip the "yes" question (for an operator who already decided)
#
# Only "up" and "upgrade" pull images or start containers. This script never installs Docker, and nothing runs it
# by itself: not Violet, not start.sh, not a git pull (there are no git hooks in this repository).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"
CONTAINER=violet-searxng
NETWORK=violet-searxng
PROJECT=violet-searxng
SEARXNG_UID=977          # the official image's searxng user (compose.yaml: user "977:977")
ASSUME_YES=0

die() { echo "错误：$*" >&2; exit 1; }
info() { echo "· $*"; }

compose() {
  if docker compose version >/dev/null 2>&1; then
    docker compose "$@"
  elif command -v docker-compose >/dev/null 2>&1; then
    docker-compose "$@"
  else
    die "没有 docker compose（v2 插件或 docker-compose）。本脚本不会安装它，见 README.md"
  fi
}

need_docker() {
  command -v docker >/dev/null 2>&1 || die "没有安装 Docker。本脚本不会安装 Docker，见 README.md"
  docker info >/dev/null 2>&1 || die "连不上 Docker（服务没有启动，或当前用户没有权限）"
}

env_value() {
  # the value of KEY in .env -- read as data, never sourced
  [[ -f .env ]] || return 0
  grep -E "^$1=" .env | tail -n 1 | cut -d= -f2- || true
}

host_port() {
  local p
  p="$(env_value SEARXNG_HOST_PORT)"
  echo "${p:-18089}"
}

confirm() {
  [[ "$ASSUME_YES" == 1 ]] && return 0
  local answer
  read -r -p "$1 输入 yes 继续: " answer
  [[ "$answer" == "yes" ]] || die "没有确认，什么都没做"
}

random_secret() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32
  elif command -v python3 >/dev/null 2>&1; then
    python3 -c 'import secrets; print(secrets.token_hex(32))'
  else
    head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'
    echo
  fi
}

port_in_use() {
  if command -v ss >/dev/null 2>&1; then
    ss -ltnH "( sport = :$1 )" 2>/dev/null | grep -q .
  elif command -v netstat >/dev/null 2>&1; then
    netstat -ltn 2>/dev/null | awk '{print $4}' | grep -Eq "[:.]$1\$"
  else
    return 1
  fi
}

cmd_init() {
  if [[ -f .env ]]; then
    info ".env 已存在，不覆盖"
  else
    local secret
    secret="$(random_secret)"
    [[ ${#secret} -ge 32 ]] || die "生成随机密钥失败"
    (umask 077 && sed "s/^SEARXNG_SECRET=.*/SEARXNG_SECRET=${secret}/" env_example > .env)
    chmod 600 .env
    info "已创建 .env（随机 SEARXNG_SECRET，权限 600，不在 git 里）"
  fi
  mkdir -p cache state
  # the container runs as 977:977 (the official image's searxng user) and must be able to write its cache
  if [[ "$(stat -c %u cache 2>/dev/null || echo "$SEARXNG_UID")" != "$SEARXNG_UID" ]]; then
    if [[ "$(id -u)" == 0 ]]; then
      chown "$SEARXNG_UID:$SEARXNG_UID" cache && info "cache/ 已改为 $SEARXNG_UID:$SEARXNG_UID（容器里的 searxng 用户）"
    else
      info "请用 root 执行一次：chown $SEARXNG_UID:$SEARXNG_UID '$HERE/cache'（容器以 $SEARXNG_UID 运行，要写缓存）"
    fi
  fi
  chmod o+r config/settings.yml 2>/dev/null || true
  info "配置 config/settings.yml（只读挂载）；缓存 cache/；部署记录 state/"
}

cmd_check() {
  local problems=0 p secret ours owner net
  need_docker
  compose version >/dev/null
  [[ -f .env ]] || die ".env 不存在：先运行 ./searxng.sh init"
  secret="$(env_value SEARXNG_SECRET)"
  if [[ ${#secret} -lt 32 ]]; then echo "✗ .env 的 SEARXNG_SECRET 为空或太短"; problems=1; fi
  if [[ ! -f config/settings.yml ]]; then
    echo "✗ 缺少 config/settings.yml"; problems=1
  elif ! grep -Eq '^[[:space:]]*-[[:space:]]*json[[:space:]]*$' config/settings.yml; then
    echo "✗ config/settings.yml 没有打开 JSON 格式（search.formats）"; problems=1
  fi
  if [[ -d cache && "$(stat -c %u cache 2>/dev/null)" != "$SEARXNG_UID" ]]; then
    echo "✗ cache/ 不属于 $SEARXNG_UID（容器写不了缓存）：root 执行 chown $SEARXNG_UID:$SEARXNG_UID '$HERE/cache'"; problems=1
  fi
  if [[ -f config/settings.yml && "$(stat -c %A config/settings.yml 2>/dev/null | cut -c8)" != "r" ]]; then
    echo "✗ config/settings.yml 对其他用户不可读（容器以 $SEARXNG_UID 运行）：chmod o+r config/settings.yml"; problems=1
  fi
  if compose config -q; then echo "✓ compose 配置语法"; else echo "✗ compose 配置有误"; problems=1; fi
  p="$(host_port)"
  if ! [[ "$p" =~ ^[0-9]+$ ]] || (( p <= 1024 || p >= 65536 )); then
    echo "✗ SEARXNG_HOST_PORT 无效：$p"; problems=1
  else
    ours="$(docker ps -q --filter "name=^/${CONTAINER}\$")"
    if [[ -z "$ours" ]] && port_in_use "$p"; then
      echo "✗ 本机端口 $p 已被别的程序占用（改 .env 的 SEARXNG_HOST_PORT，再改 Violet 的 VIOLET_WEB_SEARXNG_URL）"; problems=1
    fi
  fi
  owner="$(docker ps -a --filter "name=^/${CONTAINER}\$" --format '{{.Label "com.docker.compose.project"}}')"
  if [[ -n "$owner" && "$owner" != "$PROJECT" ]]; then
    echo "✗ 已有同名容器 $CONTAINER，属于别的项目（$owner）"; problems=1
  fi
  net="$(docker network ls --filter "name=^${NETWORK}\$" --format '{{.Label "com.docker.compose.project"}}')"
  if [[ -n "$net" && "$net" != "$PROJECT" ]]; then
    echo "✗ 已有同名网络 $NETWORK，属于别的项目（$net）"; problems=1
  fi
  if command -v python3 >/dev/null 2>&1 && python3 -c 'import yaml' >/dev/null 2>&1; then
    if python3 validate_config.py; then echo "✓ validate_config.py"; else problems=1; fi
  else
    info "没有 python3 + PyYAML，跳过 validate_config.py（compose config 已检查语法）"
  fi
  (( problems == 0 )) || die "检查没有通过"
  echo "✓ 检查通过（没有拉取镜像，没有启动容器）"
}

record_image() {
  local id digests
  mkdir -p state
  id="$(docker inspect --format '{{.Image}}' "$CONTAINER" 2>/dev/null || true)"
  digests="$(docker image inspect --format '{{join .RepoDigests " "}}' "$id" 2>/dev/null || true)"
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $1 image=${id:-none} digests=${digests:-none}" >> state/image-digests.log
}

wait_healthy() {
  local i status=missing
  for i in $(seq 1 30); do
    status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
      "$CONTAINER" 2>/dev/null || echo missing)"
    if [[ "$status" == healthy ]]; then echo "✓ 容器健康"; return 0; fi
    sleep 2
  done
  echo "容器状态：$status（日志：./searxng.sh logs）"
  return 1
}

cmd_test() {
  # one real search through SearXNG (the container queries its engines on the internet) -- only when asked
  local p url body
  p="$(host_port)"
  url="http://127.0.0.1:${p}/search?q=SearXNG&format=json"
  command -v curl >/dev/null 2>&1 || die "需要 curl"
  body="$(curl -fsS --max-time 15 "$url")" || die "请求失败：$url（容器在运行吗？403 = settings.yml 的 formats 没有 json）"
  if command -v python3 >/dev/null 2>&1; then
    printf '%s' "$body" | python3 -c 'import json, sys
d = json.load(sys.stdin)
dead = [e[0] for e in d.get("unresponsive_engines") or []]
print("✓ JSON 搜索可用：%d 条结果；没有响应的引擎：%s" % (len(d.get("results") or []), "、".join(dead) or "无"))'
  else
    echo "✓ JSON 响应 ${#body} 字节"
  fi
}

cmd_up() {
  cmd_init
  cmd_check
  confirm "将拉取镜像 $(env_value SEARXNG_IMAGE) 并启动容器 $CONTAINER（只监听 127.0.0.1:$(host_port)）。"
  compose pull
  compose up -d
  record_image up
  wait_healthy || true
  cmd_test || true
  echo "Violet（直接跑在服务器上）：.env 里 VIOLET_WEB_SEARCH_PROVIDER=searxng、VIOLET_WEB_SEARXNG_URL=http://127.0.0.1:$(host_port)"
}

cmd_upgrade() {
  need_docker
  [[ -f .env ]] || die ".env 不存在：先运行 ./searxng.sh up"
  confirm "将拉取新版镜像并重建容器 $CONTAINER（配置和缓存保留）。"
  record_image before-upgrade
  compose pull
  compose up -d
  record_image after-upgrade
  wait_healthy || true
  cmd_test || true
  echo "回滚：把 state/image-digests.log 里 before-upgrade 那行的 digest 写进 .env：SEARXNG_IMAGE=docker.io/searxng/searxng@sha256:…，再 ./searxng.sh up"
}

usage() {
  sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
}

main() {
  local args=() a
  for a in "$@"; do
    if [[ "$a" == "--yes" ]]; then ASSUME_YES=1; else args+=("$a"); fi
  done
  set -- "${args[@]+"${args[@]}"}"
  local cmd="${1:-help}"
  shift || true
  case "$cmd" in
    init) cmd_init ;;
    check) cmd_check ;;
    up) cmd_up ;;
    upgrade) cmd_upgrade ;;
    down) need_docker; compose down ;;
    restart) need_docker; compose restart ;;
    status) need_docker; compose ps; docker inspect --format 'health: {{if .State.Health}}{{.State.Health.Status}}{{else}}-{{end}}' "$CONTAINER" 2>/dev/null || true ;;
    logs) need_docker; compose logs --tail=200 "$@" ;;
    test) cmd_test ;;
    help|-h|--help) usage ;;
    *) usage; die "不认识的命令：$cmd" ;;
  esac
}

main "$@"
