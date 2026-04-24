"""Terminal display with two-box layout: quil log box + stream box.

Layout (H = terminal height, W = terminal width):

    Row 1:             ┌─── quil ───────────────────────┐
    Rows 2..S:         │ [INFO] log lines (scroll)      │  <- scroll region
    Row S+1:           └────────────────────────────────┘
    Row S+2:           ┌─── coder (0:42 / 10:00) ──────┐
    Rows S+3..S+12:    │ stream content                 │
    Row S+13:          └────────────────────────────────┘

The quil box uses an ANSI scroll region so log lines scroll
naturally within it.  The stream box is pinned to the terminal
bottom and updated in place.
"""

import atexit
import logging
import os
import sys
import textwrap
import threading
import time
from collections import deque


STREAM_HEIGHT = 10
MIN_LOG_ROWS = 3

# ANSI color codes
_RESET = "\033[0m"
_DIM = "\033[2m"
_QUIL_COLOR = "\033[36m"  # cyan
_STREAM_COLOR = "\033[33m"  # yellow
_PLAN_COLOR = "\033[32m"  # green


class OutputWindow:
    """Two-box terminal layout with scroll regions.

    Non-TTY output (pipes, file redirection) disables rendering
    entirely — log files from StreamingProcess still capture
    everything.
    """

    def __init__(self) -> None:
        self._stream_lines: deque[str] = deque(maxlen=STREAM_HEIGHT)
        self._stream_label: str = ""
        self._stream_active: bool = False
        self._stream_timeout: int = 0
        self._stream_start: float = 0.0
        self._layout_active: bool = False
        self._lock = threading.Lock()
        self._is_tty: bool = sys.stderr.isatty()
        self._w: int = 80
        self._h: int = 24
        self._scroll_bottom: int = 0
        self._quil_lines: deque[str] = deque(maxlen=500)

    @property
    def lock(self) -> threading.Lock:
        return self._lock

    # ------------------------------------------------------------------
    # Layout lifecycle
    # ------------------------------------------------------------------

    def activate(self) -> None:
        """Draw the two-box layout and set the scroll region."""
        if not self._is_tty or self._layout_active:
            return

        self._h, self._w = self._terminal_size()

        # stream box = STREAM_HEIGHT + 2 borders = 12 lines
        # quil bottom border = 1 line
        stream_total = STREAM_HEIGHT + 2
        self._scroll_bottom = self._h - stream_total - 1

        if self._scroll_bottom - 2 + 1 < MIN_LOG_ROWS:
            return  # terminal too small for layout

        out: list[str] = []
        out.append("\033[2J\033[H")  # clear screen, cursor home

        # Quil top border (row 1)
        out.append(self._quil_top_border())
        out.append("\n")

        # Replay buffered quil lines (or empty rows if none)
        log_rows = self._scroll_bottom - 2 + 1
        # Keep only the most recent lines that fit in the scroll region
        recent: list[str] = list(self._quil_lines)[-log_rows:]
        blank_rows = log_rows - len(recent)
        for _ in range(blank_rows):
            out.append(self._quil_content_line(""))
            out.append("\n")
        for line in recent:
            out.append(self._quil_content_line(line))
            out.append("\n")

        # Quil bottom border
        out.append(self._quil_bottom_border())
        out.append("\n")

        # Stream top border (no label yet)
        out.append(self._stream_top_border())
        out.append("\n")

        # Empty stream content
        for _ in range(STREAM_HEIGHT):
            out.append(self._stream_content_line(""))
            out.append("\n")

        # Stream bottom border
        out.append(self._stream_bottom_border())

        # Set scroll region to quil content area (row 2 through scroll_bottom)
        out.append(f"\033[2;{self._scroll_bottom}r")

        # Position cursor at top of scroll region
        out.append("\033[2;1H")

        sys.stderr.write("".join(out))
        sys.stderr.flush()

        self._layout_active = True
        atexit.register(self.deactivate)

    def deactivate(self) -> None:
        """Restore terminal to normal mode."""
        if not self._layout_active:
            return
        self._layout_active = False
        sys.stderr.write(f"\033[r{_RESET}")  # reset scroll region + colors
        sys.stderr.write(f"\033[{self._h};1H\n")  # cursor to bottom
        sys.stderr.flush()

    # ------------------------------------------------------------------
    # Plan display (full-screen takeover)
    # ------------------------------------------------------------------

    def pause_layout(self) -> None:
        """Temporarily exit the two-box layout for full-screen content."""
        if not self._layout_active:
            return
        self._layout_active = False
        sys.stderr.write("\033[r")  # reset scroll region
        sys.stderr.write("\033[2J\033[H")  # clear screen, cursor home
        sys.stderr.write(_RESET)
        sys.stderr.flush()

    def resume_layout(self) -> None:
        """Restore the two-box layout after a pause."""
        if self._layout_active:
            return
        self.activate()

    def show_plan(self, text: str) -> None:
        """Clear screen and display plan text in a bordered box.

        Long lines are wrapped to fit within the border. The layout
        is paused so the plan can use the full terminal height.
        """
        with self._lock:
            self.pause_layout()

        h, w = self._terminal_size()
        inner = w - 4  # "│ " + content + " │"

        # Wrap each source line to fit inside the border
        wrapped: list[str] = []
        for line in text.split("\n"):
            if not line.strip():
                wrapped.append("")
            elif len(line) <= inner:
                wrapped.append(line)
            else:
                wrapped.extend(textwrap.wrap(line, width=inner))

        # Build the bordered output
        label_part = "─── Plan "
        remaining = w - 2 - len(label_part)
        top = f"{_PLAN_COLOR}┌{label_part}{'─' * max(remaining, 0)}┐{_RESET}"
        bottom = f"{_PLAN_COLOR}└{'─' * (w - 2)}┘{_RESET}"

        out: list[str] = [top]
        for line in wrapped:
            truncated = line[:inner]
            padded = truncated.ljust(inner)
            out.append(f"{_PLAN_COLOR}│{_RESET} {padded} {_PLAN_COLOR}│{_RESET}")
        out.append(bottom)
        out.append("")  # blank line before the prompt

        sys.stderr.write("\n".join(out) + "\n")
        sys.stderr.flush()

    # ------------------------------------------------------------------
    # Stream box control
    # ------------------------------------------------------------------

    def start(self, label: str, *, timeout: int = 0) -> None:
        """Activate the stream box for a new agent phase."""
        with self._lock:
            self._stream_label = label
            self._stream_timeout = timeout
            self._stream_start = time.monotonic()
            self._stream_lines.clear()
            self._stream_active = True
            if self._layout_active:
                self._redraw_stream()

    def stop(self) -> None:
        """Deactivate the stream box."""
        with self._lock:
            self._stream_active = False
            self._stream_lines.clear()
            self._stream_label = ""
            self._stream_timeout = 0
            if self._layout_active:
                self._redraw_stream()

    def update_line(self, label: str, line: str) -> None:
        """Add a line to the stream box. Thread-safe."""
        with self._lock:
            if not self._stream_active or not self._layout_active:
                return
            self._stream_lines.append(line)
            self._redraw_stream()

    # ------------------------------------------------------------------
    # Quil box output
    # ------------------------------------------------------------------

    def emit_above(self, text: str) -> None:
        """Print a log line in the quil box (scroll region).

        Must be called with self._lock held.
        """
        stripped = text.rstrip("\n")
        self._quil_lines.append(stripped)

        if not self._layout_active:
            sys.stderr.write(text)
            sys.stderr.flush()
            return

        formatted = self._quil_content_line(stripped)

        sys.stderr.write("\033[s")  # save cursor
        sys.stderr.write("\033[S")  # scroll region up one line
        self._write_at(self._scroll_bottom, formatted)
        sys.stderr.write("\033[u")  # restore cursor
        sys.stderr.flush()

    def write(self, text: str) -> None:
        """Write bordered text into the quil box. Thread-safe.

        Each line of text is individually bordered and scrolled
        into the quil box, similar to emit_above but without
        log formatting.
        """
        with self._lock:
            for line in text.rstrip("\n").split("\n"):
                self.emit_above(line + "\n")

    # ------------------------------------------------------------------
    # Internal rendering
    # ------------------------------------------------------------------

    def _redraw_stream(self) -> None:
        """Redraw the stream box. Must hold self._lock."""
        if not self._layout_active:
            return

        stream_top_row = self._scroll_bottom + 2
        content_start = stream_top_row + 1

        sys.stderr.write("\033[s")  # save cursor

        # Update stream top border with label and timer
        self._write_at(stream_top_row, self._stream_top_border())

        # Update content lines
        for i in range(STREAM_HEIGHT):
            row = content_start + i
            if i < len(self._stream_lines):
                self._write_at(
                    row, self._stream_content_line(self._stream_lines[i])
                )
            else:
                self._write_at(row, self._stream_content_line(""))

        sys.stderr.write("\033[u")  # restore cursor
        sys.stderr.flush()

    def _elapsed_str(self) -> str:
        """Format the elapsed / max timer for the stream border."""
        if not self._stream_active:
            return ""
        elapsed = int(time.monotonic() - self._stream_start)
        em, es = divmod(elapsed, 60)
        parts = f"{em}:{es:02d}"
        if self._stream_timeout > 0:
            tm, ts = divmod(self._stream_timeout, 60)
            parts += f" / {tm}:{ts:02d}"
        return parts

    def _terminal_size(self) -> tuple[int, int]:
        try:
            size = os.get_terminal_size(sys.stderr.fileno())
            return size.lines, size.columns
        except (ValueError, OSError):
            return 24, 80

    def _inner_width(self) -> int:
        return self._w - 4  # "│ " + content + " │"

    # --- Quil box borders (cyan) ---

    def _quil_top_border(self) -> str:
        label_part = "─── quil "
        remaining = self._w - 2 - len(label_part)
        border = f"┌{label_part}{'─' * max(remaining, 0)}┐"
        return f"{_QUIL_COLOR}{border}{_RESET}"

    def _quil_bottom_border(self) -> str:
        border = f"└{'─' * (self._w - 2)}┘"
        return f"{_QUIL_COLOR}{border}{_RESET}"

    def _quil_content_line(self, text: str) -> str:
        inner = self._inner_width()
        truncated = text[:inner]
        padded = truncated.ljust(inner)
        return f"{_QUIL_COLOR}│{_RESET} {padded} {_QUIL_COLOR}│{_RESET}"

    # --- Stream box borders (yellow) ---

    def _stream_top_border(self) -> str:
        if self._stream_label:
            timer = self._elapsed_str()
            if timer:
                label_part = f"─── {self._stream_label} ({timer}) "
            else:
                label_part = f"─── {self._stream_label} "
        else:
            label_part = "─"
        remaining = self._w - 2 - len(label_part)
        border = f"┌{label_part}{'─' * max(remaining, 0)}┐"
        return f"{_STREAM_COLOR}{border}{_RESET}"

    def _stream_bottom_border(self) -> str:
        border = f"└{'─' * (self._w - 2)}┘"
        return f"{_STREAM_COLOR}{border}{_RESET}"

    def _stream_content_line(self, text: str) -> str:
        inner = self._inner_width()
        truncated = text[:inner]
        padded = truncated.ljust(inner)
        return f"{_STREAM_COLOR}│{_RESET} {_DIM}{padded}{_RESET} {_STREAM_COLOR}│{_RESET}"

    def _write_at(self, row: int, text: str) -> None:
        sys.stderr.write(f"\033[{row};1H\033[2K{text}")


class WindowAwareHandler(logging.Handler):
    """Logging handler that prints inside the quil box.

    Routes log lines through ``OutputWindow.emit_above`` so they
    appear bordered inside the scroll region.  Falls back to plain
    stderr when the layout is inactive.
    """

    def __init__(self, window: OutputWindow) -> None:
        super().__init__()
        self._window = window

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record) + "\n"
            with self._window.lock:
                self._window.emit_above(msg)
        except Exception:
            self.handleError(record)
