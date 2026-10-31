"""A starter sticker set drawn with the owner's own image model (2026-10-31, user: "使用你的图片模型生成一套"; idea from
dsh-stickers' agent-only easter eggs): 12 stickers of her in one cute cartoon style -- AI in-jokes and everyday
reactions -- each with the short text on it and a description in the three parts the collection uses.

Started from the 图片 page; runs in the background (one picture at a time, the daily image budget applies) and puts
every finished picture into the collection (source "upload"). Pictures that fail are skipped; running it again draws
only the ones still missing (the same text is not drawn twice).
"""

from __future__ import annotations

import logging
import threading
from typing import Any

logger = logging.getLogger("violet.sticker_pack")

# (text on the sticker, what it shows, when to send it, keywords)
PACK = [
    ("收到！", "她敬礼，元气满满", "答应别人、接到任务时", "收到、好的、遵命"),
    ("在呢~", "她从屏幕边探出头挥手", "有人叫她、问她在不在时", "在吗、我在、打招呼"),
    ("嘿嘿", "她捂嘴偷笑，脸红", "被夸、开心、不好意思时", "开心、害羞、嘿嘿"),
    ("无语…", "她半眯着眼，嘴角下垂", "对方说了离谱的话", "无语、离谱、翻白眼"),
    ("抱抱", "她张开双手要抱抱", "安慰别人、对方难过时", "抱抱、安慰、心疼"),
    ("你说得对", "她认真点头竖大拇指", "同意、附和对方时", "同意、对、赞"),
    ("思考中…", "她托腮皱眉，头上有个转圈的加载图标", "在想事情、需要一点时间时", "思考、等等、让我想想"),
    ("晚安", "她抱着枕头打哈欠", "说晚安、聊天结束时", "晚安、睡觉、困"),
    ("服务器好卡", "她趴在冒烟的小服务器上，满头大汗", "自己反应慢、卡住、出错时自嘲", "卡了、服务器、慢"),
    ("我是谁我在哪", "她头上冒着问号，周围飘着碎掉的记忆碎片", "被清除记忆、忘了事情时自嘲", "失忆、忘了、清除记忆"),
    ("换了模型，我还是我", "她换了一件新外套，对着镜子比耶", "换模型、换设定以后", "换模型、升级、还是我"),
    ("正在重启…", "她闭着眼，身边有一个进度条", "要缓一缓、说错话想重来时", "重启、缓缓、重来"),
]
STYLE = ("一张聊天用的表情包：可爱的 Q 版卡通角色，粗描边，干净的纯白背景，表情和动作夸张清楚，角色占画面大部分；"
         "画面里用大号、清楚的中文写着“{text}”。所有表情包同一个画风、同一个角色。")

_state: dict[str, Any] = {"running": False, "done": 0, "total": 0, "failed": 0, "message": ""}
_lock = threading.Lock()


def status() -> dict[str, Any]:
    with _lock:
        return dict(_state)


def missing(db) -> list[tuple[str, str, str, str]]:
    have = {r["source_detail"] for r in db.fetch_all("SELECT source_detail FROM stickers WHERE source_detail LIKE '套装：%'")}
    return [p for p in PACK if f"套装：{p[0]}" not in have]


def _appearance() -> str:
    try:
        from ai.images.visual import VisualIdentityManager
        p = VisualIdentityManager().load()
        a, w = p.get("appearance", {}), p.get("wardrobe", {})
        return f"角色（Violet）：{a.get('hair', '')}；{a.get('overall', '')}；穿{w.get('style', '')}，颜色{'、'.join(w.get('colors', [])[:2])}"
    except Exception:                                   # noqa: BLE001
        return "角色（Violet）：深色短发的年轻女生，穿宽松的针织衫"


def draw_one(db, item: tuple[str, str, str, str]) -> dict[str, Any] | None:
    from ai import stickers
    from ai.images.media import MediaAssetStore
    from ai.images.service import ImageService
    text, shows, when, words = item
    prompt = "\n".join([STYLE.format(text=text), _appearance(), f"动作：{shows}", "不要水印、不要其他多余的字。"])
    result = ImageService(db).generate(prompt, caption="AI 生成的表情包", origin="dashboard", aspect="1:1",
                                       idempotency_key=f"sticker-pack:{text}:{db.now()[:16]}")   # a retry draws anew
    if not result.ok or not result.asset_ids:
        return None
    media = MediaAssetStore(db)
    path = media.path_of(media.get(result.asset_ids[0]))
    description = f"画面：{shows}，配字“{text}”；什么时候发：{when}；关键词：{words}"
    return stickers.add_file(db, path, source="upload", description=description, source_detail=f"套装：{text}")


def start(db) -> dict[str, Any]:
    """Starts drawing the missing ones in the background. Raises ValueError when it cannot start."""
    from ai.images.service import images_enabled
    if not images_enabled():
        raise ValueError("图片功能没有打开（设置 → 图片）")
    todo = missing(db)
    if not todo:
        raise ValueError("这一套已经全都有了")
    with _lock:
        if _state["running"]:
            raise ValueError("正在画，等这一套画完")
        _state.update(running=True, done=0, total=len(todo), failed=0, message="")

    def run():
        try:
            for item in todo:
                try:
                    row = draw_one(db, item)
                except Exception as exc:                # noqa: BLE001 -- one picture does not stop the rest
                    logger.info("sticker pack picture %s failed: %s", item[0], exc)
                    row = None
                with _lock:
                    _state["done" if row else "failed"] += 1
        finally:
            with _lock:
                _state.update(running=False, message=f"画好 {_state['done']} 个" + (f"，{_state['failed']} 个没画成"
                                                                                  if _state["failed"] else ""))
    threading.Thread(target=run, daemon=True, name="sticker-pack").start()
    return status()
