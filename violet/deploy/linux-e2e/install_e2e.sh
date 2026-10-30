#!/usr/bin/env bash
# Linux acceptance of the one-command install (phase 10.7). Run as root on a FRESH, disposable Linux machine / VM /
# CI runner -- never on a server with a real Violet. It serves a release built from this checkout on 127.0.0.1, runs the
# exact user command (curl ... | sudo bash) and checks every promise of INSTALL.md:
#
#   bash "violet/deploy/linux-e2e/install_e2e.sh"                   # systemd if present, else the supervisor
#   VIOLET_E2E_SERVICE=supervisor bash .../install_e2e.sh                   # containers without systemd
#
# Prints PASS / FAIL per check and exits non-zero on the first failure. Needs: bash, curl, internet access to PyPI
# (the installer installs the requirements), python3 only for building the release (the install itself must find or
# install Python on its own: the build uses a separate interpreter path, the install starts from a clean PATH lookup).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT="$(cd "$HERE/../.." && pwd)"
ROOT="${VIOLET_E2E_ROOT:-/opt/violet}"
SERVICE="${VIOLET_E2E_SERVICE:-auto}"
WORK="$(mktemp -d)"
PORT_HTTP=18765
trap 'kill "${HTTP_PID:-0}" 2>/dev/null || true; rm -rf "$WORK"' EXIT

pass() { printf 'PASS %s\n' "$*"; }
fail() { printf 'FAIL %s\n' "$*"; exit 1; }
json() { python3 -c "import json,sys; d=json.loads(sys.stdin.read().strip().splitlines()[-1]); print($1)"; }

[ "$(id -u)" = "0" ] || fail "run as root (on a disposable machine)"
[ ! -e "$ROOT/install.json" ] || fail "$ROOT already has an Violet installation -- use a fresh machine"

# 1. release served locally (exactly what would be uploaded)
python3 "$PROJECT/tools/make_release.py" --base "http://127.0.0.1:$PORT_HTTP" --out "$WORK/dist" --worktree >/dev/null
( cd "$WORK/dist" && exec python3 -m http.server "$PORT_HTTP" --bind 127.0.0.1 >/dev/null 2>&1 ) &
HTTP_PID=$!
sleep 1
pass "release built and served"

# 2. the one command
OUT="$(curl -fsSL "http://127.0.0.1:$PORT_HTTP/install.sh" | bash -s -- --root "$ROOT" --service "$SERVICE" \
       --update-feed "http://127.0.0.1:$PORT_HTTP/release.json" --json)" || {
  printf '%s\n' "$OUT"; tail -n 40 "$ROOT/logs/main.log" 2>/dev/null || true; fail "install command"; }
