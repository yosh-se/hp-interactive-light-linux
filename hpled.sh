#!/bin/bash
# hpled.sh - control the "HP Interactive Light" LED on the HP ProBook x360 11 G3 EE from Linux.
#
# The Windows app calls BIOS WMI command 0x4E, which the firmware maps to the ACPI methods
# \_SB.WMIV.GALS (get) and \_SB.WMIV.SALS (set). SALS takes a 4-byte buffer:
#   byte 0: 1 = apply state+colour        byte 1: state  (0 off, 1 on, 2 blink)
#   byte 2: colour (0 white, 1 green, 2 red)   byte 3: blink flags (ignored here)
# and writes two EC registers (0xF3 colour, 0xF4 state).
#
# Backends:
#   write:  /proc/acpi/call (acpi_call module) calling SALS - the normal, firmware-checked path.
#           Fallback: raw EC writes via ec_sys write_support=1, which kernel lockdown
#           (Secure Boot) forbids, so this only works with Secure Boot off.
#   status: reads the EC registers through ec_sys (read-only, allowed under lockdown),
#           or calls GALS through acpi_call if ec_sys is unavailable.
#
# Usage: sudo ./hpled.sh status | off | white | green | red | blink-white | blink-green | blink-red
set -eu

EC=/sys/kernel/debug/ec/ec0/io
REG_COLOR=$((0xF3))
REG_STATE=$((0xF4))
ACALL=/proc/acpi/call

[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }
[ "$#" -eq 1 ] || { sed -n '/^# Usage/p' "$0" | sed 's/^# //' >&2; exit 1; }
cmd=$1

mountpoint -q /sys/kernel/debug || mount -t debugfs none /sys/kernel/debug
[ -e $ACALL ] || modprobe acpi_call 2>/dev/null || true

locked_down() { ! grep -q '\[none\]' /sys/kernel/security/lockdown 2>/dev/null; }

rd() { dd if="$EC" bs=1 skip="$1" count=1 2>/dev/null | od -An -tu1 | tr -d ' '; }
wr() { printf "$(printf '\\x%02x' "$2")" | dd of="$EC" bs=1 seek="$1" count=1 conv=notrunc 2>/dev/null; }

decode() {
    local c=$1 s=$2 name state
    case $((c & 7)) in 1) name=white ;; 2) name=green ;; 4) name=red ;; *) name="unknown($((c & 7)))" ;; esac
    case $((s & 3)) in 0) state=off ;; 1) state=on ;; 3) state=blink ;; *) state="unknown($((s & 3)))" ;; esac
    echo "state=$state colour=$name   (EC 0xF3=$c 0xF4=$s)"
}

ec_read_ok() {
    [ -e "$EC" ] || modprobe ec_sys 2>/dev/null || true
    [ -e "$EC" ]
}

if [ "$cmd" = status ]; then
    if ec_read_ok; then
        decode "$(rd $REG_COLOR)" "$(rd $REG_STATE)"
    elif [ -e $ACALL ]; then
        printf '\\_SB.WMIV.GALS' > $ACALL
        echo "GALS -> $(tr -d '\0' < $ACALL)  (package: {ok, {state, colour, flags}})"
    else
        echo "cannot read LED state: neither ec_sys nor acpi_call is available" >&2
        exit 1
    fi
    exit 0
fi

state=1  # 0 off, 1 on, 2 blink (SALS numbering)
case $cmd in
    off)          state=0; col=0 ;;
    white)        col=0 ;;
    green)        col=1 ;;
    red)          col=2 ;;
    blink-white)  state=2; col=0 ;;
    blink-green)  state=2; col=1 ;;
    blink-red)    state=2; col=2 ;;
    *) echo "unknown command: $cmd" >&2; exit 1 ;;
esac

if [ -e $ACALL ]; then
    printf '\\_SB.WMIV.SALS b%02x%02x%02x%02x' 1 "$state" "$col" 0 > $ACALL
    echo "SALS -> $(tr -d '\0' < $ACALL)"
    ec_read_ok && { echo -n "now: "; decode "$(rd $REG_COLOR)" "$(rd $REG_STATE)"; } || true
    exit 0
fi

# Fallback: raw EC writes (needs lockdown off).
if locked_down; then
    cat >&2 <<'EOF'
Cannot write: kernel lockdown (Secure Boot) blocks ec_sys write_support, and the
acpi_call module is not loaded. Install it (see instructions) or turn Secure Boot off.
EOF
    exit 1
fi
if [ "$(cat /sys/module/ec_sys/parameters/write_support 2>/dev/null)" != Y ]; then
    modprobe -r ec_sys 2>/dev/null || true
    modprobe ec_sys write_support=1
fi

b3=$(rd $REG_COLOR)
b4=$(rd $REG_STATE)
echo -n "before: "; decode "$b3" "$b4"
# Mirror SALS: colour is applied together with state; off/on keep the blink flags, blink sets both.
wr $REG_COLOR $(( (b3 & 0x18) | (3 << 5) | (1 << col) ))
case $state in
    0) wr $REG_STATE $(( (b4 & 0xFC) | 0 )) ;;
    1) wr $REG_STATE $(( (b4 & 0xFC) | 1 )) ;;
    2) wr $REG_STATE $(( (3 << 2) | 3 )) ;;
esac
echo -n "after:  "; decode "$(rd $REG_COLOR)" "$(rd $REG_STATE)"
