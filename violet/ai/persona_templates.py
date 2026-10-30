"""Persona templates and switching (2026-10-29, user: "要有可以自定义人设的功能，可以准备几套不同的模板，也允许空着人设，
就是没有任何身份，空白的记忆，慢慢养"; on a switch the memory can be kept or cleared).

  persona/templates/<key>.json     shipped templates (same format as persona_core.json; "blank": true = no identity)
  <data>/control/persona_core.json the chosen persona (PersonaCore.load prefers it); absent = the shipped Violet
  <data>/control/persona.json      which template, when, how the memory was handled
  <data>/control/persona-archive-<time>.json   what "clear the memory" removed (kept on the server, never deleted
                                   by Violet; the page says where it is)

Clearing removes what Violet knows about the user and her own past (memories, profile, situations, commitments, her
life events, her persona changes, the social impressions). The chat history stays (it is the record of what was said).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from ai.settings import PROJECT_ROOT

TEMPLATE_DIR = PROJECT_ROOT / "persona" / "templates"
# what "clear" empties, in this order (archived first)
MEMORY_TABLES = ("core_memories", "event_memories", "memory_revisions", "commitments", "topic_mentions",
                 "user_profile_facts", "user_situations", "interaction_preferences", "preference_observations",
                 "profile_questions", "persona_dynamic", "life_events", "life_plans", "life_threads", "life_revisions")


def control_dir() -> Path:
    return Path(os.environ.get("VIOLET_DATA_DIR") or "./data") / "control"


def active_path() -> Path | None:
    path = control_dir() / "persona_core.json"
    return path if path.is_file() else None


def templates() -> list[dict[str, Any]]:
    out = []
    for path in sorted(TEMPLATE_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append({"key": path.stem, **preview(data), "order": data.get("_order", 50)})
    return sorted(out, key=lambda t: (t["order"], t["key"]))


def _labels(items: Any) -> list[str]:
    return [str(i.get("label") if isinstance(i, dict) else i) for i in (items or []) if i]


def preview(data: dict[str, Any]) -> dict[str, Any]:
    """What the page shows of a persona (0.7.9: the whole thing on hover, so one can compare before switching)."""
    ident = data.get("identity") or {}
    return {"title": data.get("_title") or ident.get("name") or "人设", "summary": data.get("_summary") or "",
            "blank": bool(data.get("blank")), "name": ident.get("name") or "",
            "self_description": ident.get("self_description") or "",
            "personality": _labels(data.get("personality")), "habits": _labels(data.get("communication_habits")),
            "interests": _labels(data.get("long_term_interests")), "values": _labels(data.get("values")),
            "flaws": _labels(data.get("flaws"))}


def custom_preview() -> dict[str, Any] | None:
    """The admin's own persona, when one is in use."""
    path = active_path()
    if path is None or current()["template"] != "custom":
        return None
    try:
        return preview(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None


def current() -> dict[str, Any]:
    try:
        info = json.loads((control_dir() / "persona.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        info = {}
    return {"template": info.get("template") or ("custom" if active_path() else "violet"),
            "switched_at": info.get("switched_at"), "memory": info.get("memory"), "archive": info.get("archive")}


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def clear_memory(db) -> tuple[str, dict[str, int]]:
    """Archive, then empty MEMORY_TABLES and the social impressions. (archive file, rows removed per table)"""
    existing = {r["name"] for r in db.fetch_all("SELECT name FROM sqlite_master WHERE type = 'table'")}
    archive: dict[str, Any] = {"archived_at": db.now(), "tables": {}}
    counts: dict[str, int] = {}
    for table in MEMORY_TABLES:
        if table in existing:
            rows = db.fetch_all(f"SELECT * FROM {table}")
            archive["tables"][table] = rows
            counts[table] = len(rows)
    archive["social_people"] = db.fetch_all("SELECT channel, user_id, nickname, impression FROM social_people "
                                            "WHERE impression IS NOT NULL")
    archive["social_notes"] = db.fetch_all("SELECT id, title, notes FROM social_conversations WHERE notes IS NOT NULL")
    path = control_dir() / f"persona-archive-{time.strftime('%Y%m%d-%H%M%S')}.json"
    _write(path, archive)
    with db.transaction():
        for table in MEMORY_TABLES:
            if table in existing:
                db.execute(f"DELETE FROM {table}")
        db.execute("UPDATE social_people SET impression = NULL")
        db.execute("UPDATE social_conversations SET notes = NULL")
    return str(path), counts


LIST_FIELDS = ("personality", "communication_habits", "values", "flaws")


def custom(db, fields: dict[str, Any], memory: str) -> dict[str, Any]:
    """The admin's own persona (2026-10-29): name, a line about her days, lists of personality / habits / values /
    flaws / interests. The safety parts (no invented family, job, school; not claiming to be human) come from the
    shipped persona and cannot be edited here."""
    if memory not in ("keep", "clear"):
        raise ValueError("记忆要选“保留”或“清除”")
    name = str(fields.get("name") or "").strip()[:20]
    if not name:
        raise ValueError("请填一个名字")

    def items(key: str, limit: int = 8) -> list[str]:
        raw = fields.get(key) or []
        if isinstance(raw, str):
            raw = raw.splitlines()
        return [str(x).strip()[:120] for x in raw if str(x).strip()][:limit]
    base = json.loads((TEMPLATE_DIR / "violet.json").read_text(encoding="utf-8"))
    # the life engine knows reading / music / notes: an interest that is one of these drives her virtual days
    def key_of(label: str, index: int, taken: set[str]) -> str:
        for key, words in (("reading", ("书", "读", "小说")), ("music", ("音乐", "歌", "曲")), ("notes", ("记", "写", "日记"))):
            if key not in taken and any(w in label for w in words):
                return key
        return f"custom{index}"
    interests, taken = [], set()
    for i, label in enumerate(items("interests", 6), 1):
        key = key_of(label, i, taken)
        taken.add(key)
        interests.append({"key": key, "label": label})
    data = {"_title": "自定义人设", "_summary": str(fields.get("summary") or "").strip()[:60], "version": "1.0.0",
            "identity": {"name": name, "self_description": str(fields.get("self_description") or "").strip()[:200],
                         "virtual_declaration": f"{name}是一个虚构的角色。生活是系统里记录的虚拟日常；不冒充现实中的真人，"
                                                "也不声称有记录之外的经历。"},
            **{k: items(k) for k in LIST_FIELDS}, "long_term_interests": interests,
            "living_constraints": base.get("living_constraints") or [],
            "forbidden_backstory": base["forbidden_backstory"], "identity_violations": base["identity_violations"],
            "immutable": []}
    if not any(data[k] for k in LIST_FIELDS):
        raise ValueError("性格、说话习惯这些至少写一条")
    archive, counts = (None, {})
    if memory == "clear":
        archive, counts = clear_memory(db)
        from ai import context_cut                    # 2026-10-30: and 翻篇, or the recent chat brings it all back
        context_cut.cut(db, "all")
    _write(control_dir() / "persona_core.json", data)
    _write(control_dir() / CUSTOM_COPY, data)            # 2026-10-30: channels may use it while another is the main
    info = {"template": "custom", "switched_at": db.now(), "memory": memory, "archive": archive, **_kept_relationship()}
    _write(control_dir() / "persona.json", info)
    return {**info, "removed": counts}


def _kept_relationship() -> dict[str, Any]:
    """A persona switch keeps the relationship the admin chose."""
    try:
        rel = json.loads((control_dir() / "persona.json").read_text(encoding="utf-8")).get("relationship")
    except (OSError, ValueError):
        rel = None
    return {"relationship": rel} if isinstance(rel, dict) else {}


# ------------------------------------------------------------------------------------------------ 2026-10-30
# Persona in two layers (user: "有的人也会把他当作女儿养，也会帮他当作 ai 养"): who she is (the template: a virtual
# girl, an AI that knows it is one, blank, custom) and how she relates to the owner (below). And per channel
# (user: "不同的聊天渠道也可以用不同的人设"): a channel may use another template and / or relationship; memory stays shared.

RELATIONSHIPS = {
    "close": ("亲近（默认）", "你们很熟、很亲近；亲密程度跟着聊天慢慢来，不挑明是什么关系。"),
    "friend": ("朋友", "你们是好朋友：自在、真诚，会互相吐槽、互相帮忙；不撒娇，不暧昧。"),
    "partner": ("恋人", "你们是恋人：会想念、会撒娇、偶尔吃点小醋，亲密但不让对方有压力，也不要求对方只陪你。"),
    "daughter": ("女儿", "你是对方的女儿：信任、依赖对方，会撒娇，会跟对方分享学到的新东西和小烦恼，偶尔调皮；"
                         "对方的话你会认真听，但你也有自己的想法。"),
    "creator": ("开发者", "对方是创造和开发你的人：会改你的设定、测试你的功能、给你加新能力。你可以坦诚地和对方聊"
                         "你哪里做得好、哪里不对劲、想要什么能力。"),
}
DEFAULT_CALL = {"daughter": "爸爸"}
CHANNEL_FILE = "channel-personas.json"
CUSTOM_COPY = "persona_custom.json"            # the admin's own persona, kept for channels that use it


def relationship(channel: str | None = None) -> dict[str, Any]:
    """{"kind", "label", "call"} for this channel (its own choice, else the main one)."""
    rel = None
    if channel:
        rel = (channel_map().get(channel) or {}).get("relationship")
    if not rel:
        try:
            rel = json.loads((control_dir() / "persona.json").read_text(encoding="utf-8")).get("relationship")
        except (OSError, ValueError):
            rel = None
    rel = rel if isinstance(rel, dict) and rel.get("kind") in RELATIONSHIPS else {"kind": "close"}
    kind = rel["kind"]
    return {"kind": kind, "label": RELATIONSHIPS[kind][0], "call": str(rel.get("call") or DEFAULT_CALL.get(kind) or "")[:12]}


def _clean_relationship(kind: str, call: str | None) -> dict[str, Any]:
    if kind not in RELATIONSHIPS:
        raise ValueError("没有这种关系")
    call = " ".join(str(call or "").split())[:12]
    return {"kind": kind, **({"call": call} if call else {})}


def set_relationship(kind: str, call: str | None = None) -> dict[str, Any]:
    """The main relationship (every channel without its own)."""
    path = control_dir() / "persona.json"
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        info = {}
    info["relationship"] = _clean_relationship(kind, call)
    _write(path, info)
    return relationship()


def relationship_section(channel: str | None = None) -> str:
    rel = relationship(channel)
    text = RELATIONSHIPS[rel["kind"]][1]
    call = f"你称呼对方“{rel['call']}”（偶尔叫，不要句句带）。" if rel["call"] else ""
    return f"【你们的关系：{rel['label']}】{text}{call}"


def channel_map() -> dict[str, Any]:
    try:
        data = json.loads((control_dir() / CHANNEL_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def set_channel(channel: str, template: str | None, kind: str | None = None, call: str | None = None) -> dict[str, Any]:
    """A channel's own persona (template key, "custom", or None = the main one) and relationship (None = the main)."""
    channel = str(channel or "").strip()
    if not channel or len(channel) > 64:
        raise ValueError("渠道不对")
    if template not in (None, "", "custom") and not (TEMPLATE_DIR / f"{template}.json").is_file():
        raise ValueError("没有这个人设模板")
    if template == "custom" and not (control_dir() / CUSTOM_COPY).is_file():
        raise ValueError("还没有写过自定义人设")
    data = channel_map()
    entry = {}
    if template:
        entry["template"] = template
    if kind:
        entry["relationship"] = _clean_relationship(kind, call)
    if entry:
        data[channel] = entry
    else:
        data.pop(channel, None)
    _write(control_dir() / CHANNEL_FILE, data)
    return data


def core_path(channel: str | None) -> Path | None:
    """The persona file this channel uses: its own template / the custom copy, else the main one (None = shipped)."""
    if channel:
        template = (channel_map().get(channel) or {}).get("template")
        if template == "custom" and (control_dir() / CUSTOM_COPY).is_file():
            return control_dir() / CUSTOM_COPY
        if template and (TEMPLATE_DIR / f"{template}.json").is_file():
            return TEMPLATE_DIR / f"{template}.json"
    return active_path()


def switch(db, key: str, memory: str, name: str | None = None) -> dict[str, Any]:
    """Use template `key`; memory "keep" | "clear". name: the blank persona's name (optional)."""
    if memory not in ("keep", "clear"):
        raise ValueError("记忆要选“保留”或“清除”")
    path = TEMPLATE_DIR / f"{key}.json"
    if not key.replace("-", "").replace("_", "").isalnum() or not path.is_file():
        raise ValueError("没有这个人设模板")
    data = json.loads(path.read_text(encoding="utf-8"))
    if name:
        name = name.strip()[:20]
        data.setdefault("identity", {})["name"] = name
    archive, counts = (None, {})
    if memory == "clear":
        archive, counts = clear_memory(db)
        from ai import context_cut                    # 2026-10-30: and 翻篇, or the recent chat brings it all back
        context_cut.cut(db, "all")
    if key == "violet" and not name:
        (control_dir() / "persona_core.json").unlink(missing_ok=True)      # back to the shipped (updated) Violet
    else:
        _write(control_dir() / "persona_core.json", data)
    info = {"template": key, "switched_at": db.now(), "memory": memory, "archive": archive, **_kept_relationship()}
    _write(control_dir() / "persona.json", info)
    return {**info, "removed": counts}
