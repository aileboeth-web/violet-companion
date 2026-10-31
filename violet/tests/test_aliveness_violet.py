"""2026-10-30 (user: 人设更有灵动性; 生图不要像 AI 动漫图，尽量靠近现实的情境; ideas from dsh-aurabot-qq and
dsh-sticker-library): life-photo pictures, stickers described in three parts and filtered at intake, her own mood,
being called by name / poked in groups, how she talks with each person. Fakes only."""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from tests.test_qq_social_violet import FakeAdapter, FakeWS, NoTimer, db, engine  # noqa: F401

PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 64


@pytest.fixture()
def clean(db, tmp_path, monkeypatch):
    monkeypatch.setenv("VIOLET_DATA_DIR", str(tmp_path / "data"))
    for table in ("mood_state", "stickers", "sticker_blocklist"):
        db.execute(f"DELETE FROM {table}")
    yield db
    for table in ("mood_state", "stickers", "sticker_blocklist"):
        db.execute(f"DELETE FROM {table}")


# ------------------------------------------------------------------------------------------------ pictures

def test_pictures_of_her_are_life_photos_at_the_real_time(clean, monkeypatch):
    from ai.images.visual import VisualIdentityManager, moment_line, other_prompt
    monkeypatch.delenv("VIOLET_IMAGE_STYLE", raising=False)
    scene = {"description": "Violet 在书桌前看书", "hypothetical": False, "moment": moment_line("2026-12-03T22:10:00")}
    prompt = VisualIdentityManager().prompt_for(scene, "台灯下", shot="selfie")
    assert "真实生活照" in prompt and "不是照片" not in prompt and "动漫或二次元画风" in prompt
    assert "12 月冬天，晚上 22:10" in prompt and "窗外天黑" in prompt and "前置摄像头自拍" in prompt
    only_things = VisualIdentityManager().prompt_for(scene, shot="scene")
    assert "画面里没有她本人" in only_things and "深色及肩短发" not in only_things
    monkeypatch.setenv("VIOLET_IMAGE_STYLE", "illustration")            # the old look is one setting away
    assert "这是一张插画，不是照片" in VisualIdentityManager().prompt_for(scene)
    assert "插画风格" in other_prompt("一只猫")
    monkeypatch.setenv("VIOLET_IMAGE_STYLE", "anime")
    assert "日系动漫画风" in other_prompt("一只猫", "2026-07-01T09:00:00") and "7 月夏天" in other_prompt("猫", "2026-07-01T09:00:00")


def test_scenes_carry_the_moment_unless_illustrated(clean, monkeypatch):
    from ai.images.visual import caption_for, scene_for_eve
    monkeypatch.delenv("VIOLET_IMAGE_STYLE", raising=False)
    scene = scene_for_eve(clean)
    assert scene["moment"].startswith("时间：") and caption_for(scene) == "AI 生成的图片（想象的画面）"
    monkeypatch.setenv("VIOLET_IMAGE_STYLE", "illustration")
    scene = scene_for_eve(clean)
    assert "moment" not in scene and caption_for(scene) == "AI 生成的插画（想象的画面）"


# ------------------------------------------------------------------------------------------------ stickers

def test_sticker_descriptions_in_three_parts_and_a_verdict():
    from ai.stickers import parse_description, verdict_of
    text = "画面：一只猫翻白眼，配字“无语”\n什么时候发：对方说了离谱的话\n关键词：无语、翻白眼、猫\n判断：正常"
    body, verdict = parse_description(text)
    assert verdict == "ok" and body == "画面：一只猫翻白眼，配字“无语”；什么时候发：对方说了离谱的话；关键词：无语、翻白眼、猫"
    assert parse_description("画面：骂人的字\n判断：脏（脏话）")[1] == "dirty"
    assert parse_description("画面：一张截图\n判断：烂")[1] == "junk"
    assert parse_description("一只开心的狗") == ("一只开心的狗", "ok")              # the old one-line answer
    assert verdict_of("【不雅】画面：…") == "dirty" and verdict_of("画面：猫") == "ok"


