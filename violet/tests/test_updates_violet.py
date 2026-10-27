"""Releases and updates (offline): version rules, the GitHub "latest release" reader, `violet update`, the Control
Center check and the release workflow. Nothing here contacts GitHub."""
from __future__ import annotations

import io
import json
import re
import urllib.error
from pathlib import Path

import pytest

from tools import violet_installer as inst

PROJECT = Path(__file__).resolve().parents[1]
REPO_ROOT = PROJECT.parent
RELEASE = {"tag": "v0.2.0", "version": "0.2.0", "url": "https://github.com/o/r/releases/tag/v0.2.0",
           "published_at": "2026-11-01T00:00:00Z", "notes": "新版本",
           "install_url": "https://github.com/o/r/releases/download/v0.2.0/install.sh"}


# ---------------------------------------------------------------------------------------------- versions

@pytest.mark.parametrize("text,key", [("0.1.0", (0, 1, 0)), ("v1.2", (1, 2, 0)), ("2", (2, 0, 0)),
                                      ("0.1.0-rc1", (0, 1, 0)), ("unknown", None), ("2.0.0-p10x.1", (2, 0, 0)),
                                      ("", None), ("1.2.3.4", None)])
def test_version_key(text, key):
    assert inst.version_key(text) == key


def test_is_newer_needs_two_real_versions():
    assert inst.is_newer("0.1.1", "0.1.0") and inst.is_newer("v1.0.0", "0.9.9")
    assert not inst.is_newer("0.1.0", "0.1.0") and not inst.is_newer("0.0.9", "0.1.0")
    assert not inst.is_newer("0.2.0", "unknown") and not inst.is_newer(None, "0.1.0")


def test_the_program_version_is_a_release_version():
    assert inst.version_key((PROJECT / "VERSION").read_text(encoding="utf-8")) is not None


# ---------------------------------------------------------------------------------------------- latest release

class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_latest_release_reads_the_tag_and_builds_the_installer_address():
    seen = {}

    def opener(request, timeout):
        seen["url"], seen["agent"] = request.full_url, request.get_header("User-agent")
        return _Response(json.dumps({"tag_name": "v0.3.1", "html_url": "https://github.com/o/r/releases/tag/v0.3.1",
                                     "published_at": "2026-11-02T00:00:00Z", "body": "说明"}).encode())
    rel = inst.latest_release("o/r", opener=opener)
    assert seen["url"] == "https://api.github.com/repos/o/r/releases/latest" and seen["agent"]
    assert rel["version"] == "0.3.1" and rel["install_url"] == "https://github.com/o/r/releases/download/v0.3.1/install.sh"


@pytest.mark.parametrize("problem,message", [
    (urllib.error.HTTPError("u", 404, "nf", {}, None), "还没有发布"),
    (urllib.error.HTTPError("u", 403, "rate", {}, None), "GitHub 返回 403"),
    (urllib.error.URLError("down"), "网络不通"),
])
def test_latest_release_failures_are_explained(problem, message):
    def opener(request, timeout):
        raise problem
    with pytest.raises(inst.InstallError, match=message):
        inst.latest_release("o/r", opener=opener)


def test_a_tag_that_is_not_a_version_is_refused():
    with pytest.raises(inst.InstallError, match="不是版本号"):
        inst.latest_release("o/r", opener=lambda r, timeout: _Response(b'{"tag_name": "nightly"}'))


def test_the_repository_can_be_overridden(monkeypatch):
    monkeypatch.setenv("VIOLET_UPDATE_REPO", "someone/fork")
    assert inst.update_repo() == "someone/fork"
    monkeypatch.delenv("VIOLET_UPDATE_REPO")
    assert inst.update_repo() == inst.UPDATE_REPO == "aileboeth-web/violet-companion"


# ---------------------------------------------------------------------------------------------- violet update

def _installed(tmp_path, version="0.1.0", service="supervisor"):
    root = tmp_path / "violet"
    (root / "app").mkdir(parents=True)
    (root / "app" / "VERSION").write_text(version + "\n", encoding="utf-8")
    (root / "install.json").write_text(json.dumps({"version": version, "service": service, "port": 7100}),
                                       encoding="utf-8")
    return inst.Options(root=root, log=lambda s: None)


class _Run:
    def __init__(self, stdout, code=0):
        self.stdout, self.code, self.calls = stdout, code, []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        import subprocess
        return subprocess.CompletedProcess(cmd, self.code, self.stdout, "")


def test_update_downloads_the_releases_installer_and_runs_its_upgrade(tmp_path):
    opts = _installed(tmp_path)
    got = []
    run = _Run(json.dumps({"ok": True, "from": "0.1.0", "to": "0.2.0", "backup": "/b.tar.gz"}) + "\n")

    def download(url, target):
        got.append(url)
        target.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    result = inst.update(opts, release=RELEASE, download=download, run=run, windows=False)
    assert got == [RELEASE["install_url"]]
    assert run.calls[0][0] == "bash" and run.calls[0][2:] == ["upgrade", "--root", str(opts.root), "--json"]
    assert result["updated"] is True and result["to"] == "0.2.0" and result["backup"] == "/b.tar.gz"


