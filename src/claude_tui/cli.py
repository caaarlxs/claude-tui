"""CLI entry point for claude-tui.

All flags except --cwd and --setup-alias are passed directly to the claude CLI.
Special handling for --resume without a session ID: opens the session picker.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_ALIAS_LINE = 'alias claude="claude-tui"'


def _detect_shell_rc() -> Path | None:
    """Find the user's shell config file."""
    shell = os.environ.get("SHELL", "")
    home = Path.home()

    if "zsh" in shell:
        return home / ".zshrc"
    if "bash" in shell:
        # Prefer .bashrc, fall back to .bash_profile on macOS
        bashrc = home / ".bashrc"
        if bashrc.exists():
            return bashrc
        return home / ".bash_profile"
    if "fish" in shell:
        conf = home / ".config" / "fish" / "config.fish"
        if conf.parent.exists():
            return conf

    # Try common ones
    for name in (".zshrc", ".bashrc", ".bash_profile"):
        path = home / name
        if path.exists():
            return path
    return None


def _alias_exists(rc_file: Path) -> bool:
    """Check if the alias is already in the rc file."""
    try:
        return _ALIAS_LINE in rc_file.read_text()
    except Exception:
        return False


def setup_alias() -> None:
    """Ask the user if they want to alias 'claude' to 'claude-tui'."""
    rc = _detect_shell_rc()
    if not rc:
        print("Could not detect your shell config file.")
        print(f"Add this manually to your shell config:\n  {_ALIAS_LINE}")
        return

    if _alias_exists(rc):
        print(f"Alias already set in {rc}")
        return

    print(f'This will add the following to {rc}:')
    print(f'  {_ALIAS_LINE}')
    print()
    answer = input("Replace 'claude' with 'claude-tui'? [y/N] ").strip().lower()

    if answer in ("y", "yes"):
        try:
            with open(rc, "a") as f:
                f.write(f"\n# claude-tui: use TUI instead of bare CLI\n{_ALIAS_LINE}\n")
            print(f"Done! Added to {rc}")
            print(f"Run 'source {rc}' or open a new terminal for it to take effect.")
        except Exception as e:
            print(f"Error writing to {rc}: {e}")
            print(f"Add manually:\n  {_ALIAS_LINE}")
    else:
        print("Skipped. You can run this later with: claude-tui --setup-alias")


_SETUP_FLAG = Path.home() / ".config" / "claude-tui" / ".setup-done"


def _first_run_check() -> None:
    """On first run, ask about the alias. Only asks once."""
    if _SETUP_FLAG.exists():
        return
    # Mark as done regardless of answer
    _SETUP_FLAG.parent.mkdir(parents=True, exist_ok=True)
    _SETUP_FLAG.touch()

    print("Welcome to claude-tui!")
    print()
    setup_alias()
    print()


def main() -> None:
    cwd = "."
    claude_args: list[str] = []
    open_picker = False

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--cwd" and i + 1 < len(args):
            cwd = args[i + 1]
            i += 2
        elif args[i] in ("--help", "-h"):
            _print_help()
            sys.exit(0)
        elif args[i] == "--setup-alias":
            setup_alias()
            sys.exit(0)
        elif args[i] in ("--resume", "-r"):
            if i + 1 < len(args) and not args[i + 1].startswith("-"):
                claude_args.append(args[i])
                claude_args.append(args[i + 1])
                i += 2
            else:
                open_picker = True
                i += 1
        else:
            claude_args.append(args[i])
            i += 1

    _first_run_check()

    from claude_tui.app import ClaudeTuiApp

    app = ClaudeTuiApp(
        claude_args=claude_args,
        cwd=cwd,
        open_session_picker=open_picker,
    )
    app.run()


def _print_help() -> None:
    print("claude-tui: Terminal UI wrapper for Claude Code")
    print()
    print("Usage: claude-tui [--cwd DIR] [claude flags...]")
    print()
    print("TUI-specific flags:")
    print("  --cwd DIR        Working directory (default: current directory)")
    print("  --resume, -r     Open session picker (or pass a session ID)")
    print("  --setup-alias    Add alias so 'claude' opens claude-tui")
    print()
    print("All other flags are passed directly to 'claude'.")
    print()
    print("Examples:")
    print("  claude-tui                                # Start in current dir")
    print("  claude-tui --cwd ~/my-project             # Start in specific dir")
    print("  claude-tui --resume                       # Browse past sessions")
    print("  claude-tui --resume abc123                # Resume specific session")
    print("  claude-tui --model sonnet                 # Use specific model")
    print("  claude-tui -c                             # Continue last session")
    print("  claude-tui --dangerously-skip-permissions # Skip permission checks")
    print("  claude-tui --setup-alias                  # Alias claude -> claude-tui")


if __name__ == "__main__":
    main()
