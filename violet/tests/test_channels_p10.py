"""Phase 10.1: channel layer units -- message normalisation, the outbound contract and leak guard, identities,
the Telegram and OneBot v11 adapters against loopback fakes."""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest

from ai.channels.base import Part
from ai.channels.normalize import clean_text, coalesce, render_part, render_parts, safe_error
from ai.channels import outbound
from tests.fake_channels import TOKEN, FakeOneBot, FakeTelegram


# ------------------------------------------------------------------------------------------------ normalisation

def test_parts_are_rendered_in_plain_words_never_as_protocol():
    assert render_part(Part("text", "你好\x00​呀")) == "你好呀"
    assert render_part(Part("emoji", "微笑")) == "[表情：微笑]"
    assert render_part(Part("emoji", "", {"platform_label": "QQ"})) == "[QQ表情]"     # unknown id: no guess
    assert render_part(Part("sticker", "😂")) == "[表情包：😂]"
    assert render_part(Part("voice", "明天八点出门")) == "[语音，转成文字是：明天八点出门]"
    assert "听不了" in render_part(Part("voice", ""))
    assert render_part(Part("file", "报告.pdf")) == "[对方发来一个文件：报告.pdf（Violet 现在打不开文件）]"
    assert render_part(Part("unsupported")) == "[对方发来一条 Violet 暂时不支持的消息]"
    assert render_part(Part("image")) == ""                      # images go through the vision step
    link = render_part(Part("link", "攻略", {"url": "javascript:alert(1)"}))
    assert "javascript" not in link
    assert render_part(Part("text", "好的", {"quote": "明天几点"})) == "[引用：明天几点]\n好的"


def test_inline_pieces_are_one_line_and_images_keep_their_place():
    parts = [Part("text", "哈哈"), Part("emoji", "微笑"), Part("image", "", {"x": 1}), Part("text", "看这个")]
    merged = coalesce(parts)
    assert [p.kind for p in merged] == ["text", "image", "text"]
    assert merged[0].text == "哈哈[表情：微笑]"
    assert render_parts([Part("quote", "早"), Part("text", "嗯")]) == "[引用：早]\n嗯"


def test_text_length_is_limited_and_errors_are_scrubbed():
    assert len(clean_text("啊" * 10000)) < 4100
    err = RuntimeError(f"HTTPSConnectionPool(host='api.telegram.org'): /bot{TOKEN}/getUpdates?offset=3 failed")
    text = safe_error(err)
    assert TOKEN not in text and TOKEN.split(":")[1] not in text and "RuntimeError" in text


# ------------------------------------------------------------------------------------------------ outbound contract

SYSTEM_PROMPT = ("你是 Violet，一个虚构的女孩，正在和对方用微信聊天。你清楚自己是虚构角色，过的是系统里记录下来的虚拟日常，"
                 "不假装是现实中的真人；但不用时时把这件事挂在嘴边，平常就像熟悉的人那样自然地聊。")


@pytest.fixture()
def guard():
    outbound.reset_for_tests()
    outbound.register_instructions("ChatAgent", SYSTEM_PROMPT)
    yield outbound
    outbound.reset_for_tests()


def test_the_reply_comes_only_from_chat_content_never_from_messages_or_raw():
    pipeline = {"chat": {"content": "刚下课\n你吃饭了吗",
                         "messages": [{"role": "system", "content": SYSTEM_PROMPT}],
                         "raw": {"choices": [{"message": {"content": SYSTEM_PROMPT}}]}}}
    envelope = outbound.build_reply(pipeline, "in:1")
    assert [m.text for m in envelope.messages] == ["刚下课", "你吃饭了吗"]
    public = envelope.to_public()
    assert public["schema"] == "violet.reply/1" and SYSTEM_PROMPT[:10] not in json.dumps(public, ensure_ascii=False)
    # a failed or silent turn has nothing to send, whatever the agent left behind
    assert not outbound.build_reply({"chat": {"content": "x", "failed": True}}).should_send
    assert not outbound.build_reply({"chat": {"content": "x"}, "should_reply": False}).should_send
    assert not outbound.build_reply({"chat": {"content": {"text": "x"}}}).should_send
    assert outbound.build_reply({"chat": {"content": "[NO_REPLY]"}}).declined


