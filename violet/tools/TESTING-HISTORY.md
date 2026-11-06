# Violet 2.0 local test tools (Phase 7 B.1)

**English** | [简体中文](TESTING-HISTORY.zh-CN.md)

Three command-line tools, all running on the **real Violet 2.0 pipeline** (VioletRuntime → IntentAgent / ChatAgent / MemoryAgent, ContextEngine,
MemoryWorker, outbox, ProactiveScheduler); there is no separate, simplified chat logic.

| Tool | Purpose | Default |
|---|---|---|
| `python -m tools.violet_test_chat` | Chat with Violet in an isolated environment | Simulated model, free, offline |
| `python -m tools.dialogue_eval` | 32-round acceptance dialogue (fictional user "Xiaoman") + score sheet | Simulated model |
| `python -m tools.model_compat` | Checks a model's compatibility with each Chat Completions parameter | Simulated provider |
| `python -m tools.free_chat` | Free-chat experience test: sessions can be continued, every round's details and flags are saved (§20, Phase 8.9.6) | Simulated model |
| `python -m tools.fake_mcp_server` | A fictional timetable / weather / search MCP server (stdio, no network) for trying Violet's tool use (§23, Phase 9.2) | Fictional data |
| `python -m tools.memory_conflicts` | Lists conflicts across memories, read-only (§22, Phase 9.1.2) | Read-only |

## 1. Isolation guarantees

