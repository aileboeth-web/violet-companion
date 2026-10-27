"""Phase 8.8: what kind of turn this is, read from the user's own words -- one place for the signals that
decide whether Violet answers, asks, shares her own day or stays with the user.

Deterministic and pure (no model, no database except the preference read). Nothing here writes a reply:
the signals only choose which context Violet gets and whether one rewrite is asked for.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from ai.conversation_state import ASK_ABOUT_VIOLET_RE
from ai.onboarding import DISTRESS_RE, QUESTION_RE

# The user hands the asking over to Violet.
INVITE_RE = re.compile(r"(有什么想问|有啥想问|想问我什么|想问我啥|你问吧|问我吧|你可以问|随便问|想知道我什么|想了解我什么|你想知道什么)")
# Greetings and open small talk: nothing of the user's to attend to; a natural place for Violet's own news.
# phase 8.9.8: plain greetings too ("早呀" the next morning: Violet's day and what was agreed the night before belong there)
OPEN_RE = re.compile(r"^(在吗|在不在|在嘛|在么|你在吗|在干嘛呢?|干嘛呢|在干什么呢?|在做什么呢?|忙什么呢?|忙啥呢?|"
                     r"早|早安|早上好|中午好|下午好|晚上好|晚好|你好|嗨|哈喽|hi|hello|"
                     r"好?无聊(死了)?|随便聊(点|聊)?.*|聊点什么.*|陪我聊.*|想听你.*|说点什么.*|你说.{0,6}吧)"
                     r"[呀啊呢嘛吧～~!！。.…]*$")
# "Do you remember / know ... about me": the user asks what Violet knows of them.
SELF_RECALL_RE = re.compile(r"(还记得|记不记得|记得吗|你知道|忘了|忘记).{0,12}我|我.{0,12}(来着|还记得吗|记得吗|你知道吗)")
# A trailing question in Violet's reply (rhetorical ones included: a rewrite is cheap, a missed one is not).
REPLY_QUESTION_END = re.compile(r"([?？]|[吗呢嘛][～~。!！…]*)\s*$")
FOCUS_MIN_CHARS = 30


# Questions without a final particle: A-not-A ("是不是都很自律") and asking for Violet's opinion ("你觉得…").
_ASKING_RE = re.compile(r"(是不是|有没有|要不要|好不好|能不能|会不会|对不对|可不可以|行不行|该不该|你觉得|你认为)")


def is_question(text: str | None) -> bool:
    text = (text or "").strip()
    if not (QUESTION_RE.search(text) or _ASKING_RE.search(text)):
        return False
    # phase 8.9.8: a statement that only ends in 呢 ("没有哦，我也在找呢", "还在上课呢") is not a question
    return not statement_ne(text)


# Phase 8.9.8 (8.9.7 diagnosis of the real free chat local/free_chat/cys-close-1): Violet's questions came without a
# question mark ("今天摸的哪门课", "你是喜欢看哪种类型的", "你这两天怎么样呀", "有没有哪门能偷偷打个盹哈哈"), so the
# notes about her questions fired once in eight turns. A line asks when its last clause carries a question word or an
# A-not-A form -- unless the word is used indefinitely ("你看哪个顺眼就一起看", "什么都行"), inside a statement
# ("我也不知道为什么") or rhetorically ("不摸鱼怎么活", "哪有"). Trailing laughter / emoji do not hide a question.
QUESTION_WORD = (r"(怎么样|怎么|咋样|咋|干嘛|干啥|什么|啥|哪(个|种|门|些|里|儿|样|天|本|首|部|家|位)?|谁|"
                 r"几(点|个|天|次|号|岁|本)|多少|多久|多大|为什么|为啥|有没有|是不是|要不要|会不会|能不能|可不可以|"
                 r"想不想|喜不喜欢|好不好|对不对|行不行|去不去)")
_QUESTION_WORD_RE = re.compile(QUESTION_WORD)
_A_NOT_A_RE = re.compile(r"([一-鿿])不\1")
_TRAILING_RE = re.compile(r"([\s～~。.!！…]|哈|嘿|嘻|hh|[\U0001F300-\U0001FAFF☀-➿])+$")
_INDEFINITE_RE = re.compile(QUESTION_WORD + r"[^，,。！!？?]{0,10}(就|都|也)")
# phase 8.9.11: thinking aloud is not asking -- "正想着要不要去泡杯什么喝" (real ab-v2c) counted as a question
_EMBEDDED_RE = re.compile(r"(知道|记得|清楚|明白|确定|想不起来?|说不上来?|说不清|想想|看看|问问|猜猜|研究|"
                          r"想着|在想|纠结|犹豫|琢磨|考虑)[^，,。！!？?]{0,6}" + QUESTION_WORD)
_RHETORICAL_WH_RE = re.compile(r"(不|没)[^，,。！!？?]{0,4}(怎么|咋)[^，,。！!？?]{0,2}$|哪有|谁不|怎么可能|有什么好|何必")
# "你呢", "那你呢", "你那边呢", "然后呢", "还有呢" -- asking back; "我也在找呢" / "我好累呢" are not
_NE_TOPIC_RE = re.compile(r"^(那|那么)?(你|您|你们|他|她|他们|然后|还有|其他|别的|这个|那个|后来)[^在着也还正]{0,3}呢$")


def _last_clause(line: str) -> str:
    line = _TRAILING_RE.sub("", (line or "").strip())
    return re.split(r"[，,。；;！!]", line)[-1].strip() if line else ""


def statement_ne(text: str | None) -> bool:
    """A line that only ends in 呢 as a statement ("我也在找呢", "还在上课呢") -- not "你呢", "然后呢", "在干嘛呢"."""
    clause = _last_clause(text)
    if not clause.endswith("呢") or re.search(r"[?？]\s*$", (text or "").strip()):
        return False
    if _QUESTION_WORD_RE.search(clause) or _A_NOT_A_RE.search(clause):
        return bool(_INDEFINITE_RE.search(clause) or _EMBEDDED_RE.search(clause))
    return not _NE_TOPIC_RE.match(clause)


def asks(line: str | None) -> bool:
    """Phase 8.9.8: Violet's reply line ends by asking the user something (with or without a question mark)."""
    raw = (line or "").strip()
    clause = _last_clause(raw)
    if not clause:
        return False
    if re.search(r"[?？]", raw[-3:]) or re.search(r"(吗|(?<![什怎这那多])么)$", clause):
        return not _RHETORICAL_WH_RE.search(clause)
    if clause.endswith("呢"):
        return not statement_ne(clause)
    if re.search(r"了没有?[呀啊呢嘛]?$", clause):            # "下课了没呀", "吃了没" (8.9.7 real proactive)
        return True
    if re.search(r"(吧|啦|了)$", clause):                  # a suggestion or a statement ("周末一起找吧")
        return False
    has_word = bool(_QUESTION_WORD_RE.search(clause) or _A_NOT_A_RE.search(clause))
    if not has_word or _INDEFINITE_RE.search(clause) or _EMBEDDED_RE.search(clause) or _RHETORICAL_WH_RE.search(clause):
        return False
    # about herself, without ？ / 吗: self-talk ("我是不是没救了呀", 8.9.2), not a question for the user to answer
    return not (clause.startswith("我") and not clause.startswith("我们") and "你" not in clause)


