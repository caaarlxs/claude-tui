"""Terminal emulator widget using pyte + ptyprocess.

Runs a command in a real PTY and renders its output via pyte's VT100 emulator.
All keystrokes and mouse events are forwarded to the PTY, so interactive programs
(like Claude Code) work exactly as they do in a normal terminal.
"""

from __future__ import annotations

import os
import re
import time

import pyte
import ptyprocess
from rich.segment import Segment
from rich.style import Style
from textual import work
from textual.strip import Strip
from textual.widget import Widget


# Sequences pyte supports (VT100/xterm basics):
#   CSI n m (SGR), CSI n A/B/C/D/H/J/K/L/M/P/S/T/X/d/f/r/G (cursor, erase, scroll)
#   CSI ? n h/l (DEC private modes: 1049, 25, 1, 7, etc.)
#   OSC 0;title BEL (set title)
#   ESC ( 0/B (character sets), ESC 7/8 (save/restore cursor)
#
# Everything else causes garbled output. We filter aggressively.

# Single compiled regex that matches ALL CSI sequences starting with > < = characters.
# These are modern extensions (kitty keyboard, XTVERSION, etc.) that pyte misinterprets
# as standard SGR/cursor sequences, causing garbled output.
#
# Key culprits from Claude's actual output:
#   \x1b[>1u          kitty keyboard enable  → pyte ignores (ok)
#   \x1b[>4;2m        kitty keyboard flags   → pyte reads as SGR 4;2 (BAD! causes underline)
#   \x1b[>0q          XTVERSION query        → pyte shows "0q" as literal text
#   \x1b[<u           kitty keyboard disable → pyte shows "<u" as literal text
#   \x1b[c            device attributes      → pyte may handle but echoes response
#   \x1b[?2026h       sync update            → pyte doesn't know this mode

_FILTER_RE = re.compile(
    r'\x1b\[[><=][0-9;]*[a-zA-Z]'   # ALL CSI sequences with > < = prefix (kitty, XTVERSION, etc.)
    r'|\x1b\[\?[0-9;]*\$p'          # DECRQM (request mode)
    r'|\x1b\[\?2004[hl]'            # Bracketed paste mode
    r'|\x1b\[\?2026[hl]'            # Synchronized update mode
    r'|\x1b\[\?1004[hl]'            # Focus reporting
    r'|\x1b\[c'                      # Primary device attributes request
    r'|\x1b\[[0-9]* q'              # DECSCUSR cursor shape (note the space before q)
    r'|\x1b\[2[23];[0-9]*t'         # Window title stack push/pop
    r'|\x1b\](?:[1-9]|[1-9][0-9]+|0;)[^\x07\x1b]*(?:\x07|\x1b\\)'  # OSC (keep only OSC 0;title)
)

def _filter_unsupported(text: str) -> str:
    """Remove escape sequences that pyte can't handle."""
    return _FILTER_RE.sub('', text)

from rich.cells import cell_len


# Map pyte color names to Rich color names
_PYTE_COLOR_MAP = {
    "black": "black",
    "red": "red",
    "green": "green",
    "brown": "yellow",
    "blue": "blue",
    "magenta": "magenta",
    "cyan": "cyan",
    "white": "white",
    "default": "default",
    "brightblack": "bright_black",
    "brightred": "bright_red",
    "brightgreen": "bright_green",
    "brightbrown": "bright_yellow",
    "brightblue": "bright_blue",
    "brightmagenta": "bright_magenta",
    "brightcyan": "bright_cyan",
    "brightwhite": "bright_white",
}


def _resolve_color(color: str) -> str | None:
    """Convert a pyte color to a Rich color string."""
    if not color or color == "default":
        return None
    if color in _PYTE_COLOR_MAP:
        return _PYTE_COLOR_MAP[color]
    # 24-bit hex color
    if len(color) == 6:
        try:
            int(color, 16)
            return f"#{color}"
        except ValueError:
            pass
    return color


def _pyte_char_style(char: pyte.screens.Char) -> Style:
    """Convert a pyte character's attributes to a Rich Style."""
    fg = _resolve_color(char.fg)
    bg = _resolve_color(char.bg)

    if char.reverse:
        fg, bg = bg, fg

    return Style(
        color=fg,
        bgcolor=bg,
        bold=char.bold or None,
        italic=char.italics or None,
        underline=char.underscore or None,
        strike=char.strikethrough or None,
    )


