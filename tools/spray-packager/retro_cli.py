"""Retro terminal front-end for the Spray Packager CLI.

Green-phosphor, mainframe-flavoured output: live progress bars with ETAs,
job banner, and documented return codes. Degrades to plain line output when
stdout is not a terminal (logs, CI) or NO_COLOR is set.

Return codes:
    0   OK
    2   INPUT FILE NOT FOUND
    3   INVALID INPUT
    4   PROCESSING FAILURE
    5   PACKAGING FAILURE
    130 CANCELLED BY USER
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

from spray_packager import (DEFAULT_QUALITY, Reporter, __version__,
                            create_package, format_duration, format_size)

RC_OK = 0
RC_NOT_FOUND = 2
RC_INVALID = 3
RC_PROCESSING = 4
RC_PACKAGING = 5
RC_CANCELLED = 130

RC_TITLES = {
    RC_NOT_FOUND: "INPUT FILE NOT FOUND",
    RC_INVALID: "INVALID INPUT",
    RC_PROCESSING: "PROCESSING FAILURE",
    RC_PACKAGING: "PACKAGING FAILURE",
    RC_CANCELLED: "CANCELLED BY USER",
}

RC_HINTS = {
    RC_NOT_FOUND: "Check the paths are typed correctly and the drive is connected.",
    RC_INVALID: "Both inputs must be GeoTIFF files and quality must be 10-95.",
    RC_PROCESSING: "The orthomosaic may not be a valid GeoTIFF, or it may be corrupted.",
    RC_PACKAGING: "Check there is enough free disk space at the destination.",
}

BAR_WIDTH = 26
SPINNER = "|/-\\"
WIDTH = 66


class Term:
    """ANSI capability shim. Everything renders plain when not a TTY."""

    GREEN, BRIGHT, DIM, AMBER, RED = "32", "92;1", "2", "93", "91;1"

    def __init__(self) -> None:
        self.tty = sys.stdout.isatty()
        self.color = self.tty and not os.environ.get("NO_COLOR")
        if self.color and sys.platform == "win32":
            self._enable_vt()

    def _enable_vt(self) -> None:
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-11)
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        except Exception:
            self.color = False

    def paint(self, text: str, code: str) -> str:
        return f"\x1b[{code}m{text}\x1b[0m" if self.color else text


class CliReporter(Reporter):
    """Renders pipeline progress as a live single-line bar per phase (TTY)
    or milestone lines (pipes/CI). A ticker thread keeps the spinner and
    elapsed clock moving between ticks."""

    def __init__(self, term: Term) -> None:
        self.term = term
        self.lock = threading.Lock()
        self.index = 0
        self.count = 0
        self.title = ""
        self.started = 0.0
        self.done = 0.0
        self.total = 0.0
        self.unit = "B"
        self.spin = 0
        self.milestone = 0
        self._stop = threading.Event()
        self._ticker = None
        if term.tty:
            self._ticker = threading.Thread(target=self._tick_loop, daemon=True)
            self._ticker.start()

    # -- Reporter interface -------------------------------------------------

    def phase(self, index, count, title, detail=""):
        with self.lock:
            self._finish_line()
            self.index, self.count, self.title = index, count, title
            self.started = time.monotonic()
            self.done = self.total = 0.0
            self.milestone = 0
            header = f" [{index}/{count}] {title:<22}"
            if self.term.tty:
                print(self.term.paint(header, Term.BRIGHT)
                      + self.term.paint(f" {detail}", Term.DIM))
            else:
                print(f"{header} {detail}")
                sys.stdout.flush()

    def tick(self, done, total, unit="B"):
        with self.lock:
            self.done, self.total, self.unit = done, total, unit
            if self.term.tty:
                self._render()
            else:
                frac = done / total if total else 1.0
                while self.milestone < 4 and frac >= (self.milestone + 1) * 0.25:
                    self.milestone += 1
                    print(f"       {self.milestone * 25:3d}%"
                          f"  {self._amount(done, total, unit)}")
                    sys.stdout.flush()

    def log(self, message):
        with self.lock:
            if self.term.tty:
                self._clear_line()
            print(self.term.paint(f"       {message}", Term.DIM))
            if self.term.tty:
                self._render()

    # -- rendering ----------------------------------------------------------

    def close(self) -> None:
        self._stop.set()
        with self.lock:
            self._finish_line()

    def _tick_loop(self) -> None:
        while not self._stop.wait(0.25):
            with self.lock:
                if self.index:
                    self.spin = (self.spin + 1) % len(SPINNER)
                    self._render()

    def _amount(self, done, total, unit) -> str:
        if not total:
            return "starting"
        if unit == "level":
            return f"level {int(done)}/{int(total)}"
        return f"{format_size(done)} / {format_size(total)}"

    def _render(self) -> None:
        if not self.index:
            return
        frac = min(1.0, self.done / self.total) if self.total else 0.0
        filled = int(BAR_WIDTH * frac)
        bar = "█" * filled + "░" * (BAR_WIDTH - filled)
        elapsed = time.monotonic() - self.started
        eta = (format_duration(elapsed * (1 - frac) / frac)
               if 0 < frac < 1 and elapsed > 1 else "--:--")
        spinner = SPINNER[self.spin] if frac < 1 else "*"
        line = (f"   {spinner} {self.term.paint(bar, Term.GREEN)} "
                f"{frac:4.0%}  {self._amount(self.done, self.total, self.unit)}"
                f"  ETA {eta}  ")
        sys.stdout.write("\r" + line)
        sys.stdout.flush()

    def _clear_line(self) -> None:
        sys.stdout.write("\r" + " " * (WIDTH + 24) + "\r")

    def _finish_line(self) -> None:
        if not self.term.tty or not self.index:
            return
        self._clear_line()
        elapsed = format_duration(time.monotonic() - self.started)
        bar = "█" * BAR_WIDTH
        print(f"   * {self.term.paint(bar, Term.GREEN)} 100%"
              f"  {self.term.paint(f'done in {elapsed}', Term.DIM)}" + " " * 12)
        self.index = 0


def _banner(term: Term) -> None:
    title = "S T E V T E C H   S P R A Y   P A C K A G E R"
    subtitle = f"field package preparation system  v{__version__}"
    print(term.paint("╔" + "═" * (WIDTH - 2) + "╗", Term.GREEN))
    for text in (title, subtitle):
        print(term.paint("║" + f"  {text}".ljust(WIDTH - 2) + "║", Term.GREEN))
    print(term.paint("╚" + "═" * (WIDTH - 2) + "╝", Term.GREEN))


def run(args) -> int:
    try:
        # Never crash on box-drawing chars when stdout is a legacy-codepage
        # pipe (Windows CI, redirected logs) -- degrade to '?' instead.
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    term = Term()
    _banner(term)
    print(term.paint(f"  JOB START  {time.strftime('%Y-%m-%d %H:%M:%S')}",
                     Term.DIM))

    if not 10 <= args.quality <= 95:
        return _fail(term, None, RC_INVALID,
                     f"quality {args.quality} out of range 10-95")

    for label, raw in (("Result", args.result), ("Segment", args.segment)):
        path = Path(raw)
        if not path.is_file():
            return _fail(term, None, RC_NOT_FOUND, f"{label}: {path}")
        print(f"  INPUT   {path.name:<28} {format_size(path.stat().st_size)}")
    print(f"  OUTPUT  {Path(args.zip_path).name}")
    print()

    reporter = CliReporter(term)
    started = time.monotonic()
    try:
        out = create_package(args.result, args.segment, args.zip_path,
                             args.quality, reporter=reporter)
    except KeyboardInterrupt:
        return _fail(term, reporter, RC_CANCELLED, "interrupted")
    except FileNotFoundError as exc:
        return _fail(term, reporter, RC_NOT_FOUND, str(exc))
    except ValueError as exc:
        return _fail(term, reporter, RC_INVALID, str(exc))
    except Exception as exc:
        # OSError during the zip phase is a destination problem; anything
        # else is the raster processing itself.
        code = RC_PACKAGING if (isinstance(exc, OSError)
                                and reporter.index == 3) else RC_PROCESSING
        if os.environ.get("SPK_DEBUG"):
            import traceback
            traceback.print_exc()
        return _fail(term, reporter, code, f"{type(exc).__name__}: {exc}")

    reporter.close()
    elapsed = format_duration(time.monotonic() - started)
    print()
    print(term.paint(f"  ═══ JOB COMPLETE ═══  {out.name}  "
                     f"{format_size(out.stat().st_size)}  "
                     f"elapsed {elapsed}  RC=0", Term.BRIGHT))
    return RC_OK


def _fail(term: Term, reporter: CliReporter | None, code: int,
          message: str) -> int:
    if reporter is not None:
        reporter.close()
    print()
    print(term.paint(f"  ═══ JOB FAILED ═══  RC={code}  {RC_TITLES[code]}",
                     Term.RED))
    print(term.paint(f"  SPK-{code}: {message}", Term.RED))
    hint = RC_HINTS.get(code)
    if hint:
        print(term.paint(f"  {hint}", Term.AMBER))
    return code
