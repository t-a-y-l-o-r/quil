"""Streaming subprocess execution with real-time line capture."""

import json
import logging
import subprocess
import threading
from collections import deque
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)


def parse_stream_event(line: str) -> tuple[str | None, str | None]:
    """Parse an NDJSON stream-json event into a display line and result text.

    Returns (display_line, result_text):
    - display_line: human-readable string for the output window, or None to skip
    - result_text: the final result string from a "result" event, or None
    """
    try:
        event = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return None, None

    event_type = event.get("type")

    if event_type == "assistant":
        msg = event.get("message", {})
        parts: list[str] = []
        for block in msg.get("content", []):
            block_type = block.get("type")
            if block_type == "text":
                text = block.get("text", "").strip()
                if text:
                    parts.append(text)
            elif block_type == "tool_use":
                name = block.get("name", "?")
                inp = block.get("input", {})
                # Pick the most informative argument for display
                arg = (
                    inp.get("file_path")
                    or inp.get("command")
                    or inp.get("pattern")
                    or inp.get("query")
                    or ""
                )
                if arg:
                    parts.append(f"Tool: {name}({arg})")
                else:
                    parts.append(f"Tool: {name}")
        display = " | ".join(parts) if parts else None
        return display, None

    if event_type == "result":
        result_text = event.get("result", "")
        return None, result_text

    return None, None


class StreamingProcess:
    """Wraps Popen to stream stdout line-by-line to callbacks.

    Each line is written to an optional log file (flushed immediately),
    passed through the NDJSON parser, and forwarded to an on_line
    callback for display.  The full raw output is accumulated for
    the caller.
    """

    def __init__(
        self,
        cmd: list[str],
        label: str,
        *,
        log_file: Path | None = None,
        on_line: Callable[[str, str], None] | None = None,
        timeout: int | None = None,
        cwd: str | None = None,
    ) -> None:
        self._cmd = cmd
        self._label = label
        self._log_file = log_file
        self._on_line = on_line
        self._timeout = timeout
        self._cwd = cwd

        self._raw_lines: list[str] = []
        self._result_text: str | None = None
        self._recent: deque[str] = deque(maxlen=10)
        self._lock = threading.Lock()

    def _reader(
        self,
        pipe,  # noqa: ANN001 — typed as IO[str] at runtime
        log_fh,  # noqa: ANN001
    ) -> None:
        """Read stdout line-by-line in a daemon thread."""
        try:
            for raw_line in pipe:
                line = raw_line.rstrip("\n")
                with self._lock:
                    self._raw_lines.append(raw_line)

                if log_fh is not None:
                    log_fh.write(raw_line)
                    log_fh.flush()

                display, result_text = parse_stream_event(line)

                if result_text is not None:
                    with self._lock:
                        self._result_text = result_text

                if display and self._on_line:
                    self._on_line(self._label, display)
        except ValueError:
            # Pipe closed
            pass

    def run(self) -> str:
        """Start the subprocess, stream output, block until exit.

        Returns the result text extracted from the final NDJSON
        ``result`` event.  If no result event is found, returns
        the raw concatenated output.

        Raises subprocess.TimeoutExpired if the timeout is exceeded.
        """
        log_fh = None
        if self._log_file:
            self._log_file.parent.mkdir(parents=True, exist_ok=True)
            log_fh = open(self._log_file, "w")  # noqa: SIM115

        try:
            process = subprocess.Popen(
                self._cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=self._cwd,
            )

            reader = threading.Thread(
                target=self._reader,
                args=(process.stdout, log_fh),
                daemon=True,
            )
            reader.start()

            try:
                process.wait(timeout=self._timeout)
            except subprocess.TimeoutExpired:
                process.terminate()
                reader.join(timeout=5)
                partial = self._partial_output()
                raise subprocess.TimeoutExpired(
                    self._cmd,
                    self._timeout,
                    output=partial,
                )

            reader.join(timeout=10)

        finally:
            if log_fh is not None:
                log_fh.close()

        with self._lock:
            if self._result_text is not None:
                return self._result_text
            return self._partial_output()

    def _partial_output(self) -> str:
        """Return all raw output accumulated so far."""
        return "".join(self._raw_lines)
