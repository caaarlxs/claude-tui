"""Track file changes and generate diffs between snapshots.

Takes a git diff snapshot before each Claude turn. When the turn ends
(files change), computes the delta so the Diff tab shows exactly what
Claude changed — not the full repo history.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class FileDiff:
    """A single file's diff."""
    path: str
    status: str  # "modified", "added", "deleted", "renamed"
    diff_text: str
    additions: int = 0
    deletions: int = 0


@dataclass
class DiffSnapshot:
    """A collection of file diffs from one Claude turn."""
    files: list[FileDiff] = field(default_factory=list)
    summary: str = ""


class DiffTracker:
    """Tracks git diff state between Claude turns."""

    def __init__(self, cwd: str) -> None:
        self.cwd = cwd
        self._baseline: str = ""  # git diff output before the turn
        self._is_git_repo = self._check_git()
        self.history: list[DiffSnapshot] = []

    def _check_git(self) -> bool:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--git-dir"],
                cwd=self.cwd, capture_output=True, timeout=5,
            )
            return result.returncode == 0
        except Exception:
            return False

    def _run_git(self, *args: str) -> str:
        try:
            result = subprocess.run(
                ["git"] + list(args),
                cwd=self.cwd, capture_output=True, text=True, timeout=10,
            )
            return result.stdout
        except Exception:
            return ""

    def take_baseline(self) -> None:
        """Snapshot the current git diff state (call before Claude works)."""
        if not self._is_git_repo:
            return
        self._baseline = self._run_git("diff")

    def compute_changes(self) -> DiffSnapshot:
        """Compute what changed since the baseline (call after Claude finishes)."""
        if not self._is_git_repo:
            return DiffSnapshot(summary="Not a git repository")

        current = self._run_git("diff")

        # If baseline is empty and current is empty, check for new untracked files
        if not current and not self._baseline:
            untracked = self._run_git("ls-files", "--others", "--exclude-standard")
            if not untracked.strip():
                return DiffSnapshot(summary="No changes detected")
            # Show untracked files as "added"
            files = []
            for fpath in untracked.strip().splitlines():
                fpath = fpath.strip()
                if not fpath:
                    continue
                try:
                    content = Path(self.cwd, fpath).read_text(errors="replace")
                    diff_text = f"+++ b/{fpath}\n" + "\n".join(
                        f"+{line}" for line in content.splitlines()[:100]
                    )
                    additions = len(content.splitlines())
                    files.append(FileDiff(
                        path=fpath, status="added", diff_text=diff_text,
                        additions=additions,
                    ))
                except Exception:
                    pass
            snap = DiffSnapshot(files=files)
            snap.summary = self._make_summary(snap)
            self.history.append(snap)
            return snap

        # Use git diff --stat for a quick summary, then per-file diffs
        files = self._parse_diff(current)

        # If baseline was non-empty, try to show only the NEW changes
        # by diffing the two diffs. Simple approach: if baseline == current, no changes.
        if self._baseline == current:
            return DiffSnapshot(summary="No new changes since last turn")

        snap = DiffSnapshot(files=files)
        snap.summary = self._make_summary(snap)
        self.history.append(snap)
        return snap

    def get_full_diff(self) -> DiffSnapshot:
        """Get the full git diff (not just since baseline)."""
        if not self._is_git_repo:
            return DiffSnapshot(summary="Not a git repository")

        diff_output = self._run_git("diff")
        staged = self._run_git("diff", "--cached")
        combined = diff_output + staged

        if not combined.strip():
            untracked = self._run_git("ls-files", "--others", "--exclude-standard")
            if not untracked.strip():
                return DiffSnapshot(summary="No changes")
            files = []
            for fpath in untracked.strip().splitlines():
                fpath = fpath.strip()
                if fpath:
                    files.append(FileDiff(path=fpath, status="added", diff_text="(new file)"))
            snap = DiffSnapshot(files=files)
            snap.summary = self._make_summary(snap)
            return snap

        files = self._parse_diff(combined)
        snap = DiffSnapshot(files=files)
        snap.summary = self._make_summary(snap)
        return snap

    def _parse_diff(self, diff_text: str) -> list[FileDiff]:
        """Parse unified diff output into per-file FileDiff objects."""
        files: list[FileDiff] = []
        if not diff_text.strip():
            return files

        # Split by "diff --git" headers
        chunks = diff_text.split("diff --git ")
        for chunk in chunks[1:]:  # Skip empty first element
            lines = chunk.splitlines()
            if not lines:
                continue

            # Extract file path from "a/path b/path"
            header = lines[0]
            parts = header.split()
            if len(parts) >= 2:
                fpath = parts[1].removeprefix("b/")
            else:
                fpath = "unknown"

            # Determine status
            full_diff = "diff --git " + chunk
            if any(l.startswith("new file") for l in lines[:5]):
                status = "added"
            elif any(l.startswith("deleted file") for l in lines[:5]):
                status = "deleted"
            elif any(l.startswith("rename from") for l in lines[:5]):
                status = "renamed"
            else:
                status = "modified"

            # Count additions/deletions
            additions = sum(1 for l in lines if l.startswith("+") and not l.startswith("+++"))
            deletions = sum(1 for l in lines if l.startswith("-") and not l.startswith("---"))

            files.append(FileDiff(
                path=fpath,
                status=status,
                diff_text=full_diff.rstrip(),
                additions=additions,
                deletions=deletions,
            ))

        return files

    def _make_summary(self, snap: DiffSnapshot) -> str:
        if not snap.files:
            return "No changes"
        n = len(snap.files)
        total_add = sum(f.additions for f in snap.files)
        total_del = sum(f.deletions for f in snap.files)
        parts = [f"{n} file{'s' if n != 1 else ''}"]
        if total_add:
            parts.append(f"+{total_add}")
        if total_del:
            parts.append(f"-{total_del}")
        return " | ".join(parts)