echo "$OUT" | json 'd["core_ok"]' | grep -q True || fail "core service not healthy"
PORT="$(echo "$OUT" | json 'd["port"]')"
SCHEME="$(echo "$OUT" | json 'd["urls"][0].split(":")[0]')"
PASSWORD="$(echo "$OUT" | json 'd["admin"]["password"]')"
[ ${#PASSWORD} -eq 20 ] || fail "initial password"
echo "$OUT" | json '[l["state"] for l in d["network"] if l["layer"]=="public"][0]' | grep -qv '^ok$' || fail "public must not be claimed"
# as root the default is public access over HTTPS (https://IP:port/entrance, 1Panel style)
echo "$OUT" | json 'd["access"]' | grep -qx public || fail "a root install is not public by default"
[ "$SCHEME" = "https" ] || fail "public access without HTTPS"
grep -q '^VIOLET_DASHBOARD_HOST=0.0.0.0' "$ROOT/config/violet.env" || fail "not listening on every interface"
pass "installed on port $PORT, public over HTTPS by default ($(echo "$OUT" | json 'd["urls"][0]')), initial password shown once"

# 2b. a restrictive host firewall (VIOLET_E2E_STRICT_FIREWALL=1: the runner's iptables INPUT policy is DROP, as with
#     1Panel / CSF): the installer opened the port -- reachable on the LAN address, through a rule marked "violet"
STRICT="${VIOLET_E2E_STRICT_FIREWALL:-0}"
LANIP="$(hostname -I 2>/dev/null | awk '{print $1}')"
violet_rule() { iptables -S INPUT | grep -q -- "--dport $1 -m comment --comment violet"; }
if [ "$STRICT" = "1" ]; then
  curl -fsSk --max-time 5 "https://$LANIP:$PORT/healthz" | grep -q violet || fail "port $PORT not opened (DROP policy)"
  violet_rule "$PORT" || fail "no iptables rule marked violet for $PORT"
  pass "restrictive firewall: port $PORT opened (reachable on $LANIP)"
fi

# 3. service manager
MODE="$(python3 -c "import json;print(json.load(open('$ROOT/install.json'))['service'])")"
if [ "$MODE" = "systemd" ]; then
  systemctl is-active --quiet violet || fail "systemd unit not active"
  systemctl is-enabled --quiet violet || fail "systemd unit not enabled at boot"
  MAINPID="$(systemctl show -p MainPID --value violet)"
  kill -9 "$MAINPID"
  for _ in $(seq 1 30); do curl -fsSk "$SCHEME://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 && break; sleep 1; done
  [ "$(systemctl show -p MainPID --value violet)" != "$MAINPID" ] || fail "not restarted after a crash"
  pass "systemd: active, enabled, restarted after kill -9"
  if [ "$STRICT" = "1" ]; then
    # a reboot or a panel reloading its rules drops ours: the next start of Violet puts it back (ExecStartPre)
    iptables -D INPUT -p tcp --dport "$PORT" -m comment --comment violet -j ACCEPT
    ! violet_rule "$PORT" || fail "could not remove the rule for the test"
    systemctl restart violet
    for _ in $(seq 1 30); do curl -fsSk "$SCHEME://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 && break; sleep 1; done
    violet_rule "$PORT" || fail "the firewall rule was not put back on start"
    curl -fsSk --max-time 5 "https://$LANIP:$PORT/healthz" | grep -q violet || fail "not reachable after the rule came back"
    pass "restrictive firewall: a removed rule is put back when Violet starts"
  fi
else
  pass "service mode: $MODE"
fi
curl -fsSk "$SCHEME://127.0.0.1:$PORT/healthz" | grep -q violet || fail "healthz"

# 4. dashboard: security entrance, username + initial password, which must be changed first
ENTRANCE="$(echo "$OUT" | json 'd["admin"]["entrance"]')"
ADMIN_USER="$(echo "$OUT" | json 'd["admin"]["user"]')"
[ ${#ENTRANCE} -eq 12 ] || fail "security entrance"
echo "$OUT" | json 'd["urls"][0]' | grep -q "/$ENTRANCE\$" || fail "address without the entrance"
code() { curl -sk -o /dev/null -w '%{http_code}' "$@"; }
[ "$(code "$SCHEME://127.0.0.1:$PORT/auth/login")" = "404" ] || fail "login page reachable without the entrance"
[ "$(code "$SCHEME://127.0.0.1:$PORT/control")" = "404" ] || fail "control reachable without the entrance"
JAR="$WORK/jar"
login() {   # $1 entrance, $2 password -> the cookie jar $JAR, logged in (prints the login status code)
  rm -f "$JAR"
  [ "$(code -c "$JAR" "$SCHEME://127.0.0.1:$PORT/$1")" = "302" ] || fail "entrance /$1"
  code -b "$JAR" -c "$JAR" -H "Origin: $SCHEME://127.0.0.1:$PORT" --data-urlencode "username=$ADMIN_USER" \
       --data-urlencode "password=$2" "$SCHEME://127.0.0.1:$PORT/auth/login"
}
[ "$(login "$ENTRANCE" "$PASSWORD")" = "302" ] || fail "login with username and the initial password"
CODE="$(code -b "$JAR" -H 'Accept: application/json' "$SCHEME://127.0.0.1:$PORT/api/control/setup")"
[ "$CODE" = "200" ] || fail "the generated password should work as it is ($CODE)"   # stage 3: no forced change
pass "security entrance (404 without it); username + password login; the generated password works as it is"

# 4b. violet info / reset-entrance / reset-password / set-port (as root: the service must still read admin.json)
"$ROOT/violet" info --json | json 'd["entrance"]' | grep -qx "$ENTRANCE" || fail "info"
"$ROOT/violet" info | grep -q "$ADMIN_USER" || fail "info text"
NEW_ENTRANCE="$("$ROOT/violet" reset-entrance --json | json 'd["entrance"]')"
[ "$NEW_ENTRANCE" != "$ENTRANCE" ] || fail "reset-entrance"
[ "$(code "$SCHEME://127.0.0.1:$PORT/$ENTRANCE")" = "404" ] || fail "old entrance still works"
PASSWORD="$("$ROOT/violet" reset-password --json | json 'd["password"]')"
[ "$(login "$NEW_ENTRANCE" "$PASSWORD")" = "302" ] || fail "login after reset-entrance / reset-password"
OLD_PORT="$PORT"; PORT=$((PORT + 37))
"$ROOT/violet" set-port "$PORT" --json | json 'd["port"]' | grep -qx "$PORT" || fail "set-port"
curl -fsSk "$SCHEME://127.0.0.1:$PORT/healthz" | grep -q violet || fail "not answering on the new port"
! curl -fsSk "$SCHEME://127.0.0.1:$OLD_PORT/healthz" >/dev/null 2>&1 || fail "still answering on the old port"
if [ "$STRICT" = "1" ]; then
  curl -fsSk --max-time 5 "https://$LANIP:$PORT/healthz" | grep -q violet || fail "new port $PORT not opened"
  ! violet_rule "$OLD_PORT" || fail "the rule for the old port $OLD_PORT was not removed"
fi
pass "info, reset-entrance, reset-password and set-port ($OLD_PORT -> $PORT)"

# 4c. automatic updates (root + systemd): "update now" in the Control Center -> the root update service installs a
#     newer release (built here with a higher version and served from the same feed)
if [ "$MODE" = "systemd" ]; then
  systemctl is-active --quiet violet-update.timer || fail "update timer not active"
  systemctl is-active --quiet violet-update.path || fail "update path unit not active"
  OLD_VERSION="$(python3 -c "import json;print(json.load(open('$ROOT/install.json'))['version'])")"
  NEXT="$(python3 -c "v='$OLD_VERSION'.split('-')[0].split('.'); print('.'.join(v[:2]+[str(int(v[2])+1)]))")"
  cp -r "$PROJECT" "$WORK/next"
  echo "$NEXT" > "$WORK/next/VERSION"
  python3 "$WORK/next/tools/make_release.py" --base "http://127.0.0.1:$PORT_HTTP" --out "$WORK/dist" --worktree >/dev/null
  NEWPW="e2e-Password-$(date +%s)"
  CODE="$(code -b "$JAR" -c "$JAR" -H "Origin: $SCHEME://127.0.0.1:$PORT" -H 'Content-Type: application/json' \
          --data "{\"current\":\"$PASSWORD\",\"new\":\"$NEWPW\",\"confirm\":\"$NEWPW\"}" "$SCHEME://127.0.0.1:$PORT/auth/password")"
  [ "$CODE" = "200" ] || fail "password change ($CODE)"
  # "notify": the check below must not start the update by itself; "update now" does
  CODE="$(code -X PUT -b "$JAR" -H "Origin: $SCHEME://127.0.0.1:$PORT" -H 'Content-Type: application/json' \
          --data '{"changes":{"VIOLET_UPDATE_MODE":"notify"}}' "$SCHEME://127.0.0.1:$PORT/api/control/settings")"
  [ "$CODE" = "200" ] || fail "update mode setting ($CODE)"
  UPD="$(curl -sk -b "$JAR" -H 'Accept: application/json' "$SCHEME://127.0.0.1:$PORT/api/control/update?refresh=1")"
  echo "$UPD" | json 'd["data"]["latest"]' | grep -qx "$NEXT" || { echo "$UPD"; fail "the page does not see $NEXT"; }
  echo "$UPD" | json 'd["data"]["auto"]["available"]' | grep -q True || fail "automatic updates not available"
  sleep 5
  [ "$(python3 -c "import json;print(json.load(open('$ROOT/install.json'))['version'])")" = "$OLD_VERSION" ] ||
    fail "notify mode updated by itself"
  CODE="$(code -b "$JAR" -H "Origin: $SCHEME://127.0.0.1:$PORT" -H 'Content-Type: application/json' \
          --data '{"confirm":true}' "$SCHEME://127.0.0.1:$PORT/api/control/update/apply")"
  [ "$CODE" = "200" ] || fail "update now ($CODE)"
  for _ in $(seq 1 120); do
    python3 -c "import json,sys;d=json.load(open('$ROOT/install.json'));sys.exit(0 if d.get('version')=='$NEXT' and (d.get('auto_update') or {}).get('last_result')=='updated' else 1)" 2>/dev/null && break
    sleep 3
  done
  python3 -c "import json,sys;d=json.load(open('$ROOT/install.json'));print(d.get('version'), d.get('auto_update'))"
  [ "$(python3 -c "import json;print(json.load(open('$ROOT/install.json'))['version'])")" = "$NEXT" ] || {
    journalctl -u violet-update -n 60 --no-pager || true; fail "automatic update to $NEXT"; }
  for _ in $(seq 1 30); do curl -fsSk "$SCHEME://127.0.0.1:$PORT/healthz" 2>/dev/null | grep -q violet && break; sleep 1; done
  curl -fsSk "$SCHEME://127.0.0.1:$PORT/healthz" | grep -q violet || fail "not answering after the automatic update"
  [ ! -e "$ROOT/data/control/update-request" ] || fail "update request left behind"
  ! ls "$ROOT"/backups/*before-upgrade* >/dev/null 2>&1 || fail "backup of a successful update not deleted"
  # 0.7.0: the progress dialog's file -- written through the whole update, owned by the service account
  UPDP="$ROOT/data/control/update-progress.json"
  python3 -c "import json,sys;d=json.load(open('$UPDP'));sys.exit(0 if d['state']=='done' and d['percent']==100 and d.get('version')=='$NEXT' else 1)" \
    || { cat "$UPDP"; fail "update progress"; }
  [ "$(stat -c %U "$UPDP")" = "violet" ] || fail "update progress not readable by the service ($(stat -c %U "$UPDP"))"
  curl -sk -b "$JAR" -H 'Accept: application/json' "$SCHEME://127.0.0.1:$PORT/api/control/progress/update" \
    | json 'd["data"]["state"]' | grep -qx done || fail "progress API"
  pass "automatic update from the Control Center: $OLD_VERSION -> $NEXT (root update service, healthy)"
fi

# 4d. the phone relay (root + systemd): `violet mcp-relay install` downloads the newest relay of the violet-mcp GitHub
#     release (checked against its SHA256SUMS.txt), starts it, opens the device port, exports the pairing values for
#     the Control Center (owned by the service account, 0600) and Violet re-exports them on every start
if [ "$MODE" = "systemd" ]; then
  # 0.7.0: installed from the Control Center ("一键安装中继"): the page leaves task-request, the root task service
  # (violet-task.path -> violet-task.service) installs it and writes task-progress.json, which the page polls
  systemctl is-active --quiet violet-task.path || fail "task path unit not active"
  CODE="$(code -b "$JAR" -H "Origin: $SCHEME://127.0.0.1:$PORT" -H 'Content-Type: application/json' \
          --data '{"action":"install","confirm":true}' "$SCHEME://127.0.0.1:$PORT/api/control/phone-mcp/task")"
  [ "$CODE" = "200" ] || fail "relay install request from the page ($CODE)"
  TASK_STATE=""; MAX_PCT=0
  for _ in $(seq 1 120); do
    TP="$(curl -sk -b "$JAR" -H 'Accept: application/json' "$SCHEME://127.0.0.1:$PORT/api/control/progress/task")"
    TASK_STATE="$(echo "$TP" | json 'd["data"]["state"]')"
    PCT="$(echo "$TP" | json 'd["data"].get("percent") or 0')"; [ "$PCT" -gt "$MAX_PCT" ] && MAX_PCT="$PCT"
    case "$TASK_STATE" in done|failed|stale) break ;; esac
    sleep 2
  done
  [ "$TASK_STATE" = "done" ] || { echo "$TP"; journalctl -u violet-task -n 40 --no-pager || true; fail "relay install from the page: $TASK_STATE"; }
  [ ! -e "$ROOT/data/control/task-request" ] || fail "task request left behind"
  RELAY="$("$ROOT/violet" mcp-relay status --json)" || fail "mcp-relay status"
  echo "$RELAY" | json 'd["installed"] and d["managed"]' | grep -q True || { echo "$RELAY"; fail "relay not installed by the task"; }
  pass "relay installed from the Control Center through the root task service (progress reached $MAX_PCT% then done)"
  systemctl is-active --quiet violet-mcp-relay || fail "relay service not active"
  DEV_PORT="$(echo "$RELAY" | json 'd["device_port"]')"
  EXP="$ROOT/data/control/mcp-relay.json"
  [ "$(stat -c %U:%a "$EXP")" = "violet:600" ] || fail "pairing values file: $(stat -c %U:%a "$EXP")"
  CLIENT="$(python3 -c "import json;print(json.load(open('$EXP'))['client_url'].rsplit('/',1)[0])")"
  curl -fs "$CLIENT/health" | grep -q '"ok":true' || fail "relay health ($CLIENT)"
  python3 -c "import json;d=json.load(open('$EXP'));assert d['device_secret']==open('/var/lib/violet-mcp-relay/device.secret').read().strip();assert d['pin'].startswith('sha256/') or len(d['pin'])>20, d['pin']" \
    || fail "pairing values (secret / pin)"
  PH="$(curl -sk -b "$JAR" -H 'Accept: application/json' "$SCHEME://127.0.0.1:$PORT/api/control/phone-mcp")"
  echo "$PH" | json 'd["data"]["installed"] and bool(d["data"]["relay"]["device_secret"]) and d["data"]["health"]["reachable"]' \
    | grep -q True || { echo "$PH" | head -c 300; fail "phone card in the Control Center"; }
  if [ "$STRICT" = "1" ]; then
    [ "$(curl -sk --max-time 5 -o /dev/null -w '%{http_code}' "https://$LANIP:$DEV_PORT/device")" != "000" ] \
      || fail "device port $DEV_PORT not opened"
  fi
  # 0.6.1: move the device port (a random new one); secret and certificate stay, the rule moves with it
  SECRET_BEFORE="$(cat /var/lib/violet-mcp-relay/device.secret)"
  MOVED="$("$ROOT/violet" mcp-relay set-port --json)" || { echo "$MOVED"; fail "mcp-relay set-port"; }
  NEW_DEV="$(echo "$MOVED" | json 'd["device_port"]')"
  [ "$NEW_DEV" != "$DEV_PORT" ] || fail "set-port kept port $DEV_PORT"
  for _ in $(seq 1 20); do curl -fs "$CLIENT/health" >/dev/null 2>&1 && break; sleep 1; done
  systemctl is-active --quiet violet-mcp-relay || fail "relay not active after set-port"
  [ "$(cat /var/lib/violet-mcp-relay/device.secret)" = "$SECRET_BEFORE" ] || fail "set-port changed the device secret"
  python3 -c "import json;d=json.load(open('$EXP'));assert d['device_port']==$NEW_DEV, d" || fail "pairing values not re-exported"
  if [ "$STRICT" = "1" ]; then
    [ "$(curl -sk --max-time 5 -o /dev/null -w '%{http_code}' "https://$LANIP:$NEW_DEV/device")" != "000" ] \
      || fail "new device port $NEW_DEV not opened"
  fi
  pass "phone relay: set-port $DEV_PORT -> $NEW_DEV (secret kept, pairing values re-exported)"
  DEV_PORT="$NEW_DEV"
  rm -f "$EXP"
  systemctl restart violet
  for _ in $(seq 1 30); do curl -fsSk "$SCHEME://127.0.0.1:$PORT/healthz" 2>/dev/null | grep -q violet && break; sleep 1; done
  [ -f "$EXP" ] || fail "pairing values not exported again when Violet started"
  pass "phone relay: $(echo "$RELAY" | json 'd.get("asset") or "existing relay"') installed (sha256 checked), device port $DEV_PORT open, pairing values in the Control Center"
fi

# 4e. 0.7.1: web search -- the page's "一键安装联网搜索": the root task service puts SearXNG (source from GitHub, own
#     venv, systemd, 127.0.0.1 only) on this server; the page then switches web search on with the exported URL;
#     uninstall removes it again. Installed once more by the CLI so the purge at the end has to remove it too.
if [ "$MODE" = "systemd" ]; then
  wait_task() {   # $1 what (for messages): polls /api/control/progress/task until done (fails otherwise); WT_MAX
    local st="" tp="" pct
    WT_MAX=0
    for _ in $(seq 1 300); do
      tp="$(curl -sk -b "$JAR" -H 'Accept: application/json' "$SCHEME://127.0.0.1:$PORT/api/control/progress/task")"
      st="$(echo "$tp" | json 'd["data"]["state"]')"
      pct="$(echo "$tp" | json 'd["data"].get("percent") or 0')"; [ "$pct" -gt "$WT_MAX" ] && WT_MAX="$pct"
      case "$st" in done|failed|stale) break ;; esac
      sleep 3
    done
    [ "$st" = "done" ] || { journalctl -u violet-task -n 60 --no-pager || true
                            echo "$tp" | json '"\n".join((d["data"].get("message") or "").splitlines()[-14:])'
                            fail "$1: $st"; }
  }
  SXAPI="$SCHEME://127.0.0.1:$PORT/api/control/tools/web/searxng"
  post_sx() { code -b "$JAR" -H "Origin: $SCHEME://127.0.0.1:$PORT" -H 'Content-Type: application/json' \
                --data "{\"action\":\"$1\",\"confirm\":true}" "$SXAPI"; }
  [ "$(post_sx install)" = "200" ] || fail "web search install request"
  wait_task "SearXNG install"; MAXP="$WT_MAX"
  systemctl is-active --quiet violet-searxng || { journalctl -u violet-searxng -n 60 --no-pager || true; fail "violet-searxng not active"; }
  SXURL="$(python3 -c "import json;print(json.load(open('$ROOT/data/control/searxng.json'))['url'])")"
  case "$SXURL" in http://127.0.0.1:*) ;; *) fail "searxng url $SXURL" ;; esac
  ! ss -ltnH "( sport = :${SXURL##*:} )" | grep -v '127.0.0.1' | grep -q . || fail "SearXNG listens beyond loopback"
  curl -fsS --max-time 30 "$SXURL/search?q=violet&format=json" | python3 -c "import json,sys;d=json.load(sys.stdin);assert 'results' in d" \
    || fail "SearXNG JSON search"
  [ "$(post_sx enable)" = "200" ] || fail "web search enable"
  curl -sk -b "$JAR" -H 'Accept: application/json' "$SXAPI" | json 'd["data"]["web_enabled"] and d["data"]["in_use"]' \
    | grep -q True || fail "web search not switched on"
  pass "web search installed from the Control Center (progress reached $MAXP%), SearXNG JSON search on $SXURL, switched on"
  # 0.7.2: the engine set for servers in mainland China (bing / baidu / sogou / 360search) must be a valid SearXNG
  #        configuration: written, restarted, healthy, answering JSON; then "重新选择搜索引擎" from the page
  SXPORT="${SXURL##*:}"
  "$ROOT/venv/bin/python" -c "import sys;sys.path.insert(0,'$ROOT/app/tools');import violet_searxng as sx;open('$ROOT/searxng/settings.yml','w').write(sx.settings_text($SXPORT,'cn'))" \
    || fail "write the cn engine settings"
  systemctl restart violet-searxng
  for _ in $(seq 1 45); do curl -fs "$SXURL/healthz" >/dev/null 2>&1 && break; sleep 2; done
  curl -fs "$SXURL/healthz" >/dev/null || { journalctl -u violet-searxng -n 60 --no-pager || true; fail "SearXNG with the cn engines did not start"; }
  CN="$(curl -fsS --max-time 30 "$SXURL/search?q=weather&format=json")" || fail "SearXNG JSON search with the cn engines"
  CNN="$(echo "$CN" | python3 -c "import json,sys;d=json.load(sys.stdin);print(len(d.get('results') or []), sorted({r.get('engine') for r in d.get('results') or []}))")"
  [ "$(post_sx tune)" = "200" ] || fail "engine choice request"
  wait_task "SearXNG engine choice"
  curl -sk -b "$JAR" -H 'Accept: application/json' "$SXAPI" | json 'bool(d["data"]["engines_text"])' | grep -q True \
    || fail "engine choice not exported"
  T="$(curl -sk -b "$JAR" -H "Origin: $SCHEME://127.0.0.1:$PORT" -H 'Content-Type: application/json' -X POST --data '{}' \
        "$SCHEME://127.0.0.1:$PORT/api/control/tools/web/test")"
  echo "$T" | json 'd["data"]["search"]["ok"] and d["data"]["fetch"]["ok"]' | grep -q True || { echo "$T"; fail "web test button"; }
  pass "cn engine set valid (results, engines: $CNN); engine choice from the page; web test button: search and page reading OK"
  # 0.7.3: the admin's own choice from the page, with "只用 IPv4" (outgoing.source_ips 0.0.0.0) -- a valid setting
  CODE="$(code -b "$JAR" -H "Origin: $SCHEME://127.0.0.1:$PORT" -H 'Content-Type: application/json' \
          --data '{"action":"configure","confirm":true,"engines":["bing","sogou"],"ipv4":true}' "$SXAPI")"
  [ "$CODE" = "200" ] || fail "own engine choice request ($CODE)"
  wait_task "own engine choice"
  grep -q 'source_ips' "$ROOT/searxng/settings.yml" || fail "IPv4 only not written"
  curl -fs "$SXURL/healthz" >/dev/null || { journalctl -u violet-searxng -n 60 --no-pager || true; fail "SearXNG with IPv4 only did not start"; }
  OWN="$(curl -fsS --max-time 30 "$SXURL/search?q=weather&format=json" | python3 -c "import json,sys;d=json.load(sys.stdin);print(len(d.get('results') or []), d.get('unresponsive_engines'))")" \
    || fail "SearXNG JSON search with IPv4 only"
  pass "own engine choice from the page (bing + sogou, IPv4 only): SearXNG healthy, search: $OWN"
  # 0.7.4: "必应直连" -- the page switches the search service to Bing; the web test runs through it (the answer of
  #        Bing to a cloud machine is reported, not required)
  [ "$(post_sx bing)" = "200" ] || fail "switch to Bing"
  T="$(curl -sk -b "$JAR" -H "Origin: $SCHEME://127.0.0.1:$PORT" -H 'Content-Type: application/json' -X POST --data '{}' \
        "$SCHEME://127.0.0.1:$PORT/api/control/tools/web/test")"
  BING="$(echo "$T" | json 'd["data"]["provider"] + " " + str(d["data"]["search"]["ok"]) + " " + str(d["data"]["search"]["count"]) + " " + str(d["data"]["search"]["error"])')" \
    || { echo "$T"; fail "web test with Bing"; }
  case "$BING" in bing\ *) ;; *) fail "search service is not Bing: $BING" ;; esac
  pass "Bing as the search service from the page; web test through it: $BING"
  [ "$(post_sx uninstall)" = "200" ] || fail "web search uninstall request"
  wait_task "SearXNG uninstall"
  [ "$(post_sx disable)" = "200" ] || fail "web search disable"
  [ ! -e /etc/systemd/system/violet-searxng.service ] && [ ! -e "$ROOT/searxng" ] && [ ! -e "$ROOT/data/control/searxng.json" ] \
    || fail "SearXNG left behind after uninstall"
  pass "web search uninstalled from the Control Center (service, files and export gone)"
  "$ROOT/violet" searxng install --json >/dev/null || fail "violet searxng install (CLI)"
  systemctl is-active --quiet violet-searxng || fail "violet-searxng not active after the CLI install"

  # 4f. 2026-10-29: the channel "QQ" -- the page's "一键安装 QQ": the root task service installs SnowLuma (its Docker
  #     image with Linux QQ; GitHub's runners already have Docker), container violet-snowluma as violet-qq, ports on
  #     127.0.0.1 only; the page then shows QQ's window through VNC (the QR code is scanned in it). No QQ account can
  #     scan here: the size of the window seen from the page is reported.
  QQAPI="$SCHEME://127.0.0.1:$PORT/api/control/qq"
  CODE="$(code -b "$JAR" -H "Origin: $SCHEME://127.0.0.1:$PORT" -H 'Content-Type: application/json' --data '{}' "$QQAPI/install")"
  [ "$CODE" = "200" ] || fail "QQ install request ($CODE)"
  wait_task "QQ (SnowLuma) install"; MAXP="$WT_MAX"
  [ "$(docker inspect -f '{{.State.Running}}' violet-snowluma 2>/dev/null)" = "true" ] \
    || { docker logs --tail 80 violet-snowluma 2>&1 || true; fail "container violet-snowluma not running"; }
  id violet-qq >/dev/null 2>&1 || fail "account violet-qq missing"
  QQEXP="$ROOT/data/control/snowluma.json"
  [ -f "$QQEXP" ] || fail "snowluma export missing"
  WSPORT="$(python3 -c "import json;print(json.load(open('$QQEXP'))['ws_url'].rsplit(':',1)[1])")"
  VNCPORT="$(python3 -c "import json;print(json.load(open('$QQEXP'))['vnc_port'])")"
  for p in "$WSPORT" "$VNCPORT"; do
    ! ss -ltnH "( sport = :$p )" | grep -v '127.0.0.1' | grep -q . || fail "QQ listens beyond loopback on $p"
  done
  SHOT=""
  for _ in $(seq 1 48); do                 # QQ needs a while to open its login window
    SHOT="$(curl -sk -b "$JAR" -H 'Accept: application/json' "$QQAPI/screen")"
    echo "$SHOT" | json 'd["ok"] and d["data"]["width"] < d["data"]["screen_width"]' 2>/dev/null | grep -q True && break
    sleep 5
  done
  WIN="$(echo "$SHOT" | json '"screen %sx%s, window %sx%s at %s,%s, png %d bytes" % (d["data"]["screen_width"], d["data"]["screen_height"], d["data"]["width"], d["data"]["height"], d["data"]["x"], d["data"]["y"], len(d["data"]["image"]) * 3 // 4)')" \
    || { echo "$SHOT"; docker logs --tail 60 violet-snowluma 2>&1 || true; fail "QQ window from the page"; }
  echo "$SHOT" | json 'd["data"]["width"] < d["data"]["screen_width"]' | grep -q True \
    || { docker logs --tail 60 violet-snowluma 2>&1 || true; fail "no QQ window on the screen after 4 minutes ($WIN)"; }
  LOGIN="$(curl -sk -b "$JAR" -H 'Accept: application/json' "$QQAPI" | json 'str(d["data"].get("login"))')"
  pass "QQ (SnowLuma) installed from the Control Center (progress $MAXP%), container violet-snowluma, local only; QQ window from the page: $WIN; login $LOGIN"
fi

# 5. repair keeps port and password; backup; status
OUT2="$(curl -fsSL "http://127.0.0.1:$PORT_HTTP/install.sh" | bash -s -- --root "$ROOT" --json)" || fail "repair"
[ "$(echo "$OUT2" | json 'd["port"]')" = "$PORT" ] || fail "repair changed the port"
echo "$OUT2" | json 'd["admin"]["created"]' | grep -q False || fail "repair replaced the password"
"$ROOT/violet" backup --json | json 'd["databases_checked"]' | grep -qv '^0$' || fail "backup"
"$ROOT/violet" status --json | json 'd["service_state"]' | grep -q active || fail "status"
pass "repair kept port and password; backup verified; status active"

# 6. a broken upgrade rolls back by itself
BROKEN="$WORK/broken"
cp -r "$PROJECT" "$BROKEN"
echo 'raise SystemExit("broken build")' > "$BROKEN/main.py"
echo "9.9.9-broken" > "$BROKEN/VERSION"
if bash "$BROKEN/install.sh" upgrade --root "$ROOT" --health-timeout 20 --json >/dev/null; then fail "broken upgrade reported success"; fi
for _ in $(seq 1 30); do curl -fsSk "$SCHEME://127.0.0.1:$PORT/healthz" 2>/dev/null | grep -q violet && break; sleep 1; done
curl -fsSk "$SCHEME://127.0.0.1:$PORT/healthz" | grep -q violet || fail "old version not back after the failed upgrade"
ls "$ROOT"/backups/*before-upgrade* >/dev/null 2>&1 || fail "a failed upgrade must keep its backup"
pass "broken upgrade rolled back automatically (its backup kept)"

# 7. uninstall keeps data, purge removes everything
"$ROOT/violet" uninstall --json >/dev/null || fail "uninstall"
[ -f "$ROOT/data/violet.db" ] || fail "uninstall removed the data"
! curl -fsSk "$SCHEME://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 || fail "still running after uninstall"
[ "$MODE" != "systemd" ] || [ ! -e /etc/systemd/system/violet.service ] || fail "unit file left behind"
pass "uninstall kept data"
# the one-line complete removal from the README
curl -fsSL "http://127.0.0.1:$PORT_HTTP/install.sh" | bash -s -- uninstall --root "$ROOT" --purge --yes --json \
  || fail "uninstall --purge"
[ ! -e "$ROOT" ] || { ls -la "$ROOT"; fail "$ROOT left behind"; }
if [ "$STRICT" = "1" ]; then
  ! iptables -S | grep -q -- "--comment violet" || { iptables -S | grep violet; fail "firewall rules left behind"; }
fi
if [ "$MODE" = "systemd" ]; then
  ! id violet >/dev/null 2>&1 || fail "system account violet left behind"
  for unit in violet.service violet-update.service violet-update.timer violet-update.path violet-task.service \
              violet-task.path violet-mcp-relay.service violet-searxng.service violet-napcat.service; do
    [ ! -e "/etc/systemd/system/$unit" ] || fail "$unit left behind"
  done
  ! id violet-qq >/dev/null 2>&1 || fail "system account violet-qq left behind"
  ! docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx violet-snowluma || fail "SnowLuma container left after purge"
  [ ! -e /var/lib/private/violet-mcp-relay ] && [ ! -e /var/lib/violet-mcp-relay ] || fail "relay data left behind"
  [ ! -e /var/cache/private/violet-searxng ] && [ ! -e /var/cache/violet-searxng ] || fail "SearXNG cache left behind"
  ! systemctl is-active --quiet violet-searxng || fail "SearXNG still running after purge"
fi
pass "uninstall --purge removed everything (folder, services, update timer, phone relay, system account)"
echo "ALL PASSED"
