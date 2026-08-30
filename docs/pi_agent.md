### PI Agent Architecture

## summary how it works 

# three core principles

- `Entries` : permanent entries that are passed once and forever saved is our conversation history 

- `Registers`: "the whiteboard behind the counter" - mutable entries acts like a whiteboard

- `usage ledger`: a reciept of every action


```
Application (your CLI, Slack bot, whatever)
        │
     Harness   ← drives lanes: accepts prompts, runs steps, resumes
        │
     Session   ← the tree of entries + facts + lanes + ledger, for ONE conversation
        │
     Storage   ← the dumb, generic layer: entries / registers / usage rows,
                 knows nothing about "agents" or "conversations" at all
        │
      SQLite (or eventually Postgres)
```

- Storage is deliberately dumb — it just knows "here are three kinds of rows, and here's how to write them atomically." All the meaning (what a lane is, what an operation is) is built entirely on top, using only those three primitives. 

# The shape of a session



```
Entries (the tree):      a ── b ── c ── d
                                └── e ── f
Lanes (bookmarks):        main → d        thread-2 → f
Facts (sticky notes):     name="support-ticket-42", label on entry c = "bug found here"
Usage ledger:             [u1: $0.002][u2: $0.01][u3: $0.003]...

```

- One session = one tree of entries, with some bookmarks stuck into it (lanes), plus some sticky notes (facts), plus a receipt tape (usage ledger):


- Multiple lanes can share the same trunk of history and diverge — that's how one session supports parallel work (Slack threads, subagents) without duplicating the shared past.

# The shape of one unit of work (an operation)

Every time a lane does something (respond to a prompt, compact old messages, jump to a different branch), that's an operation. An operation has:

-      Metadata — written once at the start, frozen forever ("I am operation #7, I was asked to respond to lane main, starting from entry d")
-  Current state — a single register that gets completely    overwritten after every step ("I am now at: waiting for the tool result of call #2")

This state register is the program counter — the one thing recovery reads to know exactly where to pick up. It doesn't need a history of what happened; it just needs to know, right now, in full, what the situation is.

# The rhythm every risky action follows
```
1. INTENT      "I'm about to do X. Its result will be entry #9."     (written to disk)
2. THE ACTION                    (the only part that ISN'T written to disk)
3. SETTLEMENT  "Here's the actual result, filed under entry #9."     (written to disk)
```

If the process dies during beat 2, on restart the harness reads the state register, sees "I was mid-action," and checks a policy the tool itself declared ahead of time: is it safe to just try this again, or not?

- Safe (e.g. reading a file) → just redo it.
- Not safe (e.g. deleting files) → don't redo it. Write a synthetic "we don't know what happened" result under the ID that was already reserved, and move the conversation forward anyway.

# Why the "four verbs" ceiling matters

Storage only ever allows: insert entry, insert usage, upsert register, delete register — bundled into all-or-nothing transactions. This is deliberately restrictive. Because everything durable must go through one of exactly four operations, the system can guarantee "no half-written state" without needing anything clever — it's just math: a set of writes either all landed, or none did.

```
The system separates "what happened" (entries — permanent, append-only)
 from "what's happening right now" (registers — current-only, overwritable) 
 from "what it cost" (usage — append-only), and guarantees recoverability not by remembering history and replaying it, but by always keeping one small, complete, current snapshot of exactly where each unit of work stands — so a crash at literally any moment leaves behind a system that knows precisely what to do next.

```

