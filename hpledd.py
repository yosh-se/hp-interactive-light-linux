#!/usr/bin/env python3
"""hpledd - status daemon for the HP Interactive Light on the HP ProBook x360 11 G3 EE.

Recreates what HP's Windows "HP Interactive Light" app did with the LED, in priority order:

  1. help requested             blinking red   (overrides everything)
  2. battery low and unplugged  blinking red
  3. test / quiz / group mode   solid green / white|green|red / white|green|red
  4. internet connected         solid white
  5. otherwise                  off

Modes are set with `hpledctl`. Needs root and the acpi_call kernel module.
"""
import argparse
import glob
import logging
import os
import select
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Optional

ACPI_CALL = "/proc/acpi/call"
DEFAULT_SOCKET = "/run/hpledd/hpledd.sock"
EX_CONFIG = 78  # systemd unit uses RestartPreventExitStatus=78
# hpledgui re-sends "page quiz|group" every couple of seconds while that tab is showing; if it
# stops (window closed or crashed) the daemon goes back to showing internet after this long.
PAGE_TTL = 10.0
COLOUR_PAGES = ("quiz", "group")

# Numbering used by the firmware method \_SB.WMIV.SALS (see README).
COLOURS = {"white": 0, "green": 1, "red": 2}
STATES = {"off": 0, "on": 1, "blink": 2}

log = logging.getLogger("hpledd")


@dataclass(frozen=True)
class Led:
    state: str
    colour: str = "white"

    def __str__(self):
        if self.state == "off":
            return "off"
        return self.colour if self.state == "on" else f"{self.state}-{self.colour}"


OFF = Led("off")

MODE_LEDS = {
    "test": Led("on", "green"),
    "quiz:a": Led("on", "white"),
    "quiz:b": Led("on", "green"),
    "quiz:c": Led("on", "red"),
    "group:white": Led("on", "white"),
    "group:green": Led("on", "green"),
    "group:red": Led("on", "red"),
}


@dataclass(frozen=True)
class Sensors:
    battery_percent: Optional[int] = None
    discharging: bool = False
    internet: bool = False


def decide(help_on, mode, sensors, low_battery, page=None):
    """Pick the LED for the current inputs; returns (Led, reason).

    `page` is the hpledgui tab currently showing, if any."""
    if help_on:
        return Led("blink", "red"), "help"
    if (sensors.discharging and sensors.battery_percent is not None
            and sensors.battery_percent <= low_battery):
        return Led("blink", "red"), "low-battery"
    if mode in MODE_LEDS:
        return MODE_LEDS[mode], mode
    if page in COLOUR_PAGES:
        # White is a real choice on these pages (quiz answer A, white group), so internet
        # white would be misread; with nothing selected the light stays off.
        return OFF, f"{page}-page"
    if sensors.internet:
        return Led("on", "white"), "internet"
    return OFF, "idle"


def read_battery(base="/sys/class/power_supply"):
    """Returns (percent or None, is_discharging) for the first battery."""
    for d in sorted(glob.glob(os.path.join(base, "BAT*"))):
        try:
            with open(os.path.join(d, "capacity")) as f:
                pct = int(f.read())
            with open(os.path.join(d, "status")) as f:
                status = f.read().strip().lower()
        except (OSError, ValueError):
            continue
        return pct, status == "discharging"
    return None, False


def has_default_route(path="/proc/net/route"):
    try:
        with open(path) as f:
            lines = f.read().splitlines()[1:]
    except OSError:
        return False
    for line in lines:
        fields = line.split()
        if len(fields) >= 8 and fields[1] == "00000000" and fields[7] == "00000000":
            return True
    return False


