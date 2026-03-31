"""CLI entry point for claude-tui.

All flags except --cwd are passed directly to the claude CLI.
This means ANY claude flag works: --model, --resume, --continue,
--dangerously-skip-permissions, --add-dir, etc.
"""

from __future__ import annotations

import sys


def main() -> None:
    # Separate our own flags from claude flags
    our_flags = {"--cwd"}
    cwd = "."
    claude_args = []

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--cwd" and i + 1 < len(args):
            cwd = args[i + 1]
            i += 2
        elif args[i] == "--help" or args[i] == "-h":
            print("claude-tui: Terminal UI wrapper for Claude Code")
            print()
            print("Usage: claude-tui [--cwd DIR] [claude flags...]")
            print()
            print("TUI-specific flags:")
            print("  --cwd DIR    Working directory (default: current directory)")
            print()
            print("All other flags are passed directly to 'claude'.")
            print("Examples:")
            print("  claude-tui                                # Start in current dir")
            print("  claude-tui --cwd ~/my-project             # Start in specific dir")
            print("  claude-tui --model sonnet                 # Use specific model")
            print("  claude-tui -c                             # Continue last session")
            print("  claude-tui --dangerously-skip-permissions # Skip permission checks")
            print("  claude-tui --add-dir ~/other-project      # Add extra directory")
            sys.exit(0)
        else:
            claude_args.append(args[i])
            i += 1

    from claude_tui.app import ClaudeTuiApp

    app = ClaudeTuiApp(
        claude_args=claude_args,
        cwd=cwd,
    )
    app.run()


if __name__ == "__main__":
    main()
