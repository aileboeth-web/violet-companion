"""0.7.46 (user: telegram 表情包她看不到): a cut / markdown vision answer becomes plain words, and a cut answer
is asked again with a bigger token budget."""
from ai.images.adapters import _cut_short, tidy_vision_text


def test_cut_answer_loses_dangling_bullet_and_lead_in():
    assert tidy_vision_text("这张图片是一张卡通风格的表情包。以下是实际看到的内容：\n\n*") == "这张图片是一张卡通风格的表情包。"


def test_markdown_list_becomes_plain_lines():
    text = "以下是实际看到的内容：\n* **一只橘猫**趴在键盘上\n- 下方文字：别卷了"
    assert tidy_vision_text(text) == "一只橘猫趴在键盘上\n下方文字：别卷了"


def test_plain_answer_unchanged():
    assert tidy_vision_text("一只熊猫头，下方大字“九年义务教育教出来”") == "一只熊猫头，下方大字“九年义务教育教出来”"


def test_cut_short_detects_length_stop():
    assert _cut_short({"choices": [{"finish_reason": "length", "message": {}}]})
    assert not _cut_short({"choices": [{"finish_reason": "stop", "message": {}}]})
    assert not _cut_short({})
