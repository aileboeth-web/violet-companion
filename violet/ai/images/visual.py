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
from datetime import timedelta
from typing import Any

from ai.memory_model import parse_time
from ai.persona import persona_dir

CAPTION_AI = "AI 生成的插画"


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

    def prompt_for(self, scene: dict[str, Any], extra: str = "") -> str:
        p = self.load()
        a, w, s = p.get("appearance", {}), p.get("wardrobe", {}), p.get("art_style", {})
        lines = [
            f"画面：{scene['description']}" + ("（这是想象的画面，不是真实发生的事）" if scene.get("hypothetical") else ""),
            f"人物（Violet，虚拟人物）：{a.get('overall', '')}；发型：{a.get('hair', '')}；{a.get('face', '')}；{a.get('build', '')}",
            f"服饰：{w.get('style', '')}；颜色：{'、'.join(w.get('colors', []))}；配饰：{w.get('accessories', '')}",
            f"画风：{s.get('medium', '')}；光线：{s.get('lighting', '')}；构图：{s.get('composition', '')}；"
            f"整体色调：{'、'.join(p.get('palette', []))}",
            "这是一张插画，不是照片：不要照片写实效果、相机水印或拍摄参数。避免：" + "、".join(s.get("avoid", [])),
        ]
        if extra:
            lines.append(f"补充要求（来自对话，已过滤）：{extra[:200]}")
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


def scene_for_eve(db, mode: str = "current", life_event_id: int | None = None, imagined: str | None = None,
                  recent_hours: float = 12) -> dict[str, Any]:
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
    if scene and scene.get("hypothetical"):
        return f"{CAPTION_AI}（想象的画面）"
    return CAPTION_AI