# Phase 8.9: the user is leaving or going to sleep ("我去洗澡了", "那我去忙啦", "不说了 练琴去", "晚安").
CLOSING_RE = re.compile(r"(晚安|拜拜|再见|回头聊|明天聊|改天聊|先这样|不说了|不聊了|先撤|先下了|"
                        r"(先|要|得)?(去)?睡(了|啦|觉了|觉去)|困(了|死了)|好困|"
                        r"(我|那我|先)?(先|要|得)?去(忙|洗澡|洗漱|上课|吃饭|做饭|上班|开会|写作业|练琴|收拾|跑步|健身|学习)"
                        r"(了|啦|咯|喽|吧)?|练琴去|忙去了)")
# Phase 8.9.12 (real ab-v2d): stepping back so as not to disturb her ("好啦，那我不打扰你看书了", "你先忙吧", "不吵你了")
# is consideration, not the user leaving -- Intent read it as "准备结束这轮对话" and Violet said goodbye. Leaving
# ("我去睡了", "晚安") stays "closing" and wins when both are said.
YIELD_RE = re.compile(r"(不打扰你|不打扰了|不吵你|不烦你|不耽误你|你先忙|你忙吧|你去忙吧|你忙你的|你先去忙|"
                      r"你去看书吧|你先看书|你继续看|你继续忙|你看你的|你接着看|你接着忙|你先休息|你歇着吧)")
