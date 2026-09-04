"""
test_real_sigkill.py — the one genuine external kill -9 test, proving
CrashingStorage's simulation assumption actually holds: that "raise an
exception right after a commit, then reopen the file fresh" is truly
equivalent to the OS yanking the process out from under it.

Uses multiprocessing to run the durable loop in a REAL child process,
sends REAL SIGKILL at the moment a REAL BashTool subprocess is running
(the scariest case: an actually-unsafe, actually-executing shell command,
killed by the OS mid-flight, not by our own Python code choosing to stop).
"""

import asyncio
import multiprocessing
import os
import signal
import tempfile
import time


def _child_process(storage_path: str, ready_flag_path: str, error_flag_path: str):
    """
    Runs in a SEPARATE OS process. Starts a bash command that sleeps
    (giving the parent time to SIGKILL us mid-execution), and touches a
    ready-flag file the instant the awaiting_tool state is committed —
    the parent waits for that file before sending SIGKILL, so the kill
    lands deterministically during the tool's execution window, not
    before or after it.

    Any exception during setup (e.g. an API mismatch between this test
    and the current agent/tools code) is written to error_flag_path
    instead of silently killing the child — a `spawn`-based child process
    can lose its traceback on the way back to the parent, and without
    this the parent just sees a bare timeout with no clue why.
    """
    import asyncio as _asyncio
    import traceback

    try:
        from agent.durable import run_durable
        from agent.types import AssistantMessage, TextContent, ToolCallContent
        from ai.model import Model, StreamDone
        from storage.sqlite import SQLiteStorage
        from tools.bash import BashTool
        from tools.base import ToolContext
        from tools.permissions import PermissionPolicy
        from tools.registry import ToolRegistry
    except Exception:
        with open(error_flag_path, "w") as f:
            f.write("IMPORT ERROR:\n" + traceback.format_exc())
        return

    class OneShotBashProvider:
        def __init__(self):
            self.n = 0

        async def stream(self, model, context, options):
            self.n += 1
            if self.n == 1:
                msg = AssistantMessage(
                    content=[ToolCallContent(
                        id="c1", name="bash",
                        arguments={"command": "sleep 2 && echo done"},
                    )],
                    stop_reason="tool_use", timestamp=0,
                )
            else:
                msg = AssistantMessage(content=[TextContent(text="ok")], stop_reason="end_turn", timestamp=0)
            yield StreamDone(message=msg)

    async def main():
        storage = SQLiteStorage(storage_path)
        registry = ToolRegistry()
        registry.register(BashTool())
        ctx = ToolContext(cwd=tempfile.gettempdir(), policy=PermissionPolicy(overrides={"bash": "auto"}))

        orig_commit = storage.commit

        async def watching_commit(tx):
            result = await orig_commit(tx)
            for w in tx.writes:
                if getattr(w, "kind", None) == "set_register" and w.namespace == "op.state":
                    if isinstance(w.value, dict) and w.value.get("kind") == "awaiting_tool":
                        with open(ready_flag_path, "w") as f:
                            f.write("ready")
            return result

        storage.commit = watching_commit

        await run_durable(
            storage=storage,
            prompt_text="run a slow command",
            system_prompt="sys",
            model=Model(id="t", provider="fake", name="t", context_window=8000),
            provider=OneShotBashProvider(),
            tools=registry,
            api_key="x",
            tool_context=ctx,
        )

    try:
        _asyncio.run(main())
    except Exception:
        with open(error_flag_path, "w") as f:
            f.write("RUNTIME ERROR:\n" + traceback.format_exc())


async def main():
    with tempfile.TemporaryDirectory() as tmpdir:
        storage_path = f"{tmpdir}/session.db"
        ready_flag_path = f"{tmpdir}/ready.flag"
        error_flag_path = f"{tmpdir}/error.flag"

        ctx = multiprocessing.get_context("spawn")
        proc = ctx.Process(target=_child_process, args=(storage_path, ready_flag_path, error_flag_path))
        proc.start()

        # Wait for the child to signal it's genuinely mid-tool-execution —
        # but fail FAST and LOUD if the child crashed or died early,
        # instead of silently waiting out the full deadline and leaving
        # you to guess why.
        deadline = time.time() + 10
        while not os.path.exists(ready_flag_path):
            if os.path.exists(error_flag_path):
                with open(error_flag_path) as f:
                    raise AssertionError(f"child process failed during setup:\n\n{f.read()}")
            if not proc.is_alive():
                raise AssertionError(
                    f"child process died before signaling readiness (exitcode={proc.exitcode}), "
                    f"and left no error flag — check for a crash outside the try/except in _child_process."
                )
            if time.time() > deadline:
                proc.terminate()
                raise AssertionError("child never reached awaiting_tool state in time")
            await asyncio.sleep(0.01)

        # The child is now genuinely running `sleep 2` inside BashTool's
        # real subprocess. Kill it for real.
        os.kill(proc.pid, signal.SIGKILL)
        proc.join(timeout=5)
        assert proc.exitcode is not None and proc.exitcode != 0, "child should have been killed, not exited cleanly"
        print(f"Child process genuinely SIGKILLed (exitcode={proc.exitcode}) while bash tool was mid-execution.")

        # Now resume in THIS process, fresh SQLiteStorage, real BashTool,
        # real registry — no shared memory with the killed child at all.
        from agent.resume import resume
        from ai.model import Model, StreamDone
        from agent.types import AssistantMessage, TextContent
        from storage.sqlite import SQLiteStorage
        from tools.bash import BashTool
        from tools.base import ToolContext
        from tools.permissions import PermissionPolicy
        from tools.registry import ToolRegistry
        from tests.crash_harness import discover_operation_id

        class FinalAnswerProvider:
            async def stream(self, model, context, options):
                yield StreamDone(message=AssistantMessage(content=[TextContent(text="ok")], stop_reason="end_turn", timestamp=0))

        fresh_storage = SQLiteStorage(storage_path)
        op_id = await discover_operation_id(fresh_storage)
        registry = ToolRegistry()
        registry.register(BashTool())

        messages = await resume(
            storage=fresh_storage, operation_id=op_id, system_prompt="sys",
            model=Model(id="t", provider="fake", name="t", context_window=8000),
            provider=FinalAnswerProvider(), tools=registry, api_key="x",
            tool_context=ToolContext(cwd=tmpdir, policy=PermissionPolicy(overrides={"bash": "auto"})),
        )

        tool_result = next(m for m in messages if m.role == "tool_result")
        # bash is declared replay_safety="unsafe" -> must NOT have been
        # re-run. If it had been re-run, we'd see "done" in the output
        # (from the completed `sleep 2 && echo done`) instead of the
        # synthesized interruption message.
        assert "was NOT retried" in tool_result.content[0].text, tool_result.content[0].text
        assert tool_result.is_error is True
        await fresh_storage.close()
        print("PASS: real SIGKILL mid-bash-execution -> resume correctly did NOT re-run the unsafe command")
        print(f"      tool_result: {tool_result.content[0].text}")


if __name__ == "__main__":
    asyncio.run(main())