def read_internet():
    """True if NetworkManager reports full connectivity; falls back to a default-route check."""
    try:
        out = subprocess.run(["nmcli", "-g", "CONNECTIVITY", "general"],
                             capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        out = ""
    if out == "full":
        return True
    if out in ("none", "limited", "portal"):
        return False
    return has_default_route()


def sense_all():
    pct, discharging = read_battery()
    return Sensors(pct, discharging, read_internet())


def acpi_call(expr):
    """Evaluate an ACPI method through the acpi_call module and return its result text."""
    fd = os.open(ACPI_CALL, os.O_WRONLY)
    try:
        os.write(fd, expr.encode())
    finally:
        os.close(fd)
    fd = os.open(ACPI_CALL, os.O_RDONLY)
    try:
        out = os.read(fd, 4096)
    finally:
        os.close(fd)
    return out.rstrip(b"\0").decode(errors="replace").strip()


class AcpiLed:
    def probe(self):
        """Refuse to run on firmware without the LED method (i.e. a different model)."""
        res = acpi_call(r"\_SB.WMIV.GALS")
        if res.startswith("Error"):
            raise RuntimeError(f"firmware has no \\_SB.WMIV.GALS ({res}); unsupported model")

    def set(self, led):
        res = acpi_call(r"\_SB.WMIV.SALS b%02x%02x%02x%02x"
                        % (1, STATES[led.state], COLOURS[led.colour], 0))
        if res.startswith("Error"):
            raise OSError(res)


class DryRunLed:
    def probe(self):
        pass

    def set(self, led):
        log.info("[dry-run] LED -> %s", led)


class Daemon:
    def __init__(self, backend, low_battery=20, interval=5.0, sense=sense_all):
        self.backend = backend
        self.low_battery = low_battery
        self.interval = interval
        self.sense = sense
        self.help = False
        self.help_live = False       # help was turned on while a client was already polling us
        self.help_deadline = None    # or: an explicit "expire in N seconds" from the caller
        self.mode = "none"
        self.mode_live = False       # same idea for test/quiz/group
        self.mode_deadline = None
        self.page = None
        self.gui_until = 0.0  # cleared (help/mode/page all drop) once nobody has polled since this
        self.sensors = Sensors()
        self.applied = None
        self._last_error = None
        self._last_wall = time.time()

    def desired(self):
        now = time.monotonic()
        if now >= self.gui_until:
            # Nobody has talked to us in a while: drop whatever a live session was holding up.
            # State set with no live session behind it (e.g. a bare hpledctl command) is
            # untouched here and persists until explicitly cleared.
            if self.help_live:
                self.help, self.help_live = False, False
            if self.mode_live:
                self.mode, self.mode_live = "none", False
            self.page = None
        if self.help_deadline is not None and now >= self.help_deadline:
            self.help, self.help_deadline = False, None
        if self.mode_deadline is not None and now >= self.mode_deadline:
            self.mode, self.mode_deadline = "none", None
        return decide(self.help, self.mode, self.sensors, self.low_battery, self.page)

    def _start(self, now, gui_live, extra, value_attr, value):
        """Set help or mode to `value`. `extra` is whatever trailed the command: a single
        number of seconds sets a fixed deadline that runs regardless of who's still
        connected; with nothing extra, fall back to the live-GUI heuristic (see `handle`).
        Returns an error string, or None on success."""
        deadline = None
        if extra:
            if len(extra) > 1:
                return "error: too many arguments"
            try:
                ttl = float(extra[0])
            except ValueError:
                return f"error: invalid timeout '{extra[0]}'"
            if ttl <= 0:
                return f"error: invalid timeout '{extra[0]}'"
            deadline = now + ttl
        setattr(self, value_attr, value)
        setattr(self, f"{value_attr}_live", gui_live and deadline is None)
        setattr(self, f"{value_attr}_deadline", deadline)
        return None

    def apply(self):
        led, reason = self.desired()
        if led == self.applied:
            return
        try:
            self.backend.set(led)
        except OSError as e:
            if str(e) != self._last_error:
                log.error("cannot set LED to %s: %s", led, e)
            self._last_error = str(e)
            self.applied = None  # retry on the next tick
            return
        self._last_error = None
        self.applied = led
        log.info("LED %s (%s)", led, reason)

    def status(self):
        led, reason = self.desired()
        s = self.sensors
        if s.battery_percent is None:
            battery = "n/a"
        else:
            battery = f"{s.battery_percent}%" + (" (discharging)" if s.discharging else "")
        return (f"led={led} reason={reason} help={'on' if self.help else 'off'} "
                f"mode={self.mode} battery={battery} internet={'yes' if s.internet else 'no'}")

    def _clear_mode(self, kind):
        if self.mode == kind or self.mode.startswith(kind + ":"):
            self.mode, self.mode_live, self.mode_deadline = "none", False, None

    def handle(self, line):
        """Execute one control command; returns the reply line."""
        cmd = line.strip().lower().split()
        now = time.monotonic()
        # hpledgui polls every couple of seconds no matter which tab is showing, so if we
        # already heard from someone recently *before* this command, a live GUI session is
        # driving it and whatever it sets (help/test/quiz/group) should expire with that
        # session (see `desired`), unless an explicit timeout below overrides it. A lone
        # hpledctl invocation has no such history, so its state persists until explicitly
        # cleared. Any contact renews the deadline, keeping whatever is already live going.
        gui_live = now < self.gui_until
        self.gui_until = now + PAGE_TTL
        if cmd == ["status"]:
            return "ok " + self.status()
        verb = cmd[0] if cmd else ""
        error = None
        if verb == "help" and len(cmd) >= 2 and cmd[1] == "on":
            error = self._start(now, gui_live, cmd[2:], "help", True)
        elif verb == "help" and cmd[1:] == ["off"]:
            self.help, self.help_live, self.help_deadline = False, False, None
        elif verb == "test" and len(cmd) >= 2 and cmd[1] == "on":
            error = self._start(now, gui_live, cmd[2:], "mode", "test")
        elif verb == "test" and cmd[1:] == ["off"]:
            self._clear_mode("test")
        elif verb == "quiz" and len(cmd) >= 2 and cmd[1] in ("a", "b", "c"):
            error = self._start(now, gui_live, cmd[2:], "mode", f"quiz:{cmd[1]}")
        elif verb == "quiz" and cmd[1:] == ["off"]:
            self._clear_mode("quiz")
        elif verb == "page" and len(cmd) == 2 and cmd[1] in COLOUR_PAGES:
            self.page = cmd[1]
        elif verb == "page" and cmd[1:] == ["none"]:
            self.page = None
        elif verb == "group" and len(cmd) >= 2 and cmd[1] in ("white", "green", "red"):
            error = self._start(now, gui_live, cmd[2:], "mode", f"group:{cmd[1]}")
        elif verb == "group" and cmd[1:] == ["off"]:
            self._clear_mode("group")
        elif cmd == ["off"]:
            self.mode, self.mode_live, self.mode_deadline = "none", False, None
        elif verb in ("help", "test", "quiz", "group", "page", "off", "status"):
            return f"error: bad argument to '{verb}'"
        else:
            return f"error: unknown command '{verb}'" if verb else "error: no command given"
        if error:
            return error
        self.apply()
        return "ok " + self.status()

    def serve_one(self, srv):
        conn, _ = srv.accept()
        with conn:
            conn.settimeout(2)
            try:
                data = b""
                while b"\n" not in data and len(data) < 256:
                    chunk = conn.recv(256)
                    if not chunk:
                        break
                    data += chunk
                reply = self.handle(data.decode(errors="replace").split("\n")[0])
                conn.sendall(reply.encode() + b"\n")
            except OSError:
                pass  # slow or vanished client; nothing to do

    def run(self, sock_path):
        srv = bind_socket(sock_path)
        try:
            next_sense = 0.0
            while True:
                wall = time.time()
                if wall - self._last_wall > self.interval * 3 + 5:
                    log.info("wall clock jumped (resume from suspend?); re-asserting LED")
                    self.applied = None
                    next_sense = 0.0
                self._last_wall = wall
                if time.monotonic() >= next_sense:
                    self.sensors = self.sense()
                    next_sense = time.monotonic() + self.interval
                    self.apply()
                timeout = max(0.0, next_sense - time.monotonic())
                ready, _, _ = select.select([srv], [], [], timeout)
                if ready:
                    self.serve_one(srv)
        finally:
            srv.close()
            try:
                os.unlink(sock_path)
            except OSError:
                pass
            try:
                self.backend.set(OFF)
            except OSError:
                pass


def bind_socket(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    os.chmod(path, 0o666)  # any local user may use hpledctl; it only drives a status LED
    srv.listen(4)
    return srv


def main(argv=None):
    p = argparse.ArgumentParser(description="HP Interactive Light status daemon")
    p.add_argument("--low-battery", type=int, default=20, metavar="PERCENT",
                   help="blink red at or below this charge while unplugged (default 20)")
    p.add_argument("--interval", type=float, default=5.0, metavar="SECONDS",
                   help="how often battery/network are checked (default 5)")
    p.add_argument("--socket", default=DEFAULT_SOCKET, help=f"control socket (default {DEFAULT_SOCKET})")
    p.add_argument("--dry-run", action="store_true", help="log LED changes instead of writing them")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    if not 1 <= args.low_battery <= 99 or args.interval < 1:
        p.error("--low-battery must be 1-99 and --interval at least 1")

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s")

    backend = DryRunLed() if args.dry_run else AcpiLed()
    try:
        backend.probe()
    except FileNotFoundError:
        log.error("%s missing: load the acpi_call module (sudo modprobe acpi_call)", ACPI_CALL)
        return EX_CONFIG
    except PermissionError:
        log.error("cannot open %s: run as root", ACPI_CALL)
        return EX_CONFIG
    except RuntimeError as e:
        log.error("%s", e)
        return EX_CONFIG

    def stop(signum, frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log.info("started (low battery <= %d%%, checking every %gs)", args.low_battery, args.interval)
    Daemon(backend, args.low_battery, args.interval).run(args.socket)
    return 0


if __name__ == "__main__":
    sys.exit(main())
