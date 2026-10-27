# Offline tests

```powershell
cd "violet"
.\run_tests.ps1            # all tests
.\run_tests.ps1 -k mcp     # a subset
```

Linux / macOS: `python -m pip install -r requirements-dev.txt && python -m pytest`.

## Isolation guarantees (`conftest.py`)

- `VIOLET_ENV_FILE` points at a missing temp file, so a real `.env` is never loaded.
- `VIOLET_DB_PATH` points at a temp SQLite file; the project `violet.db` is never opened.
- All keys/tokens are fake; proxy variables are removed.
- `socket.connect` and `socket.getaddrinfo` are patched: any non-loopback connection or
  DNS lookup raises `NetworkBlocked`, so WeChat, the LLM relay and MCP relays are unreachable.
- WeChat is replaced by `FakeILinkClient`, the LLM by `FakeLLM` (real `ChatCompletion`
  objects), and MCP by real FastMCP servers bound to `127.0.0.1` (`mcp_test_server.py`).

| File | Covers |
|---|---|
| `test_database.py` | isolation, legacy DB compatibility, inbox/outbox, concurrent writes |
| `test_llm_layer.py` | bad JSON, stage failures, per-thread call context, timeouts/retries, fallback model, proactive failure states |
| `test_mcp.py` | offline / hanging / slow / unauthorised servers, circuit breaker, tool cache, permissions, secret redaction, untrusted output |
| `test_runtime.py` | poll-loop resilience, >10 messages, dedup, cursor commit, user-message persistence, send status, serialisation, recovery, proactive coordination |
| `test_dashboard.py` | auth on every route, login/CSRF, token generation, `main.py` wiring, real client cannot reach the network |
| `test_regression.py` | existing chat / silent / task / memory / recall / image / proactive / debounce behaviour |
| `test_memory_model.py` | phase 2: recall without reinforcement, no overlap merging, explicit linking, kinds, absolute time, progress never auto-"done", validity, commitments authorisation, corrections, engagement, topic cooling |
| `test_phase2_scenarios.py` | phase 2 replay scenarios 1–10 (open event not followed up, explicit resolution, brief replies, topic restart, explicit reminder, share without request, repeated ticks, MCP offline, memory retry, correction) |
| `test_profile.py` | phase 3: explicit vs inferred facts, conflicts, sensitivity, disable / delete purge, legacy import, situations, preferences, onboarding, time zone |
| `test_context_engine.py` | phase 3: relevance selection, sources/time labels, usual residence vs current location, expiry, masking, dialogue turns, budget, vehicle label |
| `test_phase3_scenarios.py` | phase 3 scenarios 1–11 (student, lives on campus, went home today, residence ≠ location, correction, no re-asking, declined question, occasional short reply, forbidden memory, expired situation, legacy compatibility) |
| `test_phase2_fixes.py` | phase 3 review of phase 2: upcoming-event allowance, three-way reply classification, user-initiated reset, time zone, who raised a topic |
| `test_profile_dashboard.py` | phase 3 dashboard API: auth, view/correct/disable/delete/source, situations, preferences, memory forbid/delete |
| `test_persona_life.py` | phase 4: 30-day life simulation, quiet days / no backfill, thread & plan state machines, restart idempotency, time plausibility, plans never told as done, read-only persona core, preference evidence & rate limit, no pandering, recent-life questions, reply guard, life-share candidates (no repeats, uninterested user, busy user), Violet's life never in user memory, external info, persona-contradicting output, dashboard, migration/rollback |
| `test_phase5a.py` | phase 5A: life scheduler independent of proactive chat (off/paused/on, no sending, restart, IANA day), optional LLM life proposals (validation, rejection, decline, outage/timeout fallback, budgets), semantic review (harmless chat untouched, false positive cleared with evidence, invented evidence ignored, timeout/malformed retry, rephrase instead of deletion, certain findings never sent), external information / reading records, migration |
| `test_web_security.py` | phase 5B: URL policy, non-public addresses, DNS rebinding (pinned address), redirects per hop, size/time/type/encoding limits, no proxy / cookies / credentials, injection neutralisation, private data and secrets never sent, budgets, disabled sources, provider failures, SearXNG adapter, verified dates |
| `test_web_mcp.py` | phase 5B: built-in web MCP through the real bridge and chat pipeline: search, small talk, provider failure, MCP offline, off by default, prompt injection cannot steer tools, link provenance, no web tools in proactive messages, tokens never in requests, offline startup |
| `test_web_explorer.py` | phase 5B: explorer two-key switch, findings + sourced reading records, nothing sent, once per day, empty results, LLM may choose nothing, budgets, queries never from user data, proactive off, candidate checks, shared once across restarts, expiry, separate sources, isolation from Violet's life and user memory, dashboard, migration |
| `test_images.py` | phase 6: registry (same model at two providers, no literal keys, unknown capabilities), the shipped config is safe, routing by verified capability / limits / priority, capability_not_supported, explicit model never switched, fallback only when not processed upstream, uncertain outcomes kept, idempotency, budgets and unknown prices, concurrency, circuit breaker, OpenAI images / chat-completions formats and errors, safe URL downloads, native extension points, multi-image input/output and vision degradation, originals never overwritten, media validation, path safety, retention, visual identity + references, life scenes, capability verification, migration |
| `test_images_chat.py` | phase 6 end to end with the fake WeChat client: inbound photo stored / described / linked, invalid files and vision outage, "show me" from the recorded life, hypothetical scenes, unplanned events refused, photo claims rewritten, attach / foreign markers, edit keeps the original, send failure states, restart never regenerates or resends, incomplete images never sent, explicit-request gate, no tools in proactive messages, one image per turn, user-named models, off by default, dashboard |
| `test_phase6_review.py` | phase 6 review of 5B: SearXNG exception not usable by fetch_url, stdio budget survives restart, only ChatAgent gets web / image tools, camera-claim checks |
| `image_fixtures.py` | fictional image providers (mock adapters, httpx.MockTransport for HTTP adapters) |
| `web_fixtures.py` | loopback "internet": LocalWeb server, test resolver / dialer (only validated public IPs reach it) |
| `samples/phase4_chat_samples.json` | multi-turn samples with review points for a future real-model evaluation (only the format is tested offline) |
| `test_proactive_policy.py` | phase 2: reply before memory, lowered defaults, backoff, fixed-task checks, reminder quiet hours / fallback / single delivery / retries, policy units |
