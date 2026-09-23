#!/usr/bin/env python3
"""hpledctl - talk to the hpledd daemon."""
import os
import socket
import sys

SOCKET = os.environ.get("HPLEDD_SOCKET", "/run/hpledd/hpledd.sock")

COMMANDS = [
    ("help on|off", "blink red until cancelled (overrides everything)"),
    ("test on|off", "solid green"),
    ("quiz a|b|c|off", "white / green / red"),
    ("group white|green|red|off", "pick a group colour"),
    ("off", "clear test/quiz/group"),
    ("status", "show what the light is doing and why"),
]
OPTIONS = [
    ("--timeout SECONDS", "auto-clear the change after this long"),
    ("-h, --help", "show this help and exit"),
]
_WIDTH = max(len(s) for s, _ in COMMANDS + OPTIONS) + 2

USAGE = "Usage: hpledctl COMMAND [ARG] [--timeout SECONDS]\n\n" \
    "Control the HP Interactive Light through the hpledd daemon.\n\n" \
    "Commands:\n" + "".join(f"  {cmd:<{_WIDTH}}{desc}\n" for cmd, desc in COMMANDS) + \
    "\nOptions:\n" + "".join(f"  {opt:<{_WIDTH}}{desc}\n" for opt, desc in OPTIONS) + \
    "\nExit status:\n" \
    "  0  on success\n" \
    "  1  if hpledd is not running\n" \
    "  2  on usage error (bad or incomplete command)\n"


def parse_args(argv):
    """Split --timeout SECONDS out of argv; returns (remaining argv, timeout or None, error)."""
    argv = list(argv)
    timeout = None
    if "--timeout" in argv:
        i = argv.index("--timeout")
        if i + 1 >= len(argv):
            return argv, None, "hpledctl: --timeout needs a value in seconds"
        timeout, argv = argv[i + 1], argv[:i] + argv[i + 2:]
    return argv, timeout, None


def main(argv):
    if argv and argv[0] in ("-h", "--help"):
        sys.stdout.write(USAGE)
        return 0
    argv, timeout, error = parse_args(argv)
    if error:
        print(error, file=sys.stderr)
        return 2
    if not argv:
        sys.stderr.write(USAGE)
        return 2
    command = " ".join(argv) + (f" {timeout}" if timeout is not None else "")

    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(10)
    try:
        s.connect(SOCKET)
    except (FileNotFoundError, ConnectionRefusedError):
        print("hpledctl: hpledd is not running (try: systemctl status hpledd)", file=sys.stderr)
        return 1
    s.sendall((command + "\n").encode())
    reply = b""
    while not reply.endswith(b"\n"):
        chunk = s.recv(4096)
        if not chunk:
            break
        reply += chunk
    text = reply.decode(errors="replace").strip()
    if not text.startswith("ok"):
        # Every error handle() can return signals bad usage, not a runtime failure, so point
        # back at the help instead of just repeating hpledd's terse wire-protocol message.
        print(f"hpledctl: {text}", file=sys.stderr)
        sys.stderr.write("\n" + USAGE)
        return 2
    if argv[0].lower() == "status":
        print(text[3:])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
