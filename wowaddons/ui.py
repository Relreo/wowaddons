"""ANSI color helpers, progress bar, and log formatter."""

from __future__ import annotations

import ctypes
import logging
import sys

from .utils import format_size


class AnsiCodes:
    """ANSI escape codes for terminal colors and styles."""

    RST   = "\033[0m"
    BOLD  = "\033[1m"
    DIM   = "\033[2m"
    RED   = "\033[91m"
    GREEN = "\033[92m"
    YELLOW= "\033[93m"
    BLUE  = "\033[94m"
    CYAN  = "\033[96m"
    WHITE = "\033[97m"
    GRAY  = "\033[90m"


_ansi_state: list[bool] = [False]


def enable_ansi() -> None:
    """Enable ANSI escape code processing, including Windows VT support."""
    if not sys.stdout.isatty():
        return
    if sys.platform == "win32":
        try:
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel32.GetStdHandle(-11)
            mode = ctypes.c_ulong()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        except (OSError, AttributeError):
            pass
    _ansi_state[0] = True


def colorize(text: str, *codes: str) -> str:
    """Wrap *text* in ANSI codes if the terminal supports them."""
    if not _ansi_state[0]:
        return text
    return "".join(codes) + text + AnsiCodes.RST


def hyperlink(text: str, url: str) -> str:
    """Make *text* a clickable OSC 8 terminal link to *url* when ANSI is on."""
    if not _ansi_state[0]:
        return text
    return f"\033]8;;{url}\033\\{text}\033]8;;\033\\"


def render_bar(done: int, total: int, width: int = 34) -> str:
    """Render a progress bar string."""
    frac = min(done / total, 1.0) if total > 0 else 0.0
    filled = int(frac * width)
    empty = width - filled
    if _ansi_state[0]:
        return AnsiCodes.CYAN + "█" * filled + AnsiCodes.GRAY + "░" * empty + AnsiCodes.RST
    return "█" * filled + "░" * empty


def print_dl_progress(label: str, done: int, total: int) -> None:
    """Print a download progress bar on the current line using \\r."""
    if not _ansi_state[0]:
        return
    pct = int(min(done / total, 1.0) * 100) if total else 0
    size_str = format_size(done)
    if total:
        size_str += f" / {format_size(total)}"
    label_s = label[:22] if len(label) > 22 else label
    line = (
        f"  {colorize('↓', AnsiCodes.CYAN)} {label_s:<22} "
        f"[{render_bar(done, total)}] {pct:3d}%  {size_str}"
    )
    print(f"\r{line:<90}", end="", flush=True)


def clear_line() -> None:
    """Overwrite the current terminal line with spaces."""
    if _ansi_state[0]:
        print(f"\r{' ' * 90}\r", end="", flush=True)


class LogFormatter(logging.Formatter):
    """Log formatter with ANSI color support."""

    def format(self, record: logging.LogRecord) -> str:
        msg = record.getMessage()
        if record.levelno == logging.DEBUG:
            return colorize(f"  [debug] {msg}", AnsiCodes.GRAY)
        if record.levelno == logging.WARNING:
            return colorize(f"  ⚠ {msg}", AnsiCodes.YELLOW)
        if record.levelno >= logging.ERROR:
            return colorize(f"  ✗ {msg}", AnsiCodes.RED)
        return msg
