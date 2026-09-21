#!/usr/bin/env python3
"""hpledctl - talk to the hpledd daemon."""
import os
import socket
import sys

SOCKET = os.environ.get("HPLEDD_SOCKET", "/run/hpledd/hpledd.sock")

USAGE = """usage: hpledctl <command>

  help on|off              blink red until cancelled (overrides everything)
  test on|off              solid green
  quiz a|b|c|off           white / green / red
  group white|green|red|off
  mode off                 clear test/quiz/group
  page quiz|group|none     used by hpledgui: which tab is showing (expires after 10 s)
  status                   show what the light is doing and why
"""


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        sys.stdout.write(USAGE)
        return 0
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(10)
    try:
        s.connect(SOCKET)
    except (FileNotFoundError, ConnectionRefusedError):
        print("hpledctl: hpledd is not running (try: systemctl status hpledd)", file=sys.stderr)
        return 1
    s.sendall((" ".join(argv) + "\n").encode())
    reply = b""
    while not reply.endswith(b"\n"):
        chunk = s.recv(4096)
        if not chunk:
            break
        reply += chunk
    text = reply.decode(errors="replace").strip()
    print(text[3:] if text.startswith("ok ") else text)
    return 0 if text.startswith("ok") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
