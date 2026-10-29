"""Violet's social life outside the one-to-one chat with the owner (2026-10-29, "QQ" work).

The owner's conversation stays exactly where it always was (table ``messages``, Chat / Memory / Proactive agents).
Everything with OTHER people -- QQ groups, other people's private chats -- lives here, in its own tables, so nothing
of it can leak into the owner's context or the owner's memories by accident:

  store.py     conversations, their messages, people, group notes, decisions
  prompts.py   the layered prompt (persona / occasion rules / relationship card / current situation)
  engine.py    observe -> decide (one fixed-format answer) -> act; watermarks, wake-ups, limits
  memory.py    impressions of people and groups (short cards, rewritten, never the owner's private things)
"""