def test_update_does_nothing_when_already_current_or_only_checking(tmp_path):
    opts = _installed(tmp_path, version="0.2.0")
    never = lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not download"))  # noqa: E731
    assert inst.update(opts, release=RELEASE, download=never, run=never, windows=False)["updated"] is False
    opts = _installed(tmp_path / "x", version="0.1.0")
    checked = inst.update(opts, check_only=True, release=RELEASE, download=never, run=never, windows=False)
    assert checked["newer"] is True and checked["updated"] is False


def test_a_failed_upgrade_is_reported_with_its_reason(tmp_path):
    opts = _installed(tmp_path)
    run = _Run(json.dumps({"ok": False, "error": "升级失败，已恢复到 0.1.0，旧版本已重新运行。"}) + "\n", code=2)
    with pytest.raises(inst.InstallError, match="已恢复到 0.1.0"):
        inst.update(opts, release=RELEASE, download=lambda u, t: t.write_text("#!", encoding="utf-8"), run=run,
                    windows=False)


def test_a_systemd_install_needs_root_and_windows_gets_instructions(tmp_path):
    never = lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run"))  # noqa: E731
    opts = _installed(tmp_path, service="systemd")
    if not inst._is_root():
        with pytest.raises(inst.InstallError, match="需要 root"):
            inst.update(opts, release=RELEASE, download=never, run=never, windows=False)
    with pytest.raises(inst.InstallError, match="Windows"):
        inst.update(_installed(tmp_path / "w"), release=RELEASE, download=never, run=never, windows=True)


def test_no_installation_no_update(tmp_path):
    with pytest.raises(inst.InstallError, match="没有安装"):
        inst.update(inst.Options(root=tmp_path / "none", log=lambda s: None), release=RELEASE)


# ---------------------------------------------------------------------------------------------- Control Center

@pytest.fixture()
def updates(monkeypatch):
    from ai import updates
    updates.reset_for_tests()
    monkeypatch.setenv("VIOLET_UPDATE_CHECK", "1")
    yield updates
    updates.reset_for_tests()


def test_check_reports_a_newer_release_and_caches_it(updates, tmp_path):
    root = tmp_path / "violet" / "app"
    root.mkdir(parents=True)
    (root / "VERSION").write_text("0.1.0\n", encoding="utf-8")
    (root.parent / "install.json").write_text("{}", encoding="utf-8")
    calls = []
    fetch = lambda: calls.append(1) or RELEASE  # noqa: E731
    t = [1000.0]
    clock = lambda: t[0]  # noqa: E731
    first = updates.check(fetch=fetch, now=clock, root=root)
    assert first["newer"] and first["latest"] == "0.2.0" and first["current"] == "0.1.0"
    assert first["command"] == f"{root.parent / 'violet'} update" and first["url"].startswith("https://github.com/")
    t[0] += 3600
    assert updates.check(fetch=fetch, now=clock, root=root)["newer"]   # an hour later: from the cache
    assert len(calls) == 1
    t[0] += 6 * 3600
    updates.check(fetch=fetch, now=clock, root=root)                   # seven hours after the first: asked again
    assert len(calls) == 2


def test_check_can_be_switched_off_and_explains_failures(updates, monkeypatch, tmp_path):
    def broken():
        raise inst.InstallError("查询最新版本失败（网络不通或被拦截）：URLError")
    failed = updates.check(fetch=broken, refresh=True)
    assert failed["error"].startswith("查询最新版本失败") and failed["newer"] is False
    # 0.2.0's switch maps to "manual" (stage 3): nothing is fetched until the admin presses "check for updates"
    monkeypatch.setenv("VIOLET_UPDATE_CHECK", "0")
    updates.reset_for_tests()
    off = updates.check(fetch=lambda: (_ for _ in ()).throw(AssertionError("no request in manual mode")))
    assert off["mode"] == "manual" and off["enabled"] is False and off["latest"] is None


def test_a_link_that_is_not_github_is_dropped(updates):
    result = updates.check(fetch=lambda: {**RELEASE, "url": "https://evil.example/x"}, refresh=True)
    assert result["url"] is None


def test_without_an_installer_layout_the_command_says_how_to_upgrade(updates, tmp_path):
    assert "install.sh upgrade" in updates.update_command(tmp_path / "src")


# ---------------------------------------------------------------------------------------------- release workflow

def test_the_release_workflow_publishes_what_make_release_builds():
    flow = (REPO_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert 'tags: ["v*"]' in flow and "contents: write" in flow
    assert 'want="v$(tr -d' in flow and "violet/VERSION" in flow                      # tag must match the version
    assert "releases/download/${GITHUB_REF_NAME}" in flow and "make_release.py" in flow
    source = (PROJECT / "tools" / "make_release.py").read_text(encoding="utf-8")
    built = re.findall(r'"([^"]+)"', re.search(r"for n in \(([^)]*)\)", source).group(1))
    assert "install.sh" in built and "violet.tar.gz" in built
    for name in built + ["SHA256SUMS"]:
        assert f"dist/{name}" in flow, name


def test_the_readme_and_install_guide_give_the_github_one_liner():
    line = "curl -fsSL https://github.com/aileboeth-web/violet-companion/releases/latest/download/install.sh | bash"
    assert line in (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert line in (PROJECT / "INSTALL.md").read_text(encoding="utf-8")