class TerminalWidget(Widget):
    """A widget that embeds a PTY-based terminal emulator.

    Forwards all keyboard and mouse input to the PTY subprocess.
    """

    can_focus = True
    can_focus_children = False

    DEFAULT_CSS = """
    TerminalWidget {
        height: 1fr;
        width: 1fr;
    }
    """

    def __init__(
        self,
        command: list[str],
        cwd: str = ".",
        env: dict[str, str] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.command = command
        self.cwd = cwd
        self.env = env
        self._pty: ptyprocess.PtyProcess | None = None
        self._screen: pyte.Screen | None = None
        self._stream: pyte.Stream | None = None
        self._started = False
        self._last_refresh = 0.0
        self._byte_buffer = b""  # Buffer for incomplete UTF-8 sequences

    def on_mount(self) -> None:
        cols = max(self.size.width, 40)
        rows = max(self.size.height, 10)
        self._screen = pyte.Screen(cols, rows)
        self._screen.set_mode(pyte.modes.LNM)
        self._stream = pyte.Stream(self._screen)
        self._start_pty(cols, rows)

    def _start_pty(self, cols: int, rows: int) -> None:
        """Spawn the PTY process."""
        env = os.environ.copy()
        env["TERM"] = "xterm-256color"
        env["COLORTERM"] = "truecolor"
        env["COLUMNS"] = str(cols)
        env["LINES"] = str(rows)
        if self.env:
            env.update(self.env)

        self._pty = ptyprocess.PtyProcess.spawn(
            self.command,
            cwd=self.cwd,
            env=env,
            dimensions=(rows, cols),
        )
        self._started = True
        self._read_pty_output()

    @work(thread=True, exclusive=True, name="pty-reader")
    def _read_pty_output(self) -> None:
        """Read PTY output in a background thread."""
        while self._pty and self._pty.isalive():
            try:
                data = self._pty.read(16384)
                if not data:
                    continue

                if not isinstance(data, bytes):
                    data = data.encode("utf-8")

                # Prepend any leftover bytes from a previous incomplete UTF-8 char
                data = self._byte_buffer + data
                self._byte_buffer = b""

                # Find the last valid UTF-8 boundary.
                # A UTF-8 continuation byte starts with 0b10xxxxxx (0x80-0xBF).
                # If the last few bytes are the start of an incomplete sequence,
                # save them for the next read.
                text = ""
                try:
                    text = data.decode("utf-8")
                except UnicodeDecodeError:
                    # Find how many trailing bytes are part of an incomplete char
                    for i in range(1, min(4, len(data) + 1)):
                        try:
                            text = data[:-i].decode("utf-8")
                            self._byte_buffer = data[-i:]
                            break
                        except UnicodeDecodeError:
                            continue
                    else:
                        # Give up — decode with replacement
                        text = data.decode("utf-8", errors="replace")

                # Filter sequences pyte can't handle
                text = _filter_unsupported(text)
                if text:
                    self._stream.feed(text)

                # Throttle refreshes
                now = time.monotonic()
                if now - self._last_refresh > 0.016:
                    self._last_refresh = now
                    self.app.call_from_thread(self.refresh)
            except EOFError:
                break
            except Exception:
                break

        self._started = False
        try:
            self.app.call_from_thread(self.refresh)
        except Exception:
            pass

    def get_content_width(self, container, viewport):
        return self.size.width

    def get_content_height(self, container, viewport, width):
        if self._screen:
            return self._screen.lines
        return self.size.height

    def on_resize(self, event) -> None:
        """Handle terminal resize."""
        cols = max(event.size.width, 10)
        rows = max(event.size.height, 5)
        if self._screen:
            self._screen.resize(rows, cols)
        if self._pty and self._pty.isalive():
            try:
                self._pty.setwinsize(rows, cols)
            except Exception:
                pass

    # ─── Keyboard input ───────────────────────────────────────────

    # Keys reserved for the TUI app — NOT forwarded to the PTY
    _RESERVED_KEYS = {"ctrl+q", "ctrl+backslash", "ctrl+t", "ctrl+b", "ctrl+p"}

    def on_key(self, event) -> None:
        """Forward keystrokes to the PTY, except reserved app shortcuts."""
        if event.key in self._RESERVED_KEYS:
            # Let the App handle these bindings
            return

        if not self._pty or not self._pty.isalive():
            return

        data = self._key_to_bytes(event)
        if data:
            try:
                self._pty.write(data)
            except Exception:
                pass
            event.prevent_default()
            event.stop()

    def _key_to_bytes(self, event) -> bytes | None:
        """Convert a Textual key event to bytes for the PTY."""
        key = event.key

        key_map = {
            "enter": b"\r",
            "tab": b"\t",
            "escape": b"\x1b",
            "backspace": b"\x7f",
            "delete": b"\x1b[3~",
            "up": b"\x1b[A",
            "down": b"\x1b[B",
            "right": b"\x1b[C",
            "left": b"\x1b[D",
            "home": b"\x1b[H",
            "end": b"\x1b[F",
            "pageup": b"\x1b[5~",
            "pagedown": b"\x1b[6~",
            "insert": b"\x1b[2~",
            "f1": b"\x1bOP",
            "f2": b"\x1bOQ",
            "f3": b"\x1bOR",
            "f4": b"\x1bOS",
            "f5": b"\x1b[15~",
            "f6": b"\x1b[17~",
            "f7": b"\x1b[18~",
            "f8": b"\x1b[19~",
            "f9": b"\x1b[20~",
            "f10": b"\x1b[21~",
            "f11": b"\x1b[23~",
            "f12": b"\x1b[24~",
        }

        if key in key_map:
            return key_map[key]

        # Ctrl+key
        if key.startswith("ctrl+"):
            ch = key[5:]
            if len(ch) == 1 and ch.isalpha():
                return bytes([ord(ch.lower()) - ord('a') + 1])
            if ch == "space":
                return b"\x00"

        # Regular character
        if event.character:
            return event.character.encode("utf-8")

        return None

    # ─── Mouse input ──────────────────────────────────────────────

    def on_mouse_down(self, event) -> None:
        """Forward mouse clicks to PTY using SGR protocol."""
        if not self._pty or not self._pty.isalive():
            return
        # SGR mouse: \x1b[<button;col;rowM
        button = event.button - 1 if hasattr(event, 'button') else 0
        col = event.x + 1
        row = event.y + 1
        seq = f"\x1b[<{button};{col};{row}M"
        try:
            self._pty.write(seq.encode())
        except Exception:
            pass

    def on_mouse_up(self, event) -> None:
        """Forward mouse release to PTY."""
        if not self._pty or not self._pty.isalive():
            return
        button = event.button - 1 if hasattr(event, 'button') else 0
        col = event.x + 1
        row = event.y + 1
        seq = f"\x1b[<{button};{col};{row}m"
        try:
            self._pty.write(seq.encode())
        except Exception:
            pass

    def on_mouse_scroll_up(self, event) -> None:
        """Forward scroll up to PTY."""
        if not self._pty or not self._pty.isalive():
            return
        col = event.x + 1
        row = event.y + 1
        seq = f"\x1b[<64;{col};{row}M"
        try:
            self._pty.write(seq.encode())
        except Exception:
            pass

    def on_mouse_scroll_down(self, event) -> None:
        """Forward scroll down to PTY."""
        if not self._pty or not self._pty.isalive():
            return
        col = event.x + 1
        row = event.y + 1
        seq = f"\x1b[<65;{col};{row}M"
        try:
            self._pty.write(seq.encode())
        except Exception:
            pass

    # ─── Rendering ────────────────────────────────────────────────

    def render_line(self, y: int) -> Strip:
        """Render a single line of the terminal as a Strip."""
        width = self.size.width

        if not self._screen or y >= self._screen.lines:
            return Strip.blank(width)

        segments: list[Segment] = []
        row = self._screen.buffer[y]
        cols = self._screen.columns
        cell_x = 0
        x = 0

        while x < cols and cell_x < width:
            char = row[x]
            ch = char.data if char.data else " "

            # Skip placeholder cells for wide characters (pyte uses empty string)
            if ch == "":
                x += 1
                continue

            style = _pyte_char_style(char)

            # Cursor
            if (self._screen.cursor.y == y and
                    self._screen.cursor.x == x and
                    self.has_focus and self._started):
                style = style + Style(reverse=True)

            # Character cell width — use Rich's calculation for consistency
            char_width = cell_len(ch)
            if char_width < 1:
                char_width = 1

            segments.append(Segment(ch, style))
            cell_x += char_width
            x += 1

        # Pad to full width
        if cell_x < width:
            segments.append(Segment(" " * (width - cell_x)))

        return Strip(segments)

    # ─── Utilities ────────────────────────────────────────────────

    def write_to_pty(self, data: str) -> None:
        """Write data directly to the PTY."""
        if self._pty and self._pty.isalive():
            self._pty.write(data.encode("utf-8"))

    @property
    def is_alive(self) -> bool:
        return self._pty is not None and self._pty.isalive()

    async def cleanup(self) -> None:
        """Clean up the PTY process."""
        if self._pty and self._pty.isalive():
            try:
                self._pty.terminate(force=True)
            except Exception:
                pass
