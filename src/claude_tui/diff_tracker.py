"""Track file changes and generate diffs.

Uses watchdog for instant filesystem event detection and git for
generating unified diffs. The tracker maintains a list of files that
changed since the last baseline so the Diff tab can show exactly what
Claude modified.
"""

from __future__ import annotations

import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from watchdog.events import FileSystemEventHandler, FileSystemEvent
from watchdog.observers import Observer


@dataclass
class FileDiff:
    """A single file's diff."""
    path: str
    status: str  # modified, added, deleted, renamed
    diff_text: str
    additions: int = 0
    deletions: int = 0


@dataclass
class DiffSnapshot:
    """A collection of file diffs."""
    files: list[FileDiff] = field(default_factory=list)
    summary: str = ""


class _ChangeHandler(FileSystemEventHandler):
    """Collects paths of changed files, ignoring junk directories."""

    _IGNORE = {".git", "__pycache__", ".venv", "node_modules", ".mypy_cache",
               ".ruff_cache", ".pytest_cache", ".DS_Store"}

    def __init__(self, callback: Callable[[], None]) -> None:
        super().__init__()
        self.changed_paths: set[str] = set()
        self._callback = callback
        self._lock = threading.Lock()

    def _should_ignore(self, path: str) -> bool:
        parts = Path(path).parts
        return any(p in self._IGNORE for p in parts)

    def _record(self, path: str) -> None:
        if self._should_ignore(path):
            return
        with self._lock:
            self.changed_paths.add(path)
        self._callback()

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._record(event.src_path)

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._record(event.src_path)

    def on_deleted(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._record(event.src_path)

    def on_moved(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._record(event.src_path)
            if hasattr(event, "dest_path"):
                self._record(event.dest_path)

    def drain(self) -> set[str]:
        with self._lock:
            paths = self.changed_paths.copy()
            self.changed_paths.clear()
        return paths


class DiffTracker:
    """Watches the filesystem and generates git diffs on demand."""

    def __init__(self, cwd: str, on_change: Callable[[], None] | None = None) -> None:
        self.cwd = cwd
        self._is_git = self._check_git()
        self._observer: Observer | None = None
        self._handler: _ChangeHandler | None = None
        self._on_change = on_change or (lambda: None)

    def _check_git(self) -> bool:
        try:
            r = subprocess.run(
                ["git", "rev-parse", "--git-dir"],
                cwd=self.cwd, capture_output=True, timeout=5,
            )
            return r.returncode == 0
        except Exception:
            return False

    def _git(self, *args: str) -> str:
        try:
            r = subprocess.run(
                ["git", *args],
                cwd=self.cwd, capture_output=True, text=True, timeout=10,
            )
            return r.stdout
        except Exception:
            return ""

    # ─── Watcher lifecycle ────────────────────────────────────────

    def start_watching(self) -> None:
        if self._observer:
            return
        self._handler = _ChangeHandler(self._on_change)
        self._observer = Observer()
        self._observer.schedule(self._handler, self.cwd, recursive=True)
        self._observer.daemon = True
        self._observer.start()

    def stop_watching(self) -> None:
        if self._observer:
            self._observer.stop()
            self._observer = None
            self._handler = None

    @property
    def has_pending_changes(self) -> bool:
        return bool(self._handler and self._handler.changed_paths)

    # ─── Diff generation ─────────────────────────────────────────

    def get_diff(self) -> DiffSnapshot:
        """Get all uncommitted changes (staged + unstaged + untracked)."""
        if not self._is_git:
            return DiffSnapshot(summary="Not a git repository")

        # Unstaged changes
        diff_output = self._git("diff")
        # Staged changes
        staged = self._git("diff", "--cached")
        combined = diff_output + staged

        files = self._parse_diff(combined) if combined.strip() else []

        # Untracked files
        untracked = self._git("ls-files", "--others", "--exclude-standard")
        for fpath in untracked.strip().splitlines():
            fpath = fpath.strip()
            if not fpath:
                continue
            try:
                content = Path(self.cwd, fpath).read_text(errors="replace")
                lines = content.splitlines()
                diff_text = f"new file: {fpath}\n" + "\n".join(f"+{l}" for l in lines[:200])
                if len(lines) > 200:
                    diff_text += f"\n... ({len(lines) - 200} more lines)"
                files.append(FileDiff(
                    path=fpath, status="added",
                    diff_text=diff_text, additions=len(lines),
                ))
            except Exception:
                files.append(FileDiff(path=fpath, status="added", diff_text="(binary or unreadable)"))

        snap = DiffSnapshot(files=files, summary=self._summarize(files))
        return snap

    def _parse_diff(self, diff_text: str) -> list[FileDiff]:
        files: list[FileDiff] = []
        if not diff_text.strip():
            return files

        chunks = diff_text.split("diff --git ")
        for chunk in chunks[1:]:
            lines = chunk.splitlines()
            if not lines:
                continue

            parts = lines[0].split()
            fpath = parts[1].removeprefix("b/") if len(parts) >= 2 else "unknown"

            if any(l.startswith("new file") for l in lines[:5]):
                status = "added"
            elif any(l.startswith("deleted file") for l in lines[:5]):
                status = "deleted"
            elif any(l.startswith("rename from") for l in lines[:5]):
                status = "renamed"
            else:
                status = "modified"

            adds = sum(1 for l in lines if l.startswith("+") and not l.startswith("+++"))
            dels = sum(1 for l in lines if l.startswith("-") and not l.startswith("---"))

            files.append(FileDiff(
                path=fpath, status=status,
                diff_text=("diff --git " + chunk).rstrip(),
                additions=adds, deletions=dels,
            ))

        return files

    def _summarize(self, files: list[FileDiff]) -> str:
        if not files:
            return "No changes"
        n = len(files)
        a = sum(f.additions for f in files)
        d = sum(f.deletions for f in files)
        parts = [f"{n} file{'s' if n != 1 else ''}"]
        if a:
            parts.append(f"+{a}")
        if d:
            parts.append(f"-{d}")
        return " | ".join(parts)
