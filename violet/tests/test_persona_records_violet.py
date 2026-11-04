"""0.7.55: every persona has ONE set of records, whatever the main persona is (ai/memory_scope.template_scope,
persona_templates.store_owner / _records_for_switch).

Friend's case 2026-11-04: Eve imported for Telegram (as Eve, main persona still Violet) -> personas/eve.db with 4507
messages; then Eve chosen as the main persona -> "the same as the main persona = the main file", Telegram read the
empty main file and Eve told him "这是你们第一次聊天". Fictional data only."""

from __future__ import annotations

import json

import pytest

from test_eve_import_violet import SAME, admin, env_guard, package, upload  # noqa: F401 -- fixtures


@pytest.fixture()
def clean_scopes(violetdb):
    """The violetdb fixture empties the persona files too (conftest)."""
    yield violetdb


def count(db, scope, table="messages"):
    from ai import memory_scope
    with memory_scope.use(scope):
        return db.fetch_one(f"SELECT COUNT(*) AS n FROM {table}")["n"]


def import_eve(admin, tmp_path, channel="telegram", persona="eve"):
    p = upload(admin, package(tmp_path)).get_json()["data"]
    r = admin.post(f"/api/control/eve-import/{p['token']}/commit", headers=SAME, json={
        "confirm": True, "channel": channel, "messages": True, "memories": True, "persona": persona, "kind": "partner"})
    assert r.status_code == 200, r.get_json()
    return r.get_json()["data"]


def test_friend_order_eve_keeps_her_records_when_she_becomes_the_main_persona(admin, tmp_path, clean_scopes):
    from ai import memory_scope
    from ai import persona_templates as pt
    db = clean_scopes
    db.add_user_message("text", "Violet 时代的一句", channel="wechat")       # the shipped Violet's own chat
    import_eve(admin, tmp_path)
    assert memory_scope.for_channel("telegram") == "eve" and count(db, "eve") == 4
    r = admin.post("/api/persona/switch", headers=SAME, json={"template": "eve", "memory": "keep", "confirm": True})
    assert r.status_code == 200 and r.get_json()["records"] == "own"
    # was: "main" (the Violet file) -> 0 of Eve's messages, 0 memories
    assert memory_scope.for_channel("telegram") == "eve" and memory_scope.for_channel("wechat") == "eve"
    assert count(db, "eve") == 4 and count(db, "eve", "core_memories") == 2
    assert count(db, memory_scope.MAIN) == 1 and pt.store_owner() == "violet"     # Violet's chat is still hers
    assert memory_scope.label(memory_scope.MAIN).startswith("Violet") and "eve" in memory_scope.all_scopes()
    # and back: Violet goes on with her own records, Eve's stay Eve's
    r = admin.post("/api/persona/switch", headers=SAME, json={"template": "violet", "memory": "keep", "confirm": True})
    assert r.get_json()["records"] == "own"
    assert memory_scope.for_channel("wechat") == memory_scope.MAIN and memory_scope.for_channel("telegram") == "eve"
    assert count(db, "eve") == 4 and count(db, memory_scope.MAIN) == 1


def test_a_server_already_in_the_broken_state_is_repaired_on_start(admin, tmp_path, clean_scopes):
    """The friend's server: persona.json without "store", main persona Eve, Eve's records in personas/eve.db."""
    from ai import memory_scope
    from ai import persona_templates as pt
    db = clean_scopes
    import_eve(admin, tmp_path)
    info = json.loads((pt.control_dir() / "persona.json").read_text(encoding="utf-8")) \
        if (pt.control_dir() / "persona.json").is_file() else {}
    info.update(template="eve")
    info.pop("store", None)
    pt._write(pt.control_dir() / "persona.json", info)                        # how 0.7.54 left it
    memory_scope.split_channels(db)                                            # runtime.start calls this
    assert pt.store_owner() == "violet"
    assert memory_scope.for_channel("telegram") == "eve" and count(db, "eve") == 4


def test_keep_takes_over_the_records_of_the_persona_she_replaces(admin, tmp_path, clean_scopes):
    """A persona without records of her own still takes over the current one's, as "keep" always did -- also when the
    current one's records are in a file of her own (moved, not copied)."""
    from ai import memory_scope
    from ai import persona_templates as pt
    db = clean_scopes
    db.add_user_message("text", "主文件里的一句", channel="wechat")
    r = admin.post("/api/persona/switch", headers=SAME, json={"template": "gentle", "memory": "keep", "confirm": True})
    assert r.get_json()["records"] == "took_over" and pt.store_owner() == "gentle"
    assert memory_scope.for_channel("wechat") == memory_scope.MAIN and count(db, memory_scope.MAIN) == 1
    # now: Eve (own file) is the main persona, then genki replaces her with "keep"
    import_eve(admin, tmp_path, channel="wechat")                    # gives WeChat Eve as its OWN persona ...
    pt.set_channel("wechat", None)                                    # ... back to following the main persona
    admin.post("/api/persona/switch", headers=SAME, json={"template": "eve", "memory": "keep", "confirm": True})
    assert memory_scope.for_channel("wechat") == "eve"
    eve_messages = count(db, "eve")
    r = admin.post("/api/persona/switch", headers=SAME, json={"template": "genki", "memory": "keep", "confirm": True})
    assert r.get_json()["records"] == "took_over" and r.get_json()["took_over"] == "eve"
    assert memory_scope.for_channel("wechat") == "genki" and count(db, "genki") == eve_messages
    assert "eve" not in db.scope_files()


def test_clear_empties_only_the_new_persona_s_records(admin, tmp_path, clean_scopes):
    from ai import memory_scope
    db = clean_scopes
    import_eve(admin, tmp_path)
    db.add_user_message("text", "Violet 的一句", channel="wechat")
    r = admin.post("/api/persona/switch", headers=SAME, json={"template": "eve", "memory": "clear", "confirm": True})
    assert r.status_code == 200 and r.get_json()["archive"]
    assert count(db, "eve", "core_memories") == 0 and count(db, "eve", "event_memories") == 0
    assert count(db, memory_scope.MAIN) == 1                                  # Violet's were not hers to clear


def test_persona_page_says_which_channels_use_each_persona(admin, tmp_path, clean_scopes):
    """user: 导入时选了人设，人设页却看不出来 -- 两边同步."""
    import_eve(admin, tmp_path)                                                # Telegram not added yet
    d = admin.get("/api/persona/templates", headers=SAME).get_json()
    assert d["used_by"]["eve"] == ["Telegram（还没添加）"] and d["used_by"]["violet"] == ["微信"]
    assert d["records"]["eve"] == 4
    assert any(c["id"] == "telegram" and c["template"] == "eve" for c in d["channels"])
