"""0.7.56 (friend's server: Docker Hub blocked, docker.1ms.run crawling at ~70 KB/s for 24 minutes, the page stuck at 84 %,
then the install failed): sources that do not answer are skipped, a pull that hangs or crawls is left for the next source,
several rounds (downloaded layers are kept), the page shows real MB."""

from __future__ import annotations

import sys

import pytest


def test_pull_skips_dead_sources_and_comes_back_to_a_stuck_one():
    from tools import violet_snowluma as sl
    ran, tried = [], []

    def watched(cmd, prefix, may_leave_slow):
        tried.append((prefix, may_leave_slow))
        if prefix == "docker.1ms.run/" and len([t for t in tried if t[0] == prefix]) == 2:
            return 0, "Status: Downloaded newer image", ""             # second round: the layers so far + the rest
        return 1, "", "stalled" if prefix == "docker.1ms.run/" else ""

    source = sl.pull(lambda cmd, **kw: ran.append(cmd), None, lambda line: None, {}, check=lambda p: p != "",
                     watched=watched)
    assert source == "docker.1ms.run"
    assert all(p != "" for p, _ in tried)                                   # Docker Hub (blocked) never tried
    assert tried[0] == ("docker.1ms.run/", True)
    assert ["docker", "tag", "docker.1ms.run/" + sl.IMAGE, sl.IMAGE] in ran


def test_pull_failure_says_why_for_each_source():
    from tools import violet_snowluma as sl
    with pytest.raises(sl.SnowlumaError) as err:
        sl.pull(lambda cmd, **kw: None, None, lambda line: None, {}, check=lambda p: p in ("docker.1ms.run/", "hub.rat.dev/"),
                watched=lambda cmd, prefix, may_leave_slow: (1, "unauthorized", "slow" if prefix == "hub.rat.dev/" else ""))
    text = str(err.value)
    assert "docker.1ms.run：unauthorized" in text and "hub.rat.dev：太慢，换下一个" in text
    assert "连不上、没有试：Docker Hub" in text


def test_the_last_round_never_leaves_a_slow_source():
    from tools import violet_snowluma as sl
    calls = []
    with pytest.raises(sl.SnowlumaError):
        sl.pull(lambda cmd, **kw: None, None, lambda line: None, {}, check=lambda p: p == "docker.1ms.run/",
                watched=lambda cmd, prefix, may_leave_slow: (calls.append(may_leave_slow), (1, "x", ""))[1])
    assert calls == [False] * sl.PULL_ROUNDS                                # only one source: never left for "slow"


def run_watched(code, **kw):
    from tools import violet_snowluma as sl
    reports = []
    out = sl.stream_watched([sys.executable, "-c", code], lambda line: None, None,
                            report=lambda got, bps: reports.append(got), poll=0.05, **kw)
    return out, reports


def test_a_pull_without_progress_is_stopped():
    (code, tail, why), _ = run_watched("import time; print('Pulling fs layer', flush=True); time.sleep(30)",
                                       measure=lambda: 100, may_leave_slow=False, stall=0.4)
    assert why == "stalled" and code != 0 and "Pulling fs layer" in tail


def test_a_crawling_pull_is_left_only_when_allowed():
    grow = iter(range(0, 10**9, 1024))                                      # 1 KB per look
    (code, _, why), reports = run_watched("import time; time.sleep(30)", measure=lambda: next(grow),
                                          may_leave_slow=True, stall=60, slow_after=0.3, slow_bps=10**9)
    assert why == "slow" and reports and reports[-1] > 0


def test_a_finished_pull_returns_its_exit_code():
    (code, tail, why), _ = run_watched("print('Status: Downloaded newer image')", measure=lambda: 0,
                                       may_leave_slow=True, stall=60)
    assert code == 0 and why == "" and "Downloaded" in tail