@pytest.mark.parametrize("text, reason", [
    (SYSTEM_PROMPT[10:60], "instruction"),
    ("好呀\n" + SYSTEM_PROMPT, "instruction"),
    ("【这一轮先查再回答】对方想知道的：天气", "internal_header"),
    ("【外部工具数据｜来源 violet-web/web_search】结果", "internal_header"),
    ('{"route": "chat", "user_intent": "撒娇", "should_reply": true}', "internal_json"),
    ('```json\n{"route": "chat"}\n```', "internal_json"),
    ('[{"role": "system", "content": "x"}]', "internal_json"),
    ("<think>用户在撒娇，我应该</think>好呀", "reasoning"),
    ("system: 你是 Violet", "role_transcript"),
    ("任务执行结果（供参考，无需重复）：已完成", "internal_marker"),
])
def test_internal_content_is_caught(guard, text, reason):
    assert guard.find_leak(text) == reason


@pytest.mark.parametrize("text", [
    "刚下课，累死了", "你也是虚构的吗哈哈", "明天要早起，我先去洗澡啦", "好呀好呀！", "这个链接你看看 https://example.org/a",
    "我平常就是这样自然地聊天的呀", "{笑}", "“微信”这个词也没问题", "【笑】",
])
def test_natural_replies_pass(guard, text):
    assert guard.find_leak(text) is None


def test_every_internal_header_in_the_code_is_known_to_the_guard():
    headers = outbound.internal_headers()
    # a few headers from different modules: context engine, tool notes, channel note, web, chat agent
    for expected in ("【这一轮先查再回答】", "【这次聊天的地方】", "【联网工具】", "【图片】"):
        assert any(h.startswith(expected[:-1]) for h in headers), expected
    assert all(h.startswith("【") and len(h) >= 4 for h in headers)


# ------------------------------------------------------------------------------------------------ identities

def test_identity_policies_and_pairing(violetdb):
    from ai.channels.identity import IdentityStore
    violetdb.execute("DELETE FROM channel_identities")
    violetdb.execute("DELETE FROM channel_pairing_codes")
    store = IdentityStore(violetdb)
    assert store.admit("wechat", "", "first_peer") == "owner"                  # single-sender transport
    assert store.admit("tg", "1", "first_peer") == "owner"                     # first peer bound
    assert store.admit("tg", "2", "first_peer") == "pending"                   # later strangers are not
    assert store.admit("qq", "9", "owner") == "pending"
    assert store.try_pairing("qq", "9", "你好") is None
    assert store.try_pairing("qq", "9", "/bind 000000") == "invalid"
    code = store.create_pairing_code("qq")["code"]
    assert len(code) == 6 and not violetdb.fetch_one("SELECT 1 FROM channel_pairing_codes WHERE code_hash = ?", (code,))
    assert store.try_pairing("tg", "2", f"/bind {code}") == "invalid"          # code made for another channel
    assert store.try_pairing("qq", "9", f"绑定 {code}") == "bound"
    assert store.admit("qq", "9", "owner") == "owner"
    assert store.try_pairing("qq", "8", f"/bind {code}") == "invalid"          # one use only
    row = store.get("qq", "9")
    store.set_status(row["id"], "blocked")
    assert store.admit("qq", "9", "owner") == "blocked"


def test_pairing_tries_are_limited_and_codes_expire(violetdb):
    from ai.channels.identity import IdentityStore
    violetdb.execute("DELETE FROM channel_pairing_codes")
    store = IdentityStore(violetdb)
    for _ in range(5):
        assert store.try_pairing("qq", "7", "/bind 123456") == "invalid"
    good = store.create_pairing_code(None)["code"]
    assert store.try_pairing("qq", "7", f"/bind {good}") == "locked"
    violetdb.execute("UPDATE channel_pairing_codes SET expires_at = '2000-01-01T00:00:00'")
    assert store.try_pairing("qq", "6", f"/bind {good}") == "invalid"


# ------------------------------------------------------------------------------------------------ telegram

