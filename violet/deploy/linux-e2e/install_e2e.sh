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
[ "$CODE" = "403" ] || fail "setup reachable before the password change ($CODE)"
pass "security entrance (404 without it); username + password login; everything locked until the password is changed"

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
  pass "automatic update from the Control Center: $OLD_VERSION -> $NEXT (root update service, healthy)"
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
  for unit in violet.service violet-update.service violet-update.timer violet-update.path; do
    [ ! -e "/etc/systemd/system/$unit" ] || fail "$unit left behind"
  done
fi
pass "uninstall --purge removed everything (folder, services, update timer, system account)"
echo "ALL PASSED"
