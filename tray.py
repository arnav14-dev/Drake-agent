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

# (colour, short label). The colour is what a person actually reads at a glance, so every
# state that means "somebody has to do something" is a state that is not green.
#
# NO TWO STATES MAY SHARE A COLOUR UNLESS THEY MEAN THE SAME THING TO THE PERSON.
# `no-drake` used to be the same grey as `starting`, so the one state a preparer can
# actually fix — open Drake — was indistinguishable from "booting up, wait a moment", and
# somebody watching the icon had no way to tell them apart. It gets its own hue (purple)
# rather than amber, because amber is already "cannot reach Fynn": painting "open Drake"
# amber would have sent the office to phone their ISP instead of clicking Drake.
#
# `error` is deliberately NOT amber either. Every unexpected internal fault used to be
# reported as `offline`, so a bug in our own JSON handling read as a network outage and the
# firm rang their IT provider about weather. It is red-family because the honest reading is
# "this needs a person", which is what the other two reds already say.
#
# `stopping` exists because the icon disappearing has to mean STOPPED. A quit waits for a
# safe point (never mid-document), so between the click and the actual stop there can be
# minutes in which the robot is still typing into Drake — see Tray._quit.
STATES = {
    "starting":  ("#9aa0a6", "starting"),
    # NOT the same grey as `starting`, which is the mistake this block's own rule exists to
    # prevent: a preparer who clicks Quit at 4:55pm and sees the boot-up grey reads "it is
    # coming back" and walks away, while the robot is still finishing a document. Darker,
    # so "on its way out" cannot be mistaken for "on its way in".
    "stopping":  ("#5f6368", "finishing what it is doing, then stopping"),
    "no-drake":  ("#a142f4", "waiting for Drake to open"),
    "offline":   ("#e8a33d", "cannot reach Fynn"),
    "error":     ("#a50e0e", "something went wrong in Fynn — see the log"),
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

    @property
    def opened(self) -> bool:
        """Did this tee actually get a file? Callers used to have no way to ask.

        install_log_tee returned the path whether or not anything opened, so the banner,
        the no-tray dialog and the tray's "Open log" all named a file that might not
        exist — sending a person to look for the record of what was typed into a return
        and finding nothing there.
        """
        return self._fh is not None

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


# The file the tee actually managed to open, and whether it managed at all. Everything
# that NAMES the log to a person — the banner, the no-tray dialog, the tray menu — reads
# these rather than LOG_PATH, so nobody is ever sent to a file that was never written.
ACTIVE_LOG_PATH = LOG_PATH
LOG_TEE_OK = False


def _log_path_candidates() -> list:
    """Where to try to write the log, best first.

    ~/.fynn-connector is the documented home and stays first. The fallbacks exist because
    the log is the record of what a robot typed into somebody's tax return, and losing it
    to a full disk, an antivirus rule, a FYNN_CONNECTOR_HOME pointed at a mapped drive
    that is not connected yet at logon, or a stray FILE named .fynn-connector is not a
    trade worth making when %LOCALAPPDATA% is sitting right there.
    """
    cands = [LOG_PATH]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        cands.append(Path(local) / "Fynn" / "connector.log")
    tmp = os.environ.get("TEMP") or os.environ.get("TMP")
    if tmp:
        cands.append(Path(tmp) / "fynn-connector.log")
    return cands


def install_log_tee() -> Path:
    """Send stdout and stderr to the log file as well as wherever they already go.

    Returns the path that was ACTUALLY opened. Called once, at the top of `run`, BEFORE
    anything is printed — including the banner, which is what tells you which version
    wrote the lines below it — and once in `main` for the commands that never reach `run`,
    because a first install that fails used to leave no trace at all.

    `sys.stdout` is None in a windowed (console=False) build, which is exactly the build
    that needs this most; `_Tee` handles a missing stream rather than assuming one.
    """
    global ACTIVE_LOG_PATH, LOG_TEE_OK
    if isinstance(sys.stdout, _Tee) and isinstance(sys.stderr, _Tee):
        # ALREADY TEED, and wrapping a tee in a tee writes every line to the log twice.
        # `setup` now tees in main() and then enters the run loop, which tees again — and
        # a log that says everything twice is a log somebody stops trusting.
        return ACTIVE_LOG_PATH
    out = None
    for cand in _log_path_candidates():
        out = _Tee(sys.stdout, cand)
        if out.opened:
            ACTIVE_LOG_PATH, LOG_TEE_OK = cand, True
            break
    if not LOG_TEE_OK:
        # Nowhere on this machine would take it. Still tee: the streams stay valid and the
        # console (if there is one) keeps working. The CALLER is told the truth by
        # LOG_TEE_OK so it can say "not being written" instead of naming a phantom file.
        ACTIVE_LOG_PATH = LOG_PATH
        out = _Tee(sys.stdout, LOG_PATH)
    sys.stdout = out
    sys.stderr = _Tee(sys.stderr, ACTIVE_LOG_PATH)
    return ACTIVE_LOG_PATH


def message_box(title: str, text: str) -> bool:
    """A message box drawn by Windows itself. The last channel left when tkinter is gone.

    `_tell`'s fallback used to be `print`, which in the windowed build it exists for is a
    guaranteed no-op — sys.stdout is None there, so print raises nothing and writes
    nowhere. A safety net made of the same material as the hole is not a net.

    MB_SETFOREGROUND|MB_TOPMOST because Drake runs maximised: a box that opens behind it
    is the same as no box at all, and this one is the fallback for the case where the
    tkinter raise never happened. Returns False if even this could not be shown.
    """
    if os.name != "nt":
        return False
    try:
        import ctypes
        MB_OK = 0x0
        MB_ICONINFORMATION = 0x40
        MB_SETFOREGROUND = 0x10000
        MB_TOPMOST = 0x40000
        ctypes.windll.user32.MessageBoxW(
            None, str(text), str(title),
            MB_OK | MB_ICONINFORMATION | MB_SETFOREGROUND | MB_TOPMOST)
        return True
    except Exception:
        return False


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
    """Show the log in whatever the machine uses for text files.

    This is the only diagnostic the product exposes, so it may not end in a silent `pass`:
    every step has a next step, and the last one is a box naming the path so it can be
    typed into Explorer by hand.

    It also NEVER creates the file. It used to write "(nothing logged yet)" into a missing
    log — so a preparer clicking this after a day of entered documents read a file
    asserting that nothing had happened, which is an affirmative lie rather than an
    absence.
    """
    path = ACTIVE_LOG_PATH
    if not path.exists():
        message_box("Fynn connector",
                    f"No log has been written on this PC yet.\n\nWhen there is one it "
                    f"will be here:\n{path}")
        return
    try:
        os.startfile(str(path))  # noqa: S606 — Windows-only, and the path is ours
        return
    except Exception:
        pass
    try:
        subprocess.Popen(["notepad.exe", str(path)])
        return
    except Exception:
        pass
    try:
        # No .log handler registered, or the shell verb is blocked by policy: show the
        # file sitting in its folder instead, which is one double-click from readable.
        subprocess.Popen(["explorer.exe", f"/select,{path}"])
        return
    except Exception:
        pass
    message_box("Fynn connector",
                f"The log could not be opened from here.\n\nIt is at:\n{path}")


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
        # One-way: set by _quit and never cleared. See _quit and set_state.
        self._stopping = False

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
        """Called from the run loop. Never raises, never blocks on the UI.

        ONCE QUIT HAS BEEN ASKED FOR, THIS STOPS LISTENING. The run loop keeps working to
        its safe point after the click — it may finish a document and then call
        set_state("idle") — and repainting the icon green, "connected, waiting for work",
        in front of somebody who just clicked Quit is a worse lie than the one this
        replaces. The latch is one-way and lives here rather than in the loop, so the tray
        still cannot influence anything: it only refuses to overwrite its own last word.
        """
        if state not in STATES or self._stopping:
            return
        self._paint(state, detail)

    def _paint(self, state: str, detail: str = "") -> None:
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
        """Ask the run loop to stop. THE ICON MUST NOT VANISH YET.

        It used to call stop() first, which removes the icon instantly — and the icon
        disappearing is the only confirmation this product gives. But a quit deliberately
        waits for a safe point (never mid-document), so the loop can still be inside a
        document, inside the 25-second long poll, or inside a report's retry chain: from
        seconds to many minutes in which the visible state said "gone" while the robot was
        still typing into a live tax return. Somebody who then starts keying into Drake by
        hand is the second keyboard this whole design exists to prevent.

        So: say "stopping", latch it so nothing repaints over it, and let the
        atexit-registered stop() remove the icon when the process actually ends. The icon
        disappearing then means STOPPED, never "asked to stop".
        """
        self._stopping = True
        self._paint("stopping", "it will not stop mid-document — this can take a minute")
        if self._on_quit is not None:
            try:
                self._on_quit()
            except Exception:
                pass
