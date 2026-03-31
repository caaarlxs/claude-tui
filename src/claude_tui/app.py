"""Main Textual application for claude-tui.

Visual wrapper around Claude Code CLI with file tree, file viewer, diff view,
command palette, and a polished custom UI.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from rich.text import Text
from textual import work, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.command import Provider, Hit, Hits
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.reactive import reactive
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
from claude_tui.terminal_widget import TerminalWidget


# ─── File type icons ─────────────────────────────────────────────────

_FILE_ICONS: dict[str, tuple[str, str]] = {
    ".py": ("py", "#3572A5"), ".js": ("js", "#f1e05a"),
    ".ts": ("ts", "#3178c6"), ".tsx": ("tx", "#3178c6"),
    ".jsx": ("jx", "#f1e05a"), ".json": ("{}", "#a8a8a2"),
    ".html": ("<>", "#e34c26"), ".css": ("# ", "#563d7c"),
    ".scss": ("# ", "#c6538c"), ".md": ("md", "#083fa1"),
    ".rs": ("rs", "#dea584"), ".go": ("go", "#00ADD8"),
    ".rb": ("rb", "#701516"), ".java": ("jv", "#b07219"),
    ".c": (" c", "#555555"), ".cpp": ("c+", "#f34b7d"),
    ".h": (".h", "#555555"), ".swift": ("sw", "#F05138"),
    ".kt": ("kt", "#A97BFF"), ".sh": ("$ ", "#89e051"),
    ".bash": ("$ ", "#89e051"), ".zsh": ("$ ", "#89e051"),
    ".yaml": ("ym", "#cb171e"), ".yml": ("ym", "#cb171e"),
    ".toml": ("tm", "#9c4221"), ".xml": ("xm", "#0060ac"),
    ".lock": ("lk", "#555555"), ".env": ("ev", "#ECD53F"),
    ".gitignore": ("gi", "#F05032"),
}


# ─── Custom header ───────────────────────────────────────────────────

class TuiHeader(Static):
    DEFAULT_CSS = """
    TuiHeader {
        dock: top;
        height: 1;
        background: #1a1a2e;
        color: #e0e0e0;
        padding: 0 1;
    }
    """
    cwd_display: reactive[str] = reactive("")

    def render(self) -> Text:
        t = Text()
        t.append(" claude-tui ", style="bold #e07a5f on #1a1a2e")
        t.append(" ", style="#1a1a2e")
        if self.cwd_display:
            t.append(f" {self.cwd_display} ", style="#8d99ae")
        return t


# ─── Custom footer ───────────────────────────────────────────────────

class TuiFooter(Static):
    DEFAULT_CSS = """
    TuiFooter {
        dock: bottom;
        height: 1;
        background: #1a1a2e;
        color: #8d99ae;
        padding: 0 0;
    }
    """

    def render(self) -> Text:
        t = Text()
        for key, label in [("^Q", "Quit"), ("^\\", "Tab"), ("^T", "Tree"),
                           ("^B", "Terminal"), ("^P", "Commands")]:
            t.append(f" {key} ", style="bold #e0e0e0 on #2d2d44")
            t.append(f" {label} ", style="#8d99ae on #1a1a2e")
        return t


# ─── File tree ───────────────────────────────────────────────────────

class FileTree(DirectoryTree):
    DEFAULT_CSS = """
    FileTree {
        background: #12121a;
        scrollbar-size: 1 1;
    }
    """
    _HIDDEN = {".git", "node_modules", "__pycache__", ".venv", ".mypy_cache",
               ".ruff_cache", ".DS_Store", ".claude", ".env", ".pytest_cache",
               ".tox", "dist", "build"}

    def filter_paths(self, paths):
        return [p for p in paths if p.name not in self._HIDDEN
                and not p.name.endswith(".egg-info")]

    def render_label(self, node, base_style, style):
        label = super().render_label(node, base_style, style)
        path = node.data.path if node.data else None
        if not path or path.is_dir():
            return label
        ext = path.suffix.lower()
        name = path.name.lower()
        entry = _FILE_ICONS.get(name) or _FILE_ICONS.get(ext)
        if not entry:
            return label
        icon, color = entry
        return Text.assemble(Text(f"{icon} ", style=f"dim {color}"), label)


# ─── Resize handle ───────────────────────────────────────────────────

class ResizeHandle(Static):
    DEFAULT_CSS = """
    ResizeHandle {
        width: 1; height: 100%;
        background: #1e1e2e; color: #3a3a5a;
    }
    ResizeHandle:hover { background: #e07a5f; color: #e07a5f; }
    """

    def __init__(self, **kwargs):
        super().__init__("│", **kwargs)
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


# ─── Diff panel widgets ──────────────────────────────────────────────

class DiffFileList(ListView):
    """List of changed files — click one to see its diff."""
    DEFAULT_CSS = """
    DiffFileList {
        height: auto;
        max-height: 10;
        background: #12121a;
        border-bottom: solid #2a2a3e;
    }
    DiffFileList > ListItem {
        padding: 0 1;
        height: 1;
    }
    DiffFileList > ListItem.-highlight {
        background: #1e1e2e;
    }
    """


class DiffSummary(Static):
    """Summary line above the file list."""
    DEFAULT_CSS = """
    DiffSummary {
        height: 1;
        background: #1a1a2e;
        color: #8d99ae;
        padding: 0 1;
    }
    """


class DiffContent(RichLog):
    """The actual diff content for the selected file."""
    DEFAULT_CSS = """
    DiffContent {
        height: 1fr;
        background: #0d0d14;
    }
    """


# ─── Command palette ─────────────────────────────────────────────────

class TuiCommands(Provider):
    async def search(self, query: str) -> Hits:
        app = self.app
        commands = [
            ("Focus Terminal", "Switch to Claude terminal", "focus_terminal"),
            ("Focus File Tree", "Switch to file tree", "focus_tree"),
            ("Tab: Terminal", "Terminal tab", "tab_terminal"),
            ("Tab: Files", "Files tab", "tab_files"),
            ("Tab: Diff", "Diff tab", "tab_diff"),
            ("Refresh File Tree", "Reload file tree", "refresh_tree"),
            ("Refresh Diff", "Re-scan for changes", "reload_diff"),
            ("Quit", "Exit claude-tui", "request_quit"),
        ]
        matcher = self.matcher(query)
        for name, help_text, action in commands:
            score = matcher.match(name)
            if score > 0:
                async def _run(act=action):
                    await app.run_action(act)
                yield Hit(score, matcher.highlight(name), _run, text=name, help=help_text)


# ─── Main App ────────────────────────────────────────────────────────

class ClaudeTuiApp(App):
    TITLE = "claude-tui"
    COMMANDS = {TuiCommands}

    CSS = """
    Screen { layout: vertical; background: #0d0d14; }
    #main-content { height: 1fr; }
    #file-tree { width: 28; min-width: 15; max-width: 60; background: #12121a; border-right: solid #2a2a3e; }
    #resize-handle { width: 1; }
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
        Binding("ctrl+backslash", "cycle_tab", "Next Tab", show=False),
        Binding("ctrl+t", "focus_tree", "Tree", show=False),
        Binding("ctrl+b", "focus_terminal", "Terminal", show=False),
        Binding("ctrl+p", "command_palette", "Commands", show=False),
        Binding("ctrl+q", "request_quit", "Quit", show=False),
    ]

    def __init__(self, claude_args: list[str] | None = None, cwd: str = ".") -> None:
        super().__init__()
        self.claude_cwd = str(Path(cwd).resolve())
        self.claude_args = claude_args or []
        self.diff_tracker = DiffTracker(self.claude_cwd)
        self._current_snapshot: DiffSnapshot | None = None
        self._last_diff_hash = ""

    def _build_claude_command(self) -> list[str]:
        return ["claude"] + self.claude_args

    def compose(self) -> ComposeResult:
        yield TuiHeader(id="tui-header")
        with Horizontal(id="main-content"):
            yield FileTree(self.claude_cwd, id="file-tree")
            yield ResizeHandle(id="resize-handle")
            with TabbedContent(id="tab-panel"):
                with TabPane("Terminal", id="tab-terminal"):
                    yield TerminalWidget(
                        command=self._build_claude_command(),
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
        self.query_one("#tui-header", TuiHeader).cwd_display = self._short_cwd()
        self.query_one("#claude-terminal", TerminalWidget).focus()
        self.diff_tracker.take_baseline()
        self._watch_pty_exit()
        self._auto_refresh_tree()
        self._watch_file_changes()

    def _short_cwd(self) -> str:
        home = os.path.expanduser("~")
        if self.claude_cwd.startswith(home):
            return "~" + self.claude_cwd[len(home):]
        return self.claude_cwd

    # ─── Background workers ───────────────────────────────────────

    @work(thread=True, name="pty-watcher")
    def _watch_pty_exit(self) -> None:
        term = self.query_one("#claude-terminal", TerminalWidget)
        while True:
            time.sleep(0.5)
            if not term.is_alive:
                self.app.call_from_thread(self.exit)
                break

    @work(thread=True, name="tree-refresher")
    def _auto_refresh_tree(self) -> None:
        while True:
            time.sleep(5)
            try:
                self.app.call_from_thread(self.query_one("#file-tree", FileTree).reload)
            except Exception:
                break

    @work(thread=True, name="diff-watcher")
    def _watch_file_changes(self) -> None:
        """Poll git diff for changes and update the diff tab automatically."""
        while True:
            time.sleep(3)
            try:
                snap = self.diff_tracker.get_full_diff()
                # Only update if something changed
                new_hash = snap.summary + "".join(f.path for f in snap.files)
                if new_hash != self._last_diff_hash:
                    self._last_diff_hash = new_hash
                    self._current_snapshot = snap
                    self.app.call_from_thread(self._update_diff_ui, snap)
            except Exception:
                break

    # ─── Quit ─────────────────────────────────────────────────────

    async def action_request_quit(self) -> None:
        try:
            await self.query_one("#claude-terminal", TerminalWidget).cleanup()
        except Exception:
            pass
        self.exit()

    async def on_unmount(self) -> None:
        try:
            await self.query_one("#claude-terminal", TerminalWidget).cleanup()
        except Exception:
            pass

    # ─── File viewer ──────────────────────────────────────────────

    def on_directory_tree_file_selected(self, event: DirectoryTree.FileSelected) -> None:
        path = str(event.path)
        try:
            content = Path(path).read_text(errors="replace")
            viewer = self.query_one("#file-viewer", TextArea)
            viewer.load_text(content)
            ext_map = {
                ".py": "python", ".js": "javascript", ".ts": "javascript",
                ".tsx": "javascript", ".jsx": "javascript",
                ".css": "css", ".html": "html", ".json": "json",
                ".md": "markdown", ".rs": "rust", ".go": "go",
                ".yaml": "yaml", ".yml": "yaml", ".toml": "toml",
                ".sh": "bash", ".bash": "bash", ".zsh": "bash",
                ".rb": "ruby", ".c": "c", ".cpp": "c", ".h": "c",
                ".java": "java", ".swift": "swift", ".kt": "kotlin",
            }
            lang = ext_map.get(Path(path).suffix)
            if lang:
                try:
                    viewer.language = lang
                except Exception:
                    pass
            self.query_one("#tab-panel", TabbedContent).active = "tab-files"
            self.notify(f"Opened {Path(path).name}", timeout=2)
        except Exception:
            pass

    # ─── Diff tab ─────────────────────────────────────────────────

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        if event.pane.id == "tab-diff" and self._current_snapshot:
            self._update_diff_ui(self._current_snapshot)

    def _update_diff_ui(self, snap: DiffSnapshot) -> None:
        """Update the diff panel with a new snapshot."""
        # Update summary
        summary = self.query_one("#diff-summary", DiffSummary)
        if not snap.files:
            summary.update(Text(f"  {snap.summary}", style="dim italic"))
        else:
            t = Text()
            t.append(f"  {snap.summary}", style="#8d99ae")
            summary.update(t)

        # Update file list
        file_list = self.query_one("#diff-file-list", DiffFileList)
        file_list.clear()
        for f in snap.files:
            status_style = {
                "modified": "#f59e0b",
                "added": "#4ade80",
                "deleted": "#f87171",
                "renamed": "#67e8f9",
            }.get(f.status, "#8d99ae")
            status_char = {"modified": "M", "added": "A", "deleted": "D", "renamed": "R"}.get(f.status, "?")
            label = Text.assemble(
                Text(f" {status_char} ", style=f"bold {status_style}"),
                Text(f.path, style="#e0e0e0"),
                Text(f"  +{f.additions} -{f.deletions}", style="dim"),
            )
            file_list.append(ListItem(Label(label), name=f.path))

        # Show first file's diff by default
        if snap.files:
            self._show_file_diff(snap.files[0])

    @on(ListView.Selected, "#diff-file-list")
    def on_diff_file_selected(self, event: ListView.Selected) -> None:
        """Show the diff for the selected file."""
        if not self._current_snapshot or not event.item.name:
            return
        for f in self._current_snapshot.files:
            if f.path == event.item.name:
                self._show_file_diff(f)
                break

    def _show_file_diff(self, file_diff: FileDiff) -> None:
        """Render a single file's diff in the content area."""
        content = self.query_one("#diff-content", DiffContent)
        content.clear()

        for line in file_diff.diff_text.splitlines():
            if line.startswith("diff --git"):
                content.write(Text(line, style="bold #c084fc"))
            elif line.startswith("index "):
                content.write(Text(line, style="dim"))
            elif line.startswith("+++") or line.startswith("---"):
                content.write(Text(line, style="bold"))
            elif line.startswith("@@"):
                content.write(Text(line, style="#67e8f9"))
            elif line.startswith("+"):
                content.write(Text(line, style="#4ade80"))
            elif line.startswith("-"):
                content.write(Text(line, style="#f87171"))
            elif line.startswith("new file") or line.startswith("deleted file"):
                content.write(Text(line, style="bold #f59e0b"))
            else:
                content.write(Text(line, style="dim"))

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
        self.query_one("#file-tree", FileTree).reload()
        self.notify("File tree refreshed", timeout=2)

    def action_reload_diff(self) -> None:
        snap = self.diff_tracker.get_full_diff()
        self._current_snapshot = snap
        self._update_diff_ui(snap)
        self.notify("Diff refreshed", timeout=2)

    def action_cycle_tab(self) -> None:
        tabs = self.query_one("#tab-panel", TabbedContent)
        order = ["tab-terminal", "tab-files", "tab-diff"]
        current = tabs.active
        try:
            idx = order.index(current)
            nxt = order[(idx + 1) % len(order)]
        except ValueError:
            nxt = "tab-terminal"
        tabs.active = nxt
        if nxt == "tab-terminal":
            self.query_one("#claude-terminal", TerminalWidget).focus()
