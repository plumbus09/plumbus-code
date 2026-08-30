# Terminal Agent — Structured Build Plan (v2, in progress)

Reflects what's actually built so far, not just the original plan. Two phases are done and tested; everything after is still ahead.

```
agent_project/
  core/
    types.py           —    Entry, OperationState, Message union, ToolSpec, ToolResult
  ai/
    model.py               done — Model, Context, StreamEvent union, Provider protocol
    openrouter.py          done — OpenRouterProvider, tested against mocked SSE
  tools/
    base.py                 done — Tool ABC, ToolContext, to_spec()
    registry.py             done — ToolRegistry
  test_openrouter_smoke.py  done — 3 passing tests, no network needed
  test_tools_smoke.py       done — 5 passing tests, no network needed
  try_openrouter.py         done — real end-to-end script, run locally with API key
```

---

## Phase 0 — Contracts 
**Files:** `core/types.py`

- `Entry` (single-lane tree node, `parent_id` kept even though unbranched)
- `OperationState` as a discriminated union: `checkpoint | awaiting_model | awaiting_tool | done`, each carrying exactly the data needed to resume from that point
- `Message` union: `UserMessage | AssistantMessage | ToolResultMessage`, all Pydantic, frozen
- `ToolSpec` / `ToolResult` — result splits `content` (model-facing) from `details` (app-facing), matching pi's `AgentToolResult<T>`
- `ToolResult.terminate` documented as batch-wide AND, not per-call (matches pi's `shouldTerminateToolBatch`)

**Key decision locked in:** everything is Pydantic (`Frozen(BaseModel)`), not dataclasses — real runtime validation at every boundary, not just static hints.

---

## Phase 1 — Model/Provider Abstraction 
**Files:** `ai/model.py`, `ai/openrouter.py`

- `Provider` Protocol: one required method, `stream(model, context, options) -> AsyncIterator[StreamEvent]`
- **The one contract that matters most in this whole layer:** `stream()` must never raise for request/model/runtime failures — network errors, bad keys, malformed responses all become `StreamDone(message=AssistantMessage(stop_reason="error", ...))`. Verified by test: an HTTP 401 comes back as data, not an exception.
- `OpenRouterProvider`: full SSE parsing, tool-call delta accumulation across chunks, `finish_reason` → `stop_reason` mapping (`stop→end_turn`, `tool_calls→tool_use`, `length→max_tokens`)
- Malformed tool-call JSON from the provider itself is caught and turned into an error message, not silently passed through

**Tested:** 3 smoke tests against a fake HTTP transport (plain text reply, tool-call reassembly across streamed chunks, HTTP-error-as-data). Real end-to-end script (`try_openrouter.py`) ready to run locally with a real key — not yet confirmed against the live API (sandbox has no network access to openrouter.ai).

**Open item:** confirm `try_openrouter.py` actually works against the live API on your machine before treating this phase as fully closed.

---

## Phase 2 — Tool Abstraction 
**Files:** `tools/base.py`, `tools/registry.py`

- `Tool` ABC: `name`, `label`, `parameters_schema`, `replay_safety`, `execution_mode`, `execute()`, `to_spec()`
- **Deliberate asymmetry vs. Phase 1:** `Tool.execute()` is *allowed* to raise (matches pi's real doc comment: "throw on failure instead of encoding errors in content"). The loop — not the tool — is responsible for catching it. Verified by test: an intentionally-failing tool's exception propagates cleanly out of `execute()`.
- `ToolContext`: carries `cwd`, an awaitable `confirm()` callback (plumbing for the permission gate, not the policy itself), and a `cancel` handle mirroring `StreamOptions.cancel`
- `ToolRegistry`: register/get/has/specs/names — deliberately no plugin discovery or dynamic loading yet

**Tested:** 5 smoke tests (registration, duplicate rejection, spec conversion, successful execution, exception propagation).

**Not built yet, on purpose:** any real tool (`read_file`, `bash`, `write`, `edit`), the permission gate/policy layer, path-scoping, command allow/deny-listing. Those are Phase 4.

---

## Phase 3 — Core Loop 
**Files:** `core/loop.py`

Translating pi's real `agent-loop.ts` (already read in full) to Python against the `Provider` protocol and `ToolRegistry` built above.

- `run_loop()`: the outer/inner loop structure — turn lifecycle, tool-call batches, steering/follow-up message hooks (skip these hooks initially; stub them as no-ops, add real behavior only when something needs them)
- **Truncated-output guard:** if `stop_reason == "max_tokens"` and the message has tool calls, fail all of them with a clear error instead of executing possibly-truncated arguments — direct port of pi's `failToolCallsFromTruncatedMessage`
- **Prepare → execute → finalize** split per tool call:
  - `prepare`: look up tool in registry, validate args against schema, run permission check (stubbed until Phase 4) — can short-circuit with an immediate error result without ever calling `execute()`
  - `execute`: the actual `Tool.execute()` call, wrapped in try/except here — this is where a raised exception becomes an error `ToolResult`
  - `finalize`: apply any result overrides (skip hook support initially)
- Sequential tool execution only for v1 — parallel batching is a real feature of pi's loop but adds real complexity; add it later only once sequential is solid and tested
- No persistence yet — this phase runs entirely in memory, one process, no crash recovery

**Exit criteria:** a real multi-step task (prompt → tool call → tool result → final answer) completes end-to-end against live OpenRouter, using a couple of trivial test tools (e.g. `echo`, maybe a real `read_file`).

---

## Phase 4 — Real Tools + Permission Gate 
**Files:** `tools/read_file.py`, `tools/bash.py`, `tools/write_file.py`, `tools/edit.py`, `runtime/permissions.py`

- Implement the actual filesystem/shell tools, each declaring `replay_safety` honestly (`read_file` → `"safe"`, `bash`/`write`/`edit` → `"unsafe"` by default)
- Persistent shell session for `bash` (sentinel-based exit code capture, so `cd` and env vars persist across calls)
- Permission gate as a wrapper around `prepare()` from Phase 3, not baked into individual tools:
  - policy: per-tool default (auto/ask/deny), path allow-list, command deny-list for `bash`
  - wires into `ToolContext.confirm()` already stubbed in Phase 2
- Truncate large tool outputs at this boundary (e.g. 2000 chars) with a note — cheap insurance against context blowup later

**Exit criteria:** an unapproved destructive `bash` command is intercepted and requires explicit confirmation; approved tools execute and results feed back into the loop correctly.

---

## Phase 5 — Storage (SQLite) 
**Files:** `storage/base.py` (interface), `storage/memory.py` (fake, for tests), `storage/sqlite.py` (real)

- Storage interface first (`commit`, `get_register`, `get_entries`) — build the in-memory fake version and get Phase 3/4 tests running against it before touching real SQLite
- Then implement the same interface against SQLite — because Phase 3/4 code only ever imports the interface, this should be close to a drop-in swap
- Four-verb write vocabulary only: insert entry, insert usage, upsert register, delete register — all wrapped in atomic transactions
- Single-lane simplification of the harness doc's model: one `current_leaf_id`, not a real multi-lane tree

**Exit criteria:** kill the process, restart, and manually inspect the SQLite file — same entries the in-memory version would have held.

---

## Phase 6 — Resumability
**Files:** `core/resume.py`

- `resume(session_id)`: read the last `OperationState` from storage, switch on its `kind`, continue from exactly that point — no replay-and-infer, straight dispatch on the discriminated union from Phase 0
- SIGKILL injection tests at 5 points: before model call, mid-stream, before tool exec, mid-tool-exec, after tool exec/before persist
- For mid-tool-exec crashes: consult the tool's declared `replay_safety`. `"safe"` → re-run. `"unsafe"` → synthesize an "interrupted" `ToolResultMessage` under the pre-reserved result id, mark it complete, move on — never guess

**Exit criteria:** all 5 SIGKILL points resume to correct, non-duplicated state. This is the hardest and most important milestone in the whole project.

---

## Phase 7 — Terminal UI 
**Files:** `tui/` (using `rich`/`textual`)

- Stream `AgentEvent`s (from the loop) into live terminal rendering: tool calls as they're issued, diffs for file edits, inline permission prompts wired to `ToolContext.confirm()`
- Clean `Ctrl+C` interrupt (a deliberate stop) — kept explicitly distinct from a SIGKILL crash (Phase 6), since they need different handling

---

## Phase 8 — Benchmark (Terminal-Bench) 
**Files:** `bench/`

- Wire the CLI entrypoint as the harness Terminal-Bench drives
- Run baseline subset, categorize every failure (tool bug / permission friction / context overflow / model reasoning) against which phase above it points back to
- Only now consider context compaction — don't build it speculatively before a real failure demonstrates the need

---

## What's deliberately excluded from this whole plan

Carried over from earlier discussion — these are real pi features solving problems you don't have:
- Multi-lane / branching tree, Slack-thread-style concurrency
- Three interchangeable storage backends + conformance suite (SQLite only, from the start)
- Writer-lease/fencing (single process, single user — no concurrent writers to fence against)
- Provider registry supporting many simultaneous providers (one provider — OpenRouter — until a second one is a real need)
- Deferred/batch request support (`fetchDeferred`/`cancelDeferred`)

If any of these becomes a real requirement later, add it then — not speculatively now.

---

## Sequencing recap

| Phase |
|---|---|---|
| 0. Contracts |  
| 1. Model/Provider | 
| 2. Tool abstraction |  
| 3. Core loop |  
| 4. Real tools + permissions |
| 5. Storage (SQLite) |
| 6. Resumability | 
| 7. Terminal UI |
| 8. Benchmark |