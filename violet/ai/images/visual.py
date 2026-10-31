"""Violet's visual identity and life-consistent scenes (phase 6).

VisualIdentityManager
  persona/visual_profile.json: appearance (no beauty template), wardrobe,
  palette, art style, things to avoid, optional reference images (asset ids in
  the media store), version + history. The dashboard may edit it (a human
  change): every save bumps the version, appends history and is also recorded
  in visual_profile_versions.
  prompt_for(scene) builds the provider-neutral text. Reference images are
  passed only to models whose reference_images capability is verified; other
  models get the text alone (consistency across models is best effort).

scene_for_eve(db, mode, ...) -- built from recorded facts only:
  "life_event"   a happened, virtual life event (by id)        -> historical scene
  "current"      a happened event of the last hours, else an active thread
                 (shown as in progress, never as finished), else a quiet
                 everyday moment marked hypothetical
  "hypothetical" anything else the user imagines -> always marked hypothetical
Plans that have not happened are never drawn as having happened. The image
model only receives text; it cannot touch the life database.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import timedelta
from typing import Any

from ai.memory_model import parse_time
from ai.persona import persona_dir

CAPTION_AI = "AI 生成的插画"
CAPTION_AI_PHOTO = "AI 生成的图片"

# 2026-10-30 (user: 生的图没有规则，很像 AI 生的那种动漫图，尽量靠近现实的情境): VIOLET_IMAGE_STYLE
#   photo (default)  like a life photo taken with a phone, in a real setting and the real time of day
#   illustration     the soft digital illustration of the visual profile (the old default)
#   anime            Japanese anime style
STYLES = ("photo", "illustration", "anime")
STYLE_RULES = {
    "photo": ("像用手机随手拍的真实生活照：真实的光源（窗外天光、台灯、路灯、屏幕光），光线和时间、天气一致；"
              "普通手机镜头的视角和景深，轻微噪点，构图随意、不摆拍；真实材质（布料褶皱、木纹、纸张、玻璃反光）、"
              "真实而普通的生活环境，东西有一点乱也没关系；人物是普通人的真实长相，皮肤有自然纹理，不磨皮、不完美对称。",
              ["动漫或二次元画风", "插画或 CG 渲染感", "过度磨皮和塑料感", "影棚灯光和完美构图", "过饱和的颜色",
               "水印、文字、相机参数", "真实品牌标志", "夸张身材或性感化", "影视或游戏角色的样子"]),
    "anime": ("日系动漫画风：干净的线条和赛璐璐上色，背景是具体的生活场景，光线和时间一致。",
              ["照片级写实", "水印或文字", "真实品牌标志", "夸张身材或性感化", "影视或游戏角色的样子"]),
}
SHOTS = {
    "selfie": "她用手机前置摄像头自拍：手臂长度的距离，略微俯拍或平视，画面里看得到她身后的真实环境",
    "candid": "像朋友在旁边随手拍下她的样子：她没看镜头或刚转过头，自然的动作",
    "scene": "只拍她眼前的东西或场景（桌上的东西、窗外、路上看到的），画面里没有她本人",
}


# 2026-10-31 (user's server, task 38): the chat model wrote its own English addition -- "non-human digital persona
# ... silver-violet hair ... tasteful anime-inspired illustration" -- and gpt-image followed it, not the life-photo
# style or her appearance. The addition may describe the scene; style words always go, and for a picture of her also
# looks (hair, eyes, skin, body) and "virtual / non-human / digital being" -- those come from the visual profile.
STYLE_WORDS = re.compile(r"(anime|manga|cartoon|chibi|illustrat|drawing|painting|painted|sketch|cel[- ]?shad|comic|"
                         r"\bcg\b|3d render|render(ed|ing)?|digital art|concept art|artstation|stylized|semi-realistic|"
                         r"watercolou?r|vector|pixel art|动漫|二次元|插画|漫画|卡通|手绘|绘画|画风|水彩|厚涂|立绘|渲染|CG|赛璐璐|Q版)", re.I)
LOOK_WORDS = re.compile(r"(hair|eyes?\b|iris|skin|face|freckle|lips|body|figure|tall|slim|cute girl|beautiful|"
                        r"non-human|not human|digital (persona|being|avatar|girl)|virtual|hologram|holographic|glowing|"
                        r"robot|android|cyborg|fictional|发色|头发|长发|短发|发型|眼睛|瞳|皮肤|五官|身材|虚拟|数字人|全息|机器人|非人类)", re.I)


def clean_extra(text: str, about_her: bool) -> str:
    """The model's addition without style words (and, for a picture of her, without looks): clause by clause."""
    style = image_style()
    clauses = re.split(r"[,，;；。\n]+", text or "")
    kept = []
    for c in clauses:
        c = c.strip()
        if not c:
            continue
        if style == "photo" and STYLE_WORDS.search(c):
            continue
        if about_her and LOOK_WORDS.search(c):
            continue
        kept.append(c)
    return "，".join(kept)[:300]


