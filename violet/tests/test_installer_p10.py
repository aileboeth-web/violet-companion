"""Phase 10.4: the installer -- port choice, network layers, config merge, systemd unit, backup / restore, and a real
install -> status -> backup -> upgrade (failing and passing) -> uninstall cycle on THIS machine with the built-in
supervisor (no systemd here: the systemd parts are checked as rendered text only -- marked in testing.md).

The real cycle copies the program into a temp root, reuses the project's venv (no download), starts Violet on a free
loopback port without any model key, and talks to it over HTTP.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import pytest

from tools import violet_installer as inst

PROJECT = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------------------------------------------ ports

def test_a_requested_port_is_used_only_when_it_is_really_free():
    busy = {7100}
    free = lambda p, h: p not in busy                        # noqa: E731
    assert inst.choose_port(7300, None, is_free=free, is_eve=lambda p, h: False) == (7300, "按 --port 指定")
    with pytest.raises(inst.InstallError, match="已被其他程序占用"):
        inst.choose_port(7100, None, is_free=free, is_eve=lambda p, h: False)
    # the same port as the running Violet of this installation is fine (a repair)
    assert inst.choose_port(7100, 7100, is_free=free, is_eve=lambda p, h: True)[0] == 7100
    with pytest.raises(inst.InstallError):
        inst.choose_port(80, None, is_free=free)


def test_automatic_port_keeps_the_previous_one_then_takes_the_next_free():
    assert inst.choose_port(None, 7150, is_free=lambda p, h: True, is_eve=lambda p, h: False) == (7150, "沿用上次安装的端口")
    busy = {7100, 7101}
    port, reason = inst.choose_port(None, None, is_free=lambda p, h: p not in busy, is_eve=lambda p, h: False)
    assert port == 7102 and "被占用" in reason
    # previous port now taken by another program: pick another one, never the foreign one
    port, _ = inst.choose_port(None, 7100, is_free=lambda p, h: p not in busy, is_eve=lambda p, h: False)
    assert port == 7102


def test_port_free_sees_a_real_listener():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        assert inst.port_free(port) is False
    assert inst.port_free(port) is True


# ------------------------------------------------------------------------------------------------ network layers

def _layers(**kw):
    defaults = dict(is_free=lambda p, h: False, connect=lambda h, p: True, health=lambda h, p: True,
                    lan=lambda: ["192.168.1.20"], firewall=lambda: {"tool": None, "active": False})
    defaults.update(kw)
    return {l["layer"]: l for l in inst.network_report(7100, kw.pop("bind", "0.0.0.0"), **defaults)}


def test_local_success_is_never_reported_as_public_access():
    layers = _layers()
    assert layers["listening"]["state"] == "ok" and layers["local"]["state"] == "ok" and layers["lan"]["state"] == "ok"
    assert layers["public"]["state"] == "not_checked" and "安全组" in layers["public"]["detail"]
    loopback = {l["layer"]: l for l in inst.network_report(7100, "127.0.0.1", is_free=lambda p, h: False,
                                                            health=lambda h, p: True, lan=lambda: ["10.0.0.5"])}
    assert loopback["lan"]["state"] == "not_applicable" and loopback["public"]["state"] == "not_applicable"
    assert "SSH" in loopback["lan"]["detail"]


def test_each_layer_fails_on_its_own():
    down = _layers(is_free=lambda p, h: True, health=lambda h, p: False)
    assert down["listening"]["state"] == "fail" and down["local"]["state"] == "fail"
    walled = _layers(connect=lambda h, p: False, firewall=lambda: {"tool": "ufw", "active": True})
    assert walled["lan"]["state"] == "fail" and "ufw" in walled["lan"]["detail"]
    public = _layers(public_url="https://violet.example", fetch=lambda url: False)
    assert public["public"]["state"] == "fail" and "安全组" in public["public"]["detail"]
    assert _layers(public_url="https://violet.example", fetch=lambda url: True)["public"]["state"] == "ok"


def test_firewall_commands_are_only_suggested():
    assert inst.firewall_open_command("ufw", 7100) == ["ufw", "allow", "7100/tcp"]
    assert inst.firewall_open_command("firewalld", 7100)[-1] == "7100/tcp"
    assert inst.firewall_open_command(None, 7100) is None


# ------------------------------------------------------------------------------------------------ config

def test_config_merge_only_adds_missing_keys_and_never_changes_user_edits():
    existing = "# mine\nVIOLET_DASHBOARD_PORT=7222\nLLM_API_KEY='keep-me'\n"
    text, added = inst.merge_env(existing, {"VIOLET_DASHBOARD_PORT": "7100", "VIOLET_DATA_DIR": "/opt/x/data"})
    assert added == ["VIOLET_DATA_DIR"] and "VIOLET_DASHBOARD_PORT=7222" in text and "LLM_API_KEY='keep-me'" in text
    assert text.startswith("# mine")
    again, added_again = inst.merge_env(text, {"VIOLET_DATA_DIR": "/elsewhere"})
    assert again == text and added_again == []
    assert inst.parse_env("A='x y'\n#B=1\nC=2") == {"A": "x y", "C": "2"}


def test_systemd_unit_restarts_on_failure_and_uses_the_install_config(tmp_path):
    layout = inst.Layout(tmp_path / "violet")
    unit = inst.systemd_unit(layout, "violet")
    assert "Restart=on-failure" in unit and f"VIOLET_ENV_FILE={layout.env_file}" in unit and "User=violet" in unit
    assert "WantedBy=multi-user.target" in unit and "ReadWritePaths=" in unit
    user_unit = inst.systemd_unit(layout, "me", user_mode=True)
    assert "User=" not in user_unit and "WantedBy=default.target" in user_unit
    assert inst.Service.pick({"os": "Linux", "systemd": True, "root": True}) == "systemd"
    assert inst.Service.pick({"os": "Linux", "systemd": True, "root": False}) == "systemd-user"
    assert inst.Service.pick({"os": "Linux", "systemd": False, "root": True}) == "supervisor"
    assert inst.Service.pick({"os": "Windows", "systemd": False}) == "supervisor"


def test_preflight_reports_problems_in_plain_words(tmp_path):
    layout = inst.Layout(tmp_path / "violet")
    info = {"os": "Linux", "arch": "riscv64", "python": "3.8.10", "root": False, "distro": "Test", "systemd": False}
    checks = {c.id: c for c in inst.preflight(layout, sys.executable, info, PROJECT)}
    assert checks["python"].ok is False and "3.11" in checks["python"].message
    # Violet stage 1: 3.10 is refused too (ai/mcp.py uses the 3.11 builtin BaseExceptionGroup)
    old = dict(info, python="3.10.12")
    assert {c.id: c for c in inst.preflight(layout, sys.executable, old, PROJECT)}["python"].ok is False
    assert {c.id: c for c in inst.preflight(layout, sys.executable, dict(info, python="3.11.4"), PROJECT)}["python"].ok
    assert checks["arch"].level == "warn" and checks["service_manager"].level == "warn"
    assert checks["source"].ok and checks["writable"].ok
    bad_source = {c.id: c for c in inst.preflight(layout, sys.executable, info, tmp_path)}
    assert bad_source["source"].ok is False


def test_the_program_copy_leaves_out_user_data_and_secrets(tmp_path):
    source = tmp_path / "src"
    (source / "ai").mkdir(parents=True)
    for name in ("main.py", "requirements.txt", "VERSION"):
        (source / name).write_text("x", encoding="utf-8")
    (source / ".env").write_text("LLM_API_KEY=secret", encoding="utf-8")
    (source / "violet.db").write_bytes(b"SQLite format 3\x00")
    (source / "data").mkdir()
    (source / "data" / "credentials.json").write_text("{}", encoding="utf-8")
    (source / "ai" / "x.py").write_text("y", encoding="utf-8")
    layout = inst.Layout(tmp_path / "root")
    layout.root.mkdir()
    assert inst.install_program(layout, source) == "copied"
    names = {p.relative_to(layout.app).as_posix() for p in layout.app.rglob("*")}
    assert "ai/x.py" in names and ".env" not in names and "violet.db" not in names and "data" not in names
    (layout.app / "marker").write_text("old", encoding="utf-8")
    inst.install_program(layout, source)                       # a reinstall keeps the old copy for rollback
    assert (layout.app_previous / "marker").exists() and not (layout.app / "marker").exists()


# ------------------------------------------------------------------------------------------------ backup / restore

def _fake_install(tmp_path) -> inst.Layout:
    layout = inst.Layout(tmp_path / "violet")
    for p in (layout.config, layout.data / "control", layout.backups):
        p.mkdir(parents=True)
    layout.env_file.write_text("VIOLET_DASHBOARD_PORT=7100\n", encoding="utf-8")
    (layout.data / "control" / "settings.json").write_text('{"values": {"a": 1}}', encoding="utf-8")
    conn = sqlite3.connect(layout.db)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, content TEXT)")
    conn.execute("INSERT INTO messages (content) VALUES ('第一句')")
    conn.commit()
    layout._conn = conn                                       # kept open: a backup while Violet is running
    return layout


def test_backup_copies_a_live_database_consistently_and_restore_puts_it_back(tmp_path):
    layout = _fake_install(tmp_path)
    archive = inst.backup(layout, "manual")
    assert inst.verify_backup(archive)["databases_checked"] == 1
    layout._conn.execute("INSERT INTO messages (content) VALUES ('备份之后')")
    layout._conn.commit()
    layout._conn.close()
    (layout.data / "control" / "settings.json").write_text('{"values": {"a": 2}}', encoding="utf-8")
    safety = inst.restore(layout, archive, service=None)
    rows = [r[0] for r in sqlite3.connect(layout.db).execute("SELECT content FROM messages")]
    assert rows == ["第一句"]
    assert json.loads((layout.data / "control" / "settings.json").read_text(encoding="utf-8"))["values"]["a"] == 1
    assert "before-restore" in safety.name and inst.verify_backup(safety)["databases_checked"] == 1


def test_a_tampered_backup_is_refused(tmp_path):
    layout = _fake_install(tmp_path)
    layout._conn.close()
    evil = tmp_path / "evil.tar.gz"
    with tarfile.open(evil, "w:gz") as tar:
        info = tarfile.TarInfo("backup.json")
        data = b"{}"
        info.size = len(data)
        import io
        tar.addfile(info, io.BytesIO(data))
        info2 = tarfile.TarInfo("../outside.txt")
        info2.size = 1
        tar.addfile(info2, io.BytesIO(b"x"))
    with pytest.raises(inst.InstallError, match="不安全"):
        inst.restore(layout, evil, service=None)
    assert not (tmp_path / "outside.txt").exists()


# ------------------------------------------------------------------------------------------------ real cycle

def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _run(args, root, timeout=300):
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "NO_PROXY": "127.0.0.1,localhost"}
    for key in ("VIOLET_DATA_DIR", "VIOLET_DB_PATH", "VIOLET_ENV_FILE", "VIOLET_ADMIN_TOKEN", "LLM_API_KEY", "LLM_API_URL"):
        env.pop(key, None)
    done = subprocess.run([sys.executable, "-B", str(PROJECT / "tools" / "violet_installer.py"), *args, "--root", str(root),
                           "--json"], capture_output=True, text=True, encoding="utf-8", timeout=timeout, env=env)
    lines = [l for l in (done.stdout or "").splitlines() if l.strip().startswith("{")]
    assert lines, done.stdout + done.stderr
    return done.returncode, json.loads(lines[-1])


def _http(port, path, data=None, cookie=None):
    import urllib.request

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None                                   # the 302 of a login is the answer we want to see
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data,
                                 headers={"Accept": "application/json", "Origin": f"http://127.0.0.1:{port}",
                                          **({"Cookie": cookie} if cookie else {})})
    try:
        with opener.open(req, timeout=10) as r:
            return r.status, r.headers, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read().decode("utf-8")


@pytest.mark.skipif(os.environ.get("VIOLET_SKIP_INSTALLER_E2E") == "1", reason="opt-out")
def test_real_install_status_upgrade_rollback_and_uninstall_on_this_machine(tmp_path):
    import urllib.parse
    root = tmp_path / "violet-root"
    port = _free_port()
    code, result = _run(["install", "--port", str(port), "--service", "supervisor", "--reuse-venv", str(Path(sys.prefix)),
                         "--health-timeout", "120"], root)
    try:
        assert code == 0, result
        entrance, user = result["admin"]["entrance"], result["admin"]["user"]
        assert len(entrance) == 12 and user.startswith("violet_")              # Violet stage 2: 1Panel-style access
        assert result["core_ok"] and result["port"] == port and result["urls"][0] == f"http://127.0.0.1:{port}/{entrance}"
        password = result["admin"]["password"]
        assert len(password) == 20 and result["admin"]["must_change"]
        layers = {l["layer"]: l["state"] for l in result["network"]}
        assert layers == {"listening": "ok", "local": "ok", "lan": "not_applicable", "public": "not_applicable"}
        assert result["public_ready"] is False
        # no secrets in the files the installer wrote, the password only as a hash
        admin_json = (root / "data" / "control" / "admin.json").read_text(encoding="utf-8")
        assert password not in admin_json and "scrypt" in admin_json
        assert password not in (root / "install.json").read_text(encoding="utf-8")
        # without the security entrance everything is a plain 404 -- the login page too
        login = urllib.parse.urlencode({"username": user, "password": password}).encode()
        assert _http(port, "/auth/login")[0] == 404 and _http(port, "/control")[0] == 404
        assert _http(port, "/auth/login", login)[0] == 404
        status, headers, _ = _http(port, f"/{entrance}")
        assert status == 302 and headers["Location"].endswith("/auth/login?next=/control")
        marked = headers["Set-Cookie"].split(";")[0]
        # the password needs the username
        bad = urllib.parse.urlencode({"username": "admin", "password": password}).encode()
        assert _http(port, "/auth/login", bad, cookie=marked)[0] == 401
        # the dashboard: login with the initial password, then everything but the password change is refused
        status, headers, _ = _http(port, "/auth/login", login, cookie=marked)
        assert status in (302, 303)
        cookie = headers["Set-Cookie"].split(";")[0]
        status, _, body = _http(port, "/api/control/setup", cookie=cookie)
        assert status == 403 and "password_change_required" in body
        # violet info / reset-entrance / reset-password / set-port
        code_i, info = _run(["info"], root)
        assert code_i == 0 and info["user"] == user and info["entrance"] == entrance and "password" not in info
        code_e, moved = _run(["reset-entrance"], root)
        assert code_e == 0 and moved["entrance"] != entrance and moved["urls"][0].endswith("/" + moved["entrance"])
        assert _http(port, "/auth/status", cookie=cookie)[0] == 404              # the old entrance mark is gone
        code_p, reset = _run(["reset-password"], root)
        assert code_p == 0 and len(reset["password"]) == 20 and reset["password"] != password
        new_port = _free_port()
        code_s, sp = _run(["set-port", str(new_port), "--health-timeout", "120"], root)
        assert code_s == 0 and sp["port"] == new_port and inst.probe_eve(new_port) is not None, sp
        port = new_port
        # repair: run install again -> same port, same password, data kept
        code2, again = _run(["install", "--service", "supervisor", "--reuse-venv", str(Path(sys.prefix))], root)
        assert code2 == 0 and again["port"] == port and again["admin"]["created"] is False and not again["fresh"]
        # the repair came up healthy: its own safety backup was deleted (no disk space used without reason)
        backups = lambda kind: [p for p in (root / "backups").iterdir() if p.name.endswith(kind + ".tar.gz")]  # noqa
        assert backups("before-install") == []
        # backup (made by hand: never deleted automatically)
        code3, bk = _run(["backup"], root)
        assert code3 == 0 and bk["databases_checked"] >= 1
        manual = backups("manual")
        assert len(manual) == 1
        # a broken upgrade is rolled back automatically
        broken = tmp_path / "broken-src"
        import shutil
        shutil.copytree(PROJECT, broken, ignore=shutil.ignore_patterns(*inst.COPY_IGNORE))
        (broken / "main.py").write_text("raise SystemExit('broken build')\n", encoding="utf-8")
        (broken / "VERSION").write_text("9.9.9-broken\n", encoding="utf-8")
        code4, failed = _run_upgrade(broken, root)
        assert code4 != 0 and "已恢复" in failed["error"]
        assert inst.probe_eve(port) is not None                    # the old version answers again
        assert (root / "app" / "VERSION").read_text(encoding="utf-8").strip() == \
            (PROJECT / "VERSION").read_text(encoding="utf-8").strip()           # the version installed before
        kept = backups("before-upgrade")
        assert len(kept) == 1 and str(kept[0]) in failed["error"]          # a failed upgrade keeps its backup
        # a good upgrade: its own backup is deleted; the failed one's and the hand-made one stay
        code_u, good = _run_upgrade(PROJECT, root)
        assert code_u == 0 and good["backup_removed"] is True and good["backup"] is None, good
        assert backups("before-upgrade") == kept and backups("manual") == manual
        assert inst.load_state(inst.Layout(root))["last_backup"] is None
        # status
        code5, st = _run(["status"], root)
        assert code5 == 0 and st["installed"] and st["service_state"] == "active"
        # uninstall keeps data
        code6, un = _run(["uninstall"], root)
        assert code6 == 0 and set(un["kept"]) >= {"config", "data", "backups"}
        assert not (root / "app").exists() and (root / "data" / "violet.db").exists()
        time.sleep(1)
        assert inst.probe_eve(port) is None
    finally:
        layout = inst.Layout(root)
        if (root / "app").exists():
            inst.supervisor_stop(layout)


def _run_upgrade(source: Path, root: Path):
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    for key in ("VIOLET_DATA_DIR", "VIOLET_DB_PATH", "VIOLET_ENV_FILE", "VIOLET_ADMIN_TOKEN"):
        env.pop(key, None)
    done = subprocess.run([sys.executable, "-B", str(source / "tools" / "violet_installer.py"), "upgrade", "--root", str(root),
                           "--reuse-venv", str(Path(sys.prefix)), "--health-timeout", "20", "--json"],
                          capture_output=True, text=True, encoding="utf-8", timeout=300, env=env)
    lines = [l for l in (done.stdout or "").splitlines() if l.strip().startswith("{")]
    assert lines, done.stdout + done.stderr
    return done.returncode, json.loads(lines[-1])


# ------------------------------------------------------------------------------------------------ one-command install

def test_a_release_contains_the_program_without_secrets_and_a_matching_checksum(tmp_path):
    import hashlib
    from tools import make_release
    meta = make_release.build("https://example.org/violet/latest", tmp_path / "dist", worktree=True)
    dist = tmp_path / "dist"
    script = (dist / "install.sh").read_text(encoding="utf-8")
    assert "__VIOLET_RELEASE_BASE__" not in script and "https://example.org/violet/latest/violet.tar.gz" not in script
    assert 'release_base="${VIOLET_RELEASE_BASE:-https://example.org/violet/latest}"' in script and "\r" not in script
    assert meta["install_command"] == "curl -fsSL https://example.org/violet/latest/install.sh | bash"
    digest = hashlib.sha256((dist / "violet.tar.gz").read_bytes()).hexdigest()
    assert (dist / "violet.tar.gz.sha256").read_text().split()[0] == digest
    with tarfile.open(dist / "violet.tar.gz") as tar:
        names = tar.getnames()
        modes = {m.name: m.mode for m in tar.getmembers()}
    assert "violet/tools/violet_installer.py" in names and "violet/main.py" in names
    assert not any("/tests/" in n or n.endswith((".env", ".db")) or "/data/" in n for n in names)
    assert modes["violet/install.sh"] == 0o755
    with pytest.raises(SystemExit):
        make_release.build("http://example.org", tmp_path / "x", worktree=True)       # plain http to the world


def _git_bash() -> str | None:
    import shutil as _sh
    for candidate in (r"D:\Development\PortableGit\bin\bash.exe", r"C:\Program Files\Git\bin\bash.exe"):
        if os.path.isfile(candidate):
            return candidate
    found = _sh.which("bash")
    return found if found and os.name != "nt" else None


@pytest.mark.skipif(_git_bash() is None or os.environ.get("VIOLET_SKIP_INSTALLER_E2E") == "1", reason="no bash here")
def test_the_one_line_remote_install_downloads_verifies_and_installs(tmp_path):
    """curl -fsSL <base>/install.sh | bash -s -- ...  against a local web server: nothing but the one command."""
    import functools
    import threading
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
    from tools import make_release
    dist = tmp_path / "dist"
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(dist))
    handler.log_message = lambda *a, **k: None
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    root = tmp_path / "violet-root"
    port = _free_port()
    try:
        make_release.build(base, dist, worktree=True)
        env = {**os.environ, "PYTHON": Path(sys.executable).as_posix(), "NO_PROXY": "127.0.0.1,localhost",
               "PYTHONIOENCODING": "utf-8"}
        env.pop("MSYS_NO_PATHCONV", None)        # Git bash: any value turns the /tmp -> C:\... conversion off
        for key in ("VIOLET_DATA_DIR", "VIOLET_DB_PATH", "VIOLET_ENV_FILE", "VIOLET_ADMIN_TOKEN", "LLM_API_KEY", "LLM_API_URL"):
            env.pop(key, None)
        # a normal user installing into a folder of their own: --user-install (no root, no sudo)
        command = (f"curl -fsSL {base}/install.sh | bash -s -- --user-install --root '{root.as_posix()}' --port {port} "
                   f"--service supervisor --reuse-venv '{Path(sys.prefix).as_posix()}' --json")
        done = subprocess.run([_git_bash(), "-c", command], capture_output=True, text=True, encoding="utf-8",
                              timeout=300, env=env)
        lines = [l for l in done.stdout.splitlines() if l.strip().startswith("{")]
        assert done.returncode == 0 and lines, done.stdout[-2000:] + done.stderr[-2000:]
        result = json.loads(lines[-1])
        assert result["core_ok"] and result["port"] == port and result["admin"]["password"]
        assert inst.probe_eve(port) is not None
        # a tampered archive is refused before anything is installed
        (dist / "violet.tar.gz.sha256").write_text("0" * 64 + "  violet.tar.gz\n")
        bad = subprocess.run([_git_bash(), "-c",
                              f"curl -fsSL {base}/install.sh | bash -s -- --user-install --root '{(tmp_path / 'r2').as_posix()}'"],
                             capture_output=True, text=True, encoding="utf-8", timeout=120, env=env)
        assert bad.returncode != 0 and "校验失败" in bad.stderr and not (tmp_path / "r2").exists()
    finally:
        server.shutdown()
        if (root / "app").exists():
            inst.supervisor_stop(inst.Layout(root))


def test_systemd_runs_eve_as_its_own_user_owning_only_what_eve_writes(tmp_path):
    layout = inst.Layout(tmp_path / "violet")
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 2 if cmd[0] == "getent" else 0, "", "")
    inst.ensure_service_user(layout, "violet", run=run, linux_root=True)
    assert calls[0] == ["getent", "passwd", "violet"] and calls[1][0] == "useradd" and "--system" in calls[1]
    chowned = [c[-1] for c in calls if c[0] == "chown"]
    assert sorted(Path(p).name for p in chowned) == ["backups", "config", "data", "logs"]
    assert not any(str(layout.app) in c or str(layout.venv) in c for c in chowned)
    unit = inst.systemd_unit(layout, "violet")
    assert "User=violet" in unit and "NoNewPrivileges=true" in unit
    calls.clear()
    inst.ensure_service_user(layout, "violet", run=run, linux_root=False)       # not root: nothing
    assert calls == []