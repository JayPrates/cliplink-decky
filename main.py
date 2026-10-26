# ClipLink — Decky Loader backend.
#
# Shares text between this machine and the ClipLink app on a Windows PC over the local network:
#   - discovery: small JSON "hello" datagrams broadcast on UDP 47391;
#   - transfer: one JSON line per TCP connection to port 47390 ({"type": "clip", "text": ...} -> {"ok": true}).
# The clipboard itself is read and written straight on the X11 displays (Steam's Xwayland in Game Mode, the
# desktop in Desktop Mode) through libX11 with ctypes, plus Klipper over D-Bus when Plasma is running.

import asyncio
import collections
import ctypes
import glob
import json
import os
import random
import select
import socket
import subprocess
import threading
import time

import decky

APP = "cliplink"
PROTOCOL = 1
TCP_PORT = int(os.environ.get("CLIPLINK_TCP_PORT", "47390"))       # overridable for tests on one machine
UDP_PORT = int(os.environ.get("CLIPLINK_UDP_PORT", "47391"))
BIND = os.environ.get("CLIPLINK_BIND", "0.0.0.0")                 # 127.0.0.1 for tests
DISCOVERY_PORT = 47391           # where hellos are sent; peers answer the address they came from
HELLO_EVERY = 10                 # seconds between discovery broadcasts
PEER_TIMEOUT = 35                # a device that hasn't said hello for this long is gone
MAX_CHARS = 1000000              # longest text sent or accepted
MAX_LINE = 16 * 1024 * 1024
HISTORY = 20
DEFAULTS = {"id": "", "auto_copy": True, "auto_send": False, "notify": True, "manual": "", "pair_code": "", "target": ""}


def system_env():
    # Decky's backend is a PyInstaller bundle; give system tools their own libraries back.
    env = dict(os.environ)
    if "LD_LIBRARY_PATH_ORIG" in env:
        env["LD_LIBRARY_PATH"] = env["LD_LIBRARY_PATH_ORIG"]
    else:
        env.pop("LD_LIBRARY_PATH", None)
    return env


def run(cmd, timeout=5, env=None):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env or system_env())
    except (OSError, subprocess.SubprocessError) as e:
        decky.logger.info("%s failed: %s", cmd[0], e)
        return None


def preview(text, limit=120):
    one_line = " ".join(text.split())
    return one_line if len(one_line) <= limit else one_line[:limit - 1] + "…"


def device_name():
    host = socket.gethostname() or "Linux"
    return "Steam Deck" if host.lower() == "steamdeck" else host


# ------------------------------------------------------------------ network helpers

def interface_addresses():
    """(local IPv4 addresses, broadcast addresses) of the interfaces that are up."""
    ips, broadcasts = [], ["255.255.255.255"]
    r = run(["ip", "-o", "-4", "addr", "show", "up"]) if os.name == "posix" else None
    if r is not None and r.returncode == 0:
        for line in r.stdout.splitlines():
            parts = line.split()
            if len(parts) < 4 or parts[1] == "lo":
                continue
            if "inet" in parts:
                ips.append(parts[parts.index("inet") + 1].split("/")[0])
            if "brd" in parts:
                broadcasts.append(parts[parts.index("brd") + 1])
    if not ips:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("192.0.2.1", 9))            # no packet is sent; this just picks the outgoing interface
                ips.append(s.getsockname()[0])
        except OSError:
            pass
    return ips, list(dict.fromkeys(broadcasts))


def parse_address(text, default_port):
    text = (text or "").strip()
    if not text:
        return None
    host, _, port = text.rpartition(":") if text.count(":") == 1 else (text, "", "")
    try:
        return host, int(port) if port else default_port
    except ValueError:
        return text, default_port


# ------------------------------------------------------------------ X11 clipboard

Window = Atom = Time = ctypes.c_ulong
CURRENT_TIME = 0
PROPERTY_NOTIFY, SELECTION_CLEAR, SELECTION_REQUEST, SELECTION_NOTIFY = 28, 29, 30, 31
XA_ATOM, XA_INTEGER, XA_STRING = 4, 19, 31
PROP_MODE_REPLACE, PROP_MODE_APPEND = 0, 2
PROPERTY_CHANGE_MASK = 1 << 22
PROPERTY_NEW_VALUE = 0
MAX_READ = 8 * 1024 * 1024


class XAnyEvent(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int), ("serial", ctypes.c_ulong), ("send_event", ctypes.c_int),
                ("display", ctypes.c_void_p), ("window", Window)]


class XPropertyEvent(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int), ("serial", ctypes.c_ulong), ("send_event", ctypes.c_int),
                ("display", ctypes.c_void_p), ("window", Window), ("atom", Atom), ("time", Time), ("state", ctypes.c_int)]


class XSelectionRequestEvent(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int), ("serial", ctypes.c_ulong), ("send_event", ctypes.c_int),
                ("display", ctypes.c_void_p), ("owner", Window), ("requestor", Window),
                ("selection", Atom), ("target", Atom), ("property", Atom), ("time", Time)]


