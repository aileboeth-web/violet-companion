"""2026-11-03 (user: 群里她看得清表情包，看不清图片): ordinary pictures in QQ chats are looked at too."""

from __future__ import annotations


class Adapter:
    def __init__(self, path):
        self.path, self.downloads = path, 0

    def download(self, part):
        self.downloads += 1
        return self.path


def _engine(db):
    from ai.social.engine import SocialEngine
    from ai.social.store import SocialStore
    engine = SocialEngine.__new__(SocialEngine)                 # only the store is needed here
    engine.store = SocialStore(db)
    return engine


def test_a_picture_gets_looked_at_and_written_into_the_message(violetdb, tmp_path, monkeypatch):
    from ai import vision
    picture = tmp_path / "p.jpg"
    picture.write_bytes(b"\xff\xd8 fake")
    asked = []
    monkeypatch.setattr(vision, "describe_image", lambda path, prompt=None: asked.append(prompt) or "一只橘猫趴在键盘上")
    engine = _engine(violetdb)
    store = engine.store
    conv = store.conversation("napcat", "group", "614", "群", default_mode="auto")
    old = store.add_message(conv["id"], content="[图片]", sender_id="1", platform_msg_id="50")
    store.add_media(old, kind="image", file_key="cat.jpg", url="http://x/cat")
    ask = store.add_message(conv["id"], content="你看得清这张图吗", sender_id="2", platform_msg_id="51")
    adapter = Adapter(picture)
    # the picture was already read in an earlier decision; the question comes later: still looked at
    engine._look_at_pictures(conv["id"], [{"id": ask}], adapter)
    content = violetdb.fetch_one("SELECT content FROM social_messages WHERE id = ?", (old,))["content"]
    assert content == "[图片：一只橘猫趴在键盘上]" and "照抄关键的字" in asked[0]
    # the same picture again (same QQ file name): no second look
    again = store.add_message(conv["id"], content="[图片]", sender_id="3", platform_msg_id="52")
    store.add_media(again, kind="image", file_key="cat.jpg", url="http://x/cat2")
    engine._look_at_pictures(conv["id"], [{"id": again}], adapter)
    assert adapter.downloads == 1 and len(asked) == 1
    assert "橘猫" in violetdb.fetch_one("SELECT content FROM social_messages WHERE id = ?", (again,))["content"]


def test_at_most_two_pictures_per_decision_and_a_switch(violetdb, tmp_path, monkeypatch):
    from ai import vision
    picture = tmp_path / "p.jpg"
    picture.write_bytes(b"\xff\xd8 fake")
    monkeypatch.setattr(vision, "describe_image", lambda path, prompt=None: "风景")
    engine = _engine(violetdb)
    store = engine.store
    conv = store.conversation("napcat", "group", "615", "群", default_mode="auto")
    ids = []
    for n in range(4):
        m = store.add_message(conv["id"], content="[图片]", sender_id="1", platform_msg_id=str(60 + n))
        store.add_media(m, kind="image", file_key=f"f{n}.jpg", url=f"http://x/{n}")
        ids.append(m)
    adapter = Adapter(picture)
    monkeypatch.setenv("VIOLET_SOCIAL_PICTURE_LOOK", "0")
    engine._look_at_pictures(conv["id"], [{"id": i} for i in ids], adapter)
    assert adapter.downloads == 0
    monkeypatch.setenv("VIOLET_SOCIAL_PICTURE_LOOK", "1")
    engine._look_at_pictures(conv["id"], [{"id": i} for i in ids], adapter)
    looked = [violetdb.fetch_one("SELECT content FROM social_messages WHERE id = ?", (i,))["content"] for i in ids]
    assert looked == ["[图片]", "[图片]", "[图片：风景]", "[图片：风景]"]          # the newest two