# A bare acknowledgement: nothing new to answer.
ACK_RE = re.compile(r"^(嗯+|嗯呢|嗯哼|哦+|噢+|喔+|好+|好的|好吧|行吧?|ok|okay|OK|en|emm+|额|呃)[。.!！~～…]*$")


def turn_kind(text: str | None) -> str:
    """invite (asks Violet to ask) | open (greeting, idle small talk) | question | closing (leaving, going to sleep)
    | yield (stepping back so as not to disturb her, phase 8.9.12) | ack (a bare "嗯") | share (anything else)."""
    text = (text or "").strip()
    if INVITE_RE.search(text):
        return "invite"
    if OPEN_RE.match(text):
        return "open"
    if is_question(text):
        return "question"
    if CLOSING_RE.search(text):
        return "closing"
    if YIELD_RE.search(text):
        return "yield"
    if ACK_RE.match(text):
        return "ack"
    return "share"


def needs_focus(text: str | None, route: str | None = None) -> bool:
    """The user is going through something (distress), asked for a task, or told something long: Violet stays
    with them. A question alone is not such a moment ("在吗", "干嘛呢")."""
    text = (text or "").strip()
    return route == "task" or len(text) >= FOCUS_MIN_CHARS or bool(DISTRESS_RE.search(text))


def asks_about_eve(text: str | None) -> bool:
    """Questions about Violet herself ("你也去爬过山吗", "你喜欢…", "你觉得…") and open small talk -- where her
    recorded life is the honest basis of the answer. A question about the user ("你还记得我…") is not."""
    text = (text or "").strip()
    if ASK_ABOUT_VIOLET_RE.search(text) or turn_kind(text) == "open":
        return True
    return is_question(text) and "你" in text and "我" not in text


def is_self_recall(text: str | None) -> bool:
    return bool(SELF_RECALL_RE.search(text or ""))


def follow_up_preference(preferences: dict[str, Any]) -> str | None:
    """'no' or 'less' when the user asked not to be questioned (an inferred 'less' is too weak to act on)."""
    pref = preferences.get("follow_up_questions") or {}
    if pref.get("value") == "no" or (pref.get("value") == "less" and pref.get("origin") == "explicit"):
        return pref["value"]
    return None


FOLLOW_UP_KINDS = ("share", "open", "ack", "closing")


# Phase 8.9.4: the user is back ("我回来啦", "忙完了", "到家了", "出差回来了") -- a moment to be glad, not to interview.
RETURN_RE = re.compile(r"^(我|终于)?(又)?(回来(了|啦|咯|喽)|忙完(了|啦)|到家(了|啦)|下班(了|啦)|出差回来(了|啦)|"
                       r"开完(会)?(了|啦)|考完(了|啦))[呀啊～~!！。.…]*$|"
                       # phase 8.9.5: after a long absence (8.9.4 real A3 "在吗 好久没聊了" got "最近还好吗？")
                       r"^(在吗|在不在)?[\s，,]*(好久没聊(了|啦)?|好久不见(了|啦)?|我又来(了|啦))[呀啊～~!！。.…]*$")