class XSelectionEvent(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int), ("serial", ctypes.c_ulong), ("send_event", ctypes.c_int),
                ("display", ctypes.c_void_p), ("requestor", Window), ("selection", Atom),
                ("target", Atom), ("property", Atom), ("time", Time)]


class XSelectionClearEvent(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int), ("serial", ctypes.c_ulong), ("send_event", ctypes.c_int),
                ("display", ctypes.c_void_p), ("window", Window), ("selection", Atom), ("time", Time)]


class XFixesSelectionNotifyEvent(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int), ("serial", ctypes.c_ulong), ("send_event", ctypes.c_int),
                ("display", ctypes.c_void_p), ("window", Window), ("subtype", ctypes.c_int),
                ("owner", Window), ("selection", Atom), ("timestamp", Time), ("selection_timestamp", Time)]


class XEvent(ctypes.Union):
    _fields_ = [("type", ctypes.c_int), ("any", XAnyEvent), ("property", XPropertyEvent),
                ("request", XSelectionRequestEvent), ("selection", XSelectionEvent),
                ("clear", XSelectionClearEvent), ("fixes", XFixesSelectionNotifyEvent), ("pad", ctypes.c_long * 24)]


ERROR_HANDLER = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
IO_ERROR_HANDLER = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p)
IO_ERROR_EXIT_HANDLER = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p)

_LIVE_DISPLAYS = {}          # Display* -> XDisplay, so the error handlers know which connection broke


def _on_x_error(dpy, event):
    return 0                 # e.g. BadWindow when a requestor closed before we answered: ignore


def _on_io_error(dpy):
    d = _LIVE_DISPLAYS.get(dpy)
    if d is not None:
        d.dead = True
    if XLIB is not None and XLIB.has_exit_handler:
        return 0             # our exit handler below runs next and returns instead of exiting
    # Old libX11 calls exit() when this handler returns, so park the display's thread instead.
    decky.logger.warning("X connection lost; parking its thread")
    while True:
        time.sleep(3600)


def _on_io_exit(dpy, data):
    decky.logger.info("X display closed")


_KEEP = [ERROR_HANDLER(_on_x_error), IO_ERROR_HANDLER(_on_io_error), IO_ERROR_EXIT_HANDLER(_on_io_exit)]


class Xlib:
    def __init__(self):
        x = self.x = ctypes.CDLL("libX11.so.6")
        x.XInitThreads()          # each display has its own thread, and displays are opened from worker threads
        p, i, u, ul, l = ctypes.c_void_p, ctypes.c_int, ctypes.c_uint, ctypes.c_ulong, ctypes.c_long
        sig = {
            "XOpenDisplay": ([ctypes.c_char_p], p), "XCloseDisplay": ([p], i), "XDefaultRootWindow": ([p], Window),
            "XCreateSimpleWindow": ([p, Window, i, i, u, u, u, ul, ul], Window), "XSelectInput": ([p, Window, l], i),
            "XInternAtom": ([p, ctypes.c_char_p, i], Atom), "XSetSelectionOwner": ([p, Atom, Window, Time], i),
            "XGetSelectionOwner": ([p, Atom], Window), "XConvertSelection": ([p, Atom, Atom, Atom, Window, Time], i),
            "XPending": ([p], i), "XNextEvent": ([p, ctypes.POINTER(XEvent)], i),
            "XSendEvent": ([p, Window, i, l, ctypes.POINTER(XEvent)], i),
            "XChangeProperty": ([p, Window, Atom, Atom, i, i, p, i], i),
            "XGetWindowProperty": ([p, Window, Atom, l, l, i, Atom, ctypes.POINTER(Atom), ctypes.POINTER(i),
                                    ctypes.POINTER(ul), ctypes.POINTER(ul), ctypes.POINTER(p)], i),
            "XDeleteProperty": ([p, Window, Atom], i), "XFree": ([p], i), "XFlush": ([p], i),
            "XConnectionNumber": ([p], i), "XMaxRequestSize": ([p], l), "XExtendedMaxRequestSize": ([p], l),
            "XSetErrorHandler": ([ERROR_HANDLER], p), "XSetIOErrorHandler": ([IO_ERROR_HANDLER], p),
        }
        for name, (args, res) in sig.items():
            f = getattr(x, name)
            f.argtypes, f.restype = args, res
        self.has_exit_handler = hasattr(x, "XSetIOErrorExitHandler")      # libX11 >= 1.7
        if self.has_exit_handler:
            x.XSetIOErrorExitHandler.argtypes, x.XSetIOErrorExitHandler.restype = [p, IO_ERROR_EXIT_HANDLER, p], None
        x.XSetErrorHandler(_KEEP[0])
        x.XSetIOErrorHandler(_KEEP[1])
        self.fixes = None
        try:
            f = ctypes.CDLL("libXfixes.so.3")
            f.XFixesQueryExtension.argtypes, f.XFixesQueryExtension.restype = [p, ctypes.POINTER(i), ctypes.POINTER(i)], i
            f.XFixesSelectSelectionInput.argtypes, f.XFixesSelectSelectionInput.restype = [p, Window, Atom, ul], None
            self.fixes = f
        except (OSError, AttributeError):
            decky.logger.info("libXfixes not available; clipboard changes are polled")