def test_bad_stickers_are_switched_off_and_deleted_ones_stay_out(clean, monkeypatch):
    from ai import stickers
    import ai.vision as vision
    db = clean
    answers = iter(["画面：猫点头\n什么时候发：同意\n关键词：好\n判断：正常", "画面：骂人\n什么时候发：吵架\n判断：脏"])
    monkeypatch.setattr(vision, "describe_image", lambda path, prompt=None: next(answers))
    good = stickers.add_bytes(db, PNG + b"1", source="qq")
    bad = stickers.add_bytes(db, PNG + b"2", source="qq")
    assert stickers.describe_pending(db) == 2
    assert stickers.get(db, good["id"])["enabled"] == 1 and stickers.get(db, bad["id"])["enabled"] == 0
    assert stickers.get(db, bad["id"])["description"].startswith("【不雅】")
    assert [line.split("：")[0] for line in stickers.prompt_lines(db)] == [f"- {good['id']}"]
    assert stickers.delete(db, good["id"])
    with pytest.raises(stickers.StickerError):
        stickers.add_bytes(db, PNG + b"1", source="group")                 # deleted once: not collected again
    assert stickers.add_bytes(db, PNG + b"1", source="upload", description="猫点头")["enabled"] == 1   # the admin's upload
    assert "没有真正合适的就不发" in stickers.chat_section(db)


def test_a_group_sticker_judged_vulgar_is_not_collected(clean, monkeypatch):
    from ai import stickers
    from ai.social.store import SocialStore
    db = clean
    store = SocialStore(db)
    conv = store.conversation("napcat", "group", "614", "群", default_mode="auto")
    msg = store.add_message(conv["id"], content="[表情包]", sender_id="1", platform_msg_id="55")
    store.add_media(msg, kind="sticker", file_key="k1", url="http://x/1", description="【不雅】画面：骂人")
    a = FakeAdapter()
    e = engine(db, a)
    monkeypatch.setattr(e, "_download", lambda adapter, url: __import__("pathlib").Path(__file__))
    assert e._collect(store.get(conv["id"]), {"message": "55"}, a) is None
    assert db.fetch_one("SELECT COUNT(*) AS n FROM stickers")["n"] == 0


# ------------------------------------------------------------------------------------------------ mood

def test_praise_and_scolding_move_her_mood_and_it_calms_down(clean, monkeypatch):
    from ai import mood
    db = clean
    assert mood.note(db) == "" and mood.feel(db, "今天吃什么") is None
    mood.feel(db, "谢谢你，你真棒")
    assert mood.current(db)["value"] == pytest.approx(0.3)
    mood.feel(db, "好可爱")
    note = mood.note(db)
    assert "心情不错" in note and "主人夸了你" in note and "表情包" in note
    assert mood.initiative_factor(db) > 1.2
    back = (datetime.fromisoformat(db.now()[:19]) - timedelta(hours=3)).isoformat()
    db.execute("UPDATE mood_state SET updated_at = ?", (back,))
    assert mood.current(db)["value"] == pytest.approx(0.3, abs=0.01)      # half of it after one half-life
    mood.feel(db, "闭嘴吧笨蛋")
    mood.feel(db, "垃圾")
    assert mood.current(db)["value"] < -0.3 and "不攻击任何人" in mood.note(db)
    mood.feel(db, "你真烦人，滚", "小周", 0.5)
    assert mood.current(db)["cause"] == "小周凶了你"
    monkeypatch.setenv("VIOLET_MOOD", "0")
    assert mood.note(db) == "" and mood.feel(db, "谢谢") is None


def test_the_mood_reaches_the_group_prompt(clean, monkeypatch):
    from ai import mood
    from ai.social.store import SocialStore
    import ai.social.llm as llm
    db = clean
    conv = SocialStore(db).conversation("napcat", "group", "614", "群", default_mode="auto")
    a = FakeAdapter(owners={"9"})
    e = engine(db, a)
    e.receive(a, kind="group", peer_id="614", sender_id="9", sender_name="主人", text="@她 谢谢你帮我查的，真棒",
              mentioned=True, platform_msg_id="1")
    assert mood.current(db)["value"] == pytest.approx(0.3)
    seen = {}
    monkeypatch.setattr(llm, "decide", lambda system, user, tools=None: (seen.update(system=system)
                                                                        or ({"action": "say", "messages": ["嘿嘿"]}, "f", 1)))
    e.decide(conv["id"])
    assert "【你现在的心情】心情不错" in seen["system"]


