# Architecture

A terminal coding agent, modeled on pi's real internal design (`packages/agent`, `packages/ai`, `packages/coding-agent`), scoped down for a single user, single process, single provider. This document is the map — what each layer owns, what it's forbidden from knowing about, and how data actually flows through one turn.

---

## 1. Layer diagram

```
┌─────────────────────────────────────────────────────────────┐
│  terminal/            (Phase 7 — not started)                │
│  Renders AgentEvents live. Owns Ctrl+C. Owns confirm() UI.    │
└───────────────────────────┬───────────────────────────────────┘
                              │ subscribes to events, calls confirm()
┌───────────────────────────▼───────────────────────────────────┐
│  agent/loop.py         (Phase 3 — done)                      │
│  The step function. Owns turn lifecycle, tool-call batching,  │
│  the truncation guard. Knows Provider + Tool contracts only.  │
└──────────┬───────────────────────────────┬─────────────────────┘
            │ calls .stream()                │ calls .execute() via registry
┌──────────▼─────────────┐       ┌──────────▼─────────────────────┐
│  ai/                    │       │  tools/                        │
│  model.py  (contract)   │       │  base.py      (contract)        │
│  openrouter.py (impl)   │       │  registry.py  (lookup)           │
│                         │       │  read_file.py, bash.py, ...      │
│                         │       │  (Phase 4 — not started)         │
└─────────────────────────┘       └───────────────────────────────────┘
            │                                     │
            └───────────────┬─────────────────────┘
                              │ both import shape only, no behavior
                    ┌────────▼─────────┐
                    │  agent/types.py   │
                    │  (Phase 0 — done) │
                    └────────┬─────────┘
                              │
                    ┌────────▼─────────────────────┐
                    │  storage/  (Phase 5 — not      │
                    │  started) — entries, registers, │
                    │  usage rows. Wraps agent/loop.py │
                    │  from OUTSIDE for durability.     │
                    └───────────────────────────────────┘
```

**The one rule that keeps this from tangling:** dependencies point downward only. `agent/loop.py` may import from `ai/` and `tools/` and `agent/types.py`. Neither `ai/` nor `tools/` may import from `agent/loop.py`, from each other, or from `storage/`. `storage/` wraps the loop from the outside — the loop has zero awareness that persistence exists. If you ever find yourself importing "up" this diagram, stop and restructure before continuing; this is the exact discipline pi's own three-package split (`ai` → `agent` → `coding-agent`) enforces, just compressed into folders instead of npm packages.

---

## 2. Directory structure — actual repo

```
plumbus-code/
├── agent/
│   ├── types.py          ✅ Entry, OperationState, Message union, ToolSpec, ToolResult
│   ├── loop.py            ✅ run_loop() — the step function (Phase 3)
│   └── resume.py           🔲 resume(session_id) — reads OperationState, dispatches (Phase 6)
├── ai/
│   ├── model.py            ✅ Model, Context, StreamEvent union, Provider protocol
│   └── openrouter.py        ✅ OpenRouterProvider — the one real implementation
├── tools/
│   ├── base.py              ✅ Tool ABC, ToolContext, to_spec()
│   ├── registry.py           ✅ ToolRegistry
│   ├── read_file.py           🔲 Phase 4
│   ├── bash.py                  🔲 Phase 4 — persistent shell, sentinel exit codes
│   ├── write_file.py             🔲 Phase 4
│   ├── edit.py                    🔲 Phase 4 — targeted string replace, not overwrite
│   └── permissions.py              🔲 Phase 4 — the gate wrapping tool dispatch
├── storage/
│   ├── base.py                     🔲 Phase 5 — Storage interface (commit/get/list)
│   ├── memory.py                    🔲 Phase 5 — in-memory fake, for tests
│   └── sqlite.py                     🔲 Phase 5 — real backend
├── terminal/                        🔲 Phase 7 — empty, TUI/CLI entrypoint (rich/textual)
├── tests/
│   ├── test_openrouter_smoke.py    ✅
│   ├── test_tools_smoke.py          ✅
│   └── test_loop_smoke.py            ✅
├── docs/
│   └── plan.md                        (this project's phase plan)
├── pyproject.toml
├── uv.lock
└── requirements.txt                    ⚠️ redundant with pyproject.toml — pick one
```

✅ = built and tested · 🔲 = planned, not started · ⚠️ = cleanup item

---

## 3. Module responsibilities in detail

### `agent/types.py` — the shared vocabulary
Owns every shape that crosses a layer boundary. Nothing here executes. Everything is Pydantic (`Frozen(BaseModel)`), not dataclasses, so a malformed object raises at construction time, not three calls deep inside a provider.

