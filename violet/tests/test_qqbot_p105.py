"""Phase 10.5: the QQ official bot channel (AppID + AppSecret, official API over WebSocket) against a loopback fake
of the platform, plus a contract test every channel type must pass. No network, fake model."""

from __future__ import annotations

import functools
import json
import os
import time

import pytest

from tests.fake_qqbot import APP_ID, APP_SECRET, FakeQQBot
from tests.fakes import wait_until
from tests.test_channels_p10_runtime import world  # noqa: F401  (fixture)


@pytest.fixture()
def qq():
    fake = FakeQQBot()
    yield fake
    fake.close()


def _factory(fake):
    from ai.channels.qqbot import QQBotChannel
    return functools.partial(QQBotChannel, token_url=fake.token_url, api_base=fake.api_base)


def _start(world, qq, secret=APP_SECRET):  # noqa: F811
    world.mgr._factory_overrides["qqbot"] = _factory(qq)
    world.rt.start(poll=False)
    world.mgr.start_enabled()
    world.mgr.add("qqbot", "qq", config={"app_id": APP_ID}, secrets={"app_secret": secret})
    return world.mgr.get("qq")


def _bind(world, qq, openid="OPENID_OWNER"):  # noqa: F811
    code = world.rt.identities.create_pairing_code("qq")["code"]
    qq.push_c2c(f"/bind {code}", openid=openid)
    assert wait_until(lambda: any("绑定好了" in t for t in qq.sent_texts))


# ------------------------------------------------------------------------------------------------ parsing

def test_qq_face_markup_mentions_and_attachments_never_reach_the_model_raw():
    import base64
    from ai.channels.normalize import render_parts
    from ai.channels.qqbot import parse_c2c
    ext = base64.b64encode(json.dumps({"text": "[微笑]"}).encode()).decode().rstrip("=")
    event = {"id": "M1", "author": {"user_openid": "U1"},
             "content": f'<@!BOT> 你好<faceType=1,faceId="14",ext="{ext}">今天',
             "attachments": [{"content_type": "voice", "url": "//x/v.amr", "asr_refer_text": "明天八点"},
                             {"content_type": "image/jpeg", "url": "https://x/a.jpg"},
                             {"content_type": "application/pdf", "filename": "课表.pdf"}]}
    message = parse_c2c(event, "qq")
    text = render_parts(message.parts)
    assert "faceType" not in text and "<@" not in text and "微笑" in text
    assert "明天八点" in text and "课表.pdf" in text
    assert message.dedup_key == "qqbot:qq:M1" and message.peer_id == "U1"
    assert any(p.kind == "image" and p.data["url"] == "https://x/a.jpg" for p in message.parts)


# ------------------------------------------------------------------------------------------------ live with the fake platform

def test_qq_official_bot_binds_the_owner_and_replies_passively_in_order(world, qq):  # noqa: F811
    adapter = _start(world, qq)
    assert wait_until(lambda: qq.identifies)
    identify = qq.identifies[0]
    assert identify["intents"] == 1 << 25 and identify["token"] == "QQBot token-1"
    qq.push_c2c("你好")                                            # a stranger first: no answer, nothing stored
    time.sleep(0.4)
    assert qq.sent == [] and world.llm.calls == []
    _bind(world, qq)
    inbound = qq.push_c2c("在吗")
    assert wait_until(lambda: qq.sent_texts[-2:] == ["好呀", "我在听"])
    replies = [m for m in qq.sent if m.get("msg_id") == inbound]
    assert [m["msg_seq"] for m in replies] == [1, 2] and all(m["openid"] == "OPENID_OWNER" for m in replies)
    chat = world.llm.calls_for(os.environ.get("CHAT_LLM_MODEL", "fake-chat"))[-1]
    note = [m["content"] for m in chat["messages"] if m["role"] == "system" and m["content"].startswith("【这次聊天的地方】")]
    assert note and "QQ" in note[0]
    assert adapter.status().state == "connected" and qq.heartbeats >= 0


def test_a_dropped_gateway_is_resumed_and_messages_keep_flowing(world, qq):  # noqa: F811
    _start(world, qq)
    _bind(world, qq)
    assert wait_until(lambda: qq.heartbeats >= 1, timeout=5)        # heartbeats with the last sequence
    qq.drop_connection()
    assert wait_until(lambda: qq.resumes, timeout=15)
    resume = qq.resumes[0]
    assert resume["session_id"] == "fake-session-1" and isinstance(resume["seq"], int) and resume["seq"] > 0
    qq.push_c2c("我回来了")
    assert wait_until(lambda: "我在听" in qq.sent_texts[-1:], timeout=10)
    assert world.mgr.get("qq").status().restarts >= 1