def _load_xlib():
    try:
        return Xlib()
    except (OSError, AttributeError) as e:
        decky.logger.info("libX11 not available: %s", e)
        return None


XLIB = _load_xlib()


class XDisplay(threading.Thread):
    """One X connection, owned by its own thread. Other threads queue jobs with call()."""

    TEXT_TARGETS = ("UTF8_STRING", "text/plain;charset=utf-8", "TEXT", "text/plain", "STRING")

    def __init__(self, name, dpy, on_change):
        super().__init__(name="x11 " + name, daemon=True)
        X = XLIB.x
        self.display_name, self.dpy, self.on_change = name, dpy, on_change
        self.dead = False
        self.text = None            # UTF-8 bytes we serve while we own CLIPBOARD
        self.changed = 0.0          # monotonic time someone else last took CLIPBOARD
        self.pending_change = False
        self.jobs = collections.deque()
        self.wake_r, self.wake_w = os.pipe()
        _LIVE_DISPLAYS[dpy] = self
        if XLIB.has_exit_handler:
            X.XSetIOErrorExitHandler(dpy, _KEEP[2], None)
        self.win = X.XCreateSimpleWindow(dpy, X.XDefaultRootWindow(dpy), 0, 0, 1, 1, 0, 0, 0)
        X.XSelectInput(dpy, self.win, PROPERTY_CHANGE_MASK)
        atom = lambda n: X.XInternAtom(dpy, n.encode(), 0)
        self.CLIPBOARD, self.TARGETS, self.TIMESTAMP = atom("CLIPBOARD"), atom("TARGETS"), atom("TIMESTAMP")
        self.UTF8, self.INCR = atom("UTF8_STRING"), atom("INCR")
        self.DATA, self.CLOCK = atom("CLIPLINK_DATA"), atom("CLIPLINK_CLOCK")
        self.targets = {atom(n): n for n in self.TEXT_TARGETS}
        self.fixes_event = None
        if XLIB.fixes is not None:
            base, err = ctypes.c_int(), ctypes.c_int()
            if XLIB.fixes.XFixesQueryExtension(dpy, ctypes.byref(base), ctypes.byref(err)):
                XLIB.fixes.XFixesSelectSelectionInput(dpy, self.win, self.CLIPBOARD, 1)   # SetSelectionOwnerNotifyMask
                self.fixes_event = base.value                                             # + XFixesSelectionNotify (0)
        units = X.XExtendedMaxRequestSize(dpy) or X.XMaxRequestSize(dpy)
        self.max_bytes = max(0, units * 4 - 4096)
        self.own_time = CURRENT_TIME
        X.XFlush(dpy)

    # ---------- called from other threads

    def call(self, fn, *args, timeout=3.0):
        if self.dead:
            return None
        done, box = threading.Event(), [None]

        def job():
            try:
                if not self.dead:
                    box[0] = fn(*args)
            except Exception as e:      # never let one bad request kill the display thread
                decky.logger.warning("x11 %s: %r", self.display_name, e)
            finally:
                done.set()

        self.jobs.append(job)
        try:
            os.write(self.wake_w, b"x")
        except OSError:
            return None
        return box[0] if done.wait(timeout) else None

    def set_text(self, text):
        return bool(self.call(self._own, text.encode("utf-8")))

    def get_text(self):
        return self.call(self._read)

    def stop(self):
        self.dead = True
        try:
            os.write(self.wake_w, b"x")
        except OSError:
            pass

    # ---------- display thread

    def run(self):
        X = XLIB.x
        fd = X.XConnectionNumber(self.dpy)
        while not self.dead:
            while self.jobs and not self.dead:
                self.jobs.popleft()()
            if self.pending_change and not self.dead:
                self.pending_change = False
                text = self._read()
                if text:
                    self.on_change(self, text)
            self._drain()
            if self.dead or self.jobs or self.pending_change:
                continue
            try:
                ready, _, _ = select.select([fd, self.wake_r], [], [], 5.0)
            except (OSError, ValueError):
                break
            if self.wake_r in ready:
                os.read(self.wake_r, 4096)
        self.dead = True
        while self.jobs:                 # release anyone still waiting
            self.jobs.popleft()()
        _LIVE_DISPLAYS.pop(self.dpy, None)
        for f in (self.wake_r, self.wake_w):
            try:
                os.close(f)
            except OSError:
                pass

    def _drain(self):
        X = XLIB.x
        while not self.dead and X.XPending(self.dpy) > 0:
            ev = XEvent()
            X.XNextEvent(self.dpy, ctypes.byref(ev))
            self._handle(ev)

    def _wait_for(self, match, timeout):
        """Handles events until one satisfies match(ev); returns it, or None on timeout."""
        X = XLIB.x
        fd = X.XConnectionNumber(self.dpy)
        deadline = time.monotonic() + timeout
        X.XFlush(self.dpy)
        while not self.dead:
            while not self.dead and X.XPending(self.dpy) > 0:
                ev = XEvent()
                X.XNextEvent(self.dpy, ctypes.byref(ev))
                if match(ev):
                    return ev
                self._handle(ev)
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            try:
                select.select([fd], [], [], left)
            except (OSError, ValueError):
                return None
        return None

    def _handle(self, ev):
        if ev.type == SELECTION_REQUEST:
            self._answer(ev.request)
        elif ev.type == SELECTION_CLEAR:
            if ev.clear.selection == self.CLIPBOARD:
                self.text = None
        elif self.fixes_event is not None and ev.type == self.fixes_event:
            f = ev.fixes
            if f.selection == self.CLIPBOARD and f.owner not in (0, self.win):
                self.changed = time.monotonic()
                self.pending_change = True

    def _server_time(self):
        # A zero-length append to our own window yields a PropertyNotify carrying the server's clock.
        X = XLIB.x
        X.XChangeProperty(self.dpy, self.win, self.CLOCK, XA_INTEGER, 8, PROP_MODE_APPEND, None, 0)
        ev = self._wait_for(lambda e: e.type == PROPERTY_NOTIFY and e.property.window == self.win
                            and e.property.atom == self.CLOCK, 1.0)
        return ev.property.time if ev is not None else CURRENT_TIME

    def _own(self, data):
        X = XLIB.x
        stamp = self._server_time()
        self.text = data
        X.XSetSelectionOwner(self.dpy, self.CLIPBOARD, self.win, stamp)
        X.XFlush(self.dpy)
        if X.XGetSelectionOwner(self.dpy, self.CLIPBOARD) != self.win:
            self.text = None
            return False
        self.own_time = stamp
        return True

    def _answer(self, req):
        X = XLIB.x
        prop = req.property or req.target          # obsolete clients pass None
        ok = False
        if self.text is not None and req.selection == self.CLIPBOARD:
            name = self.targets.get(req.target)
            if req.target == self.TARGETS:
                atoms = [self.TARGETS, self.TIMESTAMP] + list(self.targets)
                arr = (Atom * len(atoms))(*atoms)
                X.XChangeProperty(self.dpy, req.requestor, prop, XA_ATOM, 32, PROP_MODE_REPLACE, arr, len(atoms))
                ok = True
            elif req.target == self.TIMESTAMP:
                arr = (Time * 1)(self.own_time)
                X.XChangeProperty(self.dpy, req.requestor, prop, XA_INTEGER, 32, PROP_MODE_REPLACE, arr, 1)
                ok = True
            elif name is not None:
                if name == "STRING":
                    data, kind = self.text.decode("utf-8").encode("latin-1", "replace"), XA_STRING
                else:
                    data, kind = self.text, (self.UTF8 if name in ("UTF8_STRING", "TEXT") else req.target)
                if len(data) <= self.max_bytes:
                    buf = ctypes.create_string_buffer(data, len(data))
                    X.XChangeProperty(self.dpy, req.requestor, prop, kind, 8, PROP_MODE_REPLACE, buf, len(data))
                    ok = True
        reply = XEvent()
        s = reply.selection
        s.type, s.send_event, s.display = SELECTION_NOTIFY, 1, self.dpy
        s.requestor, s.selection, s.target, s.time = req.requestor, req.selection, req.target, req.time
        s.property = prop if ok else 0
        X.XSendEvent(self.dpy, req.requestor, 0, 0, ctypes.byref(reply))
        X.XFlush(self.dpy)

    def _get_property(self, prop, delete=True):
        X = XLIB.x
        kind, fmt, n, after, ptr = Atom(), ctypes.c_int(), ctypes.c_ulong(), ctypes.c_ulong(), ctypes.c_void_p()
        if X.XGetWindowProperty(self.dpy, self.win, prop, 0, MAX_READ // 4, 1 if delete else 0, 0,
                                ctypes.byref(kind), ctypes.byref(fmt), ctypes.byref(n), ctypes.byref(after),
                                ctypes.byref(ptr)) != 0:
            return None, 0
        try:
            if not ptr.value:
                return b"", kind.value
            size = n.value * (ctypes.sizeof(ctypes.c_long) if fmt.value == 32 else fmt.value // 8)
            return ctypes.string_at(ptr.value, size), kind.value
        finally:
            if ptr.value:
                X.XFree(ptr)

    def _read_incr(self):
        # Large transfers: the owner writes chunks; each delete of our property asks for the next one.
        chunks, total = [], 0
        while total <= MAX_READ:
            ev = self._wait_for(lambda e: e.type == PROPERTY_NOTIFY and e.property.window == self.win
                                and e.property.atom == self.DATA and e.property.state == PROPERTY_NEW_VALUE, 2.0)
            if ev is None:
                return None
            data, _ = self._get_property(self.DATA)
            if not data:
                return b"".join(chunks)
            chunks.append(data)
            total += len(data)
        return None

    def _read(self):
        X = XLIB.x
        owner = X.XGetSelectionOwner(self.dpy, self.CLIPBOARD)
        if owner == 0:
            return None
        if owner == self.win:
            return self.text.decode("utf-8", "replace") if self.text else None
        for target, encoding in ((self.UTF8, "utf-8"), (XA_STRING, "latin-1")):
            X.XDeleteProperty(self.dpy, self.win, self.DATA)
            X.XConvertSelection(self.dpy, self.CLIPBOARD, target, self.DATA, self.win, CURRENT_TIME)
            ev = self._wait_for(lambda e: e.type == SELECTION_NOTIFY and e.selection.requestor == self.win, 1.5)
            if ev is None:
                return None
            if ev.selection.property == 0:
                continue                    # owner doesn't offer this format
            data, kind = self._get_property(self.DATA)
            if kind == self.INCR:
                data = self._read_incr()
            if data is not None:
                return data.decode(encoding, "replace").rstrip("\0")
        return None


def _proc_environ_displays():
    """display (':0') -> XAUTHORITY files seen in the environment of our own processes (Steam, Plasma...)."""
    found = {}
    me = os.getuid() if hasattr(os, "getuid") else None
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        path = "/proc/" + pid
        try:
            if me not in (None, 0) and os.stat(path).st_uid != me:
                continue
            with open(path + "/environ", "rb") as f:
                env = dict(item.split(b"=", 1) for item in f.read().split(b"\0") if b"=" in item)
        except (OSError, ValueError):
            continue
        d = env.get(b"DISPLAY", b"").decode(errors="replace")
        if not d.startswith(":"):
            continue
        d = d.split(".")[0]
        auths = found.setdefault(d, [])
        a = env.get(b"XAUTHORITY", b"").decode(errors="replace")
        if a and a not in auths:
            auths.append(a)
    return found


class Clipboard:
    """The machine's clipboard: every reachable X display, plus Klipper in a Plasma desktop session."""

    def __init__(self, on_change):
        self.on_change = on_change         # called from a display thread with (text)
        self.displays = {}
        self.failed = {}                   # display -> monotonic time of the last failed open
        self.lock = threading.Lock()
        self.desktop_checked, self.desktop = -1e9, False

    def available(self):
        return XLIB is not None or self.in_desktop()

    def describe(self):
        names = sorted(d for d, x in self.displays.items() if not x.dead)
        parts = []
        if names:
            parts.append("display " + ", ".join(names))
        if self.in_desktop():
            parts.append("Klipper")
        if parts:
            return "Clipboard: " + " + ".join(parts)
        if XLIB is None:
            return "Can't use the clipboard here (libX11 is missing)."
        return "Waiting for a display (Steam not running?)"

    def refresh(self):
        if XLIB is None or not os.path.isdir("/tmp/.X11-unix"):
            return
        with self.lock:
            for name in [n for n, d in self.displays.items() if d.dead]:
                del self.displays[name]
            sockets = sorted(":" + f[1:] for f in os.listdir("/tmp/.X11-unix") if f.startswith("X") and f[1:].isdigit())
            todo = [d for d in sockets if d not in self.displays and time.monotonic() - self.failed.get(d, -1e9) > 30]
            if not todo:
                return
            env_auth = _proc_environ_displays()
            home = os.path.expanduser("~")
            extra = [os.path.join(home, ".Xauthority")] + glob.glob("/run/user/%d/xauth_*" % os.getuid())
            for name in todo:
                dpy = self._open(name, env_auth.get(name, []) + extra)
                if not dpy:
                    self.failed[name] = time.monotonic()
                    continue
                d = XDisplay(name, dpy, self._changed)
                d.start()
                self.displays[name] = d
                decky.logger.info("clipboard: opened display %s", name)

    @staticmethod
    def _open(name, auths):
        old = os.environ.get("XAUTHORITY")
        try:
            for a in [None] + [a for a in auths if os.path.isfile(a)]:
                if a is None:
                    os.environ.pop("XAUTHORITY", None)
                else:
                    os.environ["XAUTHORITY"] = a
                dpy = XLIB.x.XOpenDisplay(name.encode())
                if dpy:
                    return dpy
            return None
        finally:
            if old is None:
                os.environ.pop("XAUTHORITY", None)
            else:
                os.environ["XAUTHORITY"] = old

    def _changed(self, display, text):
        self.on_change(text)

    def live(self):
        return [d for d in list(self.displays.values()) if not d.dead]

    def set_text(self, text):
        self.refresh()
        ok = False
        for d in self.live():
            ok = d.set_text(text) or ok
        if self.in_desktop():
            ok = self._klipper_set(text) or ok
        return ok

    def get_text(self):
        self.refresh()
        if self.in_desktop():
            text = self._klipper_get()
            if text:
                return text
        for d in sorted(self.live(), key=lambda d: -d.changed):     # the display copied on most recently first
            text = d.get_text()
            if text:
                return text
        return None

    def stop(self):
        for d in self.live():
            d.stop()

    # ---------- Plasma (Desktop Mode)

    def in_desktop(self):
        if time.monotonic() - self.desktop_checked < 5 or not os.path.isdir("/proc") or not hasattr(os, "getuid"):
            return self.desktop
        self.desktop_checked = time.monotonic()
        found = False
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                if os.stat("/proc/" + pid).st_uid != os.getuid():
                    continue
                with open("/proc/%s/comm" % pid) as f:
                    if f.read().strip() == "plasmashell":
                        found = True
                        break
            except OSError:
                continue
        self.desktop = found and os.getuid() != 0
        return self.desktop

    def _klipper(self, method, *args):
        env = system_env()
        env["XDG_RUNTIME_DIR"] = "/run/user/%d" % os.getuid()
        env["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=/run/user/%d/bus" % os.getuid()
        cmd = ["busctl", "--user", "--json=short", "call", "org.kde.klipper", "/klipper", "org.kde.klipper.klipper", method]
        if args:
            cmd += ["s", args[0]]
        r = run(cmd, timeout=3, env=env)
        if r is None or r.returncode != 0:
            return None
        return r.stdout

    def _klipper_set(self, text):
        if len(text.encode("utf-8")) > 120000:        # command-line argument limit
            return False
        return self._klipper("setClipboardContents", text) is not None

    def _klipper_get(self):
        out = self._klipper("getClipboardContents")
        try:
            return json.loads(out)["data"][0] if out else None
        except (ValueError, KeyError, IndexError, TypeError):
            return None


# ------------------------------------------------------------------ Plugin

class _Discovery(asyncio.DatagramProtocol):
    def __init__(self, plugin):
        self.plugin = plugin

    def datagram_received(self, data, addr):
        self.plugin._on_datagram(data, addr)

    def error_received(self, exc):
        pass


class Plugin:
    async def _main(self):
        self.loop = asyncio.get_running_loop()
        self.settings_path = os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, "settings.json")
        self.settings = dict(DEFAULTS)
        try:
            with open(self.settings_path) as f:
                self.settings.update(json.load(f))
        except (OSError, ValueError):
            pass
        if not self.settings["id"]:
            self.settings["id"] = "%012x" % random.getrandbits(48)
            self._save_settings()
        self.name = device_name()
        self.peers = {}
        self.history = collections.deque(maxlen=HISTORY)
        self.next_item = 1
        self.known = collections.deque(maxlen=10)    # texts that just came from / went to another device
        self.status, self.status_error = "", False
        self.ips, self.broadcasts = interface_addresses()
        self.udp, self.server, self.tasks = None, None, []
        self.clipboard = Clipboard(self._clipboard_changed_threadsafe)
        await self.loop.run_in_executor(None, self.clipboard.refresh)

        try:
            self.server = await asyncio.start_server(self._handle_tcp, BIND, TCP_PORT, limit=MAX_LINE)
        except OSError as e:
            self._set_status("Can't listen on port %d: %s" % (TCP_PORT, e.strerror or e), True)
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.bind((BIND, UDP_PORT))
            self.udp, _ = await self.loop.create_datagram_endpoint(lambda: _Discovery(self), sock=sock)
        except OSError as e:
            decky.logger.warning("discovery unavailable: %s", e)
        self.tasks = [self.loop.create_task(self._discovery_loop()), self.loop.create_task(self._housekeeping())]
        decky.logger.info("ClipLink backend started as %s (%s)", self.name, ", ".join(self.ips) or "no network")

    async def _unload(self):
        for t in getattr(self, "tasks", []):
            t.cancel()
        if getattr(self, "udp", None) is not None:
            self._hello("bye")
            self.udp.close()
        if getattr(self, "server", None) is not None:
            self.server.close()
        if getattr(self, "clipboard", None) is not None:
            self.clipboard.stop()

    async def _uninstall(self):
        pass

    # ---------- state

    def _save_settings(self):
        try:
            os.makedirs(os.path.dirname(self.settings_path), exist_ok=True)
            with open(self.settings_path, "w") as f:
                json.dump(self.settings, f)
        except OSError as e:
            decky.logger.warning("couldn't save settings: %s", e)

    def _set_status(self, text, error=False):
        self.status, self.status_error = text, error
        if error:
            decky.logger.warning(text)

    def _add_history(self, text, direction, peer):
        item = {"id": self.next_item, "text": text, "dir": direction, "peer": peer, "time": time.time()}
        self.next_item += 1
        self.history.append(item)
        return item

    def _remember(self, text):
        if text in self.known:
            self.known.remove(text)
        self.known.append(text)

    def _expire(self):
        now = time.monotonic()
        for pid in [p for p, v in self.peers.items() if now - v["seen"] > PEER_TIMEOUT]:
            del self.peers[pid]

    def _learn(self, msg, ip):
        pid = str(msg.get("id") or ip)[:64]
        try:
            port = int(msg.get("port") or TCP_PORT)
        except (TypeError, ValueError):
            port = TCP_PORT
        known = pid in self.peers
        self.peers[pid] = {"id": pid, "name": str(msg.get("name") or ip)[:64], "kind": str(msg.get("kind") or "")[:16],
                           "ip": ip, "port": port, "seen": time.monotonic()}
        return self.peers[pid], known

    # ---------- discovery

    def _hello_message(self, kind, reply):
        return json.dumps({"app": APP, "v": PROTOCOL, "type": kind, "id": self.settings["id"], "name": self.name,
                           "kind": "deck", "port": TCP_PORT, "reply": reply}).encode("utf-8")

    def _hello(self, kind="hello", reply=False, to=None):
        if self.udp is None:
            return
        data = self._hello_message(kind, reply)
        if to is not None:
            targets = [to]
        else:
            targets = [(b, DISCOVERY_PORT) for b in self.broadcasts]
            manual = parse_address(self.settings["manual"], TCP_PORT)
            if manual:
                targets.append((manual[0], DISCOVERY_PORT))
        for t in targets:
            try:
                self.udp.sendto(data, t)
            except (OSError, ValueError) as e:
                decky.logger.debug("hello to %s: %s", t, e)

    def _on_datagram(self, data, addr):
        try:
            msg = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return
        if not isinstance(msg, dict) or msg.get("app") != APP or msg.get("id") == self.settings["id"]:
            return
        if msg.get("type") == "bye":
            self.peers.pop(str(msg.get("id")), None)
            return
        if msg.get("type") != "hello":
            return
        _, known = self._learn(msg, addr[0])
        if msg.get("reply") or not known:
            self._hello(reply=False, to=addr)

    async def _discovery_loop(self):
        first = True
        while True:
            self.ips, self.broadcasts = await self.loop.run_in_executor(None, interface_addresses)
            self._hello(reply=first)
            first = False
            self._expire()
            await asyncio.sleep(HELLO_EVERY)

    async def _housekeeping(self):
        # New displays appear when Steam/gamescope (re)starts or Desktop Mode begins. In a Plasma Wayland session
        # copies made in Wayland apps don't reach the X displays, so poll Klipper while auto-send is on.
        last_poll = None
        while True:
            await asyncio.sleep(2)
            await self.loop.run_in_executor(None, self.clipboard.refresh)
            if self.settings["auto_send"] and self.clipboard.in_desktop():
                text = await self.loop.run_in_executor(None, self.clipboard._klipper_get)
                if last_poll is not None and text and text != last_poll:
                    await self._clipboard_changed(text)
                last_poll = text
            else:
                last_poll = None

    # ---------- clipboard changes on this machine

    def _clipboard_changed_threadsafe(self, text):
        asyncio.run_coroutine_threadsafe(self._clipboard_changed(text), self.loop)

    async def _clipboard_changed(self, text):
        if not self.settings["auto_send"] or not text.strip() or text in self.known:
            return
        self._remember(text)
        await self._send(text, auto=True)

    # ---------- transfer

    def _targets(self):
        self._expire()
        peers = sorted(self.peers.values(), key=lambda p: p["name"].lower())
        if self.settings["target"] and any(p["id"] == self.settings["target"] for p in peers):
            peers = [p for p in peers if p["id"] == self.settings["target"]]
        manual = parse_address(self.settings["manual"], TCP_PORT)
        if manual and not any(p["ip"] == manual[0] for p in peers):
            peers.append({"id": "manual", "name": manual[0], "kind": "", "ip": manual[0], "port": manual[1]})
        return peers

    async def _send_one(self, peer, text):
        """None on success, otherwise a short reason."""
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(peer["ip"], peer["port"], limit=MAX_LINE), 4)
        except asyncio.TimeoutError:
            return "no answer"
        except OSError as e:
            return e.strerror or str(e)
        try:
            msg = {"app": APP, "v": PROTOCOL, "type": "clip", "id": self.settings["id"], "name": self.name,
                   "kind": "deck", "port": TCP_PORT, "key": self.settings["pair_code"].strip(), "text": text}
            writer.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), 8)
            resp = json.loads(line.decode("utf-8")) if line else {}
            if isinstance(resp, dict) and resp.get("ok"):
                return None
            return (resp.get("error") if isinstance(resp, dict) else None) or "no answer"
        except asyncio.TimeoutError:
            return "no answer"
        except (OSError, ValueError) as e:
            return str(e) or "connection failed"
        finally:
            writer.close()

    async def _send(self, text, auto=False):
        if not text:
            return {"ok": False, "message": "Nothing to send."}
        if len(text) > MAX_CHARS:
            return {"ok": False, "message": "That's too long to send (over a million characters)."}
        targets = self._targets()
        if not targets:
            msg = "No PC found. Open ClipLink on the PC, on the same network as this Deck."
            self._set_status(msg, True)
            return {"ok": False, "message": msg}
        results = await asyncio.gather(*(self._send_one(p, text) for p in targets))
        sent = [p["name"] for p, r in zip(targets, results) if r is None]
        failed = ["%s (%s)" % (p["name"], r) for p, r in zip(targets, results) if r is not None]
        if sent:
            self._remember(text)
            self._add_history(text, "out", ", ".join(sent))
        msg = ("Sent to " + ", ".join(sent) if sent else "") + \
              ((". " if sent else "") + "Couldn't reach " + ", ".join(failed) if failed else "")
        if auto:
            msg = "Copied text sent automatically to " + ", ".join(sent) if sent and not failed else msg
        self._set_status(msg, not sent)
        return {"ok": bool(sent), "message": msg}

    async def _handle_tcp(self, reader, writer):
        ip = (writer.get_extra_info("peername") or ("?",))[0]
        try:
            try:
                line = await asyncio.wait_for(reader.readline(), 15)
                msg = json.loads(line.decode("utf-8"))
                resp = await self._on_request(msg, ip) if isinstance(msg, dict) else {"ok": False, "error": "bad request"}
            except asyncio.TimeoutError:
                return
            except (ValueError, UnicodeDecodeError):
                resp = {"ok": False, "error": "bad request"}
            writer.write((json.dumps(resp) + "\n").encode("utf-8"))
            await writer.drain()
        except OSError:
            pass
        finally:
            writer.close()

    async def _on_request(self, msg, ip):
        if msg.get("app") != APP:
            return {"ok": False, "error": "not a ClipLink request"}
        kind = msg.get("type")
        if kind == "ping":
            self._learn(msg, ip)
            return {"ok": True, "id": self.settings["id"], "name": self.name, "kind": "deck"}
        if kind != "clip":
            return {"ok": False, "error": "unknown request"}
        code = self.settings["pair_code"].strip()
        if code and str(msg.get("key") or "").strip() != code:
            decky.logger.info("refused text from %s: wrong pairing code", ip)
            return {"ok": False, "error": "the pairing code doesn't match"}
        text = msg.get("text")
        if not isinstance(text, str) or not text:
            return {"ok": False, "error": "no text"}
        if len(text) > MAX_CHARS:
            return {"ok": False, "error": "too long"}
        peer, _ = self._learn(msg, ip)
        decky.logger.info("received %d characters from %s (%s)", len(text), peer["name"], ip)
        self._remember(text)
        copied = False
        if self.settings["auto_copy"]:
            copied = await self.loop.run_in_executor(None, self.clipboard.set_text, text)
        item = self._add_history(text, "in", peer["name"])
        self._set_status(("Copied text from " if copied else "Received text from ") + peer["name"])
        event = {"id": item["id"], "from": peer["name"], "copied": copied, "preview": preview(text, 80),
                 "notify": self.settings["notify"], "autoCopy": self.settings["auto_copy"]}
        if self.settings["auto_copy"] and not copied and len(text) <= 100000:
            event["text"] = text            # lets the frontend try to copy it instead
        try:
            await decky.emit("cliplink_received", event)
        except Exception as e:
            decky.logger.info("emit failed: %s", e)
        return {"ok": True, "copied": copied}

    # ---------- frontend API

    async def get_state(self):
        self._expire()
        peers = sorted(self.peers.values(), key=lambda p: p["name"].lower())
        return {
            "me": {"name": self.name, "ips": self.ips, "port": TCP_PORT},
            "peers": [{"id": p["id"], "name": p["name"], "kind": p["kind"], "ip": p["ip"]} for p in peers],
            "history": [{"id": h["id"], "dir": h["dir"], "peer": h["peer"], "time": h["time"],
                         "preview": preview(h["text"]), "length": len(h["text"])} for h in reversed(self.history)],
            "settings": {k: self.settings[k] for k in DEFAULTS if k != "id"},
            "clipboard": self.clipboard.describe(),
            "listening": self.server is not None,
            "status": self.status, "error": self.status_error,
        }

    async def send_clipboard(self):
        text = await self.loop.run_in_executor(None, self.clipboard.get_text)
        if not text:
            msg = "The clipboard is empty." if self.clipboard.available() else self.clipboard.describe()
            self._set_status(msg, True)
            return {"ok": False, "message": msg}
        return await self._send(text)

    async def send_text(self, text):
        return await self._send(str(text or ""))

    async def copy_item(self, item_id):
        item = next((h for h in self.history if h["id"] == item_id), None)
        if item is None:
            return {"ok": False, "text": ""}
        self._remember(item["text"])
        ok = await self.loop.run_in_executor(None, self.clipboard.set_text, item["text"])
        self._set_status("Copied to the clipboard." if ok else "Couldn't reach the clipboard.", not ok)
        return {"ok": ok, "text": "" if ok else item["text"]}

    async def clear_history(self):
        self.history.clear()
        self._set_status("")

    async def set_setting(self, key, value):
        if key not in DEFAULTS or key == "id":
            return
        if isinstance(DEFAULTS[key], bool):
            value = bool(value)
        else:
            value = str(value or "").strip()[:200]
        self.settings[key] = value
        self._save_settings()
        if key == "manual" and value:
            self._hello(reply=True)

    async def refresh(self):
        self.ips, self.broadcasts = await self.loop.run_in_executor(None, interface_addresses)
        self._hello(reply=True)
        await self.loop.run_in_executor(None, self.clipboard.refresh)