def test_telegram_updates_are_parsed_for_private_chats_only():
    from ai.channels.telegram import parse_update
    text = parse_update({"update_id": 5, "message": FakeTelegram.private_text("早")}, "tg")
    assert text.peer_id == "4242" and text.dedup_key == "tg:tg:5" and [p.text for p in text.parts] == ["早"]
    group = FakeTelegram.private_text("大家好")
    group["chat"] = {"id": -100, "type": "group"}
    assert parse_update({"update_id": 6, "message": group}, "tg") is None
    photo = FakeTelegram.private_text("", photo=[{"file_id": "s", "file_size": 10}, {"file_id": "b", "file_size": 99}],
                                      caption="看我做的饭")
    photo.pop("text")
    parsed = parse_update({"update_id": 7, "message": photo}, "tg")
    assert [p.kind for p in parsed.parts] == ["image", "text"] and parsed.parts[0].data["file_id"] == "b"
    sticker = FakeTelegram.private_text("", sticker={"emoji": "😂", "file_id": "x"})
    sticker.pop("text")
    assert parse_update({"update_id": 8, "message": sticker}, "tg").parts[0].text == "[表情包：😂]"
    quoted = FakeTelegram.private_text("对", reply_to_message={"text": "明天去吗"})
    assert parse_update({"update_id": 9, "message": quoted}, "tg").parts[0].text == "[引用：明天去吗]\n对"


def test_telegram_adapter_receives_sends_and_keeps_its_offset(tmp_path):
    from ai.channels.telegram import TelegramChannel
    fake = FakeTelegram()
    try:
        got = []
        adapter = TelegramChannel("tg", {"api_base": fake.url, "poll_timeout": 1}, {"bot_token": TOKEN},
                                  data_dir=tmp_path)
        adapter._sink = lambda m: got.append(m) or True
        adapter.prepare()
        fake.queue(FakeTelegram.private_text("第一条"))
        fake.queue(FakeTelegram.private_text("第二条", message_id=2))
        assert adapter.receive_once() == 2
        assert [m.parts[0].text for m in got] == ["第一条", "第二条"]
        assert adapter.receive_once() == 0 and fake.offsets[-1] == 3        # confirmed: not delivered again
        again = TelegramChannel("tg", {"api_base": fake.url}, {"bot_token": TOKEN}, data_dir=tmp_path)
        assert again._offset() == 3                                          # survives a restart
        adapter.send_text("4242", "收到啦")
        adapter.send_typing("4242", True)
        assert fake.sent_texts == ["收到啦"] and fake.actions[0]["action"] == "typing"
        assert adapter.probe()["bot"] == "violet_test_bot"
    finally:
        fake.close()


def test_telegram_offset_is_not_advanced_when_a_message_was_not_stored(tmp_path):
    from ai.channels.telegram import TelegramChannel
    fake = FakeTelegram()
    try:
        adapter = TelegramChannel("tg", {"api_base": fake.url}, {"bot_token": TOKEN}, data_dir=tmp_path)
        adapter._sink = lambda m: False
        fake.queue(FakeTelegram.private_text("会再来一次"))
        adapter.receive_once()
        assert adapter._offset() == 0
    finally:
        fake.close()


def test_telegram_bad_token_asks_for_login_and_errors_never_show_the_token(tmp_path):
    from ai.channels.base import NeedsLogin
    from ai.channels.telegram import TelegramChannel, TelegramError
    fake = FakeTelegram()
    try:
        adapter = TelegramChannel("tg", {"api_base": fake.url}, {"bot_token": TOKEN.replace("1234", "9999")},
                                  data_dir=tmp_path)
        with pytest.raises(NeedsLogin):
            adapter.prepare()
        fake.fail_next.append((429, {"ok": False, "description": "Too Many Requests", "parameters": {"retry_after": 3}}))
        adapter = TelegramChannel("tg", {"api_base": fake.url}, {"bot_token": TOKEN}, data_dir=tmp_path)
        with pytest.raises(TelegramError) as info:
            adapter.send_text("1", "x")
        assert info.value.retry_after == 3 and TOKEN not in str(info.value)
        closed = TelegramChannel("tg", {"api_base": "http://127.0.0.1:9"}, {"bot_token": TOKEN}, data_dir=tmp_path)
        with pytest.raises(TelegramError) as info:
            closed.send_text("1", "x")
        assert TOKEN not in str(info.value) and TOKEN.split(":")[1] not in str(info.value)
    finally:
        fake.close()


# ------------------------------------------------------------------------------------------------ onebot v11

def _signed(secret: str, event: dict) -> tuple[bytes, str]:
    body = json.dumps(event).encode("utf-8")
    return body, "sha1=" + hmac.new(secret.encode(), body, hashlib.sha1).hexdigest()


