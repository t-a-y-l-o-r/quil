"""Terminal display with two-box layout: quil log box + stream box.

Layout (H = terminal height, W = terminal width):

    Row 1:             ┌─── quil ───────────────────────┐
    Rows 2..S:         │ [INFO] log lines (scroll)      │  <- scroll region
    Row S+1:           └────────────────────────────────┘
    Row S+2:           ┌─── coder ──────────────────────┐
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
import threading
from collections import deque


STREAM_HEIGHT = 10
MIN_LOG_ROWS = 3


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
        self._layout_active: bool = False
        self._lock = threading.Lock()
        self._is_tty: bool = sys.stderr.isatty()
        self._w: int = 80
        self._h: int = 24
        self._scroll_bottom: int = 0

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
        out.append(self._top_border("quil"))
        out.append("\n")

        # Empty quil content rows (scroll region)
        for _ in range(self._scroll_bottom - 2 + 1):
            out.append(self._content_line(""))
            out.append("\n")

        # Quil bottom border
        out.append(self._bottom_border())
        out.append("\n")

        # Stream top border (no label yet)
        out.append(self._top_border(""))
        out.append("\n")

        # Empty stream content
        for _ in range(STREAM_HEIGHT):
            out.append(self._content_line(""))
            out.append("\n")

        # Stream bottom border
        out.append(self._bottom_border())

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
        sys.stderr.write("\033[r")  # reset scroll region
        sys.stderr.write(f"\033[{self._h};1H\n")  # cursor to bottom
        sys.stderr.flush()

    # ------------------------------------------------------------------
    # Stream box control
    # ------------------------------------------------------------------

    def start(self, label: str) -> None:
        """Activate the stream box for a new agent phase."""
        with self._lock:
            self._stream_label = label
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
        if not self._layout_active:
            sys.stderr.write(text)
            sys.stderr.flush()
            return

        formatted = self._content_line(text.rstrip("\n"))

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

        # Update stream top border with current label
        self._write_at(stream_top_row, self._top_border(self._stream_label))

        # Update content lines
        for i in range(STREAM_HEIGHT):
            row = content_start + i
            if i < len(self._stream_lines):
                self._write_at(row, self._content_line(self._stream_lines[i]))
            else:
                self._write_at(row, self._content_line(""))

        sys.stderr.write("\033[u")  # restore cursor
        sys.stderr.flush()

    def _terminal_size(self) -> tuple[int, int]:
        try:
            size = os.get_terminal_size(sys.stderr.fileno())
            return size.lines, size.columns
        except (ValueError, OSError):
            return 24, 80

    def _inner_width(self) -> int:
        return self._w - 4  # "│ " + content + " │"

    def _top_border(self, label: str) -> str:
        if label:
            label_part = f"─── {label} "
        else:
            label_part = "─"
        remaining = self._w - 2 - len(label_part)
        return f"┌{label_part}{'─' * max(remaining, 0)}┐"

    def _bottom_border(self) -> str:
        return f"└{'─' * (self._w - 2)}┘"

    def _content_line(self, text: str) -> str:
        inner = self._inner_width()
        truncated = text[:inner]
        padded = truncated.ljust(inner)
        return f"│ {padded} │"

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
