"""Lea CLI — minimal entry point."""

import argparse
import sys
from pathlib import Path

from .agent import run, list_sessions, DEFAULT_MODEL
from .prompt import DEFAULT_WORKSPACE
from .tools import _find_lake_root


def main():
    parser = argparse.ArgumentParser(
        description="Lea — a minimal Lean 4 formalization agent",
    )
    parser.add_argument(
        "task",
        nargs="?",
        help="Math statement to formalize (or reads from stdin if omitted).",
    )
    parser.add_argument(
        "-m", "--model", default=DEFAULT_MODEL, help=f"Model to use (default: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "-p", "--provider", default=None, help="Provider: gemini, anthropic, openai (auto-detected from model name if omitted)",
    )
    parser.add_argument(
        "--max-turns", type=int, default=None, help="Max agent turns (default: unlimited)",
    )
    parser.add_argument(
        "--sketch", action="store_true", help="Use the sketch prompt (produce a proof skeleton with sorry's).",
    )
    parser.add_argument(
        "--fill", action="store_true", help="Use the fill prompt (fill sorry's in an existing file).",
    )
    parser.add_argument(
        "--resume", nargs="?", const=True, default=False,
        help="Resume a session. Pass a session ID, or omit to resume the most recent.",
    )
    parser.add_argument(
        "--sessions", action="store_true", help="List recent sessions and exit.",
    )
    parser.add_argument(
        "--workspace", type=Path, default=None,
        metavar="DIR",
        help=f"Directory where the agent writes .lean files (default: {DEFAULT_WORKSPACE}). "
             "The Lake project root is inferred automatically for Mathlib search.",
    )
    parser.add_argument(
        "--bare-prompt", action="store_true",
        help="Send the prompt exactly as given, with no system prompt and no tools.",
    )
    parser.add_argument(
        "--tools-only", action="store_true",
        help="Like --bare-prompt but with tools available.",
    )
    parser.add_argument(
        "--sandbox", action="store_true",
        help="Run bash and lean_check inside a Docker container (no network access).",
    )
    parser.add_argument(
        "--docker-image", default=None, metavar="IMAGE",
        help="Docker image to use with --sandbox. Must have lean and lake available.",
    )

    args = parser.parse_args()

    if args.sessions:
        sessions = list_sessions()
        if not sessions:
            print("No sessions found.")
        for s in sessions:
            print(f"  {s['id']}  {s['model']:30s}  {s['turns']:>3} turns  {s['task']}")
        return

    task = args.task
    if not task and not args.resume:
        if sys.stdin.isatty():
            parser.print_help()
            sys.exit(1)
        task = sys.stdin.read().strip()

    # Select prompt variant
    if args.sketch:
        variant = "sketch"
    elif args.fill:
        variant = "fill"
    else:
        variant = "default"

    if args.sandbox:
        if not args.docker_image:
            print("Error: --docker-image is required when using --sandbox.", file=sys.stderr)
            sys.exit(1)
        from .sandbox import DockerSandbox
        ws = args.workspace or DEFAULT_WORKSPACE
        lake_root_str = _find_lake_root(str(ws))
        mount_path = Path(lake_root_str) if lake_root_str else ws
        print(f"sandbox:    {args.docker_image} (mount: {mount_path})", flush=True)
        with DockerSandbox(mount_path, args.docker_image) as sandbox:
            result = run(
                task=task or "",
                model=args.model,
                max_turns=args.max_turns,
                provider=args.provider,
                resume=args.resume,
                prompt_variant=variant,
                workspace=args.workspace,
                bare_prompt=args.bare_prompt,
                tools_only=args.tools_only,
                sandbox=sandbox,
            )
    else:
        result = run(
            task=task or "",
            model=args.model,
            max_turns=args.max_turns,
            provider=args.provider,
            resume=args.resume,
            prompt_variant=variant,
            workspace=args.workspace,
            bare_prompt=args.bare_prompt,
            tools_only=args.tools_only,
        )
    print(result)


if __name__ == "__main__":
    main()
