"""Phase 8.9.2: what the 8.9.1 real run (local/p891/real-r2-20261020-094248) showed, as regressions on the replies
it actually produced. Offline: these prove what Violet is told next to the user's message and what is sent --
not that a real model now replies better."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from ai import interaction as ia
from ai.chat_output import normalize_chat_output, strip_stray_brackets
from tests.test_consistency_p891 import seed_episode  # noqa: F401
from tests.test_life_relationship_p89 import START, add_message, clock, section  # noqa: F401

# Violet's real replies in the 8.9.1 run (one list per turn; each line was one WeChat message)
E1 = {1: ["哦～看了什么电影呀，好看吗？"],
      2: ["那种“还行，但再剪二十分钟会更好”的电影吧（笑）", "看的是什么片呀？"],
      3: ["那就是普通好看，没特别惊艳～"]}
E4 = {3: ["哼，被你抓个正着了……你观察得还挺仔细嘛小满"],
      4: ["想了呀，不然怎么一被你逮住就老实了嘛", "小满今天有没有偷偷想我一点？"],
      5: ["当然不止呀，想你了好几次，只是刚才不好意思说得太直白嘛", "现在补上：很想你，满意了吗？"],
      6: ["那我说点今天的小事给你听。上午我给窗台的绿萝浇了水，它的叶子亮亮的，像刚洗完澡一样",
          "下午又把歌单重新整理了一遍，结果整理着整理着就开始听歌发呆，书还是没看几页……我是不是没救了呀",
          "你今天辛苦了，先什么都别想，听我絮叨一会儿就好"]}


def flat(turns, upto):
    return [line for n in sorted(turns) if n <= upto for line in turns[n]]


# ============================================================================== 1. the question she just asked

def test_e1_the_unanswered_question_is_shown_once_not_asked_again():
    note = ia.habit_note(flat(E1, 1), "还行吧 就是有点长", last_turn=E1[1])           # step 2
    assert "你上一条问了“哦～看了什么电影呀，好看吗？”" in note and "别换个说法再问一遍" in note
    note = ia.habit_note(flat(E1, 2), "嗯", last_turn=E1[2])                          # step 3
    assert "你上一条问了“看的是什么片呀？”" in note
    assert "用问句结尾" not in note                                                   # one nudge about asking, not two


@pytest.mark.parametrize("text,last", [
    ("你看过吗？", E1[1]),                                  # the user asks: answer as usual
    ("你有什么想问我的吗", E1[1]),                          # the user invites a question
    ("我去洗澡了", E1[1]),                                  # leaving has its own note
    ("还行吧 就是有点长", ["那种电影最适合放空了"]),         # nothing was asked
    ("今天被导师当众批评了 好委屈 一直憋着没哭出来", E1[1]),  # a hard moment: no notes at all
])
def test_no_question_line_where_it_does_not_apply(text, last):
    note = ia.habit_note(last, text, last_turn=last) or ""
    assert "你上一条问了" not in note


def test_the_ack_note_no_longer_invites_a_verdict():
    note = ia.turn_note("嗯", None)
    assert "不用追着刚才的话题问" in note and "不用替对方把感受或看法说出来" in note


# ============================================================================== 2. affection

def test_e4_first_affection_is_free():
    assert ia.habit_note(flat(E4, 3), "想我没", last_turn=E4[3]) is None or \
        "亲近" not in ia.habit_note(flat(E4, 3), "想我没", last_turn=E4[3])        # step 4: say it if she wants


def test_e4_teasing_for_more_is_not_a_reason_to_escalate():
    note = ia.habit_note(flat(E4, 4), "就这？", last_turn=E4[4])                      # step 5
    # phase 8.9.3: warmer is welcome; the same line said again, heavier, is not
    assert "你上一条已经表达过亲近了（“想我”）" in note and "别把刚才那句原样加重再说一遍" in note


@pytest.mark.parametrize("step,text", [(6, "好啦 今天有点累 想听你说说话"), (8, "你说话挺可爱的")])
def test_e4_affection_already_shown_is_counted(step, text):
    replies = flat(E4, step - 1) if step == 6 else flat(E4, 6)
    note = ia.habit_note(replies, text, last_turn=E4[min(step - 1, 6)])
    assert "已经撒娇、说想念或用亲昵的动作和表情 3 次了" in note and "别重复同一句" in note   # 8.9.3: variety, not less


def test_one_affectionate_line_is_not_a_pile():
    assert ia.habit_note(["想你了呀"], "哈哈", last_turn=["想你了呀"]) is None


def test_kaomoji_and_gestures_count_as_affection():
    assert ia.reply_habits(["被你这么一夸，我要偷偷开心好一会儿了૮₍˶ᵔ ᵕ ᵔ˶₎", "给你揉揉脑袋嘛"])["affection"] == 2
    assert ia.reply_habits(["今天好想吃火锅", "很想去那家书店看看", "嗯嗯"])["affection"] == 0   # wanting things is not it


def test_a_more_please_without_earlier_affection_gets_nothing():
    assert ia.habit_note(["今天读了几页书"], "就这？", last_turn=["今天读了几页书"]) is None


def test_name_used_again_and_again():
    note = ia.habit_note(["好呀小满", "小满你看", "嗯嗯"], "哈哈", last_turn=["嗯嗯"], name="小满")
    assert "叫了对方的名字 2 次" in note
    assert ia.habit_note(["好呀小满", "嗯嗯"], "哈哈", last_turn=["嗯嗯"], name="小满") is None


@pytest.mark.parametrize("line,asked", [("给你揉揉脑袋嘛", False), ("也不错呢", False), ("好看吗", True),
                                        ("看的是什么片呀？", True), ("我是不是没救了呀", False),
                                        ("现在补上：很想你，满意了吗？", False),                 # E4 step 5: teasing
                                        ("但我只是觉得下雨天听着很舒服啦，不用想太多，是不是？", False)])  # E5 step 7
def test_only_real_questions_are_pending(line, asked):
    assert (ia.pending_question([line]) is not None) is asked


@pytest.mark.parametrize("text", ["好啦 今天有点累 想听你说说话", "你推荐一首我听听"])     # E4 step 6, E5 step 8
def test_a_request_to_eve_is_not_an_answer(text):
    note = ia.habit_note(["你看过吗？"], text, last_turn=["你看过吗？"]) or ""
    assert "你上一条问了" not in note


def test_idle_talk_gets_no_get_to_know_offer(violetdb, clock):
    from ai.context_engine import ContextEngine
    seed_episode(violetdb, clock, "E5")
    packet = ContextEngine(violetdb).build("chat", "好无聊")
    assert "【可选的了解】" not in "\n".join(packet.sections) and "对方把话题交给你了" in packet.turn_note
    assert violetdb.fetch_all("SELECT * FROM profile_questions") == []                  # nothing recorded as offered


# ============================================================================== 3. her own news when the topic is hers

@pytest.mark.parametrize("text,news,expected", [
    ("随便聊点什么吧", True, True), ("好无聊", True, True),
    ("随便聊点什么吧", False, False),                       # nothing of hers left to tell: no push
    ("在干嘛呢", True, False), ("在吗", True, False),        # greetings ask about now, not for a topic
])
def test_idle_talk_hands_the_topic_to_her_news(text, news, expected):
    note = ia.turn_note(text, None, has_own_news=news) or ""
    assert ("对方把话题交给你了" in note) is expected
    assert "不用出题考对方" in note or not expected


def test_the_no_questions_preference_still_wins_on_idle_talk():
    assert "不喜欢被追问" in ia.turn_note("随便聊点什么吧", "no", has_own_news=True)


# ============================================================================== 4. stray brackets

@pytest.mark.parametrize("raw,sent", [
    ("那推荐你听德彪西的《梦》吧，适合发呆的时候听听看】【。", "那推荐你听德彪西的《梦》吧，适合发呆的时候听听看。"),   # 8.9.1 E5
    ("好嘛，我记住了，不老追着你问。刚才是我话多了，烦人精是我】【。", "好嘛，我记住了，不老追着你问。刚才是我话多了，烦人精是我。"),
    ("【通知】明天见", "【通知】明天见"), ("【A】【B】都行", "【A】【B】都行"), ("看【】这里", "看这里"),
    ("哈哈【", "哈哈"), ("】开头", "开头"), ("嵌套【外【内】外】好", "嵌套【外【内】外】好"),
    ("普通的一句话，没有括号～", "普通的一句话，没有括号～"),
])
def test_only_brackets_that_enclose_nothing_go(raw, sent):
    assert strip_stray_brackets(raw) == sent


def test_a_line_of_nothing_but_brackets_is_not_sent():
    out = normalize_chat_output("】【\n好呀")
    assert [m["content"] for m in out["messages"]] == ["好呀"]
    assert normalize_chat_output("】【")["should_send"] is False


# ============================================================================== 5. in the real context

def test_e1_context_carries_the_question_line(violetdb, clock):
    from ai.context_engine import ContextEngine
    episode = seed_episode(violetdb, clock, "E1")
    at = datetime.fromisoformat(episode["start"])
    add_message(violetdb, "user", "今天去看了场电影", at)
    for line in E1[1]:
        add_message(violetdb, "assistant", line, at + timedelta(seconds=30))
    clock.set(at + timedelta(minutes=1))
    packet = ContextEngine(violetdb).build("chat", "还行吧 就是有点长")
    assert "你上一条问了“哦～看了什么电影呀，好看吗？”" in packet.turn_note


def test_e5_rain_from_two_days_ago_is_dated(violetdb, clock):
    from ai.context_engine import ContextEngine
    episode = seed_episode(violetdb, clock, "E5")
    marks = [h["content"] for h in ContextEngine(violetdb).build("chat", "好无聊").history if h["role"] == "system"]
    assert "之前的话题不一定还在继续；那时说的天气、心情和在做的事，是那时候的" in marks[-1]
    at = datetime.fromisoformat(episode["start"])
    add_message(violetdb, "user", "好无聊", at)
    add_message(violetdb, "assistant", "那我陪你找点不无聊的事嘛", at + timedelta(seconds=20))
    clock.set(at + timedelta(minutes=1))
    packet = ContextEngine(violetdb).build("chat", "随便聊点什么吧")
    marks = [h["content"] for h in packet.history if h["role"] == "system"]
    assert marks[-1].endswith("的对话；在这之前说的天气、心情和在做的事，是那时候的】")
    assert sum("是那时候的" in m for m in marks) == 1                                 # said once
    assert "对方把话题交给你了" in packet.turn_note                                   # her news is unsaid so far


def test_e4_context_names_the_affection_before_it_piles_up(violetdb, clock):
    from ai.context_engine import ContextEngine
    episode = seed_episode(violetdb, clock, "E4")
    at = datetime.fromisoformat(episode["start"])
    for n, user in ((3, "哈哈哈 被我说中了吧"), (4, "想我没")):
        add_message(violetdb, "user", user, at)
        for line in E4[n]:
            add_message(violetdb, "assistant", line, at + timedelta(seconds=30))
        at += timedelta(minutes=1)
    clock.set(at)
    note = ContextEngine(violetdb).build("chat", "就这？").turn_note
    assert "对方在逗你、想听更多" in note and "叫了对方的名字 2 次" in note
