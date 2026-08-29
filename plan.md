# Terminal Agent — Fresh Build Plan (pi-architecture)



1. **Core** — the model loop: turns, tool-call parsing, streaming
2. **Runtime** — tool execution, sandboxing, permissions, filesystem/shell
3. **Session** — persistence, resumability, TUI rendering

No dependency on prior projects — this is a clean repo, clean state machine, built layer by layer in the order pi's own dependency graph implies: **Core cannot depend on Runtime or Session. Runtime cannot depend on Session.** Get this direction of dependency right early or the whole thing tangles.

```
plumbus-code/
  core/         # loop, message types, streaming parser
  runtime/      # tools, sandbox, permissions
  session/      # event log, persistence, TUI
  cli.py        # composition root — wires the three layers together
  tests/
```

---

## Phase 0 — Repo & Type Contracts (0.5 day)
**Goal:** define the shapes that cross layer boundaries before writing any layer.

- `Message` — role, content blocks (text / tool_use / tool_result), never a raw string.
- `ToolCall` — id, name, input (matches Anthropic's tool_use block shape directly, don't invent your own).
- `ToolResult` — tool_call_id, content, is_error.
- `AgentEvent` — the only thing Session is allowed to persist; Core and Runtime never touch storage directly.
- Write these as frozen/immutable dataclasses or Pydantic models — the loop should never mutate a message in place, only append new ones. This one decision prevents most of the state bugs you'll hit later.

**Exit criteria:** contracts file compiles, has no imports from `runtime/` or `session/`.

---

## Phase 1 — Core: the Loop (1–2 days)
**Goal:** the minimal agent loop with zero I/O side effects beyond the model call itself. Core should be testable with a fake LLM client and no filesystem at all.

- `core/loop.py`: `step(messages, tools) -> Message` — one model call, parsed into a `Message` with typed content blocks.
- Streaming: consume Anthropic's stream, yield partial text and completed tool_use blocks as they close — don't buffer the whole response before returning.
- Turn vs. step distinction: a **turn** ends when the model stops requesting tools; a **step** is one model call. Get this vocabulary fixed now, pi's internals lean on it heavily.
- Core exposes an interface (`ToolExecutor` protocol) rather than importing `runtime/` — Runtime will implement it in Phase 2. This inversion is what keeps Core layer-pure.
- Unit test the loop against a scripted fake client: given a canned sequence of tool_use → tool_result → text, assert the loop terminates correctly and message history is well-formed.

**Exit criteria:** `core/` has passing tests with zero real tool execution, zero disk I/O, zero network beyond a mockable client.

---

## Phase 2 — Runtime: Tools & Sandbox (1–2 days)
**Goal:** implement `ToolExecutor` for real, with permissions as a first-class gate, not an afterthought bolted onto each tool.

- Tool registry: `@tool` decorator or explicit registration mapping name → JSON schema → handler.
- Core tools: `bash`, `read_file`, `write_file`, `edit` (targeted string replace, not overwrite), `glob`, `grep`.
- Persistent shell: one long-lived subprocess per session, sentinel-based exit code capture (`echo "<<<EXIT:$?>>>"` pattern) so `cd` and env vars persist across calls the way a real terminal behaves.
- Permission gate sits **in front of** the executor, as a decorator/middleware, so no individual tool can bypass it:
  - Policy object: per-tool default (auto/ask/deny), path allow-list, command deny-list for `bash`.
  - Ask-mode surfaces a callback that Session/CLI will implement in Phase 4 — Runtime doesn't know about terminals, it just calls an injected `confirm(action) -> bool`.
- Truncate large tool outputs at the Runtime boundary (e.g. 2000 chars) with a note — this is cheap insurance against context blowup you'd otherwise fight in Phase 5-equivalent work later.

**Exit criteria:** `runtime/` tools pass tests independent of Core; a destructive bash command is blocked without an approval callback returning `True`.

---

## Phase 3 — Wire Core + Runtime (0.5–1 day)
**Goal:** first real end-to-end run, still no persistence.

- `cli.py` composition root: build a real Anthropic client, instantiate Runtime's executor, inject it into Core's loop.
- Run a real multi-step task fully in-memory (e.g., "list files, read one, summarize it") and print raw output — ugly is fine, correctness is the bar.
- This is the checkpoint to verify the Core/Runtime boundary actually holds — if you find yourself importing Runtime types into `core/loop.py`, stop and fix the interface before continuing.

