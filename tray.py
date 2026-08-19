"""The connector's face on a PC with no terminal: a tray icon and a log file.

WHY THIS HAD TO EXIST BEFORE THE CONSOLE COULD BE TURNED OFF
------------------------------------------------------------
The packaged connector shipped with `console=True` on purpose. It types into live tax
returns, and while it was new, an operator being able to watch it was worth more than a
tidy desktop. But a console window is a bad permanent answer: it sits in the taskbar
looking like something that can be closed, and closing it kills the connector silently,
mid-batch, with the office assuming it is still running.

Turning the console off without replacing it would have been worse than either. A firm
would have no way to tell whether the thing was running at all, no way to see what it did,
and no way to stop it short of Task Manager. So the console is replaced by two things,
both of which have to work before `console=False` is defensible:

  1. A LOG FILE. Everything the connector and the agent print — every field entered, every
     read-back, every halt reason — goes to disk as well as to the console. This is the
     part that matters: when a preparer says "it put the wrong number in", the log is the
     record of what was actually typed, and it outlives the window.

  2. A TRAY ICON whose colour is the honest current state, with the log one click away.

THE ICON REPORTS STATE, IT DOES NOT DECIDE IT
---------------------------------------------
`state_for` is a pure function and the only place a state becomes a colour. The run loop
tells it what is true; nothing here inspects Drake, and nothing here can let work through.
A tray that could influence the run loop would be a second, unreviewed safety path.

EVERY ENTRY POINT HERE IS OPTIONAL
----------------------------------
pystray and Pillow are ordinary third-party packages and either can be missing, broken, or
blocked by a locked-down desktop. Not one call in this module is allowed to stop the
connector from entering documents — a missing tray icon is a cosmetic failure, and taking
a tax office offline over a cosmetic failure is not a trade worth making.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

# Beside the spool, so everything a person may need to look at lives in one folder.
FYNN_DIR = Path(os.environ.get("FYNN_CONNECTOR_HOME", Path.home() / ".fynn-connector"))
LOG_PATH = FYNN_DIR / "connector.log"

# Rotate at 5MB, keeping one previous file. A run that enters documents all day writes a
# few hundred KB; the cap exists so a machine left running for a year does not quietly
# fill its disk, not because anyone expects to hit it.
LOG_MAX_BYTES = 5 * 1024 * 1024


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

# (colour, short label). The colour is what a person actually reads at a glance, so the
# three states that mean "somebody has to do something" are the three that are not green.
STATES = {
    "starting":  ("#9aa0a6", "starting"),
    "no-drake":  ("#9aa0a6", "waiting for Drake to open"),
    "offline":   ("#e8a33d", "cannot reach Fynn"),
    "idle":      ("#2e9e4f", "connected, waiting for work"),
    "working":   ("#1a73e8", "entering a document"),
    "halted":    ("#d93025", "held — a document needs review in the portal"),
    "unpaired":  ("#d93025", "not paired with a firm"),
}


def state_for(paired: bool, drake_connected: bool, server_reachable: bool,
              halted: bool, working: bool) -> str:
    """The one place a set of facts becomes a state name.

    Order is the point. A halt outranks everything because it is the only state that stops
    work for the whole firm, and a person has to clear it; showing "connected, waiting for
    work" while a batch sits held is the kind of quiet lie that costs a day.
    """
    if not paired:
        return "unpaired"
    if halted:
        return "halted"
    if working:
        return "working"
    if not drake_connected:
        return "no-drake"
    if not server_reachable:
        return "offline"
    return "idle"


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

class _Tee:
    """Write to the original stream (when there is one) and to the log file.

    A tee rather than a logging framework because the connector and `agent.py` already say
    exactly the right things with `print`, and the goal is to keep every one of those
    lines, including the ones printed from inside the agent's entry path. Rewriting forty
    print calls into logger calls would have been a chance to lose one.
    """

    def __init__(self, stream, path: Path):
        self._stream = stream
        self._path = path
        self._lock = threading.Lock()
        self._fh = None
        self._written = 0
        self._open()

    def _open(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._written = self._path.stat().st_size if self._path.exists() else 0
            self._fh = open(self._path, "a", encoding="utf-8", errors="replace")
        except Exception:
            # A log we cannot write is not a reason to refuse to work.
            self._fh = None

    def _rotate_if_needed(self) -> None:
        if self._fh is None or self._written < LOG_MAX_BYTES:
            return
        try:
            self._fh.close()
            prev = self._path.with_suffix(self._path.suffix + ".1")
            if prev.exists():
                prev.unlink()
            self._path.rename(prev)
        except Exception:
            pass
        self._open()

    def write(self, text) -> int:
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.write(text)
                    self._fh.flush()   # a crash must not take the last lines with it
                    self._written += len(text)
                    self._rotate_if_needed()
                except Exception:
                    pass
        if self._stream is not None:
            try:
                return self._stream.write(text)
            except Exception:
                pass
        return len(text)

    def flush(self) -> None:
        for s in (self._fh, self._stream):
            try:
                if s is not None:
                    s.flush()
            except Exception:
                pass

    def isatty(self) -> bool:
        try:
            return bool(self._stream is not None and self._stream.isatty())
        except Exception:
            return False


def install_log_tee() -> Path:
    """Send stdout and stderr to the log file as well as wherever they already go.

    Returns the log path. Called once, at the top of `run`, BEFORE anything is printed —
    including the banner, which is what tells you which version wrote the lines below it.

    `sys.stdout` is None in a windowed (console=False) build, which is exactly the build
    that needs this most; `_Tee` handles a missing stream rather than assuming one.
    """
    sys.stdout = _Tee(sys.stdout, LOG_PATH)
    sys.stderr = _Tee(sys.stderr, LOG_PATH)
    return LOG_PATH


def attach_parent_console() -> bool:
    """Reattach to the terminal that launched us, if there was one.

    A windowed build (`console=False`) has no stdout at all. That is right for the run
    loop, which lives in the tray — but `FynnConnector.exe status` typed into a support
    call's terminal would print into the void, and a diagnostic command that silently
    prints nothing is worse than one that does not exist.

    AttachConsole(-1) borrows the parent's console when one exists and fails harmlessly
    when the exe was double-clicked. Called before anything is printed, and before the log
    tee wraps the streams, so the tee wraps a real console rather than nothing.
    """
    if os.name != "nt":
        return False
    try:
        import ctypes
        ATTACH_PARENT_PROCESS = -1
        if not ctypes.windll.kernel32.AttachConsole(ATTACH_PARENT_PROCESS):
            return False
        # The handles exist now, but Python's sys.stdout was bound to nothing at startup.
        for name, mode in (("stdout", "w"), ("stderr", "w")):
            try:
                setattr(sys, name, open("CONOUT$", mode, encoding="utf-8", errors="replace"))
            except Exception:
                pass
        return True
    except Exception:
        return False


def open_log() -> None:
    """Show the log in whatever the machine uses for text files."""
    try:
        if not LOG_PATH.exists():
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            LOG_PATH.write_text("(nothing logged yet)\n", encoding="utf-8")
        os.startfile(str(LOG_PATH))  # noqa: S606 — Windows-only, and the path is ours
    except Exception:
        try:
            subprocess.Popen(["notepad.exe", str(LOG_PATH)])
        except Exception:
            pass


# ---------------------------------------------------------------------------
# The icon
# ---------------------------------------------------------------------------

def available() -> bool:
    """True when this machine can actually show a tray icon."""
    try:
        import pystray  # noqa: F401
        from PIL import Image  # noqa: F401
        return True
    except Exception:
        return False


def _image(colour: str, size: int = 64):
    """A filled circle in the state colour, drawn rather than shipped.

    An .ico file would be another asset to keep inside the exe and another thing to get
    wrong at build time; the whole icon is nine lines of Pillow.
    """
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = size // 8
    d.ellipse((pad, pad, size - pad, size - pad), fill=colour)
    return img


class Tray:
    """A tray icon that mirrors the run loop's state, or a no-op if it cannot.

    Deliberately dumb: `set_state` is the entire input, `on_quit` is the entire output.
    """

    def __init__(self, machine_name: str = "", server: str = "", on_quit=None):
        self.machine_name = machine_name
        self.server = server
        self._on_quit = on_quit
        self._icon = None
        self._thread = None
        self._state = "starting"
        self._detail = ""

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> bool:
        """Show the icon. False if this machine cannot, which is not an error."""
        if not available():
            return False
        try:
            import pystray
            colour, label = STATES[self._state]
            self._icon = pystray.Icon(
                "fynn-connector",
                _image(colour),
                self._title(),
                menu=pystray.Menu(
                    pystray.MenuItem(lambda _: self._title(), None, enabled=False),
                    pystray.Menu.SEPARATOR,
                    pystray.MenuItem("Open log", lambda: open_log(), default=True),
                    pystray.MenuItem("Quit", self._quit),
                ),
            )
            # A daemon thread: if the run loop exits, the icon must not hold the process
            # open with nothing behind it.
            self._thread = threading.Thread(target=self._icon.run, daemon=True,
                                            name="fynn-tray")
            self._thread.start()
            return True
        except Exception:
            self._icon = None
            return False

    def stop(self) -> None:
        try:
            if self._icon is not None:
                self._icon.stop()
        except Exception:
            pass

    # -- input -------------------------------------------------------------

    def set_state(self, state: str, detail: str = "") -> None:
        """Called from the run loop. Never raises, never blocks on the UI."""
        if state not in STATES:
            return
        self._state = state
        self._detail = detail or ""
        if self._icon is None:
            return
        try:
            colour, _ = STATES[state]
            self._icon.icon = _image(colour)
            self._icon.title = self._title()
        except Exception:
            pass

    def notify(self, title: str, message: str) -> None:
        """A Windows toast — "document entered", "run stopped". Never raises, never blocks.

        Same posture as `set_state`, and the same rule: the tray REPORTS, it never
        decides. A toast is fire-and-forget output — pystray's `notify` posts the balloon
        and returns, there is nothing to wait on — and any failure in it is cosmetic. On
        a machine with no icon there is nobody to toast at, so it is a silent no-op,
        exactly like a state change on a machine that could not show the icon.
        """
        if self._icon is None:
            return
        try:
            # pystray's argument order is (message, title).
            self._icon.notify(message, title)
        except Exception:
            pass

    # -- internals ---------------------------------------------------------

    def _title(self) -> str:
        _, label = STATES[self._state]
        bits = ["Fynn connector"]
        if self.machine_name:
            bits.append(f"({self.machine_name})")
        line = " ".join(bits) + f" — {label}"
        if self._detail:
            line += f"\n{self._detail}"
        # Windows truncates a tooltip at 128 characters and simply drops the rest.
        return line[:127]

    def _quit(self) -> None:
        self.stop()
        if self._on_quit is not None:
            try:
                self._on_quit()
            except Exception:
                pass