# ------------------------------------------------------------------------------------------------ groups

def test_her_name_in_a_group_counts_as_calling_her_and_she_answers(clean, monkeypatch):
    from ai.social.store import SocialStore
    import ai.social.llm as llm
    db = clean
    store = SocialStore(db)
    conv = store.conversation("napcat", "group", "614", "群", default_mode="mention")
    a = FakeAdapter()
    a.nickname = "小紫"
    e = engine(db, a)
    e.receive(a, kind="group", peer_id="614", sender_id="1", sender_name="阿凯", text="小紫你在吗", platform_msg_id="1")
    assert store.messages(conv["id"])[-1]["mentions_self"] == 1
    calls = []

    def fake(system, user, tools=None):
        calls.append(user)
        return ({"action": "silent"} if len(calls) == 1 else {"action": "say", "messages": ["在呢"]}), "f", 1
    monkeypatch.setattr(llm, "decide", fake)
    result = e.decide(conv["id"])
    assert len(calls) == 2 and "这次要回应" in calls[1] and result["sent"] == 1 and a.sent[0][2] == "在呢"
    e.receive(a, kind="group", peer_id="614", sender_id="1", sender_name="阿凯", text="今天好热", platform_msg_id="2")
    assert store.messages(conv["id"])[-1]["mentions_self"] == 0


def test_a_poke_calls_her_and_she_pokes_back(clean, monkeypatch):
    from ai.social.store import SocialStore
    import ai.social.llm as llm
    db = clean
    store = SocialStore(db)
    conv = store.conversation("napcat", "group", "614", "群", default_mode="auto")

    class Pokable(FakeAdapter):
        poked = []

        def poke(self, kind, peer, user):
            self.poked.append((kind, peer, user))
    a = Pokable()
    e = engine(db, a)
    e.receive(a, kind="group", peer_id="614", sender_id="1", sender_name=None, text="", poke=True)
    row = store.messages(conv["id"])[-1]
    assert row["content"] == "[拍了拍你]" and row["mentions_self"] == 1
    monkeypatch.setattr(llm, "decide", lambda system, user, tools=None: ({"action": "say", "messages": ["干嘛~"], "poke": "1"},
                                                                        "f", 1))
    e.decide(conv["id"])
    assert a.poked == [("group", "614", "1")]


def test_she_remembers_how_she_talks_with_each_person_and_group(clean, monkeypatch):
    from ai.social.store import SocialStore
    import ai.social.llm as llm
    db = clean
    store = SocialStore(db)
    conv = store.conversation("napcat", "group", "614", "群", default_mode="auto")
    a = FakeAdapter()
    e = engine(db, a)
    e.receive(a, kind="group", peer_id="614", sender_id="1", sender_name="阿凯", text="哈哈", platform_msg_id="1")
    monkeypatch.setattr(llm, "decide", lambda system, user, tools=None: (
        {"action": "silent", "remember": {"style": {"group": "这个群很随便，可以互损", "people": {"1": "和他互损"}}}}, "f", 1))
    e.decide(conv["id"])
    assert store.get(conv["id"])["style"] == "这个群很随便，可以互损"
    e.receive(a, kind="group", peer_id="614", sender_id="1", sender_name="阿凯", text="在吗", platform_msg_id="2")
    seen = {}
    monkeypatch.setattr(llm, "decide", lambda system, user, tools=None: (seen.update(user=user) or ({"action": "silent"}, "f", 1)))
    e.decide(conv["id"])
    assert "你在这个群说话的方式：这个群很随便，可以互损" in seen["user"] and "你和 TA 说话：和他互损" in seen["user"]