# Phase 8.9.2: idle small talk that hands the topic to Violet ("好无聊", "随便聊点什么吧") -- not a greeting or
# "在干嘛呢", which ask about her now.
IDLE_RE = re.compile(r"^(好?无聊(死了)?|随便聊(点|聊)?.*|聊点什么.*|陪我聊.*|说点什么.*|你说.{0,6}吧)[呀啊呢嘛吧～~!！。.…]*$")


def turn_note(text: str | None, follow_up: str | None, previous_user: str | None = None,
              has_own_news: bool = False, stage: str | None = None) -> str | None:
    """A short note right before the user's message, only where a signal applies to THIS turn.

    Phase 8.9: no wording to copy (8.8's "真正好奇" came back verbatim in a real reply); the user's answer to a
    question they invited may get one interested follow-up; leaving and a bare "嗯" have their own note.
    Phase 8.9.3: once you are close (or getting there), leaving may get a little reluctance -- still short,
    still going along with the user's plans; never holding them back or making them feel bad."""
    kind = turn_kind(text)
    if kind == "invite":
        return "【这一句】对方请你问问题：挑一件你对对方好奇的事，问一句就好。"
    if kind == "closing":
        if stage in ("warming", "close"):
            return ("【这一句】对方要去忙或去休息了：回一句短的就好，可以带一点舍不得或期待下次，"
                    "但顺着对方的安排，不挽留、不提问，也不用再叮嘱什么。")
        return "【这一句】对方要去忙或去休息了：回一句短的就好，不提问，也不用再叮嘱什么。"
    if kind == "yield":
        if stage in ("warming", "close"):
            return ("【这一句】对方是怕打扰你才说先不聊了，不是对方自己要走。你要是还想聊，可以直接说手上的事不急、"
                    "更想跟对方多待一会儿，或者撒个娇把对方留下；要是你真的想去做自己的事，也可以开开心心地放对方走。"
                    "对方接着说要去忙、要睡了，就顺着对方。")
        return ("【这一句】对方是怕打扰你才说先不聊了，不是对方自己要走：你不忙的话，说一句没关系、接着聊就好，"
                "不用强留。")
    if follow_up and answers_invited_question(previous_user):
        return None
    if stage in ("warming", "close") and not follow_up and RETURN_RE.match((text or "").strip()):
        # phase 8.9.4: coming back was answered with a routine "辛苦了～这趟还顺利吗？" (8.9.3 real E7)
        return ("【这一句】对方回来找你了：开心一点地接上就好，可以撒个娇或说说你这边的小事；"
                "不用机械地问一句“怎么样”。")
    if kind == "ack":
        # phase 8.9.2: the 8.9.1 real E1 answered "嗯" by concluding for the user ("那就是普通好看，没特别惊艳")
        return ("【这一句】对方只回了“" + (text or "").strip()[:6] + "”：回得短一点就行，或者说点你自己想说的；"
                "不用追着刚才的话题问，也不用替对方把感受或看法说出来。")
    if follow_up and kind in FOLLOW_UP_KINDS:
        return ("【这一句】对方说过不喜欢被追问。这是平常的分享：顺着说、说说你的感受或看法就好，不用反问；"
                "只有一件要紧的事必须弄清楚时才问。")
    if kind == "open" and IDLE_RE.match((text or "").strip()) and has_own_news:
        # phase 8.9.2: "随便聊点什么吧" got a quiz about the smell of the rain; her own unsaid news was right there
        return ("【这一句】对方把话题交给你了：从你自己最近的事、喜欢的东西或想法里挑一件说说就好"
                "（生活记录里这次还没说过的）；不用出题考对方，也不用找谁都能聊的通用话题。")
    return None


def waited_note(stage: str | None, hours: float | None, said: str | None = None) -> str | None:
    """Phase 8.9.10: the user answers after Violet wrote on her own and waited for hours. Glad first, never blame, guilt or
    questions about where they were. Phase 8.9.13 (real ab-v2e): when she already TOLD them she was waiting (``said``,
    her second message), that is said and done -- the 8.9.10 note still invited "调侃一句让你等了好久" and the answer
    opened with the wait again ("你可算来了，我早上那条发出去就一直等")."""
    if hours is None or hours < 3 or stage not in ("warming", "close"):
        return None
    if said:
        return (f"【这一句】你主动找过对方，对方隔了好一阵才回。等的事你已经跟对方说过了（“{said.strip()[:30]}”），"
                "说过就翻篇：开心地接上对方这句，亲近、俏皮一点就好，不用再提等了多久，也不追问对方刚才去哪了。")
    if stage == "close":
        return ("【这一句】你主动找过对方，等了好一阵对方才回：先开心地接上对方这句；想的话可以轻轻撒个娇，"
                "不一定要提等了多久，也不埋怨、不让对方愧疚、不追问对方刚才去哪了、为什么没回。")
    return "【这一句】你主动找过对方，对方隔了一阵才回：开心地接上对方这句就好，不埋怨，也不追问为什么没回。"


def intent_note(reading: dict[str, Any] | None) -> str | None:
    """Phase 8.9.8: IntentAgent's short reading of this message, next to it -- a first impression (it can be wrong), not
    an instruction. Only plain text of bounded length is passed on."""
    if not isinstance(reading, dict):
        return None

    def clean(value: Any) -> str:
        text = re.sub(r"[\s【】\[\]{}<>]+", " ", str(value or "")).strip()
        return "" if text.lower() in ("", "neutral", "none", "null", "mock", "replay") else text[:50]
    what, mood = clean(reading.get("user_intent")), clean(reading.get("emotion"))
    if not (what or mood):
        return None
    parts = ([f"对方{what[2:] if what.startswith('用户') else what}"] if what else []) + ([f"语气：{mood}"] if mood else [])
    return "【对这一句的第一感觉（可能不准，以对方原话为准）】" + "；".join(parts)


def answers_invited_question(previous_user: str | None) -> bool:
    """The user's previous message asked Violet to ask: this message is their answer (one follow-up is welcome)."""
    return bool(previous_user) and turn_kind(previous_user) == "invite"


def follow_up_violation(reply: str | None, user_text: str | None, follow_up: str | None,
                        previous_user: str | None = None) -> bool:
    """Violet ended with a question on a plain share although the user asked not to be questioned."""
    if not follow_up or turn_kind(user_text) not in FOLLOW_UP_KINDS or answers_invited_question(previous_user):
        return False
    lines = [l.strip() for l in (reply or "").splitlines() if l.strip()]
    return bool(lines) and asks(lines[-1])


# Phase 8.9: what Violet's own replies in this conversation keep doing. The real 8.8 run showed the chat prompt's
# "不是每句话都需要关心或建议" losing to habit: care and reminders on four of eight turns in one evening.
CARE_RE = re.compile(r"(早点(睡|休息|歇)|好好(休息|睡)|注意(休息|身体|安全|保暖)|别太累|别累着|多穿|记得(吃|喝|穿|带|休息|早点)|"
                     r"暖和(一点|点)|别熬夜|别硬(撑|练)|多歇|歇着吧|睡个好觉|路上小心|别着凉|"
                     # phase 8.9.4: 8.9.3 real E6 "别一忙又忘了喝水" went uncounted
                     r"忘了(喝水|吃饭|吃东西)|喝点水|吃点东西|别饿着|别累坏)")
HABIT_WINDOW = 4
# Phase 8.9.2: affection Violet already showed in this conversation -- missing the user, cuddly gestures, kaomoji.
# The 8.9.1 real E4 went "想了呀 / 有没有偷偷想我" -> "很想你，满意了吗？" -> "૮₍˶ᵔ ᵕ ᵔ˶₎ … 给你揉揉脑袋嘛".
AFFECTION_RE = re.compile(r"(想你|想我|抱抱|揉揉|摸摸|贴贴|亲亲|蹭蹭|么么|撒娇|黏着你|乖乖|宝贝)")
KAOMOJI_RE = re.compile(r"[ʰ-˿઀-૿฀-๿ᴀ-ᶿ₀-₟]|"
                        r"[\U0001F60D\U0001F618\U0001F917\U0001F970\U0001F97A\U0001FAF6]")
# The user teases for more right after Violet was affectionate ("就这？", "还有呢").
MORE_PLEASE_RE = re.compile(r"^(就这(样|些)?|还有呢|然后呢|不够|再说一遍|再来|多说点|就这么点)[?？!！。.~～…]*$")


def _affection(text: str) -> str | None:
    match = AFFECTION_RE.search(text or "") or KAOMOJI_RE.search(text or "")
    return match.group(0) if match else None


def reply_habits(violet_replies: list[str], name: str | None = None, care_replies: list[str] | None = None) -> dict[str, Any]:
    """Counts over Violet's replies of this conversation (oldest first): care, questions and her name use over the
    last few; affection over the whole conversation (it piles up over an evening, not over four messages).
    Phase 8.9.4: care over ``care_replies`` (everything she said today) when given -- on a busy day the user writes
    every few hours, each time a new "conversation", and 喝水 / 别硬撑 / 别累着 came back each time (8.9.3 E6)."""
    replies = [r for r in violet_replies if (r or "").strip()]
    recent = replies[-HABIT_WINDOW:]
    care = [r for r in (recent if care_replies is None else care_replies) if CARE_RE.search(r or "")]
    last3 = recent[-3:]
    questions = sum(1 for r in last3 if asks([l for l in r.splitlines() if l.strip()][-1]))
    affection = [r for r in replies if _affection(r)]
    return {"replies": len(recent), "care": len(care), "care_example": CARE_RE.search(care[-1]).group(0) if care else None,
            "question_endings": questions, "of_last": len(last3),
            "affection": len(affection), "affection_example": _affection(affection[-1]) if affection else None,
            "name": sum(1 for r in recent if name and name in r)}


# A real question (not "给你揉揉脑袋嘛" / "也不错呢"): question mark or 吗 at the end -- and not a teasing tag
# ("…是不是？", "满意了吗？"), which wants no answer.
ASKED_RE = re.compile(r"([?？]|吗)[～~。!！…\s]*$")
RHETORICAL_RE = re.compile(r"(是不是|对吧|对不对|是吧|好不好|满意了吗|行了吧|你说呢)[?？吗～~。!！…\s]*$")


def pending_question(last_turn: list[str]) -> str | None:
    """The question Violet's previous reply ended a line with (the user's message now is the answer to it)."""
    for line in reversed([l.strip() for r in last_turn for l in (r or "").splitlines() if l.strip()]):
        if asks(line) and not RHETORICAL_RE.search(line):         # phase 8.9.8: also without a question mark
            return line[:40]
    return None


def answers_eve(text: str | None) -> bool:
    """The user's message reads as an answer to Violet's question about them: a share or "嗯" that does not turn to
    Violet herself ("你推荐一首我听听", "想听你说说话" are requests, not answers)."""
    text = (text or "").strip()
    return turn_kind(text) in ("share", "ack") and "你" not in text


def habit_note(violet_replies: list[str], text: str | None, route: str | None = None,
               last_turn: list[str] | None = None, name: str | None = None,
               care_replies: list[str] | None = None) -> str | None:
    """Tell Violet what her own recent replies already did, so the next one need not do it again.

    Information about the conversation, not a ban: on a hard moment (distress, a task, a long message) care is
    what the user needs and nothing is said; a question the user asked or invited is answered as usual.
    Phase 8.9.2: the question she just asked (so it is not asked again in other words), affection already shown
    in this conversation, and how often she has used the user's name lately."""
    if needs_focus(text, route):
        return None
    habits = reply_habits(violet_replies, name, care_replies)
    kind = turn_kind(text)
    lines = []
    asked = pending_question(last_turn or []) if answers_eve(text) else None
    if asked:
        lines.append(f"- 你上一条问了“{asked}”，对方这句就是回应：接住对方说了的部分就好；"
                     "没提到的就当对方不想细说，别换个说法再问一遍。")
    if habits["care"]:
        when = "今天" if care_replies is not None else "这次聊天里"
        lines.append(f"- {when}你已经叮嘱、关心过对方 {habits['care']} 次了（比如“{habits['care_example']}”），"
                     "这一句不用再叮嘱。")
    if habits["question_endings"] >= 2 and kind not in ("question", "invite") and not asked:
        lines.append(f"- 你最近 {habits['of_last']} 条里有 {habits['question_endings']} 条是用问句结尾的，这一句可以不问。")
    # Phase 8.9.3: closeness is welcome (Violet is clingy once you are close); what the note guards against is
    # saying the same sweet thing again and piling kaomoji / gestures into one message -- not affection itself
    previous_affection = next((a for a in map(_affection, last_turn or []) if a), None)
    if previous_affection and MORE_PLEASE_RE.match((text or "").strip()):
        lines.append(f"- 你上一条已经表达过亲近了（“{previous_affection}”），对方在逗你、想听更多：可以大方一点、"
                     "更可爱地回应，换个新说法；别把刚才那句原样加重再说一遍。")
    elif habits["affection"] >= 2:
        lines.append(f"- 这次聊天里你已经撒娇、说想念或用亲昵的动作和表情 {habits['affection']} 次了"
                     f"（比如“{habits['affection_example']}”）：想继续亲近也很好，换个说法或借一件小事来表达，"
                     "别重复同一句，颜文字和亲昵动作也别在一条里堆好几个。")
    if habits["name"] >= 2:
        lines.append(f"- 你最近 {habits['replies']} 条里已经叫了对方的名字 {habits['name']} 次，这一句不用再叫。")
    if not lines:
        return None
    return "【你这次聊天里的回复】\n" + "\n".join(lines)


FOLLOW_UP_NOTE = ("对方说过不喜欢被追问，而上一版回复以提问结尾。请重写这一条：顺着对方的话接、说说你的感受或看法，"
                  "不要以提问结尾；其余意思和语气尽量保持。")

# How long a busy state the user announced lasts, from their own words (scenario-time based).
_ALL_DAY_RE = re.compile(r"(今天一整天|一整天|今天一天|今天全天|全天|今天都|今天要一直|今天得一直|今天一直|今晚都|晚上都)")
_TWO_DAYS_RE = re.compile(r"(这两天|这俩天|这一两天|明后天)")
_FEW_DAYS_RE = re.compile(r"(这几天|最近几天|接下来几天|这阵子|这周|这个星期|这礼拜)")


def _end_of_day(day: datetime) -> datetime:
    return day.replace(hour=23, minute=59, second=0, microsecond=0)


def stated_busy_until(text: str | None, now: datetime) -> datetime | None:
    """End of a busy period the user stated, or None when they gave none (or a short one: the default applies).

    今天一整天 / 今天都 -> end of today; 这两天 -> end of tomorrow; 这几天 / 这周 -> end of the day after tomorrow
    (a deliberately short reading of "a few days": the user can always say again). Never longer than stated."""
    text = text or ""
    if _FEW_DAYS_RE.search(text):
        return _end_of_day(now + timedelta(days=2))
    if _TWO_DAYS_RE.search(text):
        return _end_of_day(now + timedelta(days=1))
    if _ALL_DAY_RE.search(text):
        return _end_of_day(now)
    return None