def test_an_expired_token_is_renewed_once_and_the_send_succeeds(world, qq):  # noqa: F811
    _start(world, qq)
    _bind(world, qq)
    qq.expire_token_once = True
    qq.push_c2c("再说一句")
    assert wait_until(lambda: qq.sent_texts[-2:] == ["好呀", "我在听"])
    assert qq.tokens_issued >= 2


def test_a_retry_of_the_same_outbox_row_is_never_delivered_twice(world, qq):  # noqa: F811
    adapter = _start(world, qq)
    _bind(world, qq)
    qq.push_c2c("在吗")
    assert wait_until(lambda: "我在听" in qq.sent_texts)
    before = len(qq.sent)
    first = adapter.send_text("OPENID_OWNER", "同一条", client_id="pybot-1")
    again = adapter.send_text("OPENID_OWNER", "同一条", client_id="pybot-1")      # e.g. after a timeout
    assert first["ok"] and again["ok"] and again.get("duplicate") and len(qq.sent) == before + 1


def test_after_the_reply_window_an_active_message_is_refused_with_a_plain_reason(world, qq):  # noqa: F811
    adapter = _start(world, qq)
    _bind(world, qq)
    qq.push_c2c("晚安")
    assert wait_until(lambda: "我在听" in qq.sent_texts)
    adapter._peers["OPENID_OWNER"]["at"] = time.time() - 2 * 3600     # the last message is two hours old
    qq.reject_active = True
    report = world.rt.send_proactive(__import__("ai.channels.outbound", fromlist=["fixed_text"]).fixed_text("想你了"))
    assert report["failed_count"] == 1 and "主动消息" in report["error"]
    row = world.db.fetch_one("SELECT status, error FROM outbound_messages WHERE content = '想你了'")
    assert row["status"] == "failed" and "主动消息" in row["error"]


def test_wrong_credentials_wait_for_the_admin_and_group_messages_are_ignored(world, qq):  # noqa: F811
    adapter = _start(world, qq, secret="WrongSecret12345678")
    assert wait_until(lambda: adapter.status().state == "needs_login", timeout=5)
    assert "AppSecret" in adapter.status().detail and APP_SECRET not in json.dumps(adapter.status().to_dict())
    world.mgr.update("qq", secrets={"app_secret": APP_SECRET})
    assert wait_until(lambda: qq.identifies, timeout=10)
    qq.push_group_at("<@!BOT> 大家好")
    time.sleep(0.4)
    assert world.db.fetch_all("SELECT * FROM inbound_messages WHERE channel = 'qq'") == []


# ------------------------------------------------------------------------------------------------ the adapter contract

@pytest.mark.parametrize("kind", ["qqbot", "telegram", "onebot11"])
def test_every_channel_type_follows_the_adapter_contract(kind, tmp_path):
    from ai.channels.base import ChannelAdapter
    from ai.channels.context import LABELS
    from ai.channels.manager import ValidationProblems, channel_types
    spec = channel_types()[kind]
    cls = spec["class"]
    assert issubclass(cls, ChannelAdapter) and cls.channel_type == kind and kind in LABELS
    assert cls.capabilities <= {"text", "image_in", "image_out", "typing", "voice_text"} and "text" in cls.capabilities
    for method in ("receive_once", "send_text", "probe", "download"):
        assert callable(getattr(cls, method, None)), method
    assert spec["help"] and set(spec["defaults"]) <= set(spec["config"])
    # missing required credentials are reported per field, nothing raised elsewhere
    from ai.channels.manager import ChannelManager

    class RT:
        db = None
    mgr = ChannelManager.__new__(ChannelManager)
    required = [n for n, (req, _c) in spec["secrets"].items() if req and n != "webhook_secret"]
    with pytest.raises(ValidationProblems) as info:
        mgr.validate(kind, {}, {})
        if not required and not spec.get("required_config"):
            raise ValidationProblems({"_": "nothing required"})
    problems = info.value.problems
    assert all(f"secret:{n}" in problems for n in required)
    assert all(k in problems for k in spec.get("required_config") or [])