**Exit criteria:** a real task completes end-to-end with real tool execution and no session/persistence layer at all.

---

## Phase 4 — Session: Event Log & Persistence (1 day)
**Goal:** everything that happened in Phase 3 becomes durable and replayable.

- SQLite-backed append-only `events` table: `(id, session_id, seq, type, payload_json, ts)`.
- Every `Message`, `ToolCall`, and `ToolResult` from Core/Runtime gets wrapped as an `AgentEvent` and appended — Session is a listener on the loop, not something the loop calls into directly (keep the inversion from Phase 0 intact).
- State reconstruction: `replay(session_id) -> list[Message]` purely by folding events — this function is your ground truth for "what actually happened," and everything else (resumability, debugging, benchmarking later) depends on it being trustworthy.
- Session listing/creation CLI commands (`agent new`, `agent resume <id>`, `agent list`).

**Exit criteria:** kill the process after a task, and `replay()` reconstructs the exact message history that was in memory at kill time.

---

## Phase 5 — Resumability (1 day)
**Goal:** actually continue a killed session, not just replay its history for inspection.

- On `agent resume <id>`: replay events → rebuild message list → rebuild persistent shell state (re-run `cd` to last known cwd; note that env vars set via `export` mid-shell are **not** recoverable this way — document that limitation rather than silently getting it wrong).
- SIGKILL injection tests at these points, each asserted separately:
  1. before the model call
  2. mid-stream
  3. before tool execution
  4. mid-tool execution (the hard one — see below)
  5. after tool execution, before the event is persisted
- For mid-tool-execution kills specifically: tag each `ToolCall` event with an idempotency key before execution starts, so on resume you can detect "this call started but we don't know if it finished" and surface it rather than silently re-running a possibly-already-applied file edit.

**Exit criteria:** all 5 SIGKILL injection points resume to correct, non-duplicated state.

---

## Phase 6 — Session: Terminal UI (1–2 days)
**Goal:** the pi-like feel — streaming, diffs, inline permission prompts.

- Use `rich`/`textual` for rendering, driven by the same streaming events Core already yields — Session subscribes to Core's stream, it doesn't poll.
- Render tool calls as they're issued (name + args), then their results, then the next model turn — all before the turn completes.
- Diff view for `edit`/`write_file` tool results (unified diff, colored) instead of raw before/after dumps.
- Wire the `confirm()` callback from Phase 2 to an actual inline terminal prompt.
- `Ctrl+C` mid-turn should cancel the current step cleanly and drop back to a prompt, not kill the process (distinguish this from the SIGKILL crash-recovery path in Phase 5 — this is a deliberate, clean interrupt).

**Exit criteria:** a task is watchable live, diffs render, `Ctrl+C` interrupts cleanly without corrupting the event log.

---

## Phase 7 — Benchmark & Harden (1–2 days)
**Goal:** run against Terminal-Bench, use failures to find real gaps.

- Wire `cli.py` as the entrypoint Terminal-Bench drives.
- Run a baseline subset; for every failure, use `replay()` from Phase 4 to reconstruct exactly what happened — this is the payoff for keeping the event log as ground truth from the start.
- Categorize failures: tool bug / permission friction / context overflow / model reasoning — each points back to a specific phase above to revisit.
- Only now consider context compaction (summarizing old tool results once you're near the token budget) — don't build it speculatively before you've seen it actually cause a failure.

**Exit criteria:** documented baseline score with per-failure root cause, not just pass/fail.

---

## Sequencing

| Phase | Depends on | Est. time |
|---|---|---|
| 0. Contracts | — | 0.5 day |
| 1. Core loop | 0 | 1–2 days |
| 2. Runtime/tools | 0 | 1–2 days |
| 3. Wire together | 1, 2 | 0.5–1 day |
| 4. Event log | 3 | 1 day |
| 5. Resumability | 4 | 1 day |
| 6. TUI | 3, 5 | 1–2 days |
| 7. Benchmark | 6 | 1–2 days |

Total: **7–11 focused days**. Phases 1 and 2 can be built in parallel since Core only depends on Runtime's *interface* (the `ToolExecutor` protocol from Phase 0), not its implementation — that's the main speed advantage of getting the layering strict from day one.