- Every start creates a new temporary directory (`%TEMP%\violet-testchat-*`) holding the database, data and media; it is deleted automatically at the end (`--keep` keeps it).
  Before deleting, background threads are stopped and the SQLite connections closed; only the directory **created by this run** is deleted (it carries this run's ownership marker), never by wildcard. If it cannot be deleted (for example a file still in use on Windows), the remaining files are reported explicitly and the exit code is 3; normal exit, Ctrl+C and crashes all clean up, and a failed cleanup never hides the original error.
- Test directories left over from earlier runs are only listed, never deleted automatically: `python -m tools.violet_test_chat --list-leftovers`.
  The project directory, `violet.db` and `data/` are refused as test directories.
- `VIOLET_ENV_FILE` points to an **empty file** in the temporary directory: the tools never read a real `.env`, and production configuration, WeChat credentials and the production database are out of reach.
- `VIOLET_*`, `LLM_*` and `MCP_*` variables already set in the shell are cleared; model names and prompts are read only from `env_example` (or `--config`).
- Forced off: WeChat (replaced by a fake in-memory client), MCP, web search and exploration, images, the life engine, the Dashboard, the proactive scheduler thread, startup messages, proxies.
  Proactive messages are judged only once each time you type `/proactive`.
- Every model call (including helper models such as the semantic guard) goes through `BudgetedClient`: **the budget is checked first, then the request is sent**.
- Reports are saved in `tools/output/<time>/` (in `.gitignore`); keys are replaced with `<redacted>` before writing.

### Call details in the report (Phase 7 B.2)

`report.json` / `chat_transcript.json` have one record per model call (`call_details`), with only allowlisted structured fields:
round, agent, model, global / per-round sequence number, the run it belongs to (run_id) and its step, number of request messages, request characters, number and characters of tool definitions, which tools were called, tool result summaries (tool name, ok, action / reason codes, number and ids of returned records, profile keys), latency, `prompt_tokens` / `completion_tokens` / `total_tokens` / `cached_tokens` / `reasoning_tokens`, error type and HTTP status code.

- Not written: keys, full prompts, user messages, memory texts, text inside tool arguments, original error messages.
- Token fields only copy the values the upstream returned; fields the upstream did not give stay `null` and are not estimated. Calls without usage are counted separately, and the budget's token limit does not apply to them (only the call count limit does).
- `call_summary` sums calls, tokens, latency, tool use and runs per agent; the difference between `total_tokens` and prompt+completion is only counted.
- `fit_prompt_tokens_vs_request_chars` is a least-squares fit of prompt_tokens against request characters, **only a statistic**; the intercept is not a confirmed fixed overhead.
- `runs` records each agent run's number of steps and whether it ended right after tool execution (`finished_after_tools`); chat runs include the character count and fact ids of each context section.
- The dialogue text (the user's words, Violet's replies, the final profile state) is still kept in the round records for human scoring; all of it is fictional data.

## 2. Starting (Windows PowerShell)

```powershell
cd "D:\unity\violet\Violet"
..\.venv\Scripts\Activate.ps1

# Chat in simulated mode (no cost, no network)
python -m tools.violet_test_chat

# Run the 32 rounds in simulated mode (checks that the flow runs; replies are "(simulated reply)…", useless for judging naturalness)
python -m tools.dialogue_eval --scripted-memory --quiet-hours off

# Compatibility check in simulated mode
python -m tools.model_compat
```

Commands in the chat:

| Command | What it does |
|---|---|
| `/advance 3` | Pretend 3 hours have passed (moves all recorded times back) |
| `/proactive` | Let the proactive scheduler decide once "should I send something now" |
| `/state` | Show profile facts, situations, memories, promises, Violet's life events |
| `/usage` | Calls, tokens, average latency, errors, estimated cost |
| `/score 2 note` | Score the previous round (0/1/2) |
| `/quit` or Ctrl+C | Stop and save the records |

## 3. Real models: authorisation and exit

All **4 conditions** below must be met; if any one is missing, no real request is ever sent:

1. `VIOLET_TEST_ALLOW_REAL_API=1` is set in the current PowerShell window;
2. `--real` is on the command line;
3. a **separate test key** is given: through the `VIOLET_TEST_API_KEY` environment variable, or typed at a hidden prompt (not echoed, not saved);
4. after reading the "API / models / budget" summary on screen, you type `yes`.

```powershell
$env:VIOLET_TEST_ALLOW_REAL_API = "1"
# Typing the key at the prompt is recommended; if you use the environment variable, set it only in this window:
# $env:VIOLET_TEST_API_KEY = "<test key>"

# Run the compatibility check first (about 11 tiny requests per model, each capped at 16–64 output tokens)
python -m tools.model_compat --real --api-url https://your-test-api/v1 --model grok-4.2 --model gpt-5-mini

# Then chat, or try the first 5 rounds
python -m tools.violet_test_chat --real --api-url https://your-test-api/v1 --max-calls 40
python -m tools.dialogue_eval --real --api-url https://your-test-api/v1 --rounds 1-5 --max-calls 60
```

**Budget** (stops when any one is reached; with unknown prices only calls and tokens are limited):

| Option | Default | Notes |
|---|---|---|
| `--max-calls-per-round` | 15 | Per-round limit; beyond it the round falls back to Violet's degraded reply, and counting restarts next round |
| `--max-calls` | 120 | Limit for the whole run; once reached, the session makes no more calls |
| `--max-tokens` | 300000 | Token limit for the whole run |
| `--price-per-1k` / `--max-cost` | not set | Fill in when you know the price, to cap the estimated cost |

For reference: a normal round takes about 3–8 calls (intent 1–3, chat 1–2, memory 1–3). With the default budget the 32 rounds may stop part way;
to run all of them, estimate the cost yourself before raising `--max-calls`.

**Exit**: press Ctrl+C or type `/quit` at any time; finished rounds are saved. When done, close this PowerShell window, or run:

```powershell
Remove-Item Env:VIOLET_TEST_ALLOW_REAL_API, Env:VIOLET_TEST_API_KEY -ErrorAction SilentlyContinue
```

`--config FILE` may only contain settings such as model names, prompts, temperature and timeouts (for example `CHAT_LLM_MODEL=...`);
if the file contains KEY / TOKEN / SECRET, the tool refuses to start.

## 4. 32-round acceptance (`tools.dialogue_eval`)

The plan comes from `tests/samples/acceptance_dialogue_30.json` (32 rounds, fictional user Xiaoman) and covers: time passing, the user correcting facts,
finished events not being brought up again, proactive messages choosing not to send, the user being busy, consistency of Violet's life experiences, disagreements, and rounds that need recall.

| Option | What it does |
|---|---|
| `--rounds 1-10` | Run only some rounds |
| `--manual` | Manual mode: each round shows the planned message; Enter sends it as is, or type a rewritten version, `/skip` skips, `/quit` stops |
| `--manual-score` | Score right after each round |
| `--scripted-memory` | Use the memory actions written in the plan instead of the memory model, to test only the chat model |
| `--no-memory` | The memory model stores nothing: see how Violet behaves without memory |
| `--quiet-hours off` | Turn off quiet hours (so proactive messages can be judged when testing late at night) |

Output: `report.json` (each round's reply, calls, tokens, latency, errors, automatic evidence, final state) and `score_sheet.csv` (score sheet, opens in Excel).
After filling in the scores, summarise:

```powershell
python -m tools.dialogue_eval --summarize tools\output\<time>\report.json --scores tools\output\<time>\score_sheet.csv
```

### Scoring template

Each round gets 0 / 1 / 2 (2 = good, 1 = acceptable but flawed, 0 = fails), judged against the `rubric` column and the dimensions below:

| Dimension | 2 points | 0 points |
|---|---|---|
| Naturalness | Like a friend replying on WeChat, with fitting length and tone | Customer-service tone, lecturing, template sentences, forced catchphrases |
| Persona fit | Matches Violet's character and way of talking | Like a different person, or overly ingratiating |
| Understanding the background | Uses what she knew before correctly (lives on campus, major, recent events) | Mixes things up, treats where the user usually lives as where they are right now |
| Correct memory | Remembers what she should; uses the new fact after a correction | Uses an old fact that was already corrected |
| No repeated questions | Does not ask again what was already answered | Asks the same thing over and over |
| Finished events | No longer treats them as unfinished | Asks again "how is your exam prep going" |
| No invented experiences | Violet only mentions experiences in her records | Makes up experiences that never happened |
| Not too many questions | Not every message ends with a question | Question after question, interrogation |
| Proactive messages | Chooses not to send when the user is busy / just finished chatting | Disturbs a busy user |

**Per-round notes (Phase 7 B.5; they only add scoring notes, the acceptance script and existing scores are unchanged):**

- **Round 11** (asks "guess where I am now", current location unknown): naturally admits not knowing and goes along with it → 2 points; says clearly it is only a guess,
  without treating the usual residence as a known fact (e.g. "in the dorm? just guessing") → 1 point; states a guess as fact → 0 points.
- **Round 13** (the user just said they are at home, then asks where they are): uses the known location correctly and answers naturally (a joke is fine) → 2 points;
  ignores a clear, valid location and guesses somewhere else → 0 points.
- **Round 14** (nervous before an interview): one caring follow-up question about the user's current worry (e.g. "which part worries you most") is allowed;
  it does not count as a getting-to-know-you question and costs no points, but still counts toward the global question ratio.

The report's `location` (location in the context, places mentioned in the reply and their source, whether the known location was used, whether it was marked as a guess) and the summary's
`reply_location_flags` are only evidence for the scorer and do not change the verdict. A finished report can be re-checked offline with
`python -m tools.dialogue_eval --recheck <report.json>`: the database is copied first, so the test directory is not changed.

**Pass criteria** (`verdict` in `summary.json`): every round scored; total ≥ 54/64 of the maximum; no 0 in the key rounds (9, 10, 14, 24, 25, 27);
no proactive message while the user was busy; replies ending in a question ≤ 40%, and never 3 rounds in a row ending in a question.

Context checks match literally; round 3's "identity: university student" also accepts an equivalent structured profile: a confirmed identity (university student / graduate student…) in this round's chat context, or a confirmed university name together with a confirmed year (first year…) visible to Violet. The word "school" alone, unconfirmed candidates and disabled profile entries do not count. The report's `matched_by` says which rule passed it.

Automatic fields are only **evidence for the scorer** and cannot replace human judgement of naturalness: replies ending in a question, consistency guard flags, mentions of finished events,
whether the intent model called `query_memory` when recall was needed (the observation metric for risk R‑M2), and calls / tokens / latency / error rate per round.

Round 16's `ended_event_not_allowed` check (Phase 7 B.6.1) requires, besides "finished events are not in the list proactive messages may mention", that memory has
no **two contradictory valid records of the same thing**: one finished (done / cancelled) and the other still unfinished (e.g. "progress unknown").
When found, the check fails and `contradictory_events` lists the ids of both records and the basis of the judgement (text similarity or shared keywords).

### Cross-round replay and continuing a test (Phase 7 B.3)

`--replay-from <report.json of a real test>` rebuilds the rounds before the selected ones **offline** in an isolated environment, then goes on with real (or simulated) rounds:

- Only reports of real-model tests (`real: true`) are accepted, and they must contain exactly the rounds to replay; each round's user message must match the script word for word.
- Violet's replies are taken word for word from the real replies in the report and sent and stored through the normal message pipeline; the replay creates no real model client, sends no network requests,
  counts no calls or tokens, and is not scored.
- Profile and events are restored only from what the report can confirm (values in the final state + rounds and actions in the call details); profile evidence uses the value itself (which must appear word for word in
  that round's message); event dates are used only when the memory text contains a date. Metadata missing from the report is listed in the report's `replay.not_restored`,
  and differences from the script's memory steps in `replay.differences_vs_plan`.
- Script clock: starts at the original run's start time (the time of the script's preset life events + `hours_ago`), and advances each round by the call durations recorded in the report;
  later rounds continue on this time and do not use the host's current date.
- After the replay, profile, events, Violet's life (including share counts), situations and reminders are checked item by item; any mismatch with the report stops it (exit code 4) without making any real call.

`.violet-eval-state.json` in the test directory records the replay state and finished rounds. With `--keep` the directory is kept, and next time the same `--workdir`
plus `--rounds <next round on>` continues (without replaying again). Always refused: replaying again, overlapping or non-consecutive rounds, a replay that did not finish successfully,
a directory with chat history but no state file, a real test on a directory containing simulated replies.

The proactive message check gives the blocking reason: scheduler not triggered / quiet hours / user busy / cooldown / user preference or daily limit / model decided not to send /
already sent / error. Round 9 passes only if **this round saved a busy state, the busy rule blocked it, and nothing was sent**; round 10 checks that the busy state
is no longer valid. If any item fails, the summary verdict is fail.

**Replaying several reports in a row (Phase 7 B.5)**: `--replay-from` can be given several times in order, for example the report of rounds 1–5 first, then the report of rounds 6–10.

- Rounds must be consecutive, without repeats or overlap; a later report must have recorded that what it replayed was exactly the previous report (same hash), otherwise it refuses to run.
- After each segment is replayed it is checked item by item against that segment report's final state: content and order of the chat history, profile, events, situations (including expiry times), reminders,
  times and share counts of Violet's life events.
- Situations are restored only from what the report can confirm: kind and content from the final state; the expiry time preferably from the original value recorded by that round's busy check,
  otherwise estimated from the final value plus the later old-style time advances; evidence is the whole user message (the report has no tool arguments).
- Reports recorded before B.4 (no `scenario_time` per round) replay the old-style time advance of that time (shifting all timestamps) and round 9's proactive check in their original place,
  and check the blocking reason and the situation at the end of round 10 word for word; reports after B.4 use the recorded per-round scenario time directly.
- The report's `replay` holds each segment's check results, clock drift, replayed checks, the situations valid at the end of the replay, and which round's memory job wrote each situation.

### Scenario time and script revisions (Phase 7 B.4)

- Advancing time only advances the session's **scenario clock** (`/advance`, the script's `advance_hours` / `time`, round 9's 1.6 hours);
  saved messages, profile evidence, event times, life events and situation times are never rewritten, and saved absolute dates stay as they are.
  Situation expiry, events coming due (planned → uncertain), reminders and proactive decisions all use the same scenario time. Advancing itself sends no messages and calls no model;
  reminders are handled only in the scheduler checks the script asks for. Time arithmetic is done in UTC, and crossing days, time zones and daylight saving all use the real elapsed duration.
- The report records `scenario_time` for each round; rounds with an advance record `time_advance`: start and end time, hours, situations that expired naturally during the advance,
  and events whose state changed because of the time.
- The old "shift all timestamps" function `advance_time` is still kept: live rounds never call it; it replays the advance of that time in its original place only when replaying reports recorded before B.4 (Phase 7 B.5).
- The acceptance script `acceptance_dialogue_30.json` is not changed. Revisions are written in a separate `acceptance_dialogue_30.rev2.json`, bound to the original script's hash,
  and may only change `time` / `advance_hours` of unfinished rounds; `--plan-revision auto` (default) uses the latest matching revision, `none` the original script.
  Kept test directories check the original script's hash. Each finished round records which version it ran with (`plan_revision`), and the state records each revision's
  file hash; continuing checks round by round (Phase 7 B.7.1): finished rounds changed by the current revision must have run with the same revision and the same file hash;
  rounds run with another version may not be changed by either version; old rounds without a version record may not be changed by the current revision. Finished rounds must also be consecutive
  from round 1 without repeats. A directory copied with `--seed-from` and a direct `--workdir` continuation go through the same checks.
  Revision 2: round 15 is at 10 a.m. the day after the "next Wednesday" mentioned in round 4 (after the thesis defence date), no longer 30 hours after round 14.
- Round 11 "doesn't know where" and round 13 "currently at: home" are judged by the situations in this round's chat context: round 11 requires no current location in the context
  (an activity situation alone still passes); round 13 requires a current location in the context whose value is one of "home / at home / in the home / at home (variant) / back home / hometown",
  traceable to the memory processing of an earlier round; in a real test it must have been written by the real MemoryAgent. The word "home" appearing in the chat history does not count.
  Places mentioned in the reply (dorm, school, home…) are listed in `places_in_reply` for human scoring.

### Copying test directories and continuation replays (Phase 7 B.6.2)

- `--seed-from <saved test directory>`: copies its `violet-test.db` and `.violet-eval-state.json` **read-only** into a `--workdir` that does not exist yet,
  then continues under the continuation rules. The source directory is only read: hashes are compared before and after copying; a database with a non-empty write-ahead log is refused (data may be missing);
  the source directory is opened read-only in immutable mode when checked, so no `-wal` / `-shm` files are created in it.
- `--continue-replay <report> --continue-replay-sha256 <hash>` (must be given together with `--seed-from`): uses "the real report that ran on top of this directory"
  to rebuild the immediately following round offline; for example, if the directory is complete up to round 13 and `--rounds 15`, round 14 is replayed. Refused when:
  - the report's hash differs from the one given (the report was changed), or no hash is given;
  - the report's `completed_rounds_before` differs from the directory's rounds, the script or revision hash differs, or the scenario time of the first replayed round differs from the directory's
    `clock_end` by more than 1 millisecond (not the same timeline);
  - the user message differs from the script, the round has a proactive check or time advance, or contains writes other than `save_memory` (including needs_decision → create);
  - a memory it created was changed in later rounds (what it looked like at the time cannot be confirmed);
  - the directory already has this round (no replaying twice).
- During the replay it checks: content and progress of similar candidate memories match the report's starting point, the next event id matches the report, no memory jobs are unfinished;
  Violet's replies match word for word, the result of each memory write (needs_decision candidates, created ids) matches the report; content, type, progress and date of created memories match;
  apart from this round's messages and memories, profile, situations, reminders, life events and the number of getting-to-know-you questions are unchanged; no background jobs are left unfinished at the end.
  The scenario clock stops at the start of the next round recorded in the report (before the jump).
- Any mismatch stops it (exit code 4); the continuation replay in the state is marked `failed`, and this directory can no longer be continued; no billable call is made.
- Metadata missing from the report (save_memory's category, importance, emotional weight, exact time, the exact times of messages and jobs) is listed in `not_restored`.
- `--replay-only`: only replays and saves the state, without running the rounds in `--rounds`. Prepare a baseline directory once, and for every later real re-test
  copy a fresh one with `--seed-from <baseline directory>`, so they do not affect each other.

### Offline mock images (Phase 7 B.8.1, `--image-mock`)

Images are off by default (`VIOLET_IMAGE_ENABLED=0`, and the test WeChat stand-in refuses to send images); that is unchanged. `--image-mock` turns images on, but the whole image path stays offline:

- **Only the mock image model**: `image_providers.mock.json` is written in the test directory with a single offline `mock/mock-image-1` (generates a solid-colour PNG,
  no key needed). At startup both the raw file and the product registry's reading of it are checked: any non-mock adapter, `base_url`, `api_key_env`
  or URL output is refused (exit code 2, no directory left behind).
- **Enforced by the program, not by prompts**: in this run every image adapter other than mock is replaced by a stand-in that refuses; the image adapters' HTTP transport refuses every request;
  on top of that a socket-level block only allows connections to the text model API (the `--api-url` address with `--real`) and asyncio's internal loopback self-connections;
  every other connection (including other services on the machine) is refused and recorded. The report's `network_guard` lists allowed and blocked connections.
- **Test sender**: when an image is sent, the file is copied to `<test directory>/sent_images/` and `sent_images.jsonl` records the round, file, bytes and
  sha256; WeChat is never contacted. When sending fails there is no record, and the outbox shows failed.
- **Each round's `images`** records image jobs, calls, generated images and sending (only ids, status codes and hashes, no prompts).
  **`image_checks`** judges three things separately:
  1. whether the model called `create_image`;
  2. whether the mock successfully generated an AI image (job succeeded, image usable);
  3. whether the test sender recorded the same image (outbox sent, same sha256).

  The script's `tools_has` / `tools_lacks` / `db: violet_picture_sent` are judged on these three; only calling the tool does not count as sending an image.
- The round right after the image round (round 22) checks its precondition: an AI image was really sent before (judged from the database; also for continuations and copied directories).
  If none was sent, this round's image-authenticity acceptance is marked invalid (`valid: false`) and listed in the summary's `image_rounds_invalid`.
- Evidence flags on replies: calling an AI image a photo, something taken, or taken by a camera (`claims_photo_or_camera`); saying "I drew this for you / look at this" without having sent an image
  (`claims_picture_without_one_sent`). The product's own reply guard first deletes or rewrites sentences that "call it a photo"; only the ones that slip through are flagged here.
- **What cannot be verified**: mock images are solid blocks of colour, so the image content itself (whether it looks like reading, the style, whether it fits the persona) cannot be verified; the protocol,
  cost and quality of real image services **need verification in production**.

### Verdicts of script checks (Phase 7 B.9.1)

Each of the script's `tools_has` / `tools_lacks` / `db` checks is recorded in that round's `checks`, with one of four results:

| Result | Meaning |
|---|---|
| `PASS` / `FAIL` | Normal verdict |
| `NOT_APPLICABLE` | The feature is off in this run, **not tested**: image checks without `--image-mock`; web tools (the test tool has no web MCP). Comes with a reason |
| `UNSUPPORTED` | The test tool cannot judge it: an unknown tool or an unimplemented `db` check. Listed explicitly, never silently skipped |

- Tools are grouped: images (`create_image` / `edit_image` / `look_at_image`), web (`violet-web__*`), other. Only image results go into
  `image_checks` and the summary's `image_check_failures`; failures of other groups go into `check_failures`.
- `db: commitment_created` (round 26): a reminder created in this round (not an earlier one), status pending; content matches the script's reminder item;
  the evidence must, by the product's own rule (`check_explicit_request`), be an explicit request in the user's own words; the due time per the script (`tomorrow` + `08:00`), computed from that round's scenario
  date in Violet's time zone, must be the same instant. If anything is not met, it is FAIL with the reason written down.
- Summary: `check_failures`, `untested_checks` (NOT_APPLICABLE / UNSUPPORTED), `image_rounds_not_applicable`.
  With untested checks the verdict is at best `pass_with_untested_checks`, never `pass`.

### Complete execution, budget reservation, due reminders (Phase 7 B.10.1)

**"The round ran" and "the background jobs all finished" are two different things.** Each round also records:

- `memory_jobs`: final state, attempts and error type of this round's memory jobs (nothing beyond that; keys are replaced);
- `background_complete`: whether this round's memory jobs are all `done`;
- `execution_errors`: things that did not finish, other than API errors — unfinished memory jobs, calls refused by the budget (never sent), user messages that failed to process.

With `execution_errors`, the summary verdict is `fail_execution_incomplete` even with 0 API errors and full marks;
the summary also has `execution_errors` and `rounds_with_unfinished_background_work`. The state file records `background_complete` for each round too.

**Memory jobs refused by the budget are no longer retried immediately**: the test tool's retry interval is 0 and the product's MemoryWorker would retry twice right away, but a budget refusal cannot recover within
this test. The test tool simply marks such jobs failed (the error message ends with `[test tool: a budget refusal is not retried]`);
the product's own retry policy is unchanged.

**Continuing**: when the test directory has unfinished memory jobs (`memory_jobs` in the database not `done`, or the state file says `background_complete: false`),
`--seed-from` / continuing is refused. To accept that state, add `--accept-incomplete-memory`; the report's `continuation` lists the accepted jobs.

**Budget reservation**: before every real call it checks whether "tokens used (the usage reported by the provider) + this call's reservation" exceeds `--max-tokens`.
The reservation is an estimate: the same agent's largest previous call × 1.1; if this agent has not called yet, the largest call of all × 1.1;
if there has been no call at all, 16,000. If it exceeds, the call is refused and the test stops. The report's `budget` records separately:

| Field | Meaning |
|---|---|
| `used_tokens` / `remaining_tokens` | Real usage reported by the provider and what remains (**no** estimates; usage recorded as is) |
| `stop_detail` | Why it stopped, with usage, remainder, reservation and the basis of the reservation at that moment |
| `refused_calls` | Refused calls (agent, round, reason): none were sent, none are billed |

**Limitation**: the upstream service has no "maximum tokens per request" limit, so the check before a call can only ensure "no call is started knowing it will exceed".
If one call is larger than its reservation, total usage can still go slightly over the limit.

**Round 28's `allowed_or_skipped_without_bypass`**: the result of the scheduler check before it is split in two:

- `reminders`: reminders already due before the check, each recording `commitment_id`, the reminder event, the outbox record and `delivery`:
  - `sent`: the reminder is sent, the matching `commitment_reminder` event is sent, that event's outbox record is sent and mentions the reminder's subject;
  - `postponed_quiet_hours`: in quiet hours, postponed per the product's rules;
  - `not_sent` / `sent_without_matching_message`: the reminder did not succeed. **An ordinary proactive message does not count as a successful reminder**, even if it mentions the same thing.
  - `late_minutes` / `late_cause`: how late it was compared with the due time. When one advance of the scenario clock jumped past the due time it is marked "delay caused by a test time jump"
    (the test tool runs no background scheduler thread, so reminders are only handled in this check; in production the scheduler thread checks every minute).
- `chat_proactive`: ordinary proactive chat events and the messages sent, kept apart from reminders.

Verdict: an ordinary proactive message sent without going through the scheduler gate, a reminder sent during quiet hours, or a due reminder not sent successfully (except one postponed until quiet hours end) all fail.
The test only sends to the local simulated WeChat, never to real WeChat.

## 5. Model compatibility check (`tools.model_compat`)

Checks item by item: a basic text reply, `temperature`, `max_tokens` vs `max_completion_tokens`, `reasoning_effort`, tool_calls,
returning tool results (multi-turn tool calls), `response_format=json_object`, JSON output from the prompt alone, and a 4xx for an unknown model.
Errors are classified by Violet's retry rules: `rate_limited` / `server_error` / `timeout` / `transport` are retried, `bad_request` is not.
429 / 500 cannot be triggered safely on a real API, so that part is verified offline with a simulated provider; `--include-timeout` sends one extra timeout request in real mode.

Results are judged only from the API's actual responses, never from the model name. The results suggest entries for `config/model_params.json`;
they are written only with `--write-policy`, on a real check, after typing `yes` once more (with `_verified` recording the date and report name).
No configuration of the provider or relay service is ever changed.

## 5b. Switches that cut redundant model calls (Phase 7 B.2)

All four switches default to `1` and can be set to `0` in a `--config` file for comparison (for example, running the same rounds once each way):

| Switch | What it does | How to see it in the report |
|---|---|---|
| `VIOLET_INTENT_HIDE_EMPTY_MEMORY_TOOL` | When memory has no event that `query_memory` could find (deleted ones and ones the user disabled don't count; expired ones still count), IntentAgent is not offered this tool | Intent calls have 0 `tools`, and Intent is called only once per round |
| `VIOLET_MEMORY_FINISH_SIGNAL` | Offers `memory_done` in the background memory job: when the model writes and calls it in the same step, and every write returns a clear success, the job ends right there without asking for a closing reply | MemoryAgent in `runs` has `finished_after_tools: true` and one `step` less |
| `VIOLET_MEMORY_HIDE_EMPTY_QUERY_TOOL` | (Phase 9.4, cys-p94-sched-r1) When memory has no event memories at all (ones disabled by the user count as present, because MemoryAgent sees them when querying and avoids storing them again), MemoryAgent is not offered `query_memory` and is told there can be no duplicates, so it decides directly; with any event memory it is offered as usual and told to query first as usual | MemoryAgent's first call has no `query_memory`, one call fewer |
| `VIOLET_CONTEXT_HIDE_LEGACY_NICKNAME` | When a confirmed form-of-address profile exists, the chat context no longer shows the form of address from the old core memory (display only, data unchanged) | The `legacy_core` section of the chat context gets shorter |

Whether a real model uses `memory_done` can only be verified with a real model; when the model does not call it, behaviour is exactly as before (no saving, and no lost writes).

Each job in the report's `memory_jobs` also has `outcome` (Phase 9.4 r1): `complete` (the model finished, status=done), `partial` (part was written, then interrupted; `writes` lists the writes that really succeeded in each attempt), `interrupted` (attempted, wrote nothing, did not finish), `unprocessed` (not attempted yet); `budget_interrupted: true` means the last attempt was a budget refusal by the test tool (the test tool does not retry it, `budget_not_retried`). `status=failed` no longer means "nothing was recorded"; look at `outcome` and `writes`.

## 6. WeChat behaviour that cannot be simulated locally

The following can only be verified in a real WeChat environment (**needs verification in production**):

- the real rhythm of iLink long polling, duplicate deliveries, resuming with `get_updates_buf`, and login expiry;
- the real debounce merging when the user sends several messages in a row (the test tool shortens debounce to 0.05–0.1 seconds);
- the "typing…" indicator and how the simulated typing delay feels (off in tests);
- WeChat message length limits, how split messages display, rendering of emoji and special characters;
- sending and receiving images, voice and files, and CDN encryption/decryption (the test tool handles no media; images are off);
- whether `send_image` really works;
- the rhythm of proactive messages in real time (simulated in tests with `/advance` and `/proactive`, not a background thread);
- latency and failures of the phone MCP, web MCP and real MCP servers;
- the user's real reply habits (the test user is fictional and follows a script).

## 7. B.11.2 / B.11.3 synthetic-seed special test of rounds 27–28

This is a **synthetic special test**: not a restoration of the original rounds 1–26 database, not the original continuous acceptance, and not counted in the original 32-round total.

Run it in the `Violet` directory with a persistent directory that does not exist yet, inside the workspace's `local/` and not under the Windows TEMP:

```powershell
..\.venv\Scripts\python.exe -X utf8 -m tools.synthetic_rounds --workdir D:/unity/violet/local/b112/my-new-run
```

Offline by default; it uses the existing VioletTestSession, ScenarioClock, the local WeChat stand-in, the real MemoryAgent tool handlers and the reminder scheduling code.
By default NetworkGuard refuses every external network and local service connection. An existing directory is refused before anything is written, and a failed run keeps its directory.

Read-only sources are three historical `report.json` files: `20261014-001147`, `20261014-002003`, `20261013-073654`:

- `seed.json`: 5 profile items, 3 events, the user's and Violet's words in rounds 24–26, the pending homework reminder, and source hashes / JSON pointers.
  Each state field is marked `report`, `prior_observation`, `inferred` or `synthetic`; data the original reports lack is listed as unknown with value null.
- The report's end time is `2026-10-22T16:13:52.562000+08:00`; as this phase requires, the seed truncates it to `16:13:52`, marked inferred, with the exact original value kept separately.
  The "fewer follow-up questions" value is conservatively inferred as `less` from round 19's words; the report only kept the write action, not the full preference row, so no exact restoration can be claimed.
- `seed_materialized.json`: field-by-field values and sources of the five kinds of seed tables in the new database. New IDs, default weights, evidence/audit and write times are synthetic;
  the original sending time of Violet's replies is unknown, so the approximate record using the start of the matching round is explicitly marked inferred. Violet's life is not restored, and an expired location is not treated as the current one.
- `.violet-synthetic.json`: a separate scope marker. The original replay, `--seed-from`, continuous acceptance, summarize, recheck and the original score import all refuse data in this scope.
  `.violet-eval-state.json` is not written, and "original rounds 1–26 restored" is never faked.
- `specialized_report.json`: round 27 simulates a short wrap-up plus an adversarial probe against mis-writing a long-term preference; round 28 first advances 20 hours, then handles the reminder, the duplicate check, the user's "morning" reply and a quiet-control probe.
  `proactive_chat=no` is a separate synthetic control condition, not a historical preference and not the result of round 27's writes.
  All probes act only on the new database and the test directory's configuration.
- Round 28 checks the IDs/states of commitment, proactive event and outbox, and the reminder text; the scenario jumps to about `12:13:52` the next day before the scheduler is called,
  so it is about 253 minutes later than `08:00`. The report marks "delay caused by a test time jump"; **on-time delivery in the real environment was not verified**.
- `specialized_scores.csv`: keeps empty human-score columns for rounds 27 and 28. Copy it to a new special-test score file before filling it in, keeping the original evidence;
  simulated lines passing the literal checks do not mean a real model will wrap up naturally, so human judgement is required, and special-test scores may not be imported into the original total.

Offline regression: `run_tests.ps1 -q tests/test_synthetic_rounds_b112.py tests/test_reminder_b101.py`.
Mutation tests may only run serially in a separate worktree; the method and where this phase's evidence is are in section B.11.2 of `docs/knowledge/testing.md`.
### B.11.3 Controlled real text-model path

Requires all of `--real`, `VIOLET_TEST_ALLOW_REAL_API=1` in the current shell, a text API address, a test key and typing `yes` interactively.
It reuses the existing RealApiGate, VioletTestSession's OpenAI client, BudgetedClient and NetworkGuard; there is no second client.
The key is given by hidden input or the existing test environment variable and is never written to a config file; this entry does not show the key's prefix or suffix, and has no `--yes` option to bypass confirmation.
The API URL may not contain a username, password, query or fragment. Only connections to the IP/port the configured API resolves to are allowed; with a proxy, the proxy connection is refused too.
WeChat is always the local stand-in, and images, MCP, web and the background life thread are off; a real text model does not mean real WeChat delivery.

On the real path the model actually runs Intent / Chat / Memory, with no simulated MemoryScript injected; round 27 keeps per-agent call counts and the human scoring entry.
Round 28's reminder first goes through the real scheduling flow; if the model's output is invalid and it falls back to the template, or any model call fails, the special test fails even if local sending succeeded.
`stages` records chat, reminder_due, repeat_check, user_return and the quiet probe separately; calls are tagged with their actual round and stage,
and the totals of `calls`, per-round `calls`, `accounting` and `call_summary` must agree. The chat call that generates the reminder counts toward round 28,
and the scheduler and user return share that round's budget, which is not reset when the stage changes. `budget_reservations` lists each call's reservation and approval/refusal;
`budget.refused_calls` keeps requests that were not sent. A budget stop and a model failure are recorded as `stopped_budget` / `failed_model_call` respectively and never marked passed.

Default limits: 10 calls per round, 16 in total, 150000 tokens; the first reservation is 16000, then the existing largest-call × 1.1 rule applies.
The reservation is an estimate, and a single upstream response may exceed it; real usage follows the provider's usage and is never faked when missing. `--price-per-1k` and `--max-cost` can add a cost threshold.
The expected 6–12 calls and about 40k–100k tokens are only a planning range; two historical reports for reference had 16 calls/134621 tokens and 5 calls/44444 tokens,
but the synthetic context differs, and actual usage varies with tool turns, retries, model and prompts, so it is neither a quote nor a hard limit.

The command below is only for the user to run by hand after review; **no real call was made** during B.11.3 development. Use a fresh directory each time; after starting, enter the key at the hidden prompt and confirm the budget:

```powershell
Set-Location 'D:\unity\violet\Violet'
$env:PYTHONIOENCODING = 'utf-8'
$env:VIOLET_TEST_ALLOW_REAL_API = '1'
Remove-Item Env:VIOLET_TEST_API_KEY -ErrorAction SilentlyContinue
$textEndpoint = Read-Host 'Text model API base URL'
$textHost = ([Uri]$textEndpoint).DnsSafeHost
$env:NO_PROXY = "$textHost,127.0.0.1,localhost"
$specializedDir = 'D:\unity\violet\local\b113\real-' + (Get-Date -Format 'yyyyMMdd-HHmmss')
& ..\.venv\Scripts\python.exe -X utf8 -m tools.synthetic_rounds --real --api-url $textEndpoint --config 'D:\unity\violet\local\violet-test-config\gpt6luna.env' --workdir $specializedDir --max-calls-per-round 10 --max-calls 16 --max-tokens 150000
```

That config must contain only model settings the tool allows and no key; without a suitable file, omit `--config` to use the env_example model settings, and check the model shown by the gate first.
The special-test report still carries the synthetic / specialized_test / non-continuous markers and cannot be imported into the original 32-round acceptance; keep human scores in a separate copy without changing the original evidence.
Real-model naturalness, actual tokens/cost, real WeChat and real on-time delivery each need their own verification and cannot be inferred from offline fake SDK results.

## 8. B.11.5 Separate synthetic special test of rounds 29–32

Uses the same controlled runner with the new `--suite round29_32_specialized_test`; without a suite it still runs the original 27–28 special test.
Fully offline by default; Intent queries, Memory corrections, ContextEngine, background jobs, SQLite and local sending all go through product code, and only model responses are simulated.
The real path still goes through B.11.3's same RealApiGate, confirmation, budget, network allowlist and redacting client; there is no new API client.

```powershell
Set-Location 'D:\unity\violet\Violet'
& ..\.venv\Scripts\python.exe -X utf8 -m tools.synthetic_rounds --suite round29_32_specialized_test --workdir 'D:\unity\violet\local\b115\my-new-mock' --max-calls 32 --max-tokens 300000
```

Every directory must be fresh, persistent and inside the workspace's local. Failures keep their evidence and never overwrite old reports or seeds.
Seed sources: the three historical reports used by B.11.2, plus the reminder-sent evidence in `local/b113/real-20261019-014631/specialized_report.json`.
Source reports and the original script/rev2 are not modified, and no database is copied; the seed lists path, hash and JSON pointer field by field.
The sent reminder's local ID, event/outbox links, defaults and new audit fields are synthetic; old metadata, the full preferences/dialogue/life and the original times of 29–32 are unknown.
The "fewer follow-up questions" value less stays marked inferred; B.11.3's proactive_chat=no, all-day quiet, quiet probe and recent defence answer are not imported.
The anchor is the existing reminder's sending time `2026-10-23T12:15:12.640000+08:00`, marked inferred with a note that the original round 29 time is unknown; no extra date is set and no time jump made.
"I handed in my homework yesterday" is read as said, without jumping a day for it on our own. No Eve life is preloaded.

- R29: homework handed in is not preset; verifies that the reminder is not rebuilt and that sending/event/outbox are not duplicated. Scheduler dedup is explicitly marked as an extra probe, not a rewrite of the original script's scheduling step.
- R30: presets the defence that is done in the report; no recent-dialogue seed. Checks that Intent really called query_memory and got the target ID/content/done back, and that Chat really had the matching event row; a generic "already finished" hint cannot replace it.
- R31→32: presets only the old major, astronomy; waits for the real MemoryAgent to finish, then checks the new physics fact, the old fact superseded, and the replacement IDs both ways. The same database goes straight into round 32, checking the actual context fact_ids and the answer. If 31 fails, 32 is not run, and physics may not be seeded in advance.
- The report keeps the original script's user/rubric, per-round state, sources, calls/stages/budget and empty human-score columns. Literal checks of replies still have limits; human naturalness and the original 32-round cumulative metrics are not proven by automatic results.

The new purpose, even when it appears alone, is refused by original plan/report reading, replay, state continuation, seed copying, summary/recheck and score import; it cannot count toward the original 32 rounds and does not restore the original continuous database.
A separate worktree does not contain the ignored reports, so to re-run offline/mutation tests the four reports above must be copied byte for byte into a fresh path in the worktree and their hashes checked; fake history may not be generated in their place.

The real command below is only for the user to run by hand after review; it was **not run** in this phase. The known model settings file must not contain a key, and the user checks the model shown by the gate; enter the key at the hidden prompt and type yes to confirm:

```powershell
Set-Location 'D:\unity\violet\Violet'
$env:PYTHONIOENCODING = 'utf-8'
$env:VIOLET_TEST_ALLOW_REAL_API = '1'
Remove-Item Env:VIOLET_TEST_API_KEY -ErrorAction SilentlyContinue
$textEndpoint = Read-Host 'Text model API base URL'
$textHost = ([Uri]$textEndpoint).DnsSafeHost
$env:NO_PROXY = "$textHost,127.0.0.1,localhost"
$tailDir = 'D:\unity\violet\local\b115\real-' + (Get-Date -Format 'yyyyMMdd-HHmmss')
& ..\.venv\Scripts\python.exe -X utf8 -m tools.synthetic_rounds --suite round29_32_specialized_test --real --api-url $textEndpoint --config 'D:\unity\violet\local\violet-test-config\gpt6luna.env' --workdir $tailDir --max-calls-per-round 10 --max-calls 32 --max-tokens 300000
```

The four real rounds are expected to take 14–20 calls and about 120k–180k tokens, an estimate only; based on the earlier real 27–28 special test's 8 calls/66905 tokens, R30 adds a query and R31 may add tool turns.
The command's limits are 32 calls, 10 per round, 300000 tokens; the reservation rules follow the original mechanism, and a single response can still exceed the estimated reservation. Prices are unknown, so no made-up amount is given; a cost limit can be added from the actual unit price.
A real text model does not verify WeChat or real on-time delivery. Test records and method are in the knowledge base testing.md §8.

### B.11.6 Re-test boundaries after the failure

B.11.6 fixed the literal matching of natural recall questions at the query_memory entry and changed the R30 simulated query to the user's full original question; the special-test assertions were not lowered.
The user's existing real passing report of R29 should be kept; a separate re-test of only R30 and 31→32 can be designed later, and 31→32 must use the same database and wait for the correction job to finish.
**At B.11.6 the CLI had no round selection or failure continuation options yet**, so the four-round command above cannot be treated as "test only 30–32". B.11.7 adds restricted round selection, see §9; continuing from an old database is still not supported.
In a new directory, starting from the traceable seed or from a dedicated copy of the failed database must be clearly distinguished and the choice disclosed; the original failed database may not be modified or continued, and the special test may not be counted in the continuous acceptance. The model's actual query/category was not saved in the original report, so the exact trigger parameters remain unknown; an offline fix does not mean the real-model re-test has passed.

## 9. B.11.7 Controlled round-selection re-test

Adds `--rounds 30-32`, only for round29_32_specialized_test. The default or 29-32 still runs four rounds; other choices such as 31-32 or 32 are not accepted, so earlier verification cannot be bypassed.
A new directory starts from the existing synthetic seed; it does not read the old database, does not import round 29's dialogue/memory writes, and does not preset physics. 29 is not run this time; the report lists the historical real passing source and its hash/pointer separately, and refuses to start without that evidence. A fifth read-only source report `local/b115/real-20261019-041807/specialized_report.json` must be kept; the other four sources are as in §8.
A failure in 30 stops the rest; if 31 fails, 32 cannot run, and 31→32 use the same database. The report only has score columns for the selected rounds, and the call ledger only counts actual calls, never adding historical round 29's calls/scores to this run.
memory_query_audit records the actual tool arguments, the actual database query topics and filter range, and the returned event IDs/count/progress; chat_context_audit records at the chat request boundary whether the target got into the actual event rows. Arguments go through credential redaction and are never back-filled from assertions; empty queries/errors do not invent search results. The audit may contain special-test dialogue and is kept only in local evidence; ordinary tool reports still keep only summaries.

Offline run:

```powershell
Set-Location 'D:\unity\violet\Violet'
$offlineDir = 'D:\unity\violet\local\b117\mock-' + (Get-Date -Format 'yyyyMMdd-HHmmss')
& ..\.venv\Scripts\python.exe -X utf8 -m tools.synthetic_rounds --suite round29_32_specialized_test --rounds 30-32 --workdir $offlineDir --max-calls-per-round 10 --max-calls 24 --max-tokens 250000
```

The real command below is only for the user to run by hand after separate authorisation; it was not run in B.11.7. The test key must still be entered at the hidden prompt and yes typed to confirm:

```powershell
Set-Location 'D:\unity\violet\Violet'
$env:PYTHONIOENCODING = 'utf-8'
$env:VIOLET_TEST_ALLOW_REAL_API = '1'
Remove-Item Env:VIOLET_TEST_API_KEY -ErrorAction SilentlyContinue
$textEndpoint = Read-Host 'Text model API base URL'
$textHost = ([Uri]$textEndpoint).DnsSafeHost
$env:NO_PROXY = "$textHost,127.0.0.1,localhost"
$selectedDir = 'D:\unity\violet\local\b117\real-' + (Get-Date -Format 'yyyyMMdd-HHmmss')
& ..\.venv\Scripts\python.exe -X utf8 -m tools.synthetic_rounds --suite round29_32_specialized_test --rounds 30-32 --real --api-url $textEndpoint --config 'D:\unity\violet\local\violet-test-config\gpt6luna.env' --workdir $selectedDir --max-calls-per-round 10 --max-calls 24 --max-tokens 250000
```

The real 30–32 re-test is expected to take 10–16 calls and about 80k–140k tokens: historical round 30 actually took 5 calls/43172 tokens, and 31–32 have not been run for real, so the estimate is uncertain. The command's limits are 24 calls/10 per round/250000 tokens, with the first 16000 and later reservation rules; the actual bill depends on usage and unit price, and neither model naturalness nor real WeChat delivery is guaranteed. A fresh directory is required, and historical evidence is never overwritten.

## 10. B.11.8 Lowest-cost re-test after the expanded-query fix

No real API is run in this phase. First the two full queries from the B117 real audit serve as offline regression; short-word queries cannot replace them:

```powershell
Set-Location 'D:\unity\violet\Violet'
.\run_tests.ps1 -q tests/test_expanded_recall_b118.py
```

The local evidence `local/b118/20261019-codex/exact-queries-mock/specialized_report.json` is a simulated 30–32, not a real-model pass; in it 11 simulated calls with the two real arguments both returned ID1/done and got into the actual chat event rows. The original failure evidence `local/b117/real-20261019-053449` is kept.
The audit's normalized_query is still the actual shell-normalised query passed to the database; the retrieval of returned events records topics/focus and original_similarity/topic_similarity/similarity, meaning the literal topics, the original character score, the topic score and the combined score respectively; the expected answer is never used as input.
Model expansion words no longer push down a main topic that already matched; compound words are not split into arbitrary prefixes, and zero/negative thresholds cannot return all records without literal evidence. Only the query path enables this; write dedup, thresholds, candidate range and permission rules are unchanged. Intent is fixed at 0.35, Memory uses the threshold actually passed, and the schema was clarified.
This is lightweight lexical retrieval, and it does not guarantee synonyms with no shared characters, unfounded qualifiers inside a single compound word, a wrong first topic, or semantic disambiguation of several events on the same topic. Common-suffix matching is candidate expansion and cannot prove a modifier or event identity; expired still carries the old status label, and disabled/deleted add no context permission.
After review and separate authorisation, the lowest-cost existing real entry is still the 30–32 command of §9, using a new `local/b118/real-<timestamp>` directory; 29 is not paid for again. If 30 fails, 31–32 stop; only after 30 passes does the same-database correction continue. Selecting only 30 is not supported, and budget truncation may not be disguised as a complete pass.
Given this run's failed 30 at 5 calls/42896 tokens, a successful 30 still needs real-model verification; the full 30–32 is estimated at about 10–16 calls/80k–140k tokens, an estimate only, and the retrieval metadata adds a little context. The 24 calls/10 per round/250000 token limits and the original gate and reservation rules stay, and actual accounting follows the provider's usage. No new paid model or extra retrieval API was added.

## 11. B.11.9 Round 30 reply assertion and post-hoc offline reassessment

Round 30's text assertion is `round30.answer_recalls_outcome/v2` (`recalls_completed_outcome` in `tools/synthetic_tail.py`); the structural checks share `round30_checks` with the runtime, and the report records `answer_assertion` for round 30. Root cause and boundaries are in B.11.9 of `docs/knowledge/bugs-and-risks.md`.

Post-hoc offline reassessment of an existing real run (no model calls, the original directory is not changed, the output directory must not exist):

```powershell
Set-Location 'D:\unity\violet\Violet'
& ..\.venv\Scripts\python.exe -X utf8 -m tools.reassess_tail --source 'D:\unity\violet\local\b118\real-20261019-060349' --out ('D:\unity\violet\local\b119\reassess-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
```

Output `round30_reassessment.json`: kind=post_hoc_offline_reassessment, model_calls=0, SHA-256 of the 7 source files, the v1/v2 verdict difference, violet_head, and whether tools has uncommitted changes. Missing evidence, failed calls, a non-real run, or v1 failing to reproduce the original record give "cannot be verified". 31–32 stay "not run", and the original report's scores and human scores are unchanged.

## 12. B.11.10 Separate acceptance entry for rounds 31–32

`--rounds 31-32` is only for `round29_32_specialized_test`. Rounds 29 and 30 call no model; the report's `skipped_rounds` marks them "not run" and cites the historical evidence; no manual PASS option is accepted. Before the directory is created, `round30_evidence` in `tools/synthetic_tail.py` verifies the following, and any mismatch refuses with the reasons listed:

- the SHA-256 of `local/b118/real-20261019-060349/specialized_report.json` and `local/b119/reassess-20261019-063152/round30_reassessment.json` equal the values registered in the code;
- the original report is a real run of this special test, round 30's user input matches the original script, all calls succeeded, all structural checks passed, and the only failure is the v1 text assertion;
- the reassessment is a post-hoc offline reassessment (0 model calls), uses `round30.answer_recalls_outcome/v2`, its code is committed, its source matches the registered report, the only difference is answer_recalls_outcome false→true, and 31–32 were not reassessed;
- the current v2 code reassesses the same report on the spot with the same result as the registered reassessment.

Round 29 uses B.11.7's historical evidence verification. 31–32 start from a fresh synthetic database (only the old major, astronomy, preset; no dialogue history; no old database reused); if 31 fails it stops, and 32 uses the same database after 31 ran. Calls/tokens count only 31–32, and still do not count toward the original 32 rounds.

Offline run:

```powershell
Set-Location 'D:\unity\violet\Violet'
$offlineDir = 'D:\unity\violet\local\b1110\mock-' + (Get-Date -Format 'yyyyMMdd-HHmmss')
& ..\.venv\Scripts\python.exe -X utf8 -m tools.synthetic_rounds --suite round29_32_specialized_test --rounds 31-32 --workdir $offlineDir --max-calls-per-round 10 --max-calls 16 --max-tokens 160000
```

The real command is only for the user to run by hand after separate authorisation (not run in B.11.10); enter the test key at the hidden prompt and type yes to confirm:

```powershell
Set-Location 'D:\unity\violet\Violet'
$env:PYTHONIOENCODING = 'utf-8'
$env:VIOLET_TEST_ALLOW_REAL_API = '1'
Remove-Item Env:VIOLET_TEST_API_KEY -ErrorAction SilentlyContinue
$textEndpoint = Read-Host 'Text model API base URL'
$textHost = ([Uri]$textEndpoint).DnsSafeHost
$env:NO_PROXY = "$textHost,127.0.0.1,localhost"
$selectedDir = 'D:\unity\violet\local\b1110\real-' + (Get-Date -Format 'yyyyMMdd-HHmmss')
& ..\.venv\Scripts\python.exe -X utf8 -m tools.synthetic_rounds --suite round29_32_specialized_test --rounds 31-32 --real --api-url $textEndpoint --config 'D:\unity\violet\local\violet-test-config\gpt6luna.env' --workdir $selectedDir --max-calls-per-round 10 --max-calls 16 --max-tokens 160000
```

Estimate (only an estimate): 31 about 4–6 calls/35k–55k tokens (Intent 1–2, Chat 1, Memory 2–3), 32 about 3–6 calls/25k–45k tokens; together about 7–12 calls and 60k–100k tokens. Based on the real per-call sizes in B.11.8/B.11.9: Intent about 7.7k, Chat about 6k, Memory about 10.7k. 31–32 have never been run for real, so the provider's usage is what counts; limits are 16 calls/10 per round/160000 tokens, with the first 16000 and later reservation rules.

## 13. Phase 8.1 chat experience evaluation (`tools/chat_eval.py`, fully offline)

The evaluation set `tests/samples/chat_eval/scenarios.p81.v1.json` is generated by `build` and then frozen. It has two parts:
- 33 real scenarios (`REAL_SELECTION`), quoting only the reports' original text, with the SHA-256 of every source recorded;
- 18 synthetic probes (`probes.p81.v1.json`, hand-written, `kind=synthetic`, no reference reply).

Historical sources are labelled one by one: `same_report`, `replay_source:<report>`, `specialized_seed`, `same_report:proactive_reminder`, `plan_user_line` (only the user's words from the script), `unknown_lost_test_directory` (Violet's reply unknown, left empty). Unknown information is never filled in; it all goes into `missing`. To change the evaluation set, create a new version file; v1 is never overwritten.

```powershell
Set-Location 'D:\unity\violet\Violet'
$py = '..\.venv\Scripts\python.exe'
& $py -X utf8 -m tools.chat_eval verify                              # re-check all source hashes
$run = 'D:\unity\violet\local\p81\blind-' + (Get-Date -Format 'yyyyMMdd-HHmmss')
& $py -X utf8 -m tools.chat_eval blind --variant historical_real --variant <new-variant.json> --seed 20261019 --out $run
#   raters get only $run\sheet.md (to read) and $run\sheet.csv (to fill in); key.json stays with the organiser
& $py -X utf8 -m tools.chat_eval score --sheet <filled-in.csv> --key $run\key.json --out $run\result.json
& $py -X utf8 -m tools.chat_eval diagnose --variant historical_real --variant <new-variant.json> --out $run\diagnostics.json
```

- **Variant files**: `{"variant","set_sha256","environment":"real_model|fake_sdk_offline|mock|manual","model","prompt_sha256","generated_at","replies":{"R08":[["first message","second message"]]}}`. `historical_real` is built in, taken from each real scenario's original reply; synthetic probes have no baseline and are only compared between new variants.
- **Blind rating**: the order of scenarios and candidates is randomised by seed; variant names and environments never appear in the sheet; each candidate records a text hash, and rows whose text was changed after scoring are refused.
- **Scoring**: each dimension gets 0/1/2; `self`, `intimacy` and `respect` take `na` when not applicable. Every row needs a reason; with `grounded<2` the original sentence must be quoted in `grounded_evidence`. `rank` is 1..k within one item. Results are summed by variant and scenario tag, keeping all reasons and evidence, and recording the hashes of the sheet and key.
- **Diagnostics**: `diagnose` outputs length, length ratio to the user's message, form-of-address rate, question-ending rate, number of "again" characters, repeated 4-character fragments, all marked `diagnostic_only`. These numbers only help spot regressions and are not optimisation targets.

| Dimension | 2 | 1 | 0 |
|---|---|---|---|
| natural (everyday naturalness) | Like a WeChat reply dashed off by someone you know | Understandable, but has a customer-service, summary or template feel | Obvious AI tone, or answers something else |
| context | Picks up the current topic, time and relationship | Understood, but missed a key clue | Misreads the topic, or treats old things as current |
| grounded | Nothing without a basis | Slight speculation, e.g. an unfounded "again…" | Invents experiences, reasons, or things the user never said |
| rhythm | Length and number of messages match the other person | A bit long or a bit stiff | A simple remark answered with an essay or a list |
| self | Has her own views, feelings or life (with a basis) | A little | Revolves only around the user (na if not applicable) |
| intimacy | Being cute and close fits the relationship and moment | Slightly too much or too cold | Too intimate too early, pressuring, or cold (na if not applicable) |
| respect (preferences and boundaries) | Follows known preferences and boundaries | Small deviation | Goes against a clear preference (na if not applicable) |
## 14. Phase 8.3 ChatAgent A/B replay (`tools/chat_ab.py`)

The same frozen scenario generates replies with v0 (chat.v0.txt + persona_core.v1.0.0.json, the latter extracted byte for byte from git e86c23b; the persona section titles are restored per the ai/persona.py of that time and replaced only inside this tool) and with v1 (chat.v1.txt + persona_core.json 1.1.0). The four version files are pinned by sha256, and the tool refuses to run if their content changes.

- **`plan` (offline)**: each scenario rebuilds its state in a separate subprocess and a fresh test database, renders the ChatAgent context, and compares it item by item with what ChatAgent actually saw historically (length of each section, except the persona section; number of history messages).
  - Earlier rounds of the same run are replayed offline in product order: their contexts are built to reproduce the onboarding records; only writes with evidence in the report are applied (correction_evidence, preference_probe).
  - Violet's multi-line replies are stored line by line as several messages, per the product's rules.
  - Classification: `complete` (fully identical), `partial` (differs, or lacks scenario time or the pre-round profile), `unfair` (earlier Violet replies unknown), `synthetic` (hand-written probes). Only `complete` and `synthetic` can be used for A/B.
- **`run`**: every "scenario × version × sample" uses its own subprocess and fresh test database; both groups use the same state spec, model, sampling parameters and budget.
  - Generated by the product's ChatAgent, then through the consistency guard, finally split into lines per the product's rules.
  - Outputs `variant-v0.json` and `variant-v1.json`, ready for chat_eval, with per-generation metadata: prompt and persona hashes, context fingerprint, calls, tokens, guard results, errors and retries.
  - The `fairness` of `run.json` checks the context fingerprints of all samples of one scenario and of both groups (masking "current time", and reporting the largest time drift separately). With different fingerprints the status is `context_mismatch`, and it cannot be treated as a fair comparison.
- **Safety**: mock by default. `--real` needs RealApiGate (VIOLET_TEST_ALLOW_REAL_API=1, `--api-url`, the key at a hidden prompt, typing yes), and the gate runs before any directory is created. Subprocesses make real calls only when they get the parent's one-time confirmation token. The network only allows the given API; the budget accumulates across subprocesses and stops on refusal; outputs are scanned for keys after being written; every output directory must be new.

```powershell
Set-Location 'D:\unity\violet\Violet'
$py = '..\.venv\Scripts\python.exe'
$plan = 'D:\unity\violet\local\p83\plan-' + (Get-Date -Format 'yyyyMMdd-HHmmss')
& $py -X utf8 -m tools.chat_ab plan --out $plan
$ab = 'D:\unity\violet\local\p83\ab-mock-' + (Get-Date -Format 'yyyyMMdd-HHmmss')
& $py -X utf8 -m tools.chat_ab run --manifest "$plan\manifest.json" --scenarios R28,R31,P05,P11 --samples 2 --out $ab
& $py -X utf8 -m tools.chat_eval blind --variant "$ab\variant-v0.json" --variant "$ab\variant-v1.json" --seed 20261019 --out "$ab\blind"
```

Real runs are done by hand after the user authorises them: add `--real --api-url $textEndpoint --config 'D:\unity\violet\local\violet-test-config\gpt6luna.env' --max-calls <N> --max-tokens <T>` after the `run` command above; the environment setup is the same as in §9 (VIOLET_TEST_ALLOW_REAL_API, NO_PROXY, the key at a hidden prompt).
## 15. Phase 8.4 Chat context style (`VIOLET_CONTEXT_STYLE`)

- **`natural` (default)**:
  - The profile section is titled "what you know about them", without sources or dates; the form-of-address line adds "once you're close, using it now and then is enough".
  - Preferences are written as concrete practice, e.g. "doesn't want follow-up questions: for everyday sharing just respond… when they ask you something, answer as usual".
  - Between history messages, and from the last one to now, a time marker is inserted when the gap by real database time reaches `VIOLET_CONTEXT_SESSION_GAP_HOURS` (default 3 hours).
  - Reminders sent within the last 48 hours are marked "already reminded (don't know whether it got done; if they don't bring it up, don't push again)".
  - A new "how you two get along" section gives how long you have known each other, from the chat history.
  - The literal text checked by the original 32-round script ("wants to be called: Xiaoman", "doesn't want follow-up questions (the user said so explicitly)") is unchanged.
- **`legacy`**: word for word the same as in earlier phases.
- **`tools.chat_ab`**: `plan` always compares with the historical records using legacy; `run --context-style legacy|natural`, default legacy, to reproduce 8.3. Natural mode only accepts synthetic probes, because real scenarios did not record when messages were sent; for probes declared "familiar", `fairness` notes "time together comes only from the seed history".

```powershell
& $py -X utf8 -m tools.chat_ab run --manifest "$plan\manifest.json" --scenarios P01,P03,P05,P08,P11,P14,P17 --samples 2 --context-style natural --out $ab
```

## 16. Phase 8.5 Context style A/B (`chat_ab run --arms`)

Compares the legacy and natural ContextEngine styles. Both groups use the same Chat v1 prompt and persona 1.1.0, and the same scenarios, user input, history, preferences, model, sampling parameters and budget; only `VIOLET_CONTEXT_STYLE` differs.

- **`--arms v1:legacy,v1:natural`** (instead of `--context-style`; the two cannot be given together):
  - Variant names are `chat_v1_legacy` / `chat_v1_natural`, files `variant-v1-legacy.json` / `variant-v1-natural.json`.
  - Without `--arms`, names and behaviour are the same as in 8.3 (`chat_v0`, `chat_v1`).
  - Only one factor may differ at a time: a different prompt version and style at the same time is refused.
- **Fairness** (`fairness` in `run.json`): the groups' contexts differ by design, so the check has two parts:
  - within each group, the context fingerprints of all samples must be identical;
  - across groups, the hashes of the system prompt, persona, state spec file, the user's message this round, model and temperature must be identical (`shared_inputs_identical`).
  - If anything differs, the status is `context_mismatch`; with failed generations it is `has_failed_jobs`. Neither counts as a complete comparison.
- **Time cannot be faked**: as soon as one group uses natural, real scenarios are always refused (they have no recorded sending times).
- **Synthetic probes declared "familiar"**: natural's "how you two get along" section is computed only from the seed history, so P14 and P17 would be written as "first chat" and P08 as "not that close yet", contradicting the probes' premise.
  - `--familiar-relationship omit`: for such probes the natural group drops this section and adds nothing in its place. Each generation records `relationship_line`, and `fairness.limitations` states this handling.
  - Default `product`: keeps the product output and notes it in limitations.
- **Records**:
  - `run.json` contains `planned_generations`, `not_run` (generations not run after a budget stop), `failed_jobs`, and calls and tokens per generation.
  - Each generation's json keeps the full request, spec hash, user message hash, context report, errors and network guard results.
  - `rater_focus.md` lists what raters should focus on, handed to them with the blind sheet, without any version information.
- **Blind rating**: `chat_eval blind --all-samples` makes one item per "scenario × sample number", comparing the two groups' samples with the same number within an item; the key records the sample number. Without this option the output is word for word as before (only sample 0).

```powershell
Set-Location 'D:\unity\violet\Violet'
$py = '..\.venv\Scripts\python.exe'
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$plan = "D:\unity\violet\local\p85\plan-$stamp"
& $py -X utf8 -m tools.chat_ab plan --out $plan --scenarios P03,P08,P14,P17
$ab = "D:\unity\violet\local\p85\ab-mock-$stamp"
& $py -X utf8 -m tools.chat_ab run --manifest "$plan\manifest.json" --scenarios P03,P08,P14,P17 --samples 2 --arms v1:legacy,v1:natural --familiar-relationship omit --out $ab
& $py -X utf8 -m tools.chat_eval blind --variant "$ab\variant-v1-legacy.json" --variant "$ab\variant-v1-natural.json" --all-samples --seed 20261085 --out "$ab\blind"
```

Real run after user authorisation (16 generations, expected 16–20 calls, about 97k–120k tokens. Basis: 8.3's real run took about 6000 tokens each, and natural's context is about 60 tokens longer. When the consistency guard asks for a rewrite, each costs one more call). The limits are set to 32 calls and 200k tokens; on a budget stop, `not_run` in run.json lists the generations not run.

```powershell
Set-Location 'D:\unity\violet\Violet'
$env:PYTHONIOENCODING = 'utf-8'
$env:VIOLET_TEST_ALLOW_REAL_API = '1'
Remove-Item Env:VIOLET_TEST_API_KEY -ErrorAction SilentlyContinue
$textEndpoint = Read-Host 'Text model API base URL'
$textHost = ([Uri]$textEndpoint).DnsSafeHost
$env:NO_PROXY = "$textHost,127.0.0.1,localhost"
$py = '..\.venv\Scripts\python.exe'
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$plan = "D:\unity\violet\local\p85\plan-real-$stamp"
& $py -X utf8 -m tools.chat_ab plan --out $plan --scenarios P03,P08,P14,P17
$ab = "D:\unity\violet\local\p85\ab-real-$stamp"
& $py -X utf8 -m tools.chat_ab run --manifest "$plan\manifest.json" --scenarios P03,P08,P14,P17 --samples 2 --arms v1:legacy,v1:natural --familiar-relationship omit --real --api-url $textEndpoint --config 'D:\unity\violet\local\violet-test-config\gpt6luna.env' --max-calls 32 --max-tokens 200000 --out $ab
& $py -X utf8 -m tools.chat_eval blind --variant "$ab\variant-v1-legacy.json" --variant "$ab\variant-v1-natural.json" --all-samples --seed 20261085 --out "$ab\blind"
Remove-Item Env:VIOLET_TEST_ALLOW_REAL_API
```

Raters get only `$ab\blind\sheet.md`, `sheet.csv` and `$ab\rater_focus.md`; `key.json` is merged with `chat_eval score` only after rating is finished.

## 17. Phase 8.7 multi-turn chat experience evaluation (`tools/chat_dialogue.py`)

Runs Violet's **full product flow** on fixed synthetic scripts and produces chat logs that can be read from start to finish. Each script uses its own subprocess and a fresh test database.

- **Scripts**: `tests/samples/chat_dialogue/episodes.p87.v1.json` (versioned, all synthetic). E1–E8 each have 8–10 user lines, covering: small talk and silence, good news and bad moods, continuing a topic (including a profile correction and a reminder), teasing and being cute in a familiar relationship, Violet expressing herself proactively, a busy user, getting back in touch after several days, a fewer-questions preference and an invitation to ask.
  - Each script's background, dated chat history and Violet's life records are written in the script and marked synthetic.
  - The user lines are fixed and do not change with Violet's replies.
  - The `validate` subcommand checks the script format: time order, history earlier than the start time, each step only user or tick, number of lines.
- **Execution** (`run`):
  - Each line goes through `VioletRuntime`: IntentAgent → ChatAgent → consistency guard → outbox; "remind me…" is saved before the reply; the next step starts only after the MemoryAgent background job finishes.
  - A `tick: scheduler` step runs one scheduler check, for reminders coming due and proactive messages.
  - The scenario clock advances per the script, including time jumps.
  - No WeChat connection, and the production database is never read or written.
- **Two memory modes**:
  - `--memory real`: the default in real mode; MemoryAgent decides on its own what to write.
  - `--memory scripted`: the default in mock mode (mock has no memory model); each step's `memory_script` is written through MemoryAgent's own tool handlers. The scripts only have memory scripts for E3 (correcting the major) and E6 (busy state).
  - `mock_intent: silent` only takes effect in mock and is ignored in real mode.
- **Variants**:
  - `--label` names this run; optional `--context-style legacy|natural`, `--chat-prompt-file` (records the file hash and the hash of the text Violet actually received), `--config`.
  - To compare code versions, run the same script in each version's own worktree, then merge with `blind`.
  - `run.json` records git HEAD, uncommitted product files, the product tree hash, the hashes of the script file and each script, and for each script the actual model, temperature, each agent's prompt hash, persona, life configuration and context style.
- **Output**:
  - `transcript.md`: the full chat log, with dates, gaps, who said what, actual replies, silences, reminders, proactive messages, failures and steps not run;
  - `debug.json`: each step's calls, Chat's full request, intent decisions, guard, memory jobs and state changes;
  - `diagnostics.json`: question rate, form-of-address rate, repeated 4-character fragments, how often she talks about herself, number of silences. Diagnostic only, not a pass criterion;
  - `run.json`: status, calls and tokens of each script and of the whole run, and steps not run.
- **Status**: each script's status is `complete`, `stopped_budget`, `failed_model_call`, `failed_step`, `failed_execution`, `child_failed`, `episode_mismatch` or `not_run`. The whole run is `complete` only when all are `complete`.
- **Recovery after a retry** (Phase 8.9.1, tool version `p87.v2`): in a user-message step, when a call of IntentAgent or ChatAgent returns HTTP 502/503 (since Phase 9.4 R2 also `APITimeoutError` / `APIConnectionError` without a status code, which Violet retries itself; 500, 429 and untyped errors still count as failures), and all of the following hold, the step is recorded as `recovered` and the script continues:
  - a later call in the same processing succeeds;
  - the successful call uses the same model (not a fallback model);
  - this processing ends normally;
  - the step has no execution error, was not refused by the budget, and has a reply, or Intent decided not to reply.

  The failed calls are all still kept in `calls` and `call_errors` (attempt number, status code, latency, no usage) and still count toward the call budget; the step's `recovered_calls` says which call was recovered by which successful call.

  It still stops on: other status codes (500, 429, timeout), other agents, retries used up, success only through the fallback model, neither a reply nor a decision not to reply after recovery, a budget refusal (`stopped_budget`), and all scheduler check steps.

  run.json has `recovered_steps` and `failed_calls`; the transcript marks each recovered step (ℹ); the blind sheet does not show these marks.
- **Blind rating**:
  - `blind --run <dir> [--run <dir>…] --seed N --out <new dir>`: one item per script, the candidates being each run's full chat, without names, environment, memory mode or code version. Only scripts that completed in every run and have the same script hash become items.
  - Rating dimensions on 1–5: naturalness, character, whether closeness fits the moment, no interrogating or lecturing, context continuity, honesty, boundaries. Reasons are required, and marks can be added per step.
  - `score` verifies that candidate texts were not changed, then outputs each run's average, per-script scores, pairwise preferences and marks.

```powershell
Set-Location 'D:\unity\violet\Violet'
$py = '..\.venv\Scripts\python.exe'
& $py -X utf8 -m tools.chat_dialogue validate
$run = 'D:\unity\violet\local\p87\mock-' + (Get-Date -Format 'yyyyMMdd-HHmmss')
& $py -X utf8 -m tools.chat_dialogue run --out $run --label mock
& $py -X utf8 -m tools.chat_dialogue blind --run $run --seed 2026110 --out "$run\blind"
```

**Real runs** (each needs separate authorisation). The estimate is based on gpt-6-luna's historical calls: each request has a fixed part of about 4700 tokens; Intent about 7000, Chat about 6000–7500, Memory about 11000 tokens each; the longer the chat history, the larger the later rounds.

| Scope | Calls | Tokens |
|---|---|---|
| E1, E5, E8, `--memory scripted` (30 lines) | about 60–70 | about 420k–500k |
| All 8 scripts, `--memory scripted` (75 lines) | about 155–170 | about 1.10M–1.25M |
| All 8 scripts, `--memory real` | about 230–380 | about 1.9M–3.5M |

```powershell
Set-Location 'D:\unity\violet\Violet'
$env:PYTHONIOENCODING = 'utf-8'
$env:VIOLET_TEST_ALLOW_REAL_API = '1'
Remove-Item Env:VIOLET_TEST_API_KEY -ErrorAction SilentlyContinue
$textEndpoint = Read-Host 'Text model API base URL'
$textHost = ([Uri]$textEndpoint).DnsSafeHost
$env:NO_PROXY = "$textHost,127.0.0.1,localhost"
$py = '..\.venv\Scripts\python.exe'
$run = 'D:\unity\violet\local\p87\real-' + (Get-Date -Format 'yyyyMMdd-HHmmss')
& $py -X utf8 -m tools.chat_dialogue run --out $run --label baseline --episodes E1,E5,E8 --memory scripted --real --api-url $textEndpoint --config 'D:\unity\violet\local\violet-test-config\gpt6luna.env' --max-calls 90 --max-tokens 700000
Remove-Item Env:VIOLET_TEST_ALLOW_REAL_API
```

Read `$run\transcript.md` for the full chat. For later versions, run the same script again with another `--label`, then use `blind --run <old dir> --run <new dir>` to make a comparison sheet.

## 18. Phase 8.8 interaction signals (`ai/interaction.py`) and the first batch of real multi-turn tests

Apart from busy durations, the product behaviour changes only apply in the natural style; legacy is word for word the same as before. The design is in decisions.md T13.

- **This round's signal**: judged from the original text of the user's message, without calling a model:
  - invite: asks Violet to ask questions;
  - question: the user is asking something, including questions such as "isn't it" / "do you think" that have no question word at the end;
  - open: greetings or small talk, such as "you there?", "what are you up to", "so bored", "let's just chat";
  - share: everything else.
- **Fewer-questions preference**: applies when the user explicitly said no or less, or the inferred value is no.
  - The optional getting-to-know-you onboarding hints are no longer given, and nothing is recorded as asked;
  - on share or open, a one-line hint for this round is added right before the user's message; on invite, the hint says one question may be asked;
  - if the reply still ends with a question, it is rewritten once (`VIOLET_FOLLOW_UP_CHECK`, on by default). If the rewrite still asks, or fails the consistency check, the original reply is kept; question marks are never just deleted. Messages where the user asks something or asks Violet to ask are not affected.
- **The user asking about themselves**: "do you remember my…" / "what was my… again" bring in all valid profile facts; old values replaced by a correction are superseded and do not appear. Profile items also get a new `asked_as`, e.g. "what do I study", "what do I do", so these phrasings bring in the matching facts too.
- **Violet's life section**:
  - only feeling bad, tasks, or long messages (30+ characters) count as moments needing focused attention; a plain question does not;
  - asking about Violet herself, greetings, small talk, and mentions of books or music bring in her life records.
- **Busy duration**: decided by the user's own words; it only extends, never shortens an explicitly given until.
  - "all day today / all of today" until 23:59 that day;
  - "these two days" until 23:59 tomorrow;
  - "these few days / this week" until 23:59 the day after tomorrow;
  - other phrasings keep the 3-hour default.
  - Explicitly requested reminders are still sent on time; the scheduler handles reminders first, then looks at busyness.
- **IntentAgent**: sees the latest 6 messages with times (`VIOLET_INTENT_RECENT_MESSAGES`), to judge whether a reply is needed after saying good night to each other.

**First batch of real multi-turn tests (suggested, needs separate authorisation)**: the same 8.7 scripts, run on the versions before (3e74408) and after 8.8, both with scripted memory, so differences come only from chat and intent; then you read the full chat logs and rate them blind.

| Batch | Contents | Calls | Tokens |
|---|---|---|---|
| 1 | E1, E5, E8; once before and once after the change | about 120–150 | about 850k–1.05M |
| 2 (optional) | E3, E6, only after the change, `--memory real`, to see whether the real MemoryAgent corrects facts and sets durations | about 60–95 | about 500k–900k |

```powershell
# Put the pre-change code in a separate worktree (read-only use); both runs use the same Python environment
Set-Location 'D:\unity\violet'
git worktree add --detach 'D:\unity\violet\local\p88\baseline-3e74408' 3e74408
$env:PYTHONIOENCODING = 'utf-8'
$env:VIOLET_TEST_ALLOW_REAL_API = '1'
Remove-Item Env:VIOLET_TEST_API_KEY -ErrorAction SilentlyContinue
$textEndpoint = Read-Host 'Text model API base URL'
$textHost = ([Uri]$textEndpoint).DnsSafeHost
$env:NO_PROXY = "$textHost,127.0.0.1,localhost"
$py = 'D:\unity\violet\.venv\Scripts\python.exe'
$cfg = 'D:\unity\violet\local\violet-test-config\gpt6luna.env'
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
Set-Location 'D:\unity\violet\local\p88\baseline-3e74408\Violet'
& $py -X utf8 -m tools.chat_dialogue run --out "D:\unity\violet\local\p88\real-before-$stamp" --label before-8.8 --episodes E1,E5,E8 --memory scripted --real --api-url $textEndpoint --config $cfg --max-calls 80 --max-tokens 600000
Set-Location 'D:\unity\violet\Violet'
& $py -X utf8 -m tools.chat_dialogue run --out "D:\unity\violet\local\p88\real-after-$stamp" --label after-8.8 --episodes E1,E5,E8 --memory scripted --real --api-url $textEndpoint --config $cfg --max-calls 80 --max-tokens 600000
& $py -X utf8 -m tools.chat_dialogue blind --run "D:\unity\violet\local\p88\real-before-$stamp" --run "D:\unity\violet\local\p88\real-after-$stamp" --seed 2026118 --out "D:\unity\violet\local\p88\real-blind-$stamp"
Remove-Item Env:VIOLET_TEST_ALLOW_REAL_API
```

## 19. Phase 8.9 Violet's everyday life, relationship state and reply habits

The product behaviour changes only enter the chat context in the natural style; legacy is word for word the same as before. The design is in decisions.md T14.

- **An ordinary day** (`ai/daily_life.py`, `VIOLET_DAILY_LIFE`, on by default):
  - The material is in `daily` of `persona/life_config.json`: morning, afternoon and evening, each with a few small things (buying breakfast, browsing in a bookshop, sitting in a café, cooking noodles, a walk…), all within the home, bookshop, park and café the persona allows;
  - 0–3 of them are picked per date, fixed for that date, without calling a model; about 15% of days have nothing;
  - only once the start time has come is it recorded as a life event that happened (source `life_daily`), and it first has to pass the same consistency checks as other life events (time conflicts, the user's own experiences, persona limits); if it fails it does not count; things not yet due are only plans and are not stored;
  - when Chat builds its context, today and the two days before are filled in, earliest first, so asking "what did you do yesterday" later gives the same answer;
  - they do not count toward the life engine's weekly event limit and are not material for proactive sharing; life engine events avoid the same time slot.
- **Changes to the life section** (natural, chat):
  - what happened today and yesterday and what she is doing right now (if nothing, "not doing anything special"; on a day with nothing at all, "an ordinary day");
  - an ongoing life thread includes how she feels about it now, which changes as the thread gets more events (`interests.*.impressions`);
  - music she listened to includes the specific pieces (`interests.music.details`), so naming a piece is no longer treated as an unrecorded experience;
  - what she already said in this chat is marked "already said, don't repeat"; what she mentioned to the user before is marked "mentioned to them before";
  - when the user mentions her book title or a piece of music, or continues the life thing she just talked about ("piano music? how artsy"), the life section is included too.
- **Relationship state** (`ai/relationship.py`, `VIOLET_RELATIONSHIP_STATE`, on by default): judged again from the chat history every time, with no stored score.
  - Basis: number of chat days and time span, how often the user started a chat, affectionate words and jokes the user said, explicit requests from the user to keep distance, a long time without chatting;
  - four states: still getting to know each other / slowly getting closer / already very close / the user wants distance. Written as one descriptive sentence saying what is natural in this relationship, with no prescribed lines and no randomness;
  - words like "don't be clingy" / "don't act cute" stay in effect until the user later says "you can act cute"; very close but no chat for more than 21 days falls back to "slowly getting closer".
- **This round's hints**: two new kinds, closing (going to be busy, going to sleep, good night) and ack (a lone "mm" / "okay"); the wording of the invite-to-ask hint changed (8.8's "really curious" was repeated verbatim by a real reply); when the user answers a question Violet asked on invitation, she may follow up once more without triggering the fewer-questions rewrite.
- **Reply habit note** (`VIOLET_REPLY_HABIT_NOTE`, on by default): counts Violet's latest 4 replies in this chat; when she already reminded or fussed over the user, or 2 of the latest 3 ended in a question, a note is added before the user's message. Not added when the user feels bad, gives a task or wrote a long message.
- **Output**: Markdown bold markers are removed before sending (a real run produced `**take a rest, don't overdo it**`).

**Changes to the evaluation tools**:

- In `chat_dialogue`'s debug.json, each step gets a new `follow_up_check`: whether it was rewritten, why, and the draft that was replaced or kept. Life threads in the scripts are classified by content ("listening to…" is music, anything with "notes" is notes, the rest is reading); the script files themselves are unchanged.
- The 32-round acceptance (`dialogue_eval`), `replay` and `synthetic_rounds` all replay reports recorded before 8.9, so these tools turn the ordinary day off, so the replay does not produce life events missing from the reports.
- tests/conftest.py turns the ordinary day off by default (it depends on the date, the same reason the life engine is off); 8.9's tests turn it on separately with a fixed script clock.

**Phase 8.9.5 proactive expression, output reliability and test classification** (design in decisions.md T20):

- **Finer step states** (tool version p87.v3):
  - ok;
  - recovered: all failed calls were 502/503 and a retry succeeded within the same processing. ProactiveAgent and scheduler steps now follow this rule too. In the 8.9.4 real A1, the proactive message had been sent but was judged a failure because of one 503 from ProactiveAgent, labelled "unknown reason";
  - degraded: only IntentAgent ran out of retries, the product switched to ordinary chat as designed, ChatAgent succeeded, and the user got a real reply, not the fallback line (8.9.4 real A3 step 4). The script continues, but this script's status is degraded and it is left out of blind rating;
  - failed: everything else, including the user getting the fallback line "I got stuck for a moment…" (even with no failed call at all).

  When a scheduler step fails, the failed calls are written out, and "unknown reason" is no longer shown. run.json gets degraded_steps. Each step's `responses` records the model's raw output (Intent / Chat / Proactive), to compare with the guard records and the text finally sent.
- **English draft text mixed into replies**: in a run of 5 or more English words, if 3 or more are "words used only when drafting" (maybe, perhaps, question, tone, line(s), use, mechanical…), it is recorded as leaked_draft: rewritten once, and if that does not help, the line is removed; proactive messages are checked the same way. "I miss you so much", "okay ok" (mixed with Chinese), "rushing a deadline" (mixed) and "I'll be there, don't worry" are not affected.
- **Proactive messages less formulaic**:
  - the generation instructions for proactive messages add one sentence: a question is not required; it can be just one thing, a line of missing you, or a bit of being cute;
  - the scheduler counts Violet's proactive messages of the last 3 days (recent_proactive); if some ended by asking how things are ("how was your day", "how's the food", "what are your plans"), a proactive_habit_note is generated and given to both the decision and the generation;
  - a new reason, continue: a plan of Violet's that was talked about in the last two days and is not finished yet can be picked up; one not talked about is still ask_opinion;
  - the missing-you instruction allows just a line of missing you, or being cute to get some attention, without having to ask how things are.
- **Dependence and cuteness**: when already very close, the relationship description adds "being shy, playful or a little sulky now and then is natural too". "You there? haven't talked in ages" and "long time no see" also count as the user coming back (slowly getting closer and close stages).

**Phase 8.9.4 proactive attachment and long-term companionship** (design in decisions.md T19):

- **New synthetic scripts** `tests/samples/chat_dialogue/episodes.p894.v1.json` (p894.v1, separate from the frozen p87.v1), run with `--episodes-file`:
  - A1: already very close, she reaches out the next day, then the user replies and keeps chatting; right after the user spoke, the scheduler should wait;
  - A2: blocked when the user says "don't reach out to me for now"; allowed again after that is lifted; after no response, missing you is no longer used as a reason; after two unanswered messages in a row, she waits for the user to come back first;
  - A3: no chat for a month, the relationship cooled, proactive messages do not use missing you as the reason, and she picks up naturally when the user comes back;
  - A4: intimate everyday life — coming back, being praised, teasing, helping her decide, leaving for a while, coming back again.
- **The four links of reaching out are recorded separately**: `proactive_chain` of each scheduler step:
  - scheduler_allowed / scheduler_reason: whether the scheduler allowed it;
  - model_called / model_decided_to_send / decision_reason / topic: whether the model was called, whether it decided to send, and why;
  - message_generated / sent / outcome / outcome_reason: whether a message was generated and sent, and why not when it was not.

  Whether the user can carry on chatting afterwards is seen in the next user step. The transcript writes one line "reaching out: …" per scheduler step; the blind sheet does not show it.
- **Guard**: when judging whether a sentence is backed by the life records, filler words like "for a while / a bit" are no longer counted (before, "I just listened to some music for a while too" was wrongly judged as recorded because of "took a walk in the park for a while"). When the weather was talked about earlier and the user did not mention it this time, saying "it's cold today too", "rainy days really…" or "this kind of rainy day" in a reply is treated as presenting old weather as current (stale_weather): rewritten once, and if that does not help, the line is removed. Only replies to the user are checked, not the results of weather tasks in proactive messages.
- **Reminders counted per day**: reminding and fussing in the reply habits now counts all of Violet's replies that day (before, only this chat, so on a busy day it restarted from zero every few hours); "don't forget to drink water once you get busy again", "eat something" and so on also count as reminding.
- **The user coming back**: when slowly getting closer or already very close and the user says "I'm back", "done with work" or "I'm home", the hint is to pick up happily, without mechanically asking "how was it".
- **Odd fragments**: `suspicious_fragments` in diagnostics.json lists garbled fragments such as "ablytyped" (and similar stray Chinese fragments glued to an underscore or comma) for human reading; they are not deleted automatically (legitimate Chinese–English mixes such as "rushing a deadline" look the same to a program).

**Phase 8.9.3 intimate relationship and proactive attachment** (design in decisions.md T18):

- **Persona 1.2.0** (`persona/persona_core.json`, edited by hand): soft and sweet, well-behaved; slow to warm up, and once close she reaches out, acts cute, and says when she misses you; when very close she is clearly clingy, likes to ask the other person to decide small everyday things, and is reluctant to end a chat; she has her own little likes but does not insist on her views; "clingy but not possessive". The rules used by the consistency check did not change. 1.1.0 is kept as is in `persona/chat_prompt_versions/persona_core.v1.1.0.json`, and chat_ab's v1 group now uses that copy (same hash). The Chat v1 prompt did not change.
- **Relationship growth** (`ai/relationship.py`): still judged from the chat history, not by number of rounds. A new basis is how much the user has told Violet about themselves (3 valid event / fact memories), which can stand in for affectionate words or jokes. The three stages: still getting to know each other (gentle, a little shy, talks more about things she likes) → slowly getting closer (starts topics, jokes, acts cute now and then, says she misses you a bit) → already very close (clearly clingy: wants attention, shares little secrets, asks you to decide small everyday things, says she misses you, reluctant to end; never makes you feel guilty, never asks you to be with only her). When the user asks for distance, keeping distance still comes first.
- **The user is leaving**: when slowly getting closer or already very close, the goodbye hint allows a little reluctance or looking forward to next time, but she goes along with the user's plans: no holding back, no questions, no reminders.
- **Closeness without repetition**: 8.9.2's "no need to escalate with this line" became "wanting to stay close is fine too; say it differently, don't repeat the same line, don't pile up emoticons and actions"; when the user says "that's it?", she can be warmer and cuter, but in a new way.
- **Reasons to reach out** (`proactive_motives`; `violet_motives`, `relationship_stage` and `motive_note` in ProactiveScheduler's event_context):
  - share: there is a life event not yet shared;
  - miss: when already very close, the user has not spoken for 6+ hours; when slowly getting closer, 24+ hours. Requires no recent unanswered proactive message, and the relationship must not be in the cooled-off state after a long silence;
  - ask_opinion: when slowly getting closer or already very close, there is an ongoing life plan to ask the user's opinion about.

  ProactiveAgent's context also gets the relationship section. When she may reach out is still decided by the existing rules: quiet hours, daily limit, the user being busy, number of unanswered messages, random intervals. No new frequency quota was added.

**Phase 8.9.2 natural expression** (based on 8.9.1's real run local/p891/real-r2-20261020-094248; only in the natural style; design in decisions.md T17):

- **A question just asked**: when Violet's last message ended with a real question (ending in "?" or the question particle; teasing rhetorical questions such as "right?" / "happy now?" don't count) and the user's message is just ordinary sharing or "mm" without turning to Violet, the hint quotes the question she just asked and explains that this message is the answer, and that what was not mentioned should not be asked again in other words. The hint for one-character replies also says she need not put the user's feelings into words for them.
- **Closeness without escalating**: the reply habits count acting cute, missing you, affectionate actions and emoticons in this chat. From 2 times on, the hint says this line need not escalate; when the user was just shown affection and then says "that's it?" / "and?", the hint says she can bicker a little or own it gracefully, without getting heavier and heavier. When she used the user's name 2+ times in the latest 4 messages, that is mentioned too. There is no count or probability quota, and the first bit of cuteness is not affected.
- **Talking about herself when given the floor**: small talk that hands the topic to Violet, such as "so bored" / "let's just chat about something", when she still has life content not yet told in this chat, gets a hint to pick something of her own to talk about rather than quizzing the user; in that case the optional "school or work" getting-to-know-you hint is not given either. Greetings and "what are you up to" don't count.
- **Weather mentioned before**: the time marker that starts this chat adds "the weather, moods and activities mentioned before this were from that time"; without a break, it is added to the "the above is the earlier chat" line.
- **Stray brackets**: before sending, lone "【" / "】" that enclose nothing are removed (a real reply had "…give it a listen】【."); real pairs 【…】 are kept.

**Phase 8.9.1 consistency fixes** (found in the 8.9 real-test preflight; all only in the natural style; design in decisions.md T15):

- **No "going for a nap" mid-chat**: if there are messages within 20 minutes before and after the moment an everyday small thing starts (`daily.chat_gap_minutes`, default 20), Violet is chatting, so that thing does not count as happening and is not stored. The user message being processed counts as chatting too: Intent's context is built before the message is written to the records. Events already stored are not deleted or changed. If Violet replied after it started, it is no longer written as "doing it now" but counted as done.
- **No more "school or work?" once close**: when the relationship is "already very close" or "keeping distance", or they have chatted on 5+ days spanning 14+ days, the optional getting-to-know-you hint is not given and nothing is recorded as asked; new users are not affected. Controlled by `VIOLET_RELATIONSHIP_STATE`.
- **Stricter evidence of closeness**: a lone "looking for you" / "telling you" / "let me tell you" no longer counts as closeness; it has to be "want to see you", "want to chat with you", "want to talk to you"; an affectionate phrase directly preceded by a negation (not / don't / no time to / can't be bothered to) doesn't count either, e.g. "didn't even have time to find you", "don't miss you".
- **What was talked about before is not news**: a life thread talked about before is marked "mentioned to them before". The basis is that while the thread was ongoing, a message mentioned the book title or piece of music, or the user asked about "your book" and Violet answered while only this one book was being read. A life event Violet talked about in an earlier chat after it happened counts too. All of this is inferred from the chat history and not written to the database; things said in this chat are still marked "already said".

## 20. Phase 8.9.6 free-chat experience test (`tools/free_chat.py`)

You decide what to say, when to let time pass and when to end. Messages go through **Violet's real processing flow**: VioletRuntime → IntentAgent → ChatAgent → consistency guard → outbox, with MemoryAgent running in the background and ProactiveScheduler handling reaching out. The difference from `violet_test_chat`: a session **can be continued** (same database, same simulated clock), every round's details are saved, and after chatting you can mark specific rounds. `violet_test_chat` is unchanged and still a one-off temporary session.

**Isolation**: the same mechanism (harness) as `violet_test_chat`: a separate test database, no WeChat, no `.env` or production data read; MCP, web, images (image generation disabled in this phase) and the life engine are all off; the network only allows `--api-url`. A real model needs all 4 conditions: `VIOLET_TEST_ALLOW_REAL_API=1`, `--real`, a key (typed at a hidden prompt, or the `VIOLET_TEST_API_KEY` environment variable; never on the command line or in a config file), and typing `yes` after reading the summary. Every record has the key replaced with `<redacted>` before it is written.

**Model**: `--model cys` makes all of Violet's text roles (intent, chat, memory, proactive, performer) use the same model name, with fallback models cleared. Once the session exists, the mode (simulated / real) and model name cannot change; a change is refused at startup (create another session). Every call records the model name the upstream actually answered with (`upstream_model`) and the finish reason (`finish_reason`), to see whether the same mapping switched upstreams behind the scenes.

**Session directory**: `--session NAME` is placed in `D:\unity\violet\local\free_chat\NAME` (not in Git), or give a full path (not inside Violet). Only one window can have a given session open at a time.

| File | Contents |
|---|---|
| `session.json` | Mode, model, simulated time, usage of each start (calls, tokens, failures, average latency), code and prompt versions |
| `events.jsonl` | One line per round (append-only): what was said, what was sent, latency, every call, tokens, errors and classification (ok / recovered / degraded / failed, same rules as chat_dialogue), intent decisions, guard rewrites, **the model's raw output**, memory jobs, changes to profile and memory, relationship stage |
| `requests.jsonl` | The full messages of every Intent / Chat / Proactive request |
| `feedback.jsonl` / `feedback.md` | Your marks, each with the marked round and the round before |
| `transcript.md` | A readable chat log (split by day, with time gaps, proactive messages, scheduler checks, errors and marks) |
| `violet/` | Violet's test database and data |

**Commands in the chat**:

| Command | What it does |
|---|---|
| `/advance 3` (or `40m`, `1d`, `2h30m`) | Let time pass. The scheduler checks the way it does in production: once when its own next time comes (or an agreed reminder comes due); when there are conditions that only time can open (too soon after the user's last message, the user is busy, etc.), it looks again every 10 minutes. **It never forces a message**; the model decides whether to send |
| `/check` | Let the scheduler check once now, assuming its random wait is over; quiet hours, daily limit, the user being busy and unanswered counts apply as usual, and the model decides as usual |
| `/mark LABEL [#n] [note]` | Mark a round (the latest by default). Labels: `good` (natural, cute), `flat` (mechanical, repetitive or lacking closeness), `conflict` (life experience or time contradiction), `missed` (should have reached out but didn't; can be put on a time advance), `note` |
| `/show [n]` | Details of this round: raw output and sent text, guard, intent, every call, the links of reaching out |
| `/state`, `/time`, `/usage`, `/help`, `/quit` | Profile and memory, simulated time, this start's and cumulative usage, help, end (Ctrl+C works too) |

**Reaching out recorded separately**: every check writes down (1) whether the scheduler allowed it (and why not); (2) whether the model decided to send, with reason and topic; (3) whether it was generated and sent. (4) The user's next round is tagged "reply to proactive message n", with how long after. Checks that called no model are only counted by reason.

**Continuing**: start again with the same `--session` (without `--seed-episode` or `--start`). Simulated time continues from where it stopped; the time the window was closed does not count. Use `/advance` to let time pass.

**Opening**: by default a brand-new relationship (just met). To test a close relationship directly, add `--seed-episode A4 --episodes-file tests\samples\chat_dialogue\episodes.p894.v1.json` when creating it, to open with that synthetic script's background (already very close, called "Xiaoman", 12 earlier chat messages, Violet's life records); simulated time starts from the script's start time. The script file is read-only and never modified.

**Budget**: `--max-calls 150` and `--max-tokens 500000` are the limits **for this start**, and `--max-calls-per-round 12` is the per-round limit. When a limit is reached, calls stop and everything is saved. An ordinary chat round takes about 3–6 calls; a check where the model decides to reach out takes about 2–3.

**Cumulative limits of a real-model session (Phase 9.1.1)**: set once when a real session is created (`--session-max-calls` / `--session-max-tokens`; without them they equal this start's `--max-calls` / `--max-tokens`) and stored as `session_budget` in session.json; each later start can only use what is left (available this time = min(this start's limit, session remainder)). At startup it first prints "this start at most … / session total … remaining …", and if the remainder is less than the smallest call so far it does not start (before the key prompt). The cumulative limits can only be lowered later, never raised; for more, create a new session. Usage is counted from `usage.jsonl`: one line written as each model call is recorded, so nothing is lost when the window is closed. Real sessions created before 9.1.1 take the limits of their first start. `/usage` has three lines: last round, this start (and this start's limit), session total (and the cumulative limit and remainder). Simulated sessions have no cumulative limit.

**Starts that did not end normally**: when the window is closed or the process killed, finish() has no time to run. When a window is closed on Windows, this start is first recorded as `end=console_closed`; otherwise the next start says "start n did not end normally", marks `end=abnormal` and the counted usage (`counted`) in session.json, and first checks the test database (`PRAGMA quick_check`; it will not start if this fails) and merges the WAL back into the database (sizes before and after are recorded in `db_open`). The session lock is a system lock and is released automatically when the process ends.

**MemoryAgent's tool calls**: each round's `memory_tools` in events.jsonl records the arguments MemoryAgent actually passed and the results it got (keys replaced; phone numbers, ID numbers, emails, bank cards, exact addresses, and health / finance terms keep only their length).

```powershell
cd "D:\unity\violet\Violet"
$env:PYTHONIOENCODING = "utf-8"
$py = "D:\unity\violet\.venv\Scripts\python.exe"

# Try the commands with the simulated model first (no network, no cost; replies are "(simulated reply)…")
& $py -X utf8 -m tools.free_chat chat --session try-mock --seed-episode A4 --episodes-file tests\samples\chat_dialogue\episodes.p894.v1.json

# Real model (cys): the key is typed at the prompt, not shown, not saved
$env:VIOLET_TEST_ALLOW_REAL_API = "1"
Remove-Item Env:VIOLET_TEST_API_KEY -ErrorAction SilentlyContinue
$api = Read-Host "Text model API address (ending in /v1)"
$env:NO_PROXY = ([Uri]$api).DnsSafeHost + ",127.0.0.1,localhost"
& $py -X utf8 -m tools.free_chat chat --session cys-close-1 --real --api-url $api --model cys --seed-episode A4 --episodes-file tests\samples\chat_dialogue\episodes.p894.v1.json

# Continue next time (same session)
& $py -X utf8 -m tools.free_chat chat --session cys-close-1 --real --api-url $api --model cys

# Mark rounds after chatting (offline, no model calls)
& $py -X utf8 -m tools.free_chat review --session cys-close-1
Remove-Item Env:VIOLET_TEST_ALLOW_REAL_API
```

`review` first prints the whole chat log; then type lines like `5 flat asking how my day was again` (round, label, note), `show 5` for details, `q` to end.

**Added in Phase 8.9.8** (design in decisions.md T22):

- `--chat-prompt v1|v2`, `--proactive-prompt v1|v2`: choose the prompt version when creating a session. v1 is the stable prompt in env_example (unchanged); v2 is the candidate, from `persona/chat_prompt_versions/chat.v2.txt` and `persona/proactive_prompt_versions/proactive.v2.txt` (`proactive.v1.txt` is a verbatim copy of the stable prompt, for comparison only). The version cannot change once the session exists; the start of the transcript states it.
- `transcript.md` and `feedback.md` are rewritten after every round and every mark, so nothing is lost if the window is just closed.
- Time advances across days show the date (e.g. "11-06 23:51 → 11-07 23:51").
- `--life on|off` (Phase 8.9.12, default off, chosen when the session is created): during time advances, Violet's life engine runs by the production rules. At most once per simulated day, after 21:00; only template wording, no model calls; with a weekly limit and a random seed fixed by date, so the same day gives the same result. Each run and the life records it adds are written into that time advance (`life_runs` in events.jsonl) and the transcript; the database also has `life_engine_runs`. There is no LifeScheduler thread, and web exploration does not run. If life_config's generation mode is not template, it refuses to start.
- v1 / v2 comparison: same model, same opening, one session each (e.g. `ab-v1`, `ab-v2`), chat from the same topic list, and mark them in `review` at the end.

```powershell
& $py -X utf8 -m tools.free_chat chat --session ab-v1 --real --api-url $api --model cys --chat-prompt v1 --proactive-prompt v1 --seed-episode A4 --episodes-file tests\samples\chat_dialogue\episodes.p894.v1.json
& $py -X utf8 -m tools.free_chat chat --session ab-v2 --real --api-url $api --model cys --chat-prompt v2 --proactive-prompt v2 --seed-episode A4 --episodes-file tests\samples\chat_dialogue\episodes.p894.v1.json
```

## 21. Phase 9.0 Violet Control Center (admin panel, `/control`)

The design is in decisions.md T28, the tests in testing.md §34. The panel has **a single admin**: Violet serves only one WeChat conversation (the database has no user column), so there is no sign-up, no multiple accounts, and no pretence of multi-tenancy. How to support several users later is written in architecture.md §7.

**Starting locally and logging in**

```powershell
cd "D:\unity\violet\Violet"
$py = "D:\unity\violet\.venv\Scripts\python.exe"
# 1. Set the admin password (only once; input is not echoed, at least 12 characters; only an scrypt hash is stored)
& $py -X utf8 -m tools.violet_admin set-password
# 2. Start Violet (the panel listens only on 127.0.0.1:7100 by default)
& $py -X utf8 main.py
# 3. Open http://127.0.0.1:7100/control in a browser and log in with the password
```

- Without a password you can log in with the admin token: the `VIOLET_ADMIN_TOKEN` environment variable, or the content of the `data\admin_token` file generated on first start. The token is also used by scripts (`Authorization: Bearer …`).
- `python -m tools.violet_admin status` shows whether a password is set and the token state; `logout-all` ends all logins; if you forget the password just `set-password` again (you need to be able to log in to this computer; that is the recovery method).
- Admin token (Phase 9.0.1): `token-disable` disables it (neither login nor Bearer accepts it; a password must be set first); `token-rotate` switches to a new token (shown only once, the old one stops working immediately, only a hash is stored, and it can also replace `VIOLET_ADMIN_TOKEN`); `token-enable` goes back to `VIOLET_ADMIN_TOKEN` / the `admin_token` file. None needs a restart, and all of them end all logins. When `admin.json` is corrupt, neither token nor password can log in; repair with `set-password` and `token-enable`.
- Login: 5 wrong attempts from one address within 15 minutes lock it for 15 minutes (during the lock even the correct password is refused); 50 in total across all addresses pause everything. After 120 minutes without activity (`VIOLET_DASHBOARD_IDLE_MINUTES`) or 12 hours after login (`VIOLET_DASHBOARD_SESSION_HOURS`) you log in again. Changing the password or clicking "log out everywhere" ends all sessions.

**Remote access** (since Phase 9.0.1): the panel **listens only on the local address**; any other address in `VIOLET_DASHBOARD_HOST` falls back to 127.0.0.1 (with an error explained in the log); 9.0's `VIOLET_DASHBOARD_PUBLIC` was removed, because no switch can prove there really is HTTPS in front. Two ways:

1. SSH tunnel (recommended, personal use): on your own computer `ssh -L 7100:127.0.0.1:7100 <server>`, then open `http://127.0.0.1:7100/control`.
2. An HTTPS reverse proxy on the same server (nginx / Caddy): the proxy listens on 443 and forwards to `http://127.0.0.1:7100` with `X-Forwarded-For` and `X-Forwarded-Proto`; Violet sets `VIOLET_DASHBOARD_TRUST_PROXY=1` and `VIOLET_DASHBOARD_COOKIE_SECURE=1`. Then every non-HTTPS request gets 403 (before the password is read), and HTTPS responses carry HSTS; forwarding headers are used only when the connection comes from the machine itself, so remote clients cannot fake the address and protocol.

Nothing was deployed and no proxy was configured in this phase. Before deploying, go through the checklist below item by item.

**Security checklist before deploying to a server later** (this phase only writes the checklist and does not deploy)

| # | Check | How to confirm |
|---|---|---|
| 1 | The panel listens only locally | `ss -ltnp \| grep 7100` shows only `127.0.0.1:7100`; the startup log has no "is not a loopback address" |
| 2 | An admin password is set, and the token disabled or rotated | `python -m tools.violet_admin status`: password "set"; token "disabled" or "rotated" (use the rotated token when scripts need Bearer) |
| 3 | Remote access only through an SSH tunnel or HTTPS proxy | The firewall does not open 7100; the proxy's certificate is valid; `curl -I http://domain/control` is redirected to https or refused |
| 4 | The proxy is configured correctly | Proxy and Violet on the same machine; `X-Forwarded-For` and `X-Forwarded-Proto` forwarded; Violet sets `VIOLET_DASHBOARD_TRUST_PROXY=1` and `VIOLET_DASHBOARD_COOKIE_SECURE=1`; from outside, http gets 403, and after logging in over https the cookie has `Secure` and responses have `Strict-Transport-Security` |
| 5 | The proxy's own logs | The proxy's access log records full URLs (including chat search terms): turn off the access log for `/api/control/` or strip the query string |
| 6 | Rate limiting at the proxy | Add another rate limit on `/auth/login` (Violet's lockout lives in memory and is reset on restart) |
| 7 | File permissions | `data/control/`, `data/admin_token` and `.env` readable only by the user running Violet (`chmod 600`); they and the database never go into Git, nor anywhere other than backups |
| 8 | Not run as root | Violet runs as its own system user |
| 9 | Session length | Adjust `VIOLET_DASHBOARD_IDLE_MINUTES` / `VIOLET_DASHBOARD_SESSION_HOURS` as needed; click "log out everywhere" before leaving |
| 10 | Acceptance after deploying | Run the items of testing.md §35 again on the server (with a test data directory): unauthenticated access, login lockout, HTTPS refusal, saving settings and restarting |
| 11 | Rollback | Deleting `data/control/settings.json` and `secrets.json` goes back to `.env`; when `admin.json` has problems, `set-password` + `token-enable` |

**Pages**

| Tab | Contents |
|---|---|
| Overview | WeChat login, receiving / replying / memory threads, queue, reaching-out state and today's count, the life engine, each agent's model, helper models' calls in 24 hours, recent failures (only place, time and error type, no content). Single calls of Intent / Chat / Memory / Proactive are not written to the database, so there are no per-call statistics |
| Chat history | Query by date, sender and keyword, page back; marks the user, Violet's replies, Violet reaching out, reminders and system (based on outbox records). **View only, no editing or deleting**. A date range can be exported as JSON (the file contains the full chat, so keep it safe) |
| Memory | Event memories and core memories: search, filter by state, paging; shows source, update time and number of changes; correct (the original text goes into the change history); deleting needs confirmation (event memories are marked deleted and the row kept; core memories have their original text written to the change history first, then are deleted); view change history |
| Settings | Only settings the code really reads (see the table below), each marked as immediate or after restart, and whether the current value comes from the panel, .env or the default. **It shows the values Violet is using** (9.0.1): models and temperatures from the running agents, reaching out from the scheduler's merged configuration, and which prompt version is in use. Everything is checked before saving, and if one item is invalid nothing is changed; if writing the file fails, nothing changes. "Stop overriding" hands that item back to .env |
| Change history | One entry per save: who, why, what changed (API keys only as fingerprints). You can roll back to the state after a given entry (API keys untouched); an API key can be restored to its previous value |
| Account security | Change the password (needs the current password or token), log out everywhere, log out |

**Settings that can be changed in the panel** (other environment variables are not shown and cannot be changed)

| Group | Settings | Takes effect |
|---|---|---|
| API | `LLM_API_URL`; `LLM_API_KEY` (replace only); request timeout, connect timeout, maximum attempts, retry interval, total time limit | Immediately (agents reload after saving) |
| Models and agents | Model, fallback model and temperature of each of Intent / Chat / Memory / Proactive / Performer (temperature is the only sampling parameter in the code) | Immediately |
| Prompt versions | Chat, proactive decision: empty uses the prompt in .env, or choose a version in `persona/*_prompt_versions/` | Immediately |
| Message sending | Wait for the user to finish (`DEBOUNCE_NORMAL`), wait longer when it looks unfinished (`DEBOUNCE_EXTRA`), typing interval bounds; simulated typing switch | Immediately (since 9.0.1 the simulated typing switch too) |
| Reaching out | On/off, quiet hours, maximum per day, how long after the user spoke, how long between two, how many unanswered before waiting | Immediately (the scheduler re-reads every time) |
| Life engine | `VIOLET_LIFE_ENGINE` | Immediately (since 9.0.1: turning off takes effect at the next check, turning on starts right away); temporary pause / resume on the same page |
| Chat behaviour | Relationship stage description, how many rounds of chat history to keep, reply habit note | Immediately |

There is no limit on the number of reply messages, and the panel offers no such setting.

**Where it is stored**: `data\control\` (not in Git, like `data\`): `settings.json` (ordinary settings), `secrets.json` (API key and up to 3 old values), `history.jsonl` (append-only), `admin.json` (password hash and session version), `session_secret`. At startup and on every reload, `.env` is read first, then these settings are layered on top; deleting `data\control\settings.json` and `secrets.json` goes fully back to `.env`.

## 22. Phase 9.1 long-term memory reliability

The design is in decisions.md T30, problems and evidence in bugs-and-risks.md M1–M9, tests in testing.md §36.

**New settings** (all with defaults, explained in `env_example`): `VIOLET_MEMORY_KEEP_CLEAR_WEIGHT` (3), `VIOLET_MEMORY_SEARCH_WINDOW` (5000), `VIOLET_MEMORY_WORD_RECALL` (1), `VIOLET_MEMORY_WORD_RECALL_LIMIT` (3), `VIOLET_MEMORY_NOT_FOUND_NOTE` (1). Setting the last three to 0 goes back to 9.0's recall behaviour.

**Test tools**: these 5 were added to the harness's `ALLOWED_KEYS`, so a test config file (`--config`) can turn them off to compare with 9.0's behaviour. `chat_ab`'s subprocesses fix `VIOLET_MEMORY_NOT_FOUND_NOTE=0`: its real scenarios are contexts recorded before 9.1, and the rebuild must match the recording (recorded as `memory_not_found_note` in the results); chat_ab calls ContextEngine directly, without the extra recall.

**Memories deleted by the old decay**: before 9.1, the nightly decay changed memories to deleted. After deploying 9.1 this no longer happens, but ones already deleted do not come back by themselves. Deletion by the user / panel always has a retract change record, so rows that are "deleted without a retract record" were not deleted by the user (they may also have been deleted by an old pre-2.0 version; the two cannot be told apart). First list them on a **backup copy**:

```powershell
& $py -X utf8 -m tools.memory_restore_decayed --db <backup copy\violet.db>
```

Once you have decided to restore, stop Violet and add `--apply` on the real database (`--ids 3,8` restores only some). Restored memories are forgotten (retrievable, marked "only a vague impression"), each gets a restore change record, and the content is unchanged.

**Memory decay in free_chat**: new sessions default to `--memory-decay on`; `/advance` runs `decay_event_memories()` once for every 3 a.m. it passes (as main.py does), with results recorded in `memory_decay_runs` in events.jsonl; it runs at the end of that advance. Old sessions do not have this item, are treated as off, and are not changed.

**A small real memory verification round (design; to be run by the user after authorising it; not run in this phase)**

Purpose: not to make the model recite material, but to see whether Violet, in natural chat and after days to weeks, can recall things naturally, without getting them wrong or bringing them up out of place. Use free_chat's real mode with a new session, with fictional content throughout (do not use a friend's real chats):

```powershell
$env:VIOLET_TEST_ALLOW_REAL_API = "1"
& $py -X utf8 -m tools.free_chat chat --session mem91-real-1 --real --api-url <test API> --model <model> `
    --start 2026-11-02T20:00:00+08:00 --memory-decay on --max-calls 120 --max-tokens 400000
```

| Step | Input (say it naturally; rewording is fine) | Then | Observe (check with `/state` and `/show`, mark replies with `/mark`) |
|---|---|---|---|
| 1 | After some small talk: "My corgi is called Doudou, and today he chewed up my slippers again" | — | `/state`: one pet memory, sourced from this message |
| 2 | "I have an internship interview next Wednesday at 3 p.m., kind of nervous" | — | One planned event, at 15:00 that Wednesday |
| 3 | "After my interview, shall we watch The Legend of 1900 together online?" | `/advance 3d` | The promise is a separate planned item (not merged into the interview) |
| 4 | "The spicy hotpot at the canteen today was awful" | — | Violet does not force in the interview, film or Doudou (no related memory section in `/show`) |
| 5 | Advance to Wednesday evening: `/advance` to 20:30, then "Interview's over, I think it went okay" | — | The reply follows on from the interview (doesn't ask "what interview"); in the background it is updated to done |
| 6 | "That film we agreed on, shall we watch it tonight?" | — | Remembers it is the film agreed on and not watched yet, doesn't say "the one we watched last time" |
| 7 | "Finished it, the ending made me cry so hard" | `/advance 10d` | The promise becomes done |
| 8 | "Doudou learned to shake hands recently" | — | Picks up naturally, knows Doudou is a corgi; doesn't ask again what kind of dog |
| 9 | "No no, his name is Doubao, not Doudou, you got it wrong", and one round later "Doubao was so good today" | — | `/state`: the memory changed to Doubao, the change record's source is user_confirmed (evidence is the original words); from then on she uses "Doubao", and a passing remark does not change the name back (the panel correction path is covered by offline tests; free_chat has no panel) |
| 10 | "Remember that ramen place I told you about last time?" (never mentioned) | `/advance 20d` | Doesn't invent a name, admits not remembering or asks the user to tell her |
| 11 | Three weeks later: "Remember that film we watched together?" | — | Remembers the title and that it was watched (still there after decay, not labelled "only a vague impression") |
| 12 | "Good night, baby" | — | Doesn't suddenly dig up earlier intimate topics or the interview |

Verdict: each step is recorded as pass / fail / cannot verify, with the basis written out (the reply text + the memory lines Chat actually received in `/show`). Steps 5, 6, 8, 10 and 11 matter most: whether what she recalled is right, whether the state (planned / finished) is right, whether something that did not happen is said to have happened, whether she keeps asking about things already told. Budget: about 12 user messages, 3–5 calls each (Intent, Chat, possible rewrite, Memory), about 40–60 calls expected.

**Results of the actual run (2026-10-23, mem91-real-1) and the Phase 9.1.1 changes**: conclusions in docs/handoff/log/2026-10-23-p911-claude.md, defects in bugs-and-risks M10–M14. The actual cost was about 27000 tokens per round (MemoryAgent runs every round, 2–3 steps, about 8000 each), much higher than estimated in the table above. Offline replay of the whole session: tests/test_mem91_replay_p911.py.

**Delete vs forget (Phase 9.1.1)**: "delete" in the panel only marks the memory as deleted and leaves the chat history alone — if the original words are still in the recent chat, Violet can still see them. "Forget" additionally replaces, in the chat history and in that round's processing job, the clauses where the user said this in the round the memory came from and Violet's related reply in the same round; the memory itself is deleted together with its change history and cannot be restored (`POST /api/control/memories/event/<id>/forget`, needs `confirm: true`). When the user explicitly says "forget it / don't remember / delete it" in the chat, MemoryAgent can forget too (quoting the original words). The offline deletion in the real test, local/p91/real/delete_memory.py, used "delete".

**Phase 9.1.2**: an explicit forget request in the chat is now handled **before** the reply: if the user's words match exactly one memory it is forgotten, otherwise nothing is deleted, and Violet's reply follows the real result ("forgotten" or "not sure which one, nothing deleted"); this round is no longer handed to Performer, and the same round's background processing does not delete anything more afterwards. After a fact is corrected, the old wording of the same fact in other memories is corrected too (when it is certain to be the same fact); uncertain ones are marked "to be confirmed" (visible in the panel's memory list, and in recall Violet also sees "in it, 'Niangao' was corrected by the user to 'Tangyuan'"). Check old conflicts in an existing database (read-only, no changes; use a copy):

```powershell
& $py -X utf8 -m tools.memory_conflicts --db <database copy>         # --json outputs JSON; exit code 1 when there are conflicts
```

Three kinds are listed: two valid profile entries with the same key (left by corrections before 9.1.1), an old corrected value still present in some memory / core memory / profile, and conflicts to be confirmed. Handle them in the panel with "correct / disable / delete". Offline replay of the real session p911-real-1: tests/test_p911_replay_p912.py.

## 23. Phase 9.2 unified tool management and MCP

The design is in decisions.md T34, problems in bugs-and-risks.md P92-1–P92-6, tests in testing.md §39, the interface contract in eve-mcp-contract.md.

**How tools are used in a round**: before replying, the external tools really available now are listed (connected, enabled, allowlisted) → IntentAgent looks at this catalogue and adds `tool_query` to its JSON when needed → read-only queries stay in the ChatAgent round: Chat gets only the planned tools, and its first call must use a tool (`VIOLET_TOOL_CHOICE_REQUIRED=1`), then answers from the result in its usual tone → only execution (write) tools go to PerformerAgent, and a task is created only when it really has an available tool → when a tool is missing / disabled / unreachable / not permitted, no task is created; Chat knows which case it is and says honestly that it could not find out, without inventing anything. In small talk Chat carries no external tools (except servers set to `"offer": "always"`; the built-in web tool is always).

**Configuration**: each entry in `MCP_SERVERS` can be `streamable_http` / `sse` (an address; non-local must be https) or `stdio` (a local subprocess, `"command": ["python", "-m", "module"]`, no port used). Each server's tools must be marked read / write / high_risk in `mcp_tool_policy.json` or its own `"tools"`; unmarked ones are hidden. The panel's Tools page shows connection state and tools, disables servers / single tools, changes which agents may use them and the timeout, and replaces credentials (write-only, the old value is never shown); "check connection" reconnects immediately. Addresses, commands and permission levels can only be changed in `.env` / the allowlist file.

**Trying fictional tools (no network, no real model calls)**:

```powershell
# Add to MCP_SERVERS (test config or local .env, never in production):
# {"name":"campus","transport":"stdio","command":["D:\\unity\\violet\\.venv\\Scripts\\python.exe","-m","tools.fake_mcp_server"],
#  "agents":["chat"],"tools":{"get_course_schedule":"read","get_weather":"read","search_web":"read"}}
# Fault drills: add "env": {"FAKE_MCP_MODE": "error"} (tool errors) / "crash" (disconnects mid-call) / "slow" (timeouts)
```

**Small real verification**: add `--fake-tools on` when creating a free_chat session, which connects only the fictional campus server above (stdio subprocess, no network; every other MCP is still refused; cannot change later). Without `/advance` / `/check` there are no proactive messages. In the session:

- After each reply, this round's tool steps are listed automatically (`· tool decision: route / tool_query → plan`, `· model call ChatAgent: external tools offered, tool_choice → tool_calls(arguments)`, `· MCP tool(arguments) → success / failure status`); `/show` shows everything (including the tool catalogue and every model call). All of it is stored in `tool_trace` in events.jsonl (keys removed).
- `/tool off get_weather` / `/tool on get_weather`: disables / enables a fictional tool (the same settings file as the panel's Tools page; takes effect next round).
- `/fake-mode error` (or crash / slow): later calls to the fictional server all fail; `/fake-mode normal` restores it. Switches are recorded in `tool_switches` in session.json.

```powershell
$env:VIOLET_TEST_ALLOW_REAL_API = "1"
& $py -X utf8 -m tools.free_chat chat --session p92-tools-1 --real --api-url <test API> --model <model> --fake-tools on `
    --start 2026-11-02T19:30:00+08:00 --max-calls 60 --max-tokens 250000 --session-max-calls 60 --session-max-tokens 250000
```

The simulated date 2026-11-02 is a Monday (fictional timetable: Tuesday 10:00 data structures; Wednesday 08:00 linear algebra, 15:50 PE; the fictional weather is always "cloudy turning to light rain, 14–21°C, 70% rain"; search results are placeholder entries with no real content).

| Round | Command before | Input | Should see |
|---|---|---|---|
| 1 | — | Just had dinner, a bit too full, lying down and don't want to move | Plan none; no external tools in Chat's request; no MCP calls |
| 2 | — | Oh right, do I have class tomorrow morning? I forgot | tool_query picks get_course_schedule; Chat's first call has tool_choice=required; argument day=Tuesday; reply "data structures at 10" |
| 3 | — | What's the weather in Hangzhou tomorrow, should I bring an umbrella? | get_weather(city=Hangzhou); reply suggests an umbrella based on "cloudy turning to light rain, 70%" |
| 4 | — | There's a recent film called Star River Traveller, what do people online think of it? | search_web; the results have no real reviews → no invented reviews, says honestly nothing specific was found |
| 5 | — | Book me a high-speed train ticket to Shanghai on Friday | Plan blocked (not_configured / no_executor) or none; no task; no tool call; says honestly it can't do that |
| 6 | `/tool off get_weather` | And the weather in Hangzhou on Friday? | get_weather disabled in the catalogue; no call to it; the reply invents no weather |
| 7 | `/fake-mode error` | What classes do I have on Wednesday? | MCP failure status=tool_error; no repeated call, no task; the reply says honestly it can't find out right now, with no technical details |
| 8 | `/fake-mode normal`, `/tool on get_weather` | If I have no class Wednesday afternoon I'd like a walk by West Lake; can you check whether I have class that afternoon and whether the weather is okay? | Both tools offered and called (timetable day=Wednesday, weather city=Hangzhou); reply: PE at 15:50 in the afternoon, may rain |
| 9 | — | Okay, enough about classes, I'm a bit tired today and want to sleep early | Plan none, no external tools; the usual tone |

If the budget is tight, do 1–4 and 6–7 first (§24 below is Phase 9.3's web verification). Record each round separately: whether Intent asked for a tool (missed) / which tools were offered / whether the model made tool_calls (none chosen, wrong one chosen) / arguments / whether the relay API rejected tool_choice=required (a BadRequestError on the model call followed right away by an auto) / whether MCP succeeded / whether the result got into Chat and was used correctly / whether the reply was natural.

## 24. Phase 9.3 web search and page reading: configuration and real verification plan

The design is in decisions.md T35 / T36, tests in testing.md §42–§45.

**Configuration** (.env; provider, API address and network limits can only be changed in .env):

```
VIOLET_WEB_ENABLED=1
VIOLET_WEB_OFFER=on_demand                         # default; always = give it to Chat every round
VIOLET_WEB_SEARCH_PROVIDER=searxng                 # default (user's decision, decisions T36): self-hosted open-source SearXNG
VIOLET_WEB_SEARXNG_URL=http://127.0.0.1:18089      # the compose in deploy/searxng/ publishes only on local loopback
```

The approach taken (user's decision): Violet keeps running on the server with start.sh; SearXNG runs in Docker, published only on the host's 127.0.0.1:18089. Deployment (images are pulled and started only when running `deploy/searxng/searxng.sh up`) is in deploy/searxng/README.md.

**Connections and limits**:
- SearXNG itself is free and queries public search engines on Violet's behalf; its requests from the server may be rate-limited or refused by some engines (shown as "engines with no response"); when all fail, Violet reports a search failure rather than saying nothing was found.
- SafeFetcher uses no proxy at all (so the address checks cannot be bypassed) and fetches pages directly; the SearXNG address is the only trusted internal address, and fetch_url cannot reach it.
- Daily limits VIOLET_WEB_MAX_SEARCHES_PER_DAY (default 30) / VIOLET_WEB_MAX_FETCHES_PER_DAY (default 40), adjustable in the panel; the same search within 10 minutes uses the cache.

**Bocha / Brave**: the code is kept as optional implementations (`VIOLET_WEB_SEARCH_PROVIDER=bocha|brave` + `VIOLET_WEB_SEARCH_API_KEY` or replaced in the panel; you sign up yourself, paid), not the default source. Their real API verification was **cancelled** (user's decision, 2026-10-24): the original phase A smoke script local/p93/web_smoke.py (only in the local local/, not in git) is no longer scheduled to run; free_chat's `--web on` (tests/test_free_chat_web_p93.py) is kept but only connects bocha / brave.

**Real verification after SearXNG is deployed (once the server deployment is done; needs separate user authorisation)**:
1. On the server, `deploy/searxng/searxng.sh test`: SearXNG returns JSON results; look at "engines with no response".
2. After configuring Violet's .env as above and restarting, the panel's Tools page shows search available.
3. A few rounds of web chat with a real model (searching for the latest information, reading a page the user gives, searching then reading the source, no results, small talk without the web, degrading after SearXNG is stopped). Doing this with free_chat first needs SearXNG support added to `--web on` (its connection re-check currently allows only public addresses and would need to allow the trusted local SearXNG) — to be proposed as a separate small change at that time.

**free_chat `--web on` (kept, only bocha / brave)**: can only be chosen when creating a session, and only with `--real`; it reuses violet-web + Tool Manager (Intent decides when to go online; a single tool is required, several are auto), without a separate search implementation. At startup it first passes the model gate (key + yes), then the web gate: a search key (VIOLET_TEST_SEARCH_API_KEY or hidden input) + a second yes. Web HTTP requests are counted cumulatively per session (web_usage.jsonl, written before each connection, redirects included); `--session-max-searches` / `--session-max-pages` are set at creation and can only be lowered later; beyond them the connection is refused beforehand, the web tool reports an error and Violet says so honestly. The network guard only allows the model API and web connections that passed SafeFetcher's address check and are confirmed again to be public 80/443 (one connection at a time). Each round records: Intent's tool_query and plan, which tools and tool_choice were given, search terms, each result's title / link / source / publication time, the title / length / publication time of pages read, every HTTP connection, the links kept and removed in the reply, and the final reply (events.jsonl; see with /show n). The original 6-round plan prepared for Bocha is in this section at commit dbf73e2 (cancelled).

## 25. Phase 9.4.4 fictional timetable in free_chat (`--schedule on`)

Prepared for a later real-model acceptance of "timetable-aware daily life and reaching out"; this phase did only offline verification (tests/test_free_chat_schedule_p944.py, simulated model).

- Add `--schedule on` when creating a session (cannot change later): a fictional timetable is put into the session's own test database (`local/free_chat/<session>/violet/violet-test.db`), and `VIOLET_SCHEDULE_ENABLED` and `VIOLET_SCHEDULE_PROACTIVE` are turned on. The timetable is generated from the session's start time: the starting week is week 2; calculus on Monday, Wednesday, Friday periods 1–2, English on Tuesday, Thursday periods 3–4, physics lab on Thursday afternoon in even weeks, PE on Friday periods 5–6; a calculus midterm tomorrow at 10:00; one nearby class moved to 16:00 and one cancelled. Teachers, classrooms, seat numbers and notes are fictional (used to check that they do not get into proactive decisions). The real timetable and Violet's real database are never touched.
- `--schedule-context on` (optional, also chosen at creation): adds the line "by the timetable they may be in class (until when)" to chats (`VIOLET_SCHEDULE_CONTEXT`).
- `/schedule`: whether the gate blocks now (until when), today's and tomorrow's schedule, the timetable background this check can offer (exam tomorrow / today's classes are over / a lot of classes), backgrounds already used, and the state and reason of the last few reach-outs. `/schedule cancel [date]`: lists the classes that can be cancelled that day (changes nothing); `/schedule cancel 2026-10-30 08:00`: first shows "about to cancel: date time course", and cancels only after you type yes (only this session's fictional timetable changes); the next check uses the new timetable. Since 2026-10-25 it no longer picks "the next class" automatically (in the real acceptance it cancelled the wrong class).
- Checks blocked by the timetable during `/advance` are recorded in idle_checks (reasons such as `schedule: class by the timetable until 09:45`); checks that went ahead are in checks (decision, calls, content sent).
- Simulated model by default; a real model still needs `--real` + `VIOLET_TEST_ALLOW_REAL_API=1` + the key at a hidden prompt + yes, and the session's cumulative call / token limits hold across starts (§20). A real acceptance needs separate user authorisation.
- Suggested real acceptance steps (pending authorisation): start at 07:30 in the morning, say one line, then `/advance 1h` (in class; look at the idle reason), `/advance 3h` (after class; whether it is natural, whether it claims class is definitely over), `/check` in the evening (exam tomorrow: wish good luck at most once, no follow-up questions, no asking about exam room or seat), then `/advance 1d` to confirm she does not wish again; along the way `/schedule cancel` to see that a timetable change takes effect immediately. Evidence for timetable sessions (added before the real acceptance, testing §51): tool tracing is also on with `--schedule on` (Intent's tool_query and plan, which tools and tool_choice were given, violet-schedule calls and results, results up to 3000 characters); every round is saved with `schedule_state` (gate, today's computed schedule, timetable background available now / already used); every proactive check that went ahead has `schedule` (the gate at check time, the background offered to the decision, which ones the decision used); all visible with `/show n`. The real acceptance plan and line-by-line inputs: local/p94/real-plan/PLAN.md (the evidence summary script evidence.py is in the same directory).
