"""0.7.46 / 0.7.47 (user: telegram 表情包她看不到 / 不能理解我的意思): a cut / markdown vision answer becomes plain
words, a cut answer is asked again with a bigger token budget, and a sticker asks what it MEANS."""
from types import SimpleNamespace

from ai.images.adapters import _cut_short, tidy_vision_text
from tests.image_fixtures import PNG, config as image_config, only, write
from tests.test_phase2_scenarios import make_world  # noqa: F401 -- fixture


def test_cut_answer_loses_dangling_bullet_and_lead_in():
    assert tidy_vision_text("这张图片是一张卡通风格的表情包。以下是实际看到的内容：\n\n*") == "这张图片是一张卡通风格的表情包。"


def test_markdown_list_becomes_plain_lines():
    text = "以下是实际看到的内容：\n* **一只橘猫**趴在键盘上\n- 下方文字：别卷了"
    assert tidy_vision_text(text) == "一只橘猫趴在键盘上\n下方文字：别卷了"


def test_ju_ti_ru_xia_lead_in_dropped():
    assert tidy_vision_text("具体内容如下：\n* 主体角色：一个Q版女孩") == "主体角色：一个Q版女孩"


def test_plain_answer_unchanged():
    assert tidy_vision_text("一只熊猫头，下方大字“九年义务教育教出来”") == "一只熊猫头，下方大字“九年义务教育教出来”"


def test_cut_short_detects_length_stop():
    assert _cut_short({"choices": [{"finish_reason": "length", "message": {}}]})
    assert not _cut_short({"choices": [{"finish_reason": "stop", "message": {}}]})
    assert not _cut_short({})


def test_sticker_question_reaches_the_legacy_vision(make_world, tmp_path, monkeypatch):
    from ai.images.chat_tools import intake_inbound_image
    from ai.stickers import STICKER_CHAT_PROMPT
    monkeypatch.setenv("VIOLET_IMAGE_PROVIDERS_FILE", str(write(only(image_config()), tmp_path / "ip.json")))
    w = make_world(VIOLET_IMAGE_ENABLED="1")
    path = tmp_path / "s.png"
    path.write_bytes(PNG)
    asked = []

    def legacy(p, prompt=None):
        asked.append(prompt)
        return "“神降临…” 张开双臂站在光里，隆重登场"
    text = intake_inbound_image(w.db, 1, path, legacy_vision=legacy, question=STICKER_CHAT_PROMPT)
    assert asked == [STICKER_CHAT_PROMPT] and "神降临" in text
    # a vision callable that takes no prompt still works
    assert "一只橘猫" in intake_inbound_image(w.db, 2, path, legacy_vision=lambda p: "一只橘猫", question="x")


def test_legacy_vision_retries_a_cut_answer(monkeypatch, tmp_path):
    from ai import vision
    calls = []

    def create(**kw):
        calls.append(kw.get("max_tokens"))
        cut = len(calls) == 1
        return SimpleNamespace(model="m", usage=None, choices=[SimpleNamespace(
            finish_reason="length" if cut else "stop",
            message=SimpleNamespace(content="左侧角色：露出了" if cut else "“神降临…” 隆重登场"))])
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(vision, "_client", lambda url, key: client)
    monkeypatch.setattr(vision, "vision_model", lambda: "m")
    monkeypatch.setenv("LLM_API_KEY", "k")
    path = tmp_path / "a.png"
    path.write_bytes(PNG)
    assert vision.describe_image(path) == "“神降临…” 隆重登场"
    assert len(calls) == 2
