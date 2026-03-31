"""Session picker screen — Textual UI for browsing and resuming sessions.

Shows projects -> sessions -> select to resume. Includes search.
Arrow keys navigate the list, Enter selects, Escape goes back/exits.
"""

from __future__ import annotations

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import (
    Input,
    Label,
    ListItem,
    ListView,
    Static,
)

from claude_tui.session_picker import Project, load_projects


class SessionPickerScreen(Screen[tuple[str, str] | None]):
    """Full-screen session picker. Returns (project_path, session_id) or None."""

    DEFAULT_CSS = """
    SessionPickerScreen {
        background: #0d0d14;
    }
    #picker-header {
        dock: top;
        height: 1;
        background: #1a1a2e;
        padding: 0 1;
    }
    #picker-search {
        dock: top;
        margin: 0 1;
    }
    #picker-list {
        height: 1fr;
        background: #0d0d14;
        margin: 0 1;
    }
    #picker-list > ListItem {
        padding: 0 1;
        height: auto;
    }
    #picker-list > ListItem.-highlight {
        background: #1e1e2e;
    }
    #picker-footer {
        dock: bottom;
        height: 1;
        background: #1a1a2e;
        padding: 0 1;
    }
    """

    BINDINGS = [
        Binding("escape", "go_back", "Back"),
        Binding("up", "cursor_up", "Up", show=False),
        Binding("down", "cursor_down", "Down", show=False),
        Binding("enter", "select_item", "Select", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._projects: list[Project] = []
        self._view: str = "projects"
        self._selected_project: Project | None = None

    def compose(self) -> ComposeResult:
        yield Static(id="picker-header")
        yield Input(placeholder="Type to filter...", id="picker-search")
        yield ListView(id="picker-list")
        yield Static(id="picker-footer")

    def on_mount(self) -> None:
        self._projects = load_projects()
        self._show_projects()
        self._update_footer()
        # Focus the list so arrows work immediately
        self.query_one("#picker-list", ListView).focus()

    def _update_footer(self) -> None:
        self.query_one("#picker-footer", Static).update(
            Text.assemble(
                Text(" Esc ", style="bold #e0e0e0 on #2d2d44"),
                Text(" Back " if self._view == "sessions" else " Exit ", style="#8d99ae on #1a1a2e"),
                Text(" Enter ", style="bold #e0e0e0 on #2d2d44"),
                Text(" Select ", style="#8d99ae on #1a1a2e"),
                Text(" / ", style="bold #e0e0e0 on #2d2d44"),
                Text(" Search ", style="#8d99ae on #1a1a2e"),
                Text(f"  {len(self._projects)} projects", style="dim #555555"),
            )
        )

    def _show_projects(self) -> None:
        self._view = "projects"
        self.query_one("#picker-header", Static).update(
            Text(" Resume Session  --  Select a project", style="bold #e07a5f")
        )
        lv = self.query_one("#picker-list", ListView)
        lv.clear()
        query = self.query_one("#picker-search", Input).value.lower()
        for p in self._projects:
            if query and query not in p.display_name.lower() and query not in p.short_name.lower():
                # Also search in session summaries
                if not any(query in s.display_name.lower() for s in p.sessions):
                    continue
            n = len(p.sessions)
            when = p.sessions[0].relative_time if p.sessions else ""
            label = Text.assemble(
                Text(f"  {p.short_name}", style="bold #67e8f9"),
                Text(f"  {n} session{'s' if n != 1 else ''}", style="#6b7280"),
                Text(f"  {when}", style="#6b7280"),
                Text(f"\n  {p.display_name}", style="dim #555555"),
            )
            lv.append(ListItem(Label(label), name=p.path))
        self._update_footer()

    def _show_sessions(self, project: Project) -> None:
        self._view = "sessions"
        self._selected_project = project
        self.query_one("#picker-header", Static).update(
            Text.assemble(
                Text(f" {project.short_name}", style="bold #67e8f9"),
                Text(f"  --  {len(project.sessions)} sessions", style="#8d99ae"),
            )
        )
        lv = self.query_one("#picker-list", ListView)
        lv.clear()
        query = self.query_one("#picker-search", Input).value.lower()
        for s in project.sessions:
            if query and query not in s.display_name.lower():
                continue
            tags = " ".join(f"[{t}]" for t in s.tags)
            label = Text.assemble(
                Text(f"  {s.display_name[:70]}", style="#e0e0e0"),
                Text(f"\n  {s.relative_time}", style="#6b7280"),
                Text(f"  {s.message_count} msgs", style="#6b7280"),
                Text(f"  {s.branch}" if s.branch else "", style="#a78bfa"),
                Text(f"  {tags}" if tags else "", style="dim #f59e0b"),
            )
            lv.append(ListItem(Label(label), name=s.session_id))
        self._update_footer()
        lv.focus()

    # ─── Key handling ─────────────────────────────────────────────

    def on_key(self, event) -> None:
        """Route arrow keys to the list even if search has focus."""
        if event.key in ("up", "down", "enter"):
            lv = self.query_one("#picker-list", ListView)
            if not lv.has_focus:
                lv.focus()
                # Don't consume — let ListView handle it

    @on(Input.Changed, "#picker-search")
    def _on_search(self, event: Input.Changed) -> None:
        if self._view == "projects":
            self._show_projects()
        elif self._selected_project:
            self._show_sessions(self._selected_project)

    @on(Input.Submitted, "#picker-search")
    def _on_search_submit(self, event: Input.Submitted) -> None:
        """Enter in search box focuses the list."""
        self.query_one("#picker-list", ListView).focus()

    @on(ListView.Selected, "#picker-list")
    def _on_selected(self, event: ListView.Selected) -> None:
        if not event.item.name:
            return
        if self._view == "projects":
            for p in self._projects:
                if p.path == event.item.name:
                    self.query_one("#picker-search", Input).value = ""
                    self._show_sessions(p)
                    break
        elif self._view == "sessions" and self._selected_project:
            self.dismiss((self._selected_project.path, event.item.name))

    def action_go_back(self) -> None:
        if self._view == "sessions":
            self._view = "projects"
            self.query_one("#picker-search", Input).value = ""
            self._show_projects()
            self.query_one("#picker-list", ListView).focus()
        else:
            # At project level, cancel = exit
            self.dismiss(None)

    def action_cursor_up(self) -> None:
        self.query_one("#picker-list", ListView).action_cursor_up()

    def action_cursor_down(self) -> None:
        self.query_one("#picker-list", ListView).action_cursor_down()

    def action_select_item(self) -> None:
        lv = self.query_one("#picker-list", ListView)
        if lv.highlighted_child:
            lv.action_select_cursor()
