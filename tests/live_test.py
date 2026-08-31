"""
FULL live integration test — real OpenRouterProvider + real BashTool +
real agent/loop.py. No scripting, no fakes. Run this on YOUR machine:

    export OPENROUTER_API_KEY="sk-or-..."
    uv run python test_integration_live.py
"""

import asyncio
import os
import sys
import tempfile

from ai.model import Model
from ai.api.openrouter import OpenRouterProvider
from agent.loop import run_loop
from tools.bash import BashTool
from tools.read import ReadFileTool
from tools.write import WriteFileTool
from tools.edit import EditTool
from tools.grep import GrepTool
from tools.glob import GlobTool
from tools.base import ToolContext
from tools.registry import ToolRegistry


async def main():
    api_key = (sys.argv[1] if len(sys.argv) > 1 else None) or os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        print("No API key found. Set OPENROUTER_API_KEY or pass it as an argument.")
        return

    with tempfile.TemporaryDirectory() as tmpdir:
        registry = ToolRegistry()
        registry.register(BashTool())
        registry.register(ReadFileTool())
        registry.register(WriteFileTool())
        registry.register(EditTool())
        registry.register(GrepTool())
        registry.register(GlobTool())
        tool_context = ToolContext(cwd=tmpdir)

        model = Model(
        id="nvidia/nemotron-3-ultra-550b-a55b:free",   # any OpenRouter model id works here
        provider="NVIDIA",
        name=" Nemotron_3_Ultra",
        context_window=256_000,
        )
        provider = OpenRouterProvider(api_key=api_key)

        print(f"Working directory: {tmpdir}\n")
        messages = await run_loop(
            prompt_text=(
                "Create a file called notes.txt with three lines: 'alpha', 'beta', 'gamma'. "
                "Then use grep to confirm 'beta' is in the file. "
                "Then edit the file to change 'beta' to 'BETA'. "
                "Then read the file back and show me its final contents. "
                "Then use glob to list all .txt files in the directory."
            ),
            messages=[],
            system_prompt="You are a terminal coding agent. Use the available tools to complete tasks step by step.",
            model=model,
            provider=provider,
            tools=registry,
            api_key=api_key,
            tool_context=tool_context,
            max_turns=10,
        )

        print("--- Full transcript ---\n")
        for m in messages:
            if m.role == "user":
                print(f"[user] {m.content[0].text}")
            elif m.role == "assistant":
                for block in m.content:
                    if block.type == "text":
                        print(f"[assistant] {block.text}")
                    elif block.type == "tool_call":
                        print(f"[assistant -> tool_call] {block.name}({block.arguments})")
                if m.stop_reason not in ("end_turn", "tool_use"):
                    print(f"    !! stop_reason={m.stop_reason} error={m.error_message}")
            elif m.role == "tool_result":
                status = "ERROR" if m.is_error else "ok"
                print(f"[tool_result:{status}] {m.content[0].text[:300]}")

        print("\n--- Done ---")


if __name__ == "__main__":
    asyncio.run(main())