Key types and why they're shaped the way they are:
- **`Message` union** (`UserMessage | AssistantMessage | ToolResultMessage`) — immutable; the loop only ever appends a new message, never mutates one in place.
- **`ToolResult`** — splits `content` (goes to the model) from `details` (goes to your UI/logs). A single blob would conflate two audiences with different trust levels and different lifetimes.
- **`OperationState`** — a discriminated union (`checkpoint | awaiting_model | awaiting_tool | done`), the durable "program counter" from the harness-doc research. Not used yet (Phase 5/6), defined now so the loop can be written against it later without a redesign.
- **`Entry`** — single-lane tree node. Keeps `parent_id` even though the lane never branches: cheap to add now, and it's what makes replay an unambiguous walk instead of a trust-insertion-order guess.

**Dependency rule:** imports nothing from `ai/`, `tools/`, `storage/`, or `terminal/`. Everything else imports from here.

### `ai/model.py` + `ai/openrouter.py` — the model-call boundary
`model.py` defines `Provider` as a `Protocol` with one required method: `stream(model, context, options) -> AsyncIterator[StreamEvent]`.

**The load-bearing contract:** `stream()` must never raise for request/model/runtime failures. Every failure — bad key, timeout, malformed SSE, provider 5xx — becomes a `StreamDone(message=AssistantMessage(stop_reason="error", ...))`. This is what lets `agent/loop.py` call the model with zero try/except around it.

`openrouter.py` is the only concrete implementation: HTTP + SSE parsing, OpenAI-wire-format translation, tool-call delta accumulation across streamed chunks. If a second provider is ever needed, it goes here as a sibling file implementing the same `Provider` protocol — `agent/loop.py` doesn't change.

**Dependency rule:** imports only `agent/types.py`. Never imports `tools/`, `agent/loop.py`, or `storage/`.

### `tools/base.py` + `tools/registry.py` — the tool-call boundary
`Tool` is an ABC with `execute()` as its one abstract method. **Deliberate asymmetry vs. `Provider`:** `execute()` is *allowed* to raise. The loop, not the tool, is responsible for catching it — this is what pi's real code does too, and it's what keeps individual tools simple to write (a tool author never has to remember to catch their own exceptions).

