# tools/

This directory holds two kinds of things: install and management scripts used on the server, and test tools used during development. The full record of development testing (Phase 7–9 acceptance, real-model evaluations) is in [TESTING-HISTORY.md](TESTING-HISTORY.md) (Chinese).

## Used on the server

Users normally only run `/opt/violet/violet <command>`, which calls the scripts below. The one-click install buttons in the web control panel call them too.

| File | Command | What it does |
|---|---|---|
| `violet_installer.py` | `install.sh`, `violet update / backup / restore / uninstall / info / reset-password / set-port …` | Install, repair, upgrade (automatic rollback on failure), backup, uninstall, access mode and firewall. Python standard library only |
| `violet_setup.py` | (runs automatically during install) | Creates the configuration and data directories; safe to run again |
| `violet_service.py` | (when there is no systemd) | Violet's own supervisor, for Windows, macOS, containers and minimal Linux |
| `violet_admin.py` | `python -m tools.violet_admin set-password` | Control panel admin password |
| `violet_searxng.py` | `violet searxng install / uninstall / status` | Private SearXNG web search, listening only on the server itself, no Docker needed |
| `violet_snowluma.py` | `violet qq install / uninstall / status / restart` | Personal QQ account (SnowLuma, runs in Docker). Switches to a mirror automatically when pulling the image |
| `violet_voice.py`, `voice_server.py` | `violet voice install / uninstall / status` | Her voice: CosyVoice running on the server CPU, cloning a voice from one recording |
| `violet_relay.py` | `violet mcp-relay install / status / uninstall / set-port` | Relay for the Violet MCP phone app |
| `make_release.py` | (maintainers) | Builds the release files (`install.sh`, `violet.tar.gz`, `SHA256SUMS`). GitHub Actions uses it when publishing |

## Used during development

All of these run the real Violet flow in an isolated sandbox: their own database, no connection to WeChat or other chat platforms, and no access to production data. By default they use a simulated model, which costs nothing; when a real model is wanted they ask you for the key first and do not store it.

| File | What it does |
|---|---|
| `violet_test_chat.py`, `free_chat.py` | Chat with her yourself. A `free_chat` session can be continued next time, and can include a fake timetable and fake MCP tools |
| `model_compat.py` | Checks whether an OpenAI-compatible API works: JSON output, tool calls, timeouts |
| `chat_eval.py`, `chat_ab.py`, `chat_dialogue.py`, `dialogue_eval.py`, `scenario_eval.py`, `decision_eval.py` | Evaluation and A/B comparison of the chat experience: fixed scenarios, multi-turn dialogues, whether a capability should be used, whether the same result can be had with fewer tokens |
| `memory_conflicts.py`, `memory_restore_decayed.py` | Lists conflicting memories (read-only); recovers event memories that an older version deleted by mistake |
| `schedule_rollback.py` | Drops the timetable tables from the database (used when rolling back the timetable feature) |
| `fake_mcp_server.py` | An MCP server with only made-up read-only tools, for testing |
| `harness.py`, `replay.py`, `legacy.py`, `synthetic_*.py`, `reassess_tail.py`, `memory_query_audit.py` | Shared parts of the tools above, and special-purpose scripts from early acceptance testing |

To run one: in the `violet/` directory, run `python -m tools.<name> --help`.