FINAL = {"photo": "最终效果：一张真实的手机照片（photorealistic candid smartphone photo, natural light, real skin texture）。"
                  "不是动漫、不是插画、不是 CG（not anime, not illustration, not 3D render）。",
         "anime": "最终效果：日系动漫画风（anime style）。"}


def image_style() -> str:
    value = (os.environ.get("VIOLET_IMAGE_STYLE") or "photo").strip().lower()
    return value if value in STYLES else "photo"


def moment_line(now_iso: str) -> str:
    """The real time of day and season, so the light and the clothes match (a 10 pm picture is not a sunny park)."""
    from datetime import datetime
    try:
        now = datetime.fromisoformat(now_iso[:19])
    except ValueError:
        return ""
    h = now.hour
    part = ("深夜" if h < 5 else "清晨" if h < 8 else "上午" if h < 11 else "中午" if h < 13 else "下午" if h < 17
            else "傍晚" if h < 19 else "晚上" if h < 23 else "深夜")
    season = {12: "冬天", 1: "冬天", 2: "冬天", 3: "春天", 4: "春天", 5: "春天", 6: "夏天", 7: "夏天", 8: "夏天"}.get(
        now.month, "秋天")
    light = {"深夜": "室内灯光或屏幕光，窗外是黑的", "清晨": "清晨偏冷的天光", "晚上": "室内灯光，窗外天黑",
             "傍晚": "傍晚的暖色天光"}.get(part, "白天的自然光")
    return f"时间：{now.month} 月{season}，{part} {now:%H:%M}，{light}；衣服和环境要符合这个季节"


class SceneError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class VisualIdentityManager:
    FILE = "visual_profile.json"

    def __init__(self, db=None):
        self.db = db

    @property
    def path(self):
        return persona_dir() / self.FILE

    def load(self) -> dict[str, Any]:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def sha(self, data: dict[str, Any] | None = None) -> str:
        raw = json.dumps(data or self.load(), ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def reference_asset_ids(self) -> list[int]:
        return [int(i) for i in self.load().get("reference_images") or [] if str(i).isdigit()]

    def prompt_for(self, scene: dict[str, Any], extra: str = "", shot: str | None = None) -> str:
        p = self.load()
        a, w, s = p.get("appearance", {}), p.get("wardrobe", {}), p.get("art_style", {})
        style = image_style()
        shot = shot if shot in SHOTS else None
        lines = [f"画面：{scene['description']}" + ("（这是想象的画面，不是真实发生的事）" if scene.get("hypothetical") else "")]
        if scene.get("moment"):
            lines.append(scene["moment"])
        if shot:
            lines.append(f"拍法：{SHOTS[shot]}")
        if shot != "scene":
            lines += [f"人物（Violet）：{a.get('overall', '')}；发型：{a.get('hair', '')}；{a.get('face', '')}；"
                      f"{a.get('build', '')}",
                      f"服饰：{w.get('style', '')}；颜色：{'、'.join(w.get('colors', []))}；配饰：{w.get('accessories', '')}"]
        if style == "illustration":
            lines += [f"画风：{s.get('medium', '')}；光线：{s.get('lighting', '')}；构图：{s.get('composition', '')}；"
                      f"整体色调：{'、'.join(p.get('palette', []))}",
                      "这是一张插画，不是照片：不要照片写实效果、相机水印或拍摄参数。避免：" + "、".join(s.get("avoid", []))]
        else:
            rule, avoid = STYLE_RULES[style]
            lines += [f"风格：{rule}", "避免：" + "、".join(avoid)]
        extra = clean_extra(extra, about_her=shot != "scene")
        if extra:
            lines.append(f"情境补充（只描述在做什么、在哪里；长相和画风以上面为准）：{extra}")
        if style in FINAL:
            lines.append(FINAL[style])               # last, so it is what the model reads last
        return "\n".join(lines)


    def update(self, patch: dict[str, Any], actor: str = "dashboard", note: str = "") -> dict[str, Any]:
        allowed = {"appearance", "wardrobe", "palette", "art_style", "reference_images", "identity_note"}
        bad = set(patch) - allowed
        if bad:
            raise ValueError(f"not editable: {', '.join(sorted(bad))}")
        if "reference_images" in patch and not all(str(i).isdigit() for i in patch["reference_images"]):
            raise ValueError("reference_images must be media asset ids")
        data = self.load()
        old_version = str(data.get("version", "1.0.0"))
        major, minor, *_ = (old_version.split(".") + ["0", "0"])
        data.update(patch)
        data["version"] = f"{major}.{int(minor) + 1}.0"
        history = list(data.get("history") or [])
        history.append({"version": data["version"], "changed_at": (self.db.now() if self.db else "")[:19],
                        "actor": actor, "note": note[:200], "fields": sorted(patch)})
        data["history"] = history[-50:]
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)
        if self.db is not None:
            self.db.execute("INSERT INTO visual_profile_versions (version, sha256, content, actor, note, created_at) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            (data["version"], self.sha(data), json.dumps(data, ensure_ascii=False), actor, note[:200],
                             self.db.now()))
        return data


