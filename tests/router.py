"""
Real OpenRouter test — run this on YOUR machine, not in a sandbox without
network access. Uses your actual API key and hits the real API.

Setup:
    export OPENROUTER_API_KEY="sk-or-..."
    cd agent_project
    python3 try_openrouter.py

Or pass the key inline:
    python3 try_openrouter.py sk-or-...
"""

import asyncio
import os
from pathlib import Path
import sys

# Ensure project root is in sys.path when running script directly
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

from ai.model import Context, Model, StreamOptions
from ai.api.openrouter import OpenRouterProvider
from agent.types import TextContent, UserMessage

load_dotenv()  # Load environment variables from .env file


async def main():
    api_key = (sys.argv[1] if len(sys.argv) > 1 else None) or os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        print("No API key found. Set OPENROUTER_API_KEY or pass it as an argument.")
        return

    provider = OpenRouterProvider(api_key=api_key)

    model = Model(
        id="poolside/laguna-s-2.1:free",   # any OpenRouter model id works here
        provider="openrouter",
        name="laguna",
        context_window=256_000,
    )
    context = Context(
        system_prompt="You are a terse assistant. Answer in one short sentence.",
        messages=[UserMessage(content=[TextContent(text="What is 2+2, and why does it matter?")], timestamp=0)],
    )

    print("Streaming response:\n")
    async for event in provider.stream(model, context, StreamOptions(api_key=api_key)):
        if event.type == "text_delta":
            print(event.delta, end="", flush=True)
        elif event.type == "done":
            print("\n\n--- done ---")
            print("stop_reason:", event.message.stop_reason)
            if event.message.error_message:
                print("error_message:", event.message.error_message)


if __name__ == "__main__":
    asyncio.run(main())