def test_qq_poke_notices_are_routed(monkeypatch):
    from ai.channels.snowluma import SnowLumaChannel
    ch = SnowLumaChannel("napcat", ws_factory=lambda *a, **k: FakeWS([]))
    ch.self_id = "100"
    got, social = [], []
    ch.runtime = SimpleNamespace(identities=SimpleNamespace(bound_peers=lambda c: ["9"]))
    ch._sink = lambda m: got.append(m) or True
    monkeypatch.setattr(ch, "_social", lambda: SimpleNamespace(receive=lambda adapter, **kw: social.append(kw)))
    poke = {"post_type": "notice", "notice_type": "notify", "sub_type": "poke", "target_id": 100, "time": 1}
    ch.handle_event({**poke, "user_id": 9})                                  # the owner, privately
    ch.handle_event({**poke, "user_id": 7, "group_id": 614})
    ch.handle_event({**poke, "user_id": 7, "target_id": 555})              # someone else was poked
    assert [p.text for p in got[0].parts] == ["[拍了拍你]"] and got[0].peer_id == "9"
    assert len(social) == 1 and social[0]["poke"] and social[0]["peer_id"] == "614" and social[0]["kind"] == "group"
    sent = []
    monkeypatch.setattr(ch, "call", lambda action, params=None, timeout=20.0: sent.append((action, params)))
    ch.poke("group", "614", "7")
    assert sent == [("send_poke", {"user_id": 7, "group_id": 614})]


def test_the_models_own_addition_cannot_turn_her_into_an_anime_girl(clean, monkeypatch):
    """2026-10-31, the user's server (task 38): the chat model added "silver-violet hair ... anime-inspired
    illustration" and gpt-image drew an anime girl with purple hair."""
    from ai.images.visual import VisualIdentityManager, clean_extra, other_prompt
    monkeypatch.delenv("VIOLET_IMAGE_STYLE", raising=False)
    real = ("A fictional AI assistant named Violet shown as a non-human digital persona in a cozy virtual server room, "
            "silver-violet hair, subtle glowing interface reflections, friendly relaxed expression, holding up a small "
            "wave toward the camera, tasteful anime-inspired illustration, warm monitor light, no text")
    kept = clean_extra(real, about_her=True)
    for gone in ("anime", "illustration", "hair", "non-human", "virtual", "glowing"):
        assert gone not in kept
    assert "holding up a small wave toward the camera" in kept and "warm monitor light" in kept
    prompt = VisualIdentityManager().prompt_for({"description": "Violet 在书桌前"}, real, shot="selfie")
    assert "虚拟人物" not in prompt and "anime-inspired" not in prompt and "silver-violet" not in prompt
    assert prompt.splitlines()[-1].startswith("最终效果：一张真实的手机照片")
    assert "silver" in clean_extra("a silver laptop on the desk", about_her=False)      # things keep their colours
    assert other_prompt("a cat, anime style").splitlines()[0] == "画面：a cat"
    monkeypatch.setenv("VIOLET_IMAGE_STYLE", "anime")
    assert "anime style" in clean_extra("cat, anime style", about_her=False)          # the anime setting keeps it


def test_a_sticker_set_is_drawn_with_the_image_model(clean, monkeypatch, tmp_path):
    from ai import sticker_pack, stickers
    import ai.images.service as service
    import ai.images.media as media
    db = clean
    drawn = []
    monkeypatch.setattr(service, "images_enabled", lambda: True)

    def generate(self, prompt, **kw):
        drawn.append(prompt)
        return SimpleNamespace(ok=len(drawn) != 3, asset_ids=[len(drawn)])
    monkeypatch.setattr(service.ImageService, "generate", generate)
    monkeypatch.setattr(media.MediaAssetStore, "get", lambda self, asset_id: {"id": asset_id})

    def path_of(self, asset):
        f = tmp_path / f"{asset['id']}.png"
        f.write_bytes(PNG + str(asset["id"]).encode())
        return f
    monkeypatch.setattr(media.MediaAssetStore, "path_of", path_of)
    sticker_pack.start(db)
    for _ in range(200):
        if not sticker_pack.status()["running"]:
            break
        __import__("time").sleep(0.02)
    s = sticker_pack.status()
    assert s["done"] == len(sticker_pack.PACK) - 1 and s["failed"] == 1
    assert "Q 版卡通" in drawn[0] and "“收到！”" in drawn[0]
    rows = stickers.listing(db)
    assert len(rows) == len(sticker_pack.PACK) - 1 and all(r["enabled"] for r in rows)
    assert any("服务器好卡" in (r["description"] or "") and "关键词" in r["description"] for r in rows)
    assert len(sticker_pack.missing(db)) == 1                                # only the failed one is drawn again
    monkeypatch.setattr(service, "images_enabled", lambda: False)
    with pytest.raises(ValueError):
        sticker_pack.start(db)