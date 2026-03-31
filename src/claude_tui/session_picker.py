"""Session picker — browse and resume past Claude Code conversations.

Reads ~/.claude/history.jsonl to discover all past sessions across all
projects. Groups by project, sorts by last activity, auto-generates tags,
and presents a navigable TUI for session selection.

Inspired by github.com/crlxs/claude-history (Go implementation).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SessionMeta:
    """Metadata for a single Claude Code session."""
    session_id: str
    project_path: str
    summary: str = ""
    name: str = ""
    message_count: int = 0
    timestamp: float = 0.0
    branch: str = ""
    tags: list[str] = field(default_factory=list)

    @property
    def display_name(self) -> str:
        return self.name or self.summary or self.session_id[:12]

    @property
    def relative_time(self) -> str:
        if not self.timestamp:
            return ""
        import time
        delta = time.time() - self.timestamp / 1000  # timestamp is ms
        if delta < 60:
            return "just now"
        if delta < 3600:
            return f"{int(delta / 60)}m ago"
        if delta < 86400:
            return f"{int(delta / 3600)}h ago"
        days = int(delta / 86400)
        if days == 1:
            return "1d ago"
        if days < 30:
            return f"{days}d ago"
        return f"{int(days / 30)}mo ago"


@dataclass
class Project:
    """A project directory with its sessions."""
    path: str
    sessions: list[SessionMeta] = field(default_factory=list)

    @property
    def display_name(self) -> str:
        return self.path.replace(os.path.expanduser("~"), "~")

    @property
    def last_activity(self) -> float:
        return max((s.timestamp for s in self.sessions), default=0)

    @property
    def short_name(self) -> str:
        return Path(self.path).name


def _encode_project_path(path: str) -> str:
    """Convert /Users/x/path to -Users-x-path (Claude's encoding)."""
    return path.replace("/", "-")


def _generate_tags(messages_text: str, tool_names: set[str], msg_count: int) -> list[str]:
    """Auto-generate tags based on conversation content."""
    tags: list[str] = []
    text_lower = messages_text.lower()

    # Topic tags
    if any(w in text_lower for w in ("bug", "fix", "error", "issue", "broken")):
        tags.append("bug fix")
    if any(w in text_lower for w in ("refactor", "clean up", "reorganize")):
        tags.append("refactor")
    if any(w in text_lower for w in ("react", "css", "html", "frontend", "component", "ui")):
        tags.append("frontend")
    if any(w in text_lower for w in ("api", "endpoint", "server", "backend", "database")):
        tags.append("backend")
    if any(w in text_lower for w in ("docker", "deploy", "ci", "pipeline", "kubernetes")):
        tags.append("devops")
    if any(w in text_lower for w in ("test", "spec", "coverage")):
        tags.append("testing")

    # Activity tags
    if tool_names & {"Edit", "Write"}:
        tags.append("coding")
    elif tool_names & {"WebSearch", "WebFetch"}:
        tags.append("research")
    elif tool_names & {"Read", "Glob", "Grep"} and not tool_names & {"Edit", "Write"}:
        tags.append("exploration")

    # Size tags
    if msg_count <= 5:
        tags.append("quick")
    elif msg_count > 80:
        tags.append("long session")

    return tags[:4]


def load_projects() -> list[Project]:
    """Load all Claude Code projects and sessions from ~/.claude/."""
    claude_dir = Path.home() / ".claude"
    history_file = claude_dir / "history.jsonl"

    if not history_file.exists():
        return []

    # Parse history.jsonl — each line is a JSON object with session info
    sessions_by_project: dict[str, dict[str, SessionMeta]] = {}

    try:
        with open(history_file, "r", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                session_id = entry.get("sessionId", "")
                project_path = entry.get("project", "") or entry.get("directory", "") or entry.get("cwd", "")
                timestamp = entry.get("timestamp", 0)
                display = entry.get("display", "") or entry.get("query", "")

                if not session_id or not project_path:
                    continue

                if project_path not in sessions_by_project:
                    sessions_by_project[project_path] = {}

                if session_id not in sessions_by_project[project_path]:
                    # Extract summary from the display/query field
                    if isinstance(display, list):
                        parts = []
                        for item in display:
                            if isinstance(item, dict) and item.get("type") == "text":
                                parts.append(item.get("text", ""))
                            elif isinstance(item, str):
                                parts.append(item)
                        display = " ".join(parts)

                    # Skip slash commands and empty prompts for summary
                    summary = str(display).strip()
                    if summary.startswith("/") or len(summary) < 3:
                        summary = ""
                    else:
                        summary = summary[:120]

                    sessions_by_project[project_path][session_id] = SessionMeta(
                        session_id=session_id,
                        project_path=project_path,
                        summary=summary,
                        timestamp=timestamp,
                    )
                else:
                    # Update timestamp, message count, and summary if empty
                    meta = sessions_by_project[project_path][session_id]
                    meta.message_count += 1
                    if timestamp > meta.timestamp:
                        meta.timestamp = timestamp
                    if not meta.summary and isinstance(display, str):
                        s = display.strip()
                        if not s.startswith("/") and len(s) >= 3:
                            meta.summary = s[:120]
    except Exception:
        return []

    # Enrich sessions with data from session files
    projects_dir = claude_dir / "projects"
    for project_path, sessions in sessions_by_project.items():
        encoded = _encode_project_path(project_path)
        session_dir = projects_dir / encoded

        for session_id, meta in sessions.items():
            session_file = session_dir / f"{session_id}.jsonl"
            if session_file.exists():
                _enrich_session(meta, session_file)

    # Load session names
    _load_session_names(claude_dir, sessions_by_project)

    # Build project list
    projects: list[Project] = []
    for path, sessions in sessions_by_project.items():
        session_list = sorted(sessions.values(), key=lambda s: s.timestamp, reverse=True)
        projects.append(Project(path=path, sessions=session_list))

    projects.sort(key=lambda p: p.last_activity, reverse=True)
    return projects


def _enrich_session(meta: SessionMeta, session_file: Path) -> None:
    """Read a session JSONL file to extract tags, branch, and better summary."""
    try:
        text_parts: list[str] = []
        tool_names: set[str] = set()
        msg_count = 0

        with open(session_file, "r", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                msg_type = entry.get("type", "")

                if msg_type == "human":
                    msg_count += 1
                    content = entry.get("content", "")
                    if isinstance(content, str):
                        text_parts.append(content[:500])
                    elif isinstance(content, list):
                        for item in content:
                            if isinstance(item, dict) and item.get("type") == "text":
                                text_parts.append(item.get("text", "")[:500])

                elif msg_type == "assistant":
                    msg_count += 1
                    content = entry.get("content", [])
                    if isinstance(content, list):
                        for item in content:
                            if isinstance(item, dict):
                                if item.get("type") == "tool_use":
                                    tool_names.add(item.get("name", ""))

                # Check for git branch in metadata
                if "branch" in entry:
                    meta.branch = entry["branch"]
                elif isinstance(entry.get("metadata"), dict):
                    meta.branch = entry["metadata"].get("branch", meta.branch)

        meta.message_count = max(meta.message_count, msg_count)
        all_text = " ".join(text_parts)
        meta.tags = _generate_tags(all_text, tool_names, msg_count)

        # Better summary from first meaningful message
        if text_parts and (not meta.summary or len(meta.summary) < 10):
            meta.summary = text_parts[0][:120]

    except Exception:
        pass


def _load_session_names(claude_dir: Path, sessions_by_project: dict) -> None:
    """Load user-set session names."""
    # Claude stores session metadata in different places depending on version
    # Try the sessions directory
    sessions_dir = claude_dir / "sessions"
    if not sessions_dir.exists():
        return

    for json_file in sessions_dir.glob("*.json"):
        try:
            data = json.loads(json_file.read_text())
            session_id = data.get("id", json_file.stem)
            name = data.get("name", "")
            if not name:
                continue
            # Find this session in our data and set the name
            for sessions in sessions_by_project.values():
                if session_id in sessions:
                    sessions[session_id].name = name
        except Exception:
            pass
