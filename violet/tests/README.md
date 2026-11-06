# Offline automated tests

How to run:

```powershell
cd violet
.\run_tests.ps1            # everything (Windows; the first run creates .venv one level up and installs the dependencies)
.\run_tests.ps1 -k persona # only tests with "persona" in the name
```

Linux / macOS:

```bash
python -m pip install -r requirements-dev.txt && python -m pytest
```

More than 140 test files, about 3000 tests, all offline; a full run takes a few minutes.

## Nothing real is touched (`conftest.py`)

- **Configuration**: `VIOLET_ENV_FILE` points to a temporary file that does not exist, so the real `.env` is never read.
- **Database**: `VIOLET_DB_PATH` points to a temporary SQLite file, so the project's `violet.db` is never opened. It is emptied before each test, and so are the per-persona database files.
- **Secrets**: keys and tokens are fake, and proxy-related environment variables are removed.
- **Network**: `socket.connect` and `socket.getaddrinfo` are replaced. Any connection or DNS lookup to a non-local address raises `NetworkBlocked`, so WeChat, QQ, Telegram, model APIs and the MCP relay cannot be reached.
- **Fake external services**:
  - WeChat is replaced by `FakeILinkClient`, the model by `FakeLLM` (which returns real `ChatCompletion` objects).
  - MCP uses a real FastMCP server bound to `127.0.0.1` (`mcp_test_server.py`).
  - Web access uses the local "internet" in `web_fixtures.py`, and drawing uses the fake API in `image_fixtures.py`.
- **Phone state**: off by default (`VIOLET_PHONE_CONTEXT=0`); tests that need it start their own fake phone on the local machine.

## Finding a file

The file name is the thing it tests. The main groups:

| Files | What they test |
|---|---|
| `test_database.py`, `test_runtime.py`, `test_llm_layer.py` | Database, the main send/receive loop, model calls (timeouts, retries, fallback models) |
| `test_memory_*`, `test_profile*`, `test_context_engine.py` | Memory, the profile about you, the context given to the model each turn |
| `test_persona*`, `test_phase5a.py`, `test_aliveness_violet.py` | Personas, relationships, virtual life, mood, each persona's own records |
| `test_proactive*`, `test_holidays_violet.py`, `test_phone_*` | Reaching out, holiday greetings, phone state |
| `test_channels_p10*` (including Telegram), `test_qq*`, `test_social*` | Chat channels, QQ groups |
| `test_web*`, `test_images*`, `test_mcp*`, `test_voice*`, `test_schedule*` | Web access, seeing and drawing images, MCP tools, voice, timetable |
| `test_control_center*`, `test_dashboard.py`, `test_eve_import_violet.py` | Every web control panel endpoint, login and permissions, import from Project Eve |
| `test_installer_p10.py`, `test_updates_violet.py`, `test_snowluma*`, `test_root_task*` | Install, upgrade, uninstall, and the optional parts installed with one click |

`samples/` holds sample conversations for real-model evaluations; the offline tests only check their format.
