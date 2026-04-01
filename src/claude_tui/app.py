"""Main Textual application for claude-tui.

Visual wrapper around Claude Code CLI. Claude runs in a real PTY so all
native behavior works (permissions, slash commands, interactive prompts).
We add: file tree with git status, file viewer, diff navigator, and a
command palette.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from rich.text import Text
from textual import work, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.command import Provider, Hit, Hits
from textual.containers import Horizontal, Vertical
from textual.widgets import (
    DirectoryTree,
    Label,
    ListItem,
    ListView,
    RichLog,
    Static,
    TabbedContent,
    TabPane,
    TextArea,
)

from claude_tui.diff_tracker import DiffTracker, DiffSnapshot, FileDiff
from claude_tui.session_screen import SessionPickerScreen
from claude_tui.terminal_widget import TerminalWidget


# ─── Constants ────────────────────────────────────────────────────────

_FILE_ICONS: dict[str, tuple[str, str]] = {
    ".py": ("py", "#3572A5"), ".js": ("js", "#f1e05a"),
    ".ts": ("ts", "#3178c6"), ".tsx": ("tx", "#3178c6"),
    ".jsx": ("jx", "#f1e05a"), ".json": ("{}", "#a8a8a2"),
    ".html": ("<>", "#e34c26"), ".css": ("# ", "#563d7c"),
    ".md": ("md", "#083fa1"), ".rs": ("rs", "#dea584"),
    ".go": ("go", "#00ADD8"), ".sh": ("$ ", "#89e051"),
    ".yaml": ("ym", "#cb171e"), ".yml": ("ym", "#cb171e"),
    ".toml": ("tm", "#9c4221"),
}

_STATUS_STYLE = {
    "modified": ("M", "#f59e0b"),
    "added": ("A", "#4ade80"),
    "deleted": ("D", "#f87171"),
    "renamed": ("R", "#67e8f9"),
    "untracked": ("?", "#8d99ae"),
}

_LANG_MAP = {
    ".py": "python", ".js": "javascript", ".ts": "javascript",
    ".tsx": "javascript", ".jsx": "javascript",
    ".css": "css", ".html": "html", ".json": "json",
    ".md": "markdown", ".rs": "rust", ".go": "go",
    ".yaml": "yaml", ".yml": "yaml", ".toml": "toml",
    ".sh": "bash", ".bash": "bash", ".zsh": "bash",
    ".rb": "ruby", ".c": "c", ".cpp": "c", ".h": "c",
    ".java": "java", ".swift": "swift", ".kt": "kotlin",
}


# ─── Widgets ──────────────────────────────────────────────────────────

class TuiHeader(Static):
    DEFAULT_CSS = """
    TuiHeader { dock: top; height: 1; background: #1a1a2e; padding: 0 1; }
    """

    def __init__(self, cwd: str = "", **kw):
        super().__init__(**kw)
        self._cwd = cwd

    def render(self) -> Text:
        t = Text()
        t.append(" claude-tui ", style="bold #e07a5f on #1a1a2e")
        t.append("  ", style="#1a1a2e")
        t.append(self._cwd, style="#8d99ae")
        return t


class TuiFooter(Static):
    DEFAULT_CSS = """
    TuiFooter { dock: bottom; height: 1; background: #1a1a2e; }
    """

    def render(self) -> Text:
        t = Text()
        for key, label in [("^Q", "Quit"), ("^\\", "Tab"), ("^T", "Tree"),
                           ("^B", "Terminal"), ("^P", "Commands"), ("^S-C", "Copy")]:
            t.append(f" {key} ", style="bold #e0e0e0 on #2d2d44")
            t.append(f" {label} ", style="#8d99ae on #1a1a2e")
        return t


class FileTree(DirectoryTree):
    """File tree with git status indicators and type icons."""

    DEFAULT_CSS = """
    FileTree { background: #12121a; scrollbar-size: 1 1; }
    """

    _HIDDEN = {".git", "node_modules", "__pycache__", ".venv", ".mypy_cache",
               ".ruff_cache", ".DS_Store", ".claude", ".pytest_cache", "dist", "build"}

    def __init__(self, path: str, **kw):
        super().__init__(path, **kw)
        self._git_status: dict[str, str] = {}

    def filter_paths(self, paths):
        return [p for p in paths
                if p.name not in self._HIDDEN and not p.name.endswith(".egg-info")]

    def refresh_git_status(self) -> None:
        """Run git status and cache the results."""
        try:
            r = subprocess.run(
                ["git", "status", "--porcelain", "-u"],
                cwd=str(self.path), capture_output=True, text=True, timeout=5,
            )
            status: dict[str, str] = {}
            for line in r.stdout.splitlines():
                if len(line) < 4:
                    continue
                code = line[:2].strip()
                fpath = line[3:].strip()
                if code in ("M", "MM", "AM"):
                    status[fpath] = "modified"
                elif code in ("A", "??"):
                    status[fpath] = "added" if code == "A" else "untracked"
                elif code == "D":
                    status[fpath] = "deleted"
                elif code.startswith("R"):
                    status[fpath] = "renamed"
            self._git_status = status
        except Exception:
            pass

    def render_label(self, node, base_style, style):
        label = super().render_label(node, base_style, style)
        path = node.data.path if node.data else None
        if not path:
            return label

        parts = []

        # File type icon (files only)
        if not path.is_dir():
            entry = _FILE_ICONS.get(path.name.lower()) or _FILE_ICONS.get(path.suffix.lower())
            if entry:
                parts.append(Text(f"{entry[0]} ", style=f"dim {entry[1]}"))

        parts.append(label)

        # Git status indicator
        try:
            rel = str(path.relative_to(self.path))
        except ValueError:
            rel = ""
        if rel in self._git_status:
            st = self._git_status[rel]
            char, color = _STATUS_STYLE.get(st, ("?", "#8d99ae"))
            parts.append(Text(f" {char}", style=f"bold {color}"))

        return Text.assemble(*parts)


class ResizeHandle(Static):
    DEFAULT_CSS = """
    ResizeHandle { width: 1; height: 100%; background: #1e1e2e; color: #3a3a5a; }
    ResizeHandle:hover { background: #e07a5f; color: #e07a5f; }
    """

    def __init__(self, **kw):
        super().__init__("│", **kw)
        self._dragging = False

    def on_mouse_down(self, event):
        self._dragging = True
        self.capture_mouse()
        event.stop()

    def on_mouse_move(self, event):
        if self._dragging:
            self.app.query_one("#file-tree", FileTree).styles.width = max(15, min(60, event.screen_x))
            event.stop()

    def on_mouse_up(self, event):
        if self._dragging:
            self._dragging = False
            self.release_mouse()
            event.stop()


class DiffSummary(Static):
    DEFAULT_CSS = """
    DiffSummary { height: 1; background: #1a1a2e; color: #8d99ae; padding: 0 1; }
    """


class DiffFileList(ListView):
    DEFAULT_CSS = """
    DiffFileList { height: auto; max-height: 10; background: #12121a; border-bottom: solid #2a2a3e; }
    DiffFileList > ListItem { padding: 0 1; height: 1; }
    DiffFileList > ListItem.-highlight { background: #1e1e2e; }
    """


class DiffContent(RichLog):
    DEFAULT_CSS = """
    DiffContent { height: 1fr; background: #0d0d14; }
    """


# ─── Command palette ─────────────────────────────────────────────────

class TuiCommands(Provider):
    async def search(self, query: str) -> Hits:
        app = self.app
        commands = [
            ("Focus Terminal", "Switch to Claude terminal", "focus_terminal"),
            ("Focus File Tree", "Switch to file tree", "focus_tree"),
            ("Tab: Terminal", "Switch to Terminal tab", "tab_terminal"),
            ("Tab: Files", "Switch to Files tab", "tab_files"),
            ("Tab: Diff", "Switch to Diff tab", "tab_diff"),
            ("Refresh File Tree", "Reload the file tree", "refresh_tree"),
            ("Refresh Diff", "Re-scan for changes", "reload_diff"),
            ("Resume Session", "Browse and resume past sessions", "open_session_picker"),
            ("Copy Terminal", "Copy terminal content to clipboard", "copy_terminal"),
            ("Quit", "Exit claude-tui", "request_quit"),
        ]
        matcher = self.matcher(query)
        for name, help_text, action in commands:
            async def _run(act=action):
                await app.run_action(act)

            if not query:
                # No query: show all commands
                yield Hit(1.0, name, _run, text=name, help=help_text)
            else:
                score = matcher.match(name)
                if score > 0:
                    yield Hit(score, matcher.highlight(name), _run, text=name, help=help_text)


# ─── App ──────────────────────────────────────────────────────────────

class ClaudeTuiApp(App):
    """Terminal UI IDE wrapper for Claude Code."""

    TITLE = "claude-tui"
    COMMANDS = {TuiCommands}

    CSS = """
    Screen { layout: vertical; background: #0d0d14; }
    #main-content { height: 1fr; }
    #file-tree { width: 28; min-width: 15; max-width: 60; background: #12121a; border-right: solid #2a2a3e; }
    #tab-panel { width: 1fr; background: #0d0d14; }
    #claude-terminal { height: 1fr; width: 1fr; }
    #file-viewer { height: 1fr; background: #0d0d14; }
    #diff-panel { height: 1fr; }
    TabPane { padding: 0; }
    TabbedContent { background: #0d0d14; }
    ContentSwitcher { background: #0d0d14; }
    Tabs { background: #12121a; height: 1; }
    Tab { background: #12121a; color: #6b7280; padding: 0 2; }
    Tab.-active { background: #0d0d14; color: #e07a5f; text-style: bold; }
    Tab:hover { color: #e0e0e0; }
    """

    BINDINGS = [
        Binding("ctrl+backslash", "cycle_tab", show=False),
        Binding("ctrl+t", "focus_tree", show=False),
        Binding("ctrl+b", "focus_terminal", show=False),
        Binding("ctrl+p", "command_palette", show=False),
        Binding("ctrl+shift+c", "copy_terminal", show=False),
        Binding("ctrl+q", "request_quit", show=False),
    ]

    def __init__(
        self,
        claude_args: list[str] | None = None,
        cwd: str = ".",
        open_session_picker: bool = False,
    ) -> None:
        super().__init__()
        self.claude_cwd = str(Path(cwd).resolve())
        self.claude_args = claude_args or []
        self._open_session_picker = open_session_picker
        self._current_snap: DiffSnapshot | None = None
        self._exit_message: str = ""  # Message to print after TUI exits
        self._resuming = False  # True while switching sessions (suppresses auto-exit)
        self.diff_tracker = DiffTracker(
            self.claude_cwd,
            on_change=self._on_files_changed,
        )

    def compose(self) -> ComposeResult:
        yield TuiHeader(cwd=self._short_cwd(), id="tui-header")
        with Horizontal(id="main-content"):
            yield FileTree(self.claude_cwd, id="file-tree")
            yield ResizeHandle(id="resize-handle")
            with TabbedContent(id="tab-panel"):
                with TabPane("Terminal", id="tab-terminal"):
                    yield TerminalWidget(
                        command=["claude"] + self.claude_args,
                        cwd=self.claude_cwd,
                        id="claude-terminal",
                    )
                with TabPane("Files", id="tab-files"):
                    yield TextArea(id="file-viewer", read_only=True)
                with TabPane("Diff", id="tab-diff"):
                    with Vertical(id="diff-panel"):
                        yield DiffSummary("No changes", id="diff-summary")
                        yield DiffFileList(id="diff-file-list")
                        yield DiffContent(id="diff-content", wrap=True, highlight=True, markup=True)
        yield TuiFooter(id="tui-footer")

    def on_mount(self) -> None:
        self.diff_tracker.start_watching()
        self._refresh_git_status()
        self._watch_pty_exit()

        # Disable Textual's mouse tracking so the terminal emulator handles
        # mouse natively — enables text selection and copy/paste without
        # holding modifier keys. All TUI navigation works via keyboard.
        sys.stdout.write("\x1b[?1000l\x1b[?1003l\x1b[?1015l\x1b[?1006l")
        sys.stdout.flush()

        if self._open_session_picker:
            self._show_session_picker()
        else:
            self.query_one("#claude-terminal", TerminalWidget).focus()

    def _short_cwd(self) -> str:
        home = os.path.expanduser("~")
        return ("~" + self.claude_cwd[len(home):]) if self.claude_cwd.startswith(home) else self.claude_cwd

    def _show_session_picker(self) -> None:
        """Open the session picker and resume the selected session."""
        async def _on_result(result: tuple[str, str] | None) -> None:
            if result is None:
                if self._open_session_picker:
                    # Opened with --resume, cancel = exit app
                    await self.action_request_quit()
                else:
                    # Opened from command palette, cancel = back to terminal
                    self.query_one("#claude-terminal", TerminalWidget).focus()
                return
            project_path, session_id = result
            # Prevent pty-watcher from killing the app while we swap terminals
            self._resuming = True
            try:
                old_term = self.query_one("#claude-terminal", TerminalWidget)
                await old_term.cleanup()
                await old_term.remove()
            except Exception:
                pass
            # Update cwd to the session's project
            self.claude_cwd = project_path
            self.query_one("#tui-header", TuiHeader)._cwd = self._short_cwd()
            self.query_one("#tui-header", TuiHeader).refresh()
            # Mount new terminal with --resume
            pane = self.query_one("#tab-terminal", TabPane)
            tabs = self.query_one("#tab-panel", TabbedContent)
            tabs.active = "tab-terminal"
            new_term = TerminalWidget(
                command=["claude", "--resume", session_id] + self.claude_args,
                cwd=project_path,
                id="claude-terminal",
            )
            await pane.mount(new_term)
            new_term.focus()
            self._resuming = False
            # Restart the watcher for the new terminal
            self._watch_pty_exit()
            self.notify(f"Resumed session in {Path(project_path).name}", timeout=3)

        self.push_screen(SessionPickerScreen(), callback=_on_result)

    def _on_files_changed(self) -> None:
        """Called by watchdog when files change — update diff + tree."""
        try:
            self.call_from_thread(self._refresh_diff)
            self.call_from_thread(self._refresh_git_status)
        except Exception:
            pass

    def _refresh_git_status(self) -> None:
        tree = self.query_one("#file-tree", FileTree)
        tree.refresh_git_status()
        tree.refresh()

    def _refresh_diff(self) -> None:
        snap = self.diff_tracker.get_diff()
        self._current_snap = snap
        self._render_diff_ui(snap)

    # ─── Background workers ───────────────────────────────────────

    @work(thread=True, name="pty-watcher")
    def _watch_pty_exit(self) -> None:
        import time
        start = time.monotonic()
        while True:
            time.sleep(0.5)
            # Don't exit while resuming a different session
            if self._resuming:
                continue
            try:
                term = self.query_one("#claude-terminal", TerminalWidget)
            except Exception:
                continue
            if not term.is_alive:
                elapsed = time.monotonic() - start
                if elapsed < 3:
                    # Died too fast — likely a bad flag. Wait so user can see error.
                    time.sleep(3)
                # Capture session info from terminal for the exit message
                text = term.get_visible_text()
                self._extract_exit_info(text)
                self.app.call_from_thread(self._graceful_exit)
                break

    def _extract_exit_info(self, terminal_text: str) -> None:
        """Extract session ID from terminal output for the exit message."""
        import re
        # Claude prints "To resume: claude --resume <id>" on exit
        match = re.search(r"--resume\s+([a-f0-9-]{36})", terminal_text)
        if match:
            session_id = match.group(1)
            self._exit_message = (
                f"\nTo resume this session:\n"
                f"  claude-tui --resume {session_id}\n"
                f"  claude-tui --resume  (session picker)\n"
            )

    def _graceful_exit(self) -> None:
        self.diff_tracker.stop_watching()
        self.exit()

    # ─── Quit ─────────────────────────────────────────────────────

    async def action_request_quit(self) -> None:
        self.diff_tracker.stop_watching()
        try:
            term = self.query_one("#claude-terminal", TerminalWidget)
            text = term.get_visible_text()
            self._extract_exit_info(text)
            await term.cleanup()
        except Exception:
            pass
        self.exit()

    async def on_unmount(self) -> None:
        self.diff_tracker.stop_watching()
        try:
            await self.query_one("#claude-terminal", TerminalWidget).cleanup()
        except Exception:
            pass
        # Print exit message after TUI clears the screen
        if self._exit_message:
            print(self._exit_message)

    # ─── File viewer ──────────────────────────────────────────────

    def on_directory_tree_file_selected(self, event: DirectoryTree.FileSelected) -> None:
        path = str(event.path)
        try:
            viewer = self.query_one("#file-viewer", TextArea)
            viewer.load_text(Path(path).read_text(errors="replace"))
            lang = _LANG_MAP.get(Path(path).suffix)
            if lang:
                try:
                    viewer.language = lang
                except Exception:
                    pass
            self.query_one("#tab-panel", TabbedContent).active = "tab-files"
        except Exception:
            pass

    # ─── Diff tab ─────────────────────────────────────────────────

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        if event.pane.id == "tab-diff":
            self._refresh_diff()

    def _render_diff_ui(self, snap: DiffSnapshot) -> None:
        self.query_one("#diff-summary", DiffSummary).update(
            Text(f"  {snap.summary}", style="dim italic" if not snap.files else "#8d99ae")
        )

        file_list = self.query_one("#diff-file-list", DiffFileList)
        file_list.clear()
        for f in snap.files:
            char, color = _STATUS_STYLE.get(f.status, ("?", "#8d99ae"))
            label = Text.assemble(
                Text(f" {char} ", style=f"bold {color}"),
                Text(f.path, style="#e0e0e0"),
                Text(f"  +{f.additions} -{f.deletions}", style="dim"),
            )
            file_list.append(ListItem(Label(label), name=f.path))

        if snap.files:
            self._render_file_diff(snap.files[0])
        else:
            self.query_one("#diff-content", DiffContent).clear()

    @on(ListView.Selected, "#diff-file-list")
    def _on_diff_file_selected(self, event: ListView.Selected) -> None:
        if not self._current_snap or not event.item.name:
            return
        for f in self._current_snap.files:
            if f.path == event.item.name:
                self._render_file_diff(f)
                break

    def _render_file_diff(self, f: FileDiff) -> None:
        content = self.query_one("#diff-content", DiffContent)
        content.clear()
        for line in f.diff_text.splitlines():
            if line.startswith("diff --git"):
                style = "bold #c084fc"
            elif line.startswith(("new file", "deleted file")):
                style = "bold #f59e0b"
            elif line.startswith(("+++", "---")):
                style = "bold"
            elif line.startswith("@@"):
                style = "#67e8f9"
            elif line.startswith("+"):
                style = "#4ade80"
            elif line.startswith("-"):
                style = "#f87171"
            else:
                style = "dim"
            content.write(Text(line, style=style))

    # ─── Actions ──────────────────────────────────────────────────

    def action_focus_tree(self) -> None:
        self.query_one("#file-tree", FileTree).focus()

    def action_focus_terminal(self) -> None:
        self.query_one("#claude-terminal", TerminalWidget).focus()

    def action_tab_terminal(self) -> None:
        self.query_one("#tab-panel", TabbedContent).active = "tab-terminal"
        self.query_one("#claude-terminal", TerminalWidget).focus()

    def action_tab_files(self) -> None:
        self.query_one("#tab-panel", TabbedContent).active = "tab-files"

    def action_tab_diff(self) -> None:
        self.query_one("#tab-panel", TabbedContent).active = "tab-diff"

    def action_refresh_tree(self) -> None:
        tree = self.query_one("#file-tree", FileTree)
        tree.refresh_git_status()
        tree.reload()
        self.notify("File tree refreshed", timeout=2)

    def action_reload_diff(self) -> None:
        self._refresh_diff()
        self.notify("Diff refreshed", timeout=2)

    def action_copy_terminal(self) -> None:
        """Copy visible terminal text to system clipboard."""
        try:
            term = self.query_one("#claude-terminal", TerminalWidget)
            text = term.get_visible_text()
            if not text:
                self.notify("Nothing to copy", timeout=2)
                return
            proc = subprocess.run(
                ["pbcopy"], input=text.encode(), timeout=5,
            )
            if proc.returncode == 0:
                self.notify("Terminal content copied", timeout=2)
        except Exception:
            self.notify("Copy failed", timeout=2)

    def action_open_session_picker(self) -> None:
        self._show_session_picker()

    def action_cycle_tab(self) -> None:
        tabs = self.query_one("#tab-panel", TabbedContent)
        order = ["tab-terminal", "tab-files", "tab-diff"]
        try:
            nxt = order[(order.index(tabs.active) + 1) % len(order)]
        except ValueError:
            nxt = "tab-terminal"
        tabs.active = nxt
        if nxt == "tab-terminal":
            self.query_one("#claude-terminal", TerminalWidget).focus()