def test_onebot_cq_strings_and_segments_never_reach_the_model_raw():
    from ai.channels.onebot11 import parse_cq_string, parse_event
    segs = parse_cq_string("哈哈[CQ:face,id=14]你看[CQ:image,file=abc.jpg,url=https://x/y?a=1&amp;b=2]&#91;不是码&#93;")
    assert [s["type"] for s in segs] == ["text", "face", "text", "image", "text"]
    assert segs[3]["data"]["url"] == "https://x/y?a=1&b=2" and segs[4]["data"]["text"] == "[不是码]"
    event = {"post_type": "message", "message_type": "private", "user_id": 777, "self_id": 10001, "message_id": 5,
             "message": "早[CQ:face,id=14][CQ:face,id=99999][CQ:json,data={\"app\":\"x\"}]", "sender": {"nickname": "小满"}}
    parsed = parse_event(event, "qq")
    assert parsed.peer_id == "777" and len(parsed.parts) == 1
    text = parsed.parts[0].text
    assert text.startswith("早[表情：微笑][QQ表情]") and "CQ:" not in text and "app" not in text
    group = {**event, "message_type": "group", "group_id": 1}
    assert parse_event(group, "qq") is None
    array = {**event, "message": [{"type": "text", "data": {"text": "在吗"}}, {"type": "image", "data": {"url": "https://a/b"}}]}
    assert [p.kind for p in parse_event(array, "qq").parts] == ["text", "image"]


def test_onebot_webhook_needs_a_valid_signature_and_a_local_sender(tmp_path):
    from ai.channels.onebot11 import OneBotChannel
    got = []
    adapter = OneBotChannel("qq", {"api_base": "http://127.0.0.1:9"}, {"webhook_secret": "s" * 24}, data_dir=tmp_path)
    adapter._sink = lambda m: got.append(m) or True
    event = {"post_type": "message", "message_type": "private", "user_id": 777, "self_id": 1, "message_id": 9,
             "message": [{"type": "text", "data": {"text": "你好"}}]}
    body, sig = _signed("s" * 24, event)
    assert adapter.handle_webhook(body, None, "127.0.0.1")[0] == 401
    assert adapter.handle_webhook(body, "sha1=00", "127.0.0.1")[0] == 401
    assert adapter.handle_webhook(body, sig, "203.0.113.5")[0] == 403
    assert adapter.handle_webhook(b"not json", _signed("s" * 24, {})[1], "127.0.0.1")[0] == 401
    assert adapter.handle_webhook(body, sig, "127.0.0.1") == (204, "ok") and got[0].parts[0].text == "你好"
    no_secret = OneBotChannel("qq", {}, {}, data_dir=tmp_path)
    assert no_secret.handle_webhook(body, sig, "127.0.0.1")[0] == 401           # no secret configured: refuse


def test_onebot_sends_private_messages_with_the_access_token(tmp_path):
    from ai.channels.base import NeedsLogin
    from ai.channels.onebot11 import OneBotChannel
    fake = FakeOneBot(access_token="tok-123456")
    try:
        adapter = OneBotChannel("qq", {"api_base": fake.url}, {"access_token": "tok-123456", "webhook_secret": "x" * 20},
                                data_dir=tmp_path)
        adapter.prepare()
        adapter.send_text("777", "你好呀")
        assert fake.sent[0]["user_id"] == 777 and fake.sent_texts == ["你好呀"]
        wrong = OneBotChannel("qq", {"api_base": fake.url}, {"access_token": "nope"}, data_dir=tmp_path)
        with pytest.raises(NeedsLogin):
            wrong.prepare()
    finally:
        fake.close()


# ------------------------------------------------------------------------------------------------ supervisor

def test_an_adapter_loop_survives_errors_and_reports_them(tmp_path):
    from ai.channels.base import ChannelAdapter

    class Flaky(ChannelAdapter):
        channel_type = "telegram"

        def __init__(self):
            super().__init__("flaky", sleep=lambda s: time.sleep(0.01))
            self.calls = 0

        def receive_once(self):
            self.calls += 1
            if self.calls <= 2:
                raise ConnectionError(f"down {TOKEN}")
            return 0

        def idle_delay(self):
            return 0.01

        def send_text(self, peer_id, text, client_id=None):
            return {}

    adapter = Flaky()
    adapter.start(lambda m: True)
    deadline = time.monotonic() + 5
    while adapter.calls < 4 and time.monotonic() < deadline:
        time.sleep(0.01)
    status = adapter.status().to_dict()
    adapter.stop()
    assert adapter.calls >= 4 and status["state"] == "connected" and status["restarts"] == 2
    assert TOKEN not in (status["last_error"] or "") and "ConnectionError" in status["last_error"]
    assert adapter.status().state == "stopped"
