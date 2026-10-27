"""Violet stage 3: every everyday setting in the Control Center, restart from the page, update modes and the root
update service (automatic updates). Temp data dirs, fake releases and fake systemctl -- no network, no service."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from tools import violet_installer as inst

TOKEN = os.environ["VIOLET_ADMIN_TOKEN"]
SAME = {"Origin": "http://localhost"}
PROJECT = Path(__file__).resolve().parent.parent
NEWER = {"tag": "v9.1.0", "version": "9.1.0", "url": "https://github.com/x/y/releases/tag/v9.1.0",
         "published_at": "2026-10-27T00:00:00Z", "notes": "", "install_url": "https://github.com/x/y/install.sh"}


@pytest.fixture()
def env_guard(tmp_path, monkeypatch):
    import ai.control_config as cc
    saved = dict(os.environ)
    monkeypatch.setenv("VIOLET_DATA_DIR", str(tmp_path / "data"))
    cc._ORIGINALS.clear()
    cc._DEFAULT_STORE = None
    yield tmp_path / "data"
    os.environ.clear()
    os.environ.update(saved)
    cc._ORIGINALS.clear()
    cc._DEFAULT_STORE = None


@pytest.fixture()
def app(env_guard, violetdb):
    from wx_ilink_py import ILinkClient, create_app
    made = create_app(client=ILinkClient(data_dir=env_guard, auto_login=False), violetdb=violetdb,
                      proactive_scheduler=None, all_agents=[], admin_token=TOKEN)
    made.testing = True
    return made


def _admin(app):
    c = app.test_client()
    assert c.post("/auth/login", data={"token": TOKEN}, headers=SAME).status_code == 302
    return c


# ------------------------------------------------------------------------------------------------ settings

def test_every_setting_is_read_by_the_program():
    """A setting in the Control Center that no code reads would be a switch that does nothing."""
    from ai.control_config import SETTINGS
    sources = "\n".join(p.read_text(encoding="utf-8") for p in PROJECT.rglob("*.py")
                        if "tests" not in p.parts and p.name != "control_config.py" and ".venv" not in p.parts)
    missing = [s.key for s in SETTINGS if s.target == "env" and not re.search(r"_(LLM_MODEL|TEMPERATURE)$", s.key)
               and not s.key.endswith("FALLBACK_MODEL") and f'"{s.key}"' not in sources]
    assert missing == []


def test_every_default_and_choice_is_valid():
    from ai.control_config import SETTINGS, validate
    for s in SETTINGS:
        if s.kind == "choice":
            assert s.default in {v for v, _l in s.choices}, s.key
            for value, _label in s.choices:
                assert validate(s.key, value) == value
        if s.default not in (None, "") and s.kind not in ("secret", "prompt_version"):
            validate(s.key, s.default)


@pytest.mark.parametrize("key, value, ok", [
    ("VIOLET_UPDATE_MODE", "auto", True), ("VIOLET_UPDATE_MODE", "sometimes", False),
    ("VIOLET_WEB_SEARCH_PROVIDER", "mock", False),                    # the offline test provider is not offered
    ("VIOLET_TIMEZONE", "Europe/London", True), ("VIOLET_TIMEZONE", "Mars/Olympus", False),
    ("VIOLET_TIMEZONE", "../../etc/passwd", False),
    ("VIOLET_FALLBACK_REPLY", "稍等一下哦", True), ("VIOLET_FALLBACK_REPLY", "两行\n文字", False),
    ("VIOLET_FALLBACK_REPLY", "长" * 201, False),
    ("VIOLET_LOG_LEVEL", "DEBUG", True), ("VIOLET_LOG_LEVEL", "TRACE", False),
    ("MCP_CALL_RETRIES", 9, False), ("VIOLET_COMMITMENT_DEFAULT_TIME", "25:00", False),
])
def test_validation_of_the_new_kinds(key, value, ok):
    from ai.control_config import validate
    if ok:
        assert validate(key, value) == value
    else:
        with pytest.raises(ValueError):
            validate(key, value)


def test_an_empty_text_is_a_real_value_that_switches_the_message_off(env_guard, monkeypatch):
    from ai.control_config import ControlStore, validate
    assert validate("VIOLET_STARTUP_MESSAGE", "") == "" and validate("VIOLET_STARTUP_MESSAGE", None) is None
    store = ControlStore(env_guard)
    monkeypatch.setenv("VIOLET_FALLBACK_REPLY", "env text")
    store.save({"VIOLET_FALLBACK_REPLY": ""}, "test")
    environ = {"VIOLET_FALLBACK_REPLY": "env text"}
    store.apply_to_environ(environ)
    assert environ["VIOLET_FALLBACK_REPLY"] == ""
    monkeypatch.setenv("VIOLET_FALLBACK_REPLY", "")
    row = next(r for r in store.describe() if r["key"] == "VIOLET_FALLBACK_REPLY")
    assert row["value"] == "" and row["source"] == "dashboard"


def test_saving_a_choice_through_the_api_applies_it(app, env_guard):
    admin = _admin(app)
    resp = admin.put("/api/control/settings", json={"changes": {"VIOLET_UPDATE_MODE": "notify",
                                                                "VIOLET_TIMEZONE": "Europe/London"}}, headers=SAME)
    body = resp.get_json()["data"]
    assert resp.status_code == 200 and body["applied_now"] == ["VIOLET_UPDATE_MODE"]
    assert body["needs_restart"] == ["VIOLET_TIMEZONE"]
    assert os.environ["VIOLET_UPDATE_MODE"] == "notify"
    rows = {r["key"]: r for r in admin.get("/api/control/settings").get_json()["data"]["settings"]}
    assert rows["VIOLET_UPDATE_MODE"]["choices"][0] == {"value": "auto", "label": "自动更新"}
    bad = admin.put("/api/control/settings", json={"changes": {"VIOLET_UPDATE_MODE": "x"}}, headers=SAME)
    assert bad.status_code == 400 and "VIOLET_UPDATE_MODE" in bad.get_json()["problems"]


# ------------------------------------------------------------------------------------------------ restart

def test_supervised_means_systemd_or_violets_own_supervisor():
    from wx_ilink_py.restart import supervised
    assert supervised({"INVOCATION_ID": "abc"}) and supervised({"VIOLET_SUPERVISED": "1"}) and not supervised({})


def test_restart_is_offered_only_under_a_service_manager(app):
    admin = _admin(app)
    info = admin.get("/api/control/restart").get_json()["data"]
    assert info["available"] is False and "命令行" in info["reason"]
    assert admin.post("/api/control/restart", json={"confirm": True}, headers=SAME).status_code == 409
    calls = []
    app.extensions["violet_restart"] = {"restart": lambda: calls.append("restart"), "supervised": True}
    assert admin.post("/api/control/restart", json={}, headers=SAME).status_code == 400
    import wx_ilink_py.restart as restart
    resp = admin.post("/api/control/restart", json={"confirm": True}, headers=SAME)
    assert resp.status_code == 200 and resp.get_json()["data"]["restarting"]
    import time
    for _ in range(40):
        if calls:
            break
        time.sleep(0.05)
    assert calls == ["restart"]
    assert restart.RESTART_EXIT_CODE != 0              # systemd's Restart=on-failure needs a non-zero exit


def test_main_exits_with_the_restart_code_and_the_supervisor_marks_its_child():
    main = (PROJECT / "main.py").read_text(encoding="utf-8")
    assert 'sys.exit(RESTART_EXIT_CODE if _restart["requested"] else 0)' in main
    assert '"VIOLET_SUPERVISED": "1"' in (PROJECT / "tools" / "violet_service.py").read_text(encoding="utf-8")
    assert "Restart=on-failure" in inst.systemd_unit(inst.Layout(Path("/opt/violet")), "violet")


# ------------------------------------------------------------------------------------------------ ai.updates

@pytest.fixture()
def updates(monkeypatch, env_guard):
    from ai import updates
    updates.reset_for_tests()
    monkeypatch.delenv("VIOLET_UPDATE_CHECK", raising=False)
    monkeypatch.delenv("VIOLET_UPDATE_MODE", raising=False)
    yield updates
    updates.reset_for_tests()


def _installed_app(tmp_path, agent=True, last=None):
    root = tmp_path / "violet" / "app"
    root.mkdir(parents=True)
    (root / "VERSION").write_text("0.3.0\n", encoding="utf-8")
    state = {"version": "0.3.0", **({"auto_update_agent": "systemd"} if agent else {}),
             **({"auto_update": last} if last else {})}
    (root.parent / "install.json").write_text(json.dumps(state), encoding="utf-8")
    return root


def test_the_update_mode_and_the_old_switch(updates, monkeypatch):
    assert updates.mode() == "auto"
    monkeypatch.setenv("VIOLET_UPDATE_CHECK", "0")
    assert updates.mode() == "manual"
    monkeypatch.setenv("VIOLET_UPDATE_MODE", "notify")
    assert updates.mode() == "notify"
    monkeypatch.setenv("VIOLET_UPDATE_MODE", "weird")
    assert updates.mode() == "manual"


def test_auto_mode_asks_the_update_service_when_it_sees_a_newer_release(updates, tmp_path, env_guard):
    root = _installed_app(tmp_path)
    result = updates.check(fetch=lambda: NEWER, root=root)
    request = env_guard / "control" / "update-request"
    assert result["newer"] and result["auto"]["available"] and result["auto"]["requested"]
    assert request.read_text(encoding="utf-8") == "auto"


def test_notify_mode_only_reports(updates, tmp_path, env_guard, monkeypatch):
    monkeypatch.setenv("VIOLET_UPDATE_MODE", "notify")
    root = _installed_app(tmp_path)
    result = updates.check(fetch=lambda: NEWER, root=root)
    assert result["newer"] and not (env_guard / "control" / "update-request").exists()


def test_manual_mode_fetches_only_on_the_button_and_keeps_showing_that_result(updates, tmp_path, monkeypatch):
    monkeypatch.setenv("VIOLET_UPDATE_MODE", "manual")
    root = _installed_app(tmp_path)
    calls = []
    fetch = lambda: calls.append(1) or NEWER  # noqa: E731
    assert updates.check(fetch=fetch, root=root)["latest"] is None and calls == []
    assert updates.check(fetch=fetch, refresh=True, root=root)["newer"] and calls == [1]
    later = updates.check(fetch=fetch, root=root, now=lambda: 10 ** 12)          # days later, no button pressed
    assert later["latest"] == "9.1.0" and calls == [1]


def test_without_the_root_update_service_the_page_shows_the_command(updates, tmp_path, env_guard):
    root = _installed_app(tmp_path, agent=False)
    result = updates.check(fetch=lambda: NEWER, root=root)
    assert result["auto"]["available"] is False and "不能自动更新" in result["auto"]["reason"]
    assert not (env_guard / "control" / "update-request").exists()
    with pytest.raises(PermissionError):
        updates.request_update("manual", root)


def test_the_last_automatic_run_is_shown(updates, tmp_path):
    root = _installed_app(tmp_path, last={"last_run_at": 1, "last_result": "failed", "last_message": "x",
                                          "failed_version": "9.1.0"})
    auto = updates.automatic(root)
    assert auto["last_result"] == "failed" and auto["failed_version"] == "9.1.0"


def test_update_now_through_the_api(app, updates, monkeypatch, tmp_path, env_guard):
    admin = _admin(app)
    assert admin.post("/api/control/update/apply", json={"confirm": True}, headers=SAME).status_code == 409
    root = _installed_app(tmp_path)
    monkeypatch.setattr(updates.request_update, "__defaults__", ("manual", root))      # as if installed at root
    assert admin.post("/api/control/update/apply", json={}, headers=SAME).status_code == 400
    resp = admin.post("/api/control/update/apply", json={"confirm": True, "url": "https://evil.example/x"},
                      headers=SAME)
    assert resp.status_code == 200
    assert (env_guard / "control" / "update-request").read_text(encoding="utf-8") == "manual"   # nothing else


# ------------------------------------------------------------------------------------------------ installer: units

def _layout(tmp_path, version="0.3.0", **state):
    layout = inst.Layout(tmp_path / "root")
    for folder in (layout.config, layout.data / "control", layout.app):
        folder.mkdir(parents=True, exist_ok=True)
    (layout.app / "VERSION").write_text(version + "\n", encoding="utf-8")
    inst.save_state(layout, {"version": version, "service": "systemd", "user": "violet", **state})
    return layout


def test_the_update_units_run_only_the_fixed_command(tmp_path):
    layout = inst.Layout(Path("/opt/violet"))
    units = {k: v.replace("\\", "/") for k, v in inst.updater_units(layout).items()}     # Linux paths on Windows
    service = units["violet-update.service"]
    assert "ExecStart=/opt/violet/violet auto-update --json" in service and "User=" not in service
    assert "Environment" not in service and "Type=oneshot" in service
    assert "PathExists=/opt/violet/data/control/update-request" in units["violet-update.path"]
    assert "OnUnitActiveSec=6h" in units["violet-update.timer"]


def test_register_updater_only_for_system_systemd(tmp_path, monkeypatch):
    calls = []
    run = lambda cmd, **kw: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", "")  # noqa: E731
    layout = _layout(tmp_path)
    monkeypatch.setattr(inst.Service, "unit_path", lambda self: tmp_path / "units" / "violet.service")
    (tmp_path / "units").mkdir()
    assert inst.Service(layout, "supervisor", run=run).register_updater() is False
    assert inst.Service(layout, "systemd-user", run=run).register_updater() is False
    assert inst.Service(layout, "systemd", run=run).register_updater() is True
    assert sorted(p.name for p in (tmp_path / "units").iterdir()) == \
        ["violet-update.path", "violet-update.service", "violet-update.timer"]
    assert ["systemctl", "enable", "--now", "violet-update.timer", "violet-update.path"] in calls
    assert not any("violet-update.service" in c and ("restart" in c or "start" in c) for c in calls)
    inst.Service(layout, "systemd", run=run).unregister()
    assert list((tmp_path / "units").iterdir()) == []
    assert ["systemctl", "disable", "--now", "violet-update.timer", "violet-update.path"] in calls


# ------------------------------------------------------------------------------------------------ installer: auto-update

def _settings(layout, mode):
    (layout.data / "control" / "settings.json").write_text(json.dumps({"values": {"VIOLET_UPDATE_MODE": mode}}),
                                                           encoding="utf-8")


def test_update_mode_is_read_from_the_settings_and_only_three_words_count(tmp_path):
    layout = _layout(tmp_path)
    assert inst.update_mode(layout) == "auto"
    _settings(layout, "notify")
    assert inst.update_mode(layout) == "notify"
    _settings(layout, "rm -rf /")
    assert inst.update_mode(layout) == "auto"
    (layout.data / "control" / "settings.json").write_text("not json", encoding="utf-8")
    layout.env_file.write_text("VIOLET_UPDATE_CHECK=0\n", encoding="utf-8")
    assert inst.update_mode(layout) == "manual"


def test_a_symlinked_settings_file_is_not_followed(tmp_path):
    layout = _layout(tmp_path)
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps({"values": {"VIOLET_UPDATE_MODE": "manual"}}), encoding="utf-8")
    try:
        (layout.data / "control" / "settings.json").symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available here")
    assert inst.update_mode(layout) == "auto"


def test_the_request_file_is_taken_once(tmp_path):
    layout = _layout(tmp_path)
    request = layout.data / "control" / "update-request"
    assert inst.take_update_request(layout) is None
    request.write_text("auto", encoding="utf-8")
    assert inst.take_update_request(layout) == "auto" and not request.exists()
    request.write_text("https://evil.example/install.sh", encoding="utf-8")
    assert inst.take_update_request(layout) == "manual" and not request.exists()     # content is never a source


def _fake_update(layout, calls, fail=False):
    def do_update(opts, release):
        calls.append(release["version"])
        if fail:
            raise inst.InstallError("新版本启动后健康检查没有通过")
        state = inst.load_state(layout)                         # what install.sh upgrade writes
        state.update(version=release["version"], previous_version="0.3.0")
        inst.save_state(layout, state)
        return {"updated": True, "to": release["version"]}
    return do_update


def test_auto_update_in_auto_mode_installs_a_newer_release_and_records_it(tmp_path):
    layout = _layout(tmp_path)
    calls = []
    result = inst.auto_update(inst.Options(root=layout.root, log=lambda s: None), release=NEWER,
                              do_update=_fake_update(layout, calls))
    assert result["action"] == "updated" and calls == ["9.1.0"]
    state = inst.load_state(layout)
    assert state["version"] == "9.1.0"                                       # the update's own record survives
    assert state["auto_update"]["last_result"] == "updated" and state["auto_update"]["updated_to"] == "9.1.0"


def test_auto_update_does_nothing_in_notify_or_manual_mode_unless_asked(tmp_path):
    layout = _layout(tmp_path)
    calls = []
    opts = inst.Options(root=layout.root, log=lambda s: None)
    for mode in ("notify", "manual"):
        _settings(layout, mode)
        assert inst.auto_update(opts, release=NEWER, do_update=_fake_update(layout, calls))["action"] == "skipped"
    assert calls == []
    (layout.data / "control" / "update-request").write_text("manual", encoding="utf-8")
    assert inst.auto_update(opts, release=NEWER, do_update=_fake_update(layout, calls))["action"] == "updated"
    assert calls == ["9.1.0"]


def test_auto_update_when_already_up_to_date(tmp_path):
    layout = _layout(tmp_path, version="9.1.0")
    result = inst.auto_update(inst.Options(root=layout.root, log=lambda s: None), release=NEWER,
                              do_update=_fake_update(layout, []))
    assert result["action"] == "up_to_date" and inst.load_state(layout)["auto_update"]["last_result"] == "up_to_date"


def test_a_failed_release_is_not_retried_automatically_for_a_day(tmp_path):
    layout = _layout(tmp_path)
    opts = inst.Options(root=layout.root, log=lambda s: None)
    calls = []
    t = [1_000_000.0]
    with pytest.raises(inst.InstallError):
        inst.auto_update(opts, release=NEWER, now=lambda: t[0], do_update=_fake_update(layout, calls, fail=True))
    entry = inst.load_state(layout)["auto_update"]
    assert entry["last_result"] == "failed" and entry["failed_version"] == "9.1.0"
    t[0] += 3600
    assert inst.auto_update(opts, release=NEWER, now=lambda: t[0],
                            do_update=_fake_update(layout, calls))["action"] == "waiting"
    (layout.data / "control" / "update-request").write_text("auto", encoding="utf-8")    # the page's auto request
    assert inst.auto_update(opts, release=NEWER, now=lambda: t[0],
                            do_update=_fake_update(layout, calls))["action"] == "waiting"
    (layout.data / "control" / "update-request").write_text("manual", encoding="utf-8")  # "update now" always tries
    assert inst.auto_update(opts, release=NEWER, now=lambda: t[0],
                            do_update=_fake_update(layout, calls))["action"] == "updated"
    assert calls == ["9.1.0", "9.1.0"] and inst.load_state(layout)["auto_update"]["failed_version"] is None


def test_auto_update_records_a_failed_check(tmp_path, monkeypatch):
    layout = _layout(tmp_path)

    def broken(state):
        raise inst.InstallError("查询最新版本失败（网络不通或被拦截）：URLError")
    monkeypatch.setattr(inst, "release_for", broken)
    with pytest.raises(inst.InstallError):
        inst.auto_update(inst.Options(root=layout.root, log=lambda s: None))
    assert inst.load_state(layout)["auto_update"]["last_result"] == "error"


# ------------------------------------------------------------------------------------------------ installer: feed

@pytest.mark.parametrize("url, ok", [
    ("https://mirror.example/violet/release.json", True), ("http://127.0.0.1:18765/release.json", True),
    ("http://mirror.example/release.json", False), ("https://mirror.example/install.sh", False),
    ("https://user:pw@mirror.example/release.json", False), ("https://mirror.example/release.json?x=1", False),
])
def test_only_https_or_local_feeds(url, ok):
    if ok:
        assert inst.feed_url_checked(url) == url
    else:
        with pytest.raises(inst.InstallError):
            inst.feed_url_checked(url)


def test_the_release_source_comes_only_from_install_json(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(inst, "latest_release", lambda repo=None, **kw: seen.append(repo) or NEWER)
    monkeypatch.setattr(inst, "feed_release", lambda url, **kw: seen.append(url) or NEWER)
    inst.release_for({"update_repo": "someone/violet-fork"})
    inst.release_for({"update_feed": "https://mirror.example/release.json", "update_repo": "x/y"})
    inst.release_for({})
    assert seen == ["someone/violet-fork", "https://mirror.example/release.json", inst.UPDATE_REPO]
    assert inst.repo_checked("someone/violet-fork") == "someone/violet-fork"
    for bad in ("someone", "a/b/c", "https://github.com/a/b", "a/b;rm"):
        with pytest.raises(inst.InstallError):
            inst.repo_checked(bad)
    # the Control Center's check uses the same source, never the service's environment
    from ai import updates
    monkeypatch.setenv("VIOLET_UPDATE_REPO", "evil/repo")
    root = _installed_app(tmp_path)
    state = json.loads((root.parent / "install.json").read_text(encoding="utf-8"))
    (root.parent / "install.json").write_text(json.dumps({**state, "update_repo": "someone/violet-fork"}),
                                              encoding="utf-8")
    seen.clear()
    updates._fetch_default(root)()
    assert seen == ["someone/violet-fork"]


def test_a_feed_release_points_at_its_own_install_script(tmp_path):
    import io

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    body = json.dumps({"version": "0.4.0", "base": "http://127.0.0.1:18765", "built_at": "2026-10-27T10:00:00"})
    rel = inst.feed_release("http://127.0.0.1:18765/release.json", opener=lambda req, timeout: Response(body.encode()))
    assert rel["version"] == "0.4.0" and rel["install_url"] == "http://127.0.0.1:18765/install.sh"
    with pytest.raises(inst.InstallError):
        inst.feed_release("http://127.0.0.1:18765/release.json", opener=lambda req, timeout: Response(b'{"a": 1}'))


# ------------------------------------------------------------------------------------------------ root / sudo (Debian)

def test_the_violet_command_of_a_root_install_elevates_itself():
    script = inst.cli_script(Path("/opt/violet/venv/bin/python"), Path("/opt/violet/app/tools/violet_installer.py"),
                             Path("/opt/violet"), elevate=True).replace("\\", "/")
    assert 'if [ "$(id -u)" != "0" ]' in script and 'exec sudo "$0" "$@"' in script and "su -" in script
    assert script.rstrip().endswith('--root "/opt/violet"')
    user = inst.cli_script(Path("/h/v/venv/bin/python"), Path("/h/v/app/tools/violet_installer.py"), Path("/h/v"),
                           elevate=False)
    assert "sudo" not in user                         # a --user-install stays the user


def test_one_command_for_root_without_sudo_and_for_sudo_users():
    """Debian minimal: root, no sudo. Ubuntu: a user with sudo. The documented command has no sudo in it."""
    sh = (PROJECT / "install.sh").read_text(encoding="utf-8")
    assert "--user-install" in sh and 'sudo bash "$script" "$command" "$@"' in sh and "su -" in sh
    assert sh.index("! is_root") < sh.index("find_python || true")       # elevate before anything is installed
    import tools.make_release as mr
    assert '| bash"' in (PROJECT / "tools" / "make_release.py").read_text(encoding="utf-8") and mr
    for doc in (PROJECT / "INSTALL.md", PROJECT.parent / "README.md"):
        text = doc.read_text(encoding="utf-8")
        assert "| sudo bash" not in text and "sudo /opt/violet" not in text, doc


# ------------------------------------------------------------------------------------------------ public by default

def test_switching_access_rewrites_only_the_installer_keys():
    before = "# my notes\nLLM_API_KEY=keep\nVIOLET_DASHBOARD_HOST=127.0.0.1\nVIOLET_DASHBOARD_PORT=7101\n"
    after = inst.set_env_keys(before, {"VIOLET_DASHBOARD_HOST": "0.0.0.0", "VIOLET_DASHBOARD_PORT": "7101",
                                       "VIOLET_DASHBOARD_TLS_CERT": "/opt/violet/config/tls/violet.crt",
                                       "VIOLET_DASHBOARD_TRUST_PROXY": None})
    assert "# my notes\nLLM_API_KEY=keep\nVIOLET_DASHBOARD_HOST=0.0.0.0\n" in after
    assert "VIOLET_DASHBOARD_TLS_CERT=/opt/violet/config/tls/violet.crt" in after and "TRUST_PROXY" not in after
    back = inst.set_env_keys(after, {"VIOLET_DASHBOARD_HOST": "127.0.0.1", "VIOLET_DASHBOARD_TLS_CERT": None})
    assert "TLS_CERT" not in back and "VIOLET_DASHBOARD_HOST=127.0.0.1" in back and "LLM_API_KEY=keep" in back


def test_public_ip_takes_the_first_public_ipv4_answer():
    import io

    class Answer(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    answers = iter([OSError("offline"), b"10.0.0.5\n", b"203.0.113.9\n"])

    def opener(request, timeout):
        a = next(answers)
        if isinstance(a, Exception):
            raise a
        return Answer(a)
    # 203.0.113.0/24 is a documentation range (not global) -> rejected too; a real public address is accepted
    assert inst.public_ip(opener=opener) is None
    assert inst.public_ip(opener=lambda r, timeout: Answer(b"8.8.8.8")) == "8.8.8.8"


def test_the_public_address_comes_first():
    urls = inst.panel_urls("https", 7101, "0.0.0.0", "kg9icfqhxzji", lan=lambda: ["10.0.0.5"], public="8.8.8.8")
    assert urls == ["https://8.8.8.8:7101/kg9icfqhxzji", "https://127.0.0.1:7101/kg9icfqhxzji",
                    "https://10.0.0.5:7101/kg9icfqhxzji"]


def test_default_access(monkeypatch):
    monkeypatch.setattr(inst, "_is_root", lambda: True)
    monkeypatch.setattr(inst.os, "name", "posix")
    assert inst.default_access() == "public"
    monkeypatch.setattr(inst, "_is_root", lambda: False)
    assert inst.default_access() == "local"


def test_purge_removes_firewall_rules_and_only_our_own_account(tmp_path):
    layout = inst.Layout(Path("/opt/violet"))
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        if cmd[:2] == ["getent", "passwd"]:
            home = "/opt/violet/data" if cmd[2] == "violet" else "/home/alice"
            return subprocess.CompletedProcess(cmd, 0, f"{cmd[2]}:x:999:999::{home}:/usr/sbin/nologin\n", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    state = {"user": "violet", "firewall_rules": [{"tool": "ufw", "port": 7101}]}
    import shutil as _sh
    real_which = _sh.which
    try:
        inst.shutil.which = lambda name: "/usr/sbin/" + name
        removed = inst.purge_system(layout, state, run=run, linux_root=True)
        assert ["ufw", "delete", "allow", "7101/tcp"] in calls and ["userdel", "violet"] in calls
        assert removed == ["防火墙规则 7101/tcp", "系统账号 violet"]
        calls.clear()
        inst.purge_system(layout, {"user": "alice"}, run=run, linux_root=True)    # someone else's account
        assert not any(c[0] == "userdel" for c in calls)
    finally:
        inst.shutil.which = real_which
    assert inst.purge_system(layout, state, run=run, linux_root=False) == []


def test_purge_removes_the_folder_but_not_foreign_files(tmp_path):
    for foreign in (False, True):
        root = tmp_path / ("with-foreign" if foreign else "clean")
        layout = inst.Layout(root)
        for folder in (layout.config, layout.data, layout.backups, layout.logs, layout.app):
            folder.mkdir(parents=True)
        (layout.data / "violet.db").write_text("x", encoding="utf-8")
        if foreign:
            (root / "my-notes.txt").write_text("mine", encoding="utf-8")
        inst.save_state(layout, {"version": "0.3.3", "service": "none"})
        result = inst.uninstall(inst.Options(root=root, log=lambda s: None), purge=True)
        if foreign:
            assert root.exists() and (root / "my-notes.txt").exists() and result["leftovers"] == ["my-notes.txt"]
            assert not layout.data.exists()
        else:
            assert not root.exists() and result["note"] == "已全部删除"
