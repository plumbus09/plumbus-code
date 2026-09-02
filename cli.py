"""
cli.py — Composition root and CLI entry point for Plumbus-Code Agent.

Wires together Core Agent Loop, OpenRouter AI Provider, Tool Registry,
Permission Policy, and Terminal UI without modifying any existing functions.
"""

import argparse
import asyncio
import os
import sys
from dotenv import load_dotenv
from rich.console import Console

from agent.types import Message
from ai.api.openrouter import OpenRouterProvider
from ai.model import Model
from terminal.runner import run_terminal_session
from tools import default_tools
from tools.permissions import PermissionPolicy
from terminal.banner import print_banner

def parse_args():
    parser = argparse.ArgumentParser(
        description="Plumbus-Code — Autonomous Terminal AI Agent"
    )
    parser.add_argument(
        "--api-key",
        type=str,
        help="OpenRouter API key (defaults to OPENROUTER_API_KEY env var)",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=os.environ.get("AGENT_MODEL", "openai/gpt-4o-mini"),
        help="OpenRouter model ID (default: openai/gpt-4o-mini)",
    )
    parser.add_argument(
        "--cwd",
        type=str,
        default=os.getcwd(),
        help="Working directory path (default: current directory)",
    )
    parser.add_argument(
        "--auto-approve",
        action="store_true",
        help="Auto-approve tool execution prompts without asking for [y/N]",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=20,
        help="Maximum turn count per prompt (default: 20)",
    )
    return parser.parse_args()


async def main_async():
    load_dotenv()
    args = parse_args()
    console = Console()

    api_key = args.api_key or os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        console.print(
            "[bold red]Error:[/bold red] No OpenRouter API key provided.\n"
            "Set [yellow]OPENROUTER_API_KEY[/yellow] in your .env file or pass [yellow]--api-key sk-or-...[/yellow]"
        )
        sys.exit(1)

    working_dir = os.path.abspath(args.cwd)
    if not os.path.exists(working_dir):
        console.print(f"[bold red]Error:[/bold red] Working directory does not exist: {working_dir}")
        sys.exit(1)

    model = Model(
        id=args.model,
        provider="openrouter",
        name=args.model.split("/")[-1],
        context_window=200_000,
    )
    provider = OpenRouterProvider(api_key=api_key)
    tools = default_tools()
    policy = PermissionPolicy()

    print_banner(console, model.id, working_dir)

    messages: list[Message] = []
    system_prompt = (
        "You are an expert AI terminal coding agent. "
        "Use your available tools (read_file, write_file, edit, bash, glob, grep) "
        "to inspect, design, write, test, and debug code in the workspace."
    )

    while True:
        try:
            user_input = console.input("[bold cyan]Agent > [/bold cyan]").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]Exiting session.[/dim]")
            break

        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit"):
            console.print("[dim]Exiting session.[/dim]")
            break

        messages = await run_terminal_session(
            prompt_text=user_input,
            messages=messages,
            system_prompt=system_prompt,
            model=model,
            provider=provider,
            tools=tools,
            api_key=api_key,
            cwd=working_dir,
            policy=policy,
            console=console,
            auto_approve=args.auto_approve,
            max_turns=args.max_turns,
        )


def main():
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        print("\nExited.")


if __name__ == "__main__":
    main()