def other_prompt(text: str, now_iso: str = "") -> str:
    """A picture of something else, in the chosen style (2026-10-30: was always "插画风格")."""
    style = image_style()
    moment = moment_line(now_iso) if now_iso and style != "illustration" else ""
    if style == "illustration":
        return text + "\n（插画风格，不要伪装成照片）"
    rule, avoid = STYLE_RULES[style]
    text = clean_extra(text, about_her=False) or text
    return "\n".join(x for x in [f"画面：{text}", moment, f"风格：{rule}", "避免：" + "、".join(avoid), FINAL[style]] if x)


def scene_for_eve(db, mode: str = "current", life_event_id: int | None = None, imagined: str | None = None,
                  recent_hours: float = 12) -> dict[str, Any]:
    scene = _scene(db, mode, life_event_id, imagined, recent_hours)
    if image_style() != "illustration":                # 2026-10-30: the light and the season of that moment
        at = db.now()
        if scene.get("life_event_id") and scene["kind"] == "life_event":
            from ai.life_store import LifeStore
            event = LifeStore(db).event(scene["life_event_id"]) or {}
            at = str(event.get("occurred_at") or at)
        scene["moment"] = moment_line(at)
    return scene


def _scene(db, mode: str, life_event_id: int | None, imagined: str | None, recent_hours: float) -> dict[str, Any]:
    from ai.life_store import LifeStore
    store = LifeStore(db)
    now = parse_time(db.now())
    if mode == "life_event":
        event = store.event(life_event_id) if life_event_id else None
        if not event or event["status"] != "happened" or not event["is_virtual"] or parse_time(event["occurred_at"]) > now:
            raise SceneError("event_not_happened", "only a recorded, happened life event can be drawn as having happened")
        return {"kind": "life_event", "life_event_id": event["id"], "hypothetical": False,
                "description": f"Violet {event['content']}（{event['occurred_at'][:10]} 发生过的事）"}
    if mode == "hypothetical":
        text = (imagined or "").strip()[:120] or "一个安静的日常时刻"
        return {"kind": "hypothetical", "life_event_id": None, "hypothetical": True, "description": f"Violet {text}"}
    if mode != "current":
        raise SceneError("invalid_scene", f"unknown scene mode {mode!r}")
    recent = [e for e in store.recent_happened(days=recent_hours / 24)
              if now - parse_time(e["occurred_at"]) <= timedelta(hours=recent_hours)]
    if recent:
        event = recent[-1]
        return {"kind": "life_event", "life_event_id": event["id"], "hypothetical": False,
                "description": f"Violet 刚才{event['content']}"}
    threads = store.threads(["active"])
    if threads:
        thread = threads[-1]
        return {"kind": "current_thread", "life_event_id": None, "hypothetical": False, "thread_id": thread["id"],
                "description": f"Violet 正在{thread['title']}（进行中，还没有完成）"}
    return {"kind": "hypothetical", "life_event_id": None, "hypothetical": True,
            "description": "Violet 在一个安静的日常时刻（没有记录的具体活动，这是想象的画面）"}


def caption_for(scene: dict[str, Any] | None) -> str:
    base = CAPTION_AI if image_style() == "illustration" else CAPTION_AI_PHOTO
    if scene and scene.get("hypothetical"):
        return f"{base}（想象的画面）"
    return base