`ToolContext` carries `cwd`, a stubbed `confirm()` callback (the permission gate's future entry point), and a `cancel` handle. `ToolRegistry` is a flat name → `Tool` lookup — no plugin discovery, no dynamic loading, added only if a real need for it shows up.

**Dependency rule:** imports only `agent/types.py`. Never imports `ai/`, `agent/loop.py`, or `storage/`.

### `agent/loop.py` — the step function
The only file that imports *both* `ai/` and `tools/`. Owns:
- Turn lifecycle (prompt in → model call → tool calls → tool results → next model call → ... → final answer)
- The **truncation guard**: if `stop_reason == "max_tokens"` and the message has tool calls, fail all of them rather than executing possibly-corrupted arguments (direct port of pi's `failToolCallsFromTruncatedMessage`)
- The **prepare → execute → finalize** split per tool call — this is where a raised `Tool.execute()` exception gets caught and turned into an error `ToolResult`, and the only place that's allowed to happen
- Sequential tool execution only (v1) — pi supports a parallel mode; add it later only once sequential is solid, not before

Runs entirely in memory. Has zero knowledge that persistence will eventually wrap it — that's intentional, matching how pi's own `agent-loop.ts` has no idea the `harness/` durability layer exists above it.

**Dependency rule:** imports `agent/types.py`, `ai/model.py` (never `ai/openrouter.py` directly — always through the `Provider` protocol), `tools/base.py`, `tools/registry.py`.

### `storage/` — durability (not started)
Will implement the three-store model from the harness-doc research (entries / registers / usage rows), scoped to a single lane, no branching, no multi-writer lease. Interface first (`storage/base.py`), fake in-memory version second (`storage/memory.py`, used to test `agent/loop.py` and future resumability logic without touching disk), real SQLite version last (`storage/sqlite.py`) — built to the same interface so the swap is close to free.

**Dependency rule:** imports `agent/types.py` only. Wraps `agent/loop.py` from the outside; `agent/loop.py` never imports `storage/`.

### `agent/resume.py` — crash recovery (not started)
Reads the last `OperationState` from `storage/`, switches on its `kind`, and continues from exactly that point — no replaying a log and inferring position. For a tool call interrupted mid-execution, consults the tool's declared `replay_safety` (`"safe"` → re-run; `"unsafe"` → synthesize an "interrupted" result under the pre-reserved id, never guess).

### `terminal/` — the TUI (not started)
Subscribes to whatever event stream `agent/loop.py` eventually exposes (an `AgentEvent`-equivalent, not built yet — currently the loop just returns a final list of messages, no incremental events). Owns rendering, diffs for file edits, and the real implementation behind `ToolContext.confirm()`. Owns `Ctrl+C` handling, kept explicitly distinct from a SIGKILL crash — a clean interrupt and a crash need different recovery paths.

---

## 4. Data flow — one full turn, current implementation

```
run_loop(prompt_text, messages, ...)
  │
  ├─ wrap prompt_text as UserMessage, append to history
  │
  ├─ LOOP (max_turns):
  │    │
  │    ├─ _stream_assistant_response(history, ...)
  │    │     └─ provider.stream(model, Context(history, tools.specs()), options)
  │    │           └─ [ai/openrouter.py: POST, parse SSE, accumulate deltas]
  │    │           └─ yields StreamStart, TextDelta*, ToolCallDelta*, StreamDone
  │    │     └─ returns final AssistantMessage
  │    │
  │    ├─ if stop_reason in (error, aborted): STOP, return history
  │    │
  │    ├─ extract tool_calls from AssistantMessage.content
  │    ├─ if none: STOP, return history  (model is done)
  │    │
  │    ├─ if stop_reason == max_tokens:
  │    │     fail ALL tool calls with a truncation error (never execute them)
  │    ├─ else:
  │    │     for each tool_call, sequentially:
  │    │       ┌─ PREPARE: registry.get(name) → tool.prepare_arguments(args)
  │    │       │    (unknown tool / bad args → immediate error ToolResult)
  │    │       ├─ [permission gate — Phase 4, not yet wired]
  │    │       ├─ EXECUTE: await tool.execute(...) inside try/except
  │    │       │    (raised exception → caught HERE, becomes error ToolResult)
  │    │       └─ FINALIZE: [afterToolCall-equivalent override — not yet wired]
  │    │            → ToolResultMessage
  │    │
  │    ├─ append all ToolResultMessages to history
  │    └─ loop again (next model call sees the tool results)
  │
  └─ return full new_messages list
```

**What's real vs. stubbed in this flow today:** everything up through "append all ToolResultMessages to history" is implemented and tested. The permission gate and the finalize-override hook are marked insertion points with no behavior yet — adding them later shouldn't require restructuring this flow, only filling in the two marked gaps.

---

## 5. State model (defined, not yet wired to real durability)

```
OperationState (agent/types.py) — a discriminated union, checked by `kind`:

  checkpoint          idle between turns, safe resume point
  awaiting_model      intent committed (response id reserved), model call
                      in flight — the one genuinely uncertain window
  awaiting_tool       tracks pending/completed tool_call_ids + each one's
                      declared replay_safety, for crash recovery decisions
  done                terminal state, carries final stop_reason
```

This union exists today purely as a type — `agent/loop.py` doesn't write to it or read from it yet, because there's no storage layer to persist it to. When Phase 5/6 land, the loop will be wrapped (not rewritten) by code that commits one of these four states after every step, and `agent/resume.py` will read only that register on restart — never replaying history to infer position.

---

## 6. What's deliberately excluded

Real features of pi's actual production system, each solving a problem this project doesn't have:

| Excluded | pi's reason for it | Why you don't need it |
|---|---|---|
| Multi-lane / branching tree | Slack threads, subagents sharing history | Single user, single conversation thread |
| 3 interchangeable storage backends + conformance suite | Ship flexibility across deployment targets | SQLite only, from the start |
| Writer-lease / fencing | Multiple processes could touch one session | One process, one user, no concurrent writer to fence against |
| Provider registry (many providers, many auth schemes) | Anthropic, OpenAI, Bedrock, local models, simultaneously | One provider (OpenRouter) until a second is a real need |
| Deferred/batch requests (`fetchDeferred`/`cancelDeferred`) | Cheaper non-urgent batch-style calls | No batch workload in a live terminal session |
| Parallel tool execution | Throughput at scale | Sequential is simpler and correct; add parallelism only after sequential is solid |

If any of these becomes a genuine requirement later, add it then, against the interface it slots into — not speculatively now.

---

## 7. Open items / cleanup

- `requirements.txt` and `pyproject.toml` both exist — pick one (recommend keeping `pyproject.toml` + `uv.lock`, deleting `requirements.txt`) to avoid dependency drift.
- `try_openrouter.py` (real end-to-end script against live API) has not yet been confirmed working against the actual OpenRouter API — sandbox environment used to build this has no network access to verify. Run it once for real before treating Phase 1 as fully closed.
- No JSON-schema validation of tool arguments yet in `agent/loop.py`'s prepare step — add `jsonschema` (or similar) the first time a model sends malformed arguments in practice, not speculatively now.