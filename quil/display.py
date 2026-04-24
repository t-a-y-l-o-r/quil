"""Terminal display for the 10-line subprocess output window."""

import logging
import os
import sys
import threading
from collections import deque


WINDOW_HEIGHT = 10


class OutputWindow:
    """A 10-line scrolling window rendered at the bottom of the terminal.

    Uses ANSI escape codes to maintain a fixed-height box that
    coexists with Python logging output above it.  When the window
    is active, log lines are printed above the box (the box is
    erased, the line is emitted, then the box is redrawn).

    Non-TTY output (pipes, file redirection) disables rendering
    entirely — log files from StreamingProcess still capture
    everything.
    """

    def __init__(self) -> None:
        self._lines: deque[str] = deque(maxlen=WINDOW_HEIGHT)
        self._label: str = ""
        self._active: bool = False
        self._drawn: bool = False
        self._lock = threading.Lock()
        self._is_tty: bool = sys.stderr.isatty()

    @property
    def lock(self) -> threading.Lock:
        return self._lock

    def start(self, label: str) -> None:
        """Activate the window for a new agent phase."""
        with self._lock:
            self._label = label
            self._lines.clear()
            self._active = True
            self._drawn = False
            if self._is_tty:
                self._draw()

    def stop(self) -> None:
        """Deactivate and erase the window."""
        with self._lock:
            if self._is_tty and self._drawn:
                self._erase()
            self._active = False
            self._drawn = False

    def update_line(self, label: str, line: str) -> None:
        """Called by StreamingProcess for each displayable line.

        Thread-safe.  Appends the line to the deque and redraws.
        """
        with self._lock:
            if not self._active or not self._is_tty:
                return
            self._lines.append(line)
            if self._drawn:
                self._move_to_top()
            self._draw()

    def emit_above(self, text: str) -> None:
        """Print a line above the window (used by WindowAwareHandler).

        Erases the window, prints the text, then redraws.  Must be
        called with self._lock held.
        """
        if not self._is_tty or not self._active:
            sys.stderr.write(text)
            sys.stderr.flush()
            return

        if self._drawn:
            self._move_to_top()
            self._clear_window_lines()

        sys.stderr.write(text)
        sys.stderr.flush()
        self._draw()

    def _term_width(self) -> int:
        try:
            return os.get_terminal_size(sys.stderr.fileno()).columns
        except (ValueError, OSError):
            return 80

    def _draw(self) -> None:
        """Draw the full window box at the current cursor position."""
        w = self._term_width()
        inner = w - 4  # "│ " + content + " │"

        # Top border: ┌─── label ───...─┐
        label_part = f"─── {self._label} "
        remaining = w - 2 - len(label_part)  # minus ┌ and ┐
        top = f"┌{label_part}{'─' * max(remaining, 0)}┐"

        # Bottom border: └───...─┘
        bottom = f"└{'─' * (w - 2)}┘"

        buf: list[str] = [top]
        for i in range(WINDOW_HEIGHT):
            if i < len(self._lines):
                content = self._lines[i][:inner]
                padded = content.ljust(inner)
            else:
                padded = " " * inner
            buf.append(f"│ {padded} │")
        buf.append(bottom)

        output = "\n".join(buf) + "\n"
        sys.stderr.write(output)
        sys.stderr.flush()
        self._drawn = True

    def _move_to_top(self) -> None:
        """Move cursor to the top of the drawn window."""
        total_lines = WINDOW_HEIGHT + 2  # content + top + bottom borders
        sys.stderr.write(f"\033[{total_lines}A")

    def _clear_window_lines(self) -> None:
        """Clear all lines occupied by the window."""
        total_lines = WINDOW_HEIGHT + 2
        for _ in range(total_lines):
            sys.stderr.write("\033[2K\n")
        # Move back up
        sys.stderr.write(f"\033[{total_lines}A")
        sys.stderr.flush()

    def _erase(self) -> None:
        """Erase the window from the terminal."""
        self._move_to_top()
        self._clear_window_lines()
        sys.stderr.flush()


class WindowAwareHandler(logging.Handler):
    """Logging handler that prints above the output window.

    When the window is active, log lines are inserted above the
    box by erasing it, printing the line, and redrawing.  When
    inactive, behaves identically to StreamHandler(stderr).
    """

    def __init__(self, window: OutputWindow) -> None:
        super().__init__()
        self._window = window

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record) + "\n"
            with self._window.lock:
                if self._window._active:
                    self._window.emit_above(msg)
                else:
                    sys.stderr.write(msg)
                    sys.stderr.flush()
        except Exception:
            self.handleError(record)
