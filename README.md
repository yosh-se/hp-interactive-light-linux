# hp-interactive-light-linux

Control the **HP Interactive Light** LED found on some HP educational laptop directly from Linux. 

Since HP only ships the light's driver and app for Windows I wanted to see how hard it'd be to get working in Linux.

Tested on:
HP ProBook x360 11 G3 EE, BIOS Q95 Ver. 01.05.00

It will only work on other models if their firmware has the same WMI
interface (see [Other models](#other-models)).

## Requirements

- An HP educational laptop with the Interactive Light. Tested on the **HP ProBook x360 11 G3 EE**;
  other models only work if their firmware has the same interface (see [Other models](#other-models)).
- Linux with systemd, and administrator (`sudo`) rights. The commands below are for
  Ubuntu and other Debian-based systems.
- Python 3 (already installed on nearly every distribution). The optional window also needs
  PyGObject and GTK 3 (`python3-gi`, `gir1.2-gtk-3.0`), which standard Ubuntu desktops include.
- The [`acpi_call`](https://github.com/nix-community/acpi_call) kernel module, installed in step 1.

## Installation

**1. Install the `acpi_call` module** (lets Linux talk to the light through the laptop's firmware):

```
sudo apt install dkms linux-headers-$(uname -r) acpi-call-dkms
sudo modprobe acpi_call
```

If the installer asks you to set a password, or `modprobe` says *"Key was rejected by service"*,
your computer has Secure Boot enabled and needs to be told to trust the module. Run
`sudo mokutil --import /var/lib/shim-signed/mok/MOK.der`, choose a password, reboot, and
in the blue screen that appears choose **Enroll MOK**, then enter that password. Afterwards run
`sudo modprobe acpi_call` again.

**2. Check that your laptop is supported** (this only reads, nothing changes):

```
echo '\_SB.WMIV.GALS' | sudo tee /proc/acpi/call > /dev/null; sudo cat /proc/acpi/call
```

If the output starts with `Error`, your model doesn't have the light interface; stop here.

**3. Install.** Download or clone this repository, open a terminal in its folder and run:

```
sudo ./install.sh install
```

This installs the programs to `/usr/local/bin`, adds "HP Interactive Light" to your app menu,
and starts a background service that controls the light. It should light up white.

**4. Try it:**

```
hpledctl help on     # the light blinks red
hpledctl help off
```

With nothing selected, the light is solid white while you have internet and blinks red when the battery is low.

**Prefer buttons?** Open **HP Interactive Light** from your app menu (or run `hpledgui`). It kinda works like HP's Windows app:

- **Help** blinks red until you cancel it.
- **Test** turns the light solid green.
- **Quiz** lets you pick an answer: A is white, B is green, C is red.
- **Group** lets you pick a group colour: white, green or red.

A dot on a tab shows which one is active, and choosing the same answer or group again clears it.
When on the Quiz and Group tabs, the "internet connected" white light stays off until you switch mode, so that an idle light won't be mistaken for a choice.

## Components

### Status daemon (hpledd)

`hpledd` is a small systemd service (Python 3, standard library only) that recreates what HP's Windows app did with the light. The first rule that applies wins:

| Priority | Condition | Light |
|----------|-----------|-------|
| 1 | help requested | blinking red |
| 2 | battery at or below 20% and unplugged | blinking red |
| 3 | test / quiz / group mode selected | green / white, green or red / white, green or red |
| 4 | Quiz or Group tab open in `hpledgui`, nothing selected | off |
| 5 | internet connected | solid white |
| 6 | none of the above | off |

Internet counts as connected when NetworkManager reports `full` connectivity (a captive portal or filtered network does not count). Without NetworkManager it falls back to "there is a default route". Battery and network are checked every 5 seconds.

```
hpledctl status                # what the light is doing and why
hpledctl help on               # blink red until...
hpledctl help off
hpledctl test on|off           # solid green
hpledctl quiz a|b|c|off        # white / green / red
hpledctl group white|green|red|off
hpledctl mode off              # clear test/quiz/group
```

`hpledctl` needs no root: the daemon listens on `/run/hpledd/hpledd.sock`, which is writable by every local user. Anyone logged in can therefore change the light, which seemed fine for a status LED. Change the socket mode in `bind_socket()` if you disagree.

### GUI (hpledgui)

`hpledgui` is a small window modelled on HP's app: tabs for Help, Test, Quiz, Group and
About, with big buttons (Request Help / Turn On Test Mode, A B C for quiz, White Green
Red for group). If the window closes or crashes,
the daemon drops back to normal operation after 10 seconds.


## How it works

The Windows app (`AD2F1837.HPInteractiveLight`) declares in its manifest that it may
call `ExecuteBiosWmiCommand` with **commandType 78 (0x4E)** and command 1 (get) or
2 (set). In HP's BIOS WMI interface (GUID `5FB7F034-2C63-45E9-BE91-3D44E2C707E4`, the one
the `hp_wmi` driver uses) that command lands in the ACPI methods:

| Command | ACPI method       | Purpose        |
|---------|-------------------|----------------|
| 1 (get) | `\_SB.WMIV.GALS`  | read LED state |
| 2 (set) | `\_SB.WMIV.SALS`  | set LED state  |

`SALS` takes a 4-byte buffer:

| Byte | Field | Values |
|------|-------|--------|
| 0 | apply flag | `1` = apply state and colour |
| 1 | state | `0` off, `1` on, `2` blink |
| 2 | colour | `0` white, `1` green, `2` red |
| 3 | blink flags | ignored when byte 0 is `1` |

The method then writes two embedded-controller registers (EC offsets on this model):

| EC offset | Bits | Field | Values |
|-----------|------|-------|--------|
| `0xF3` | 0-2 | `COL1` colour | `1` white, `2` green, `4` red |
| `0xF3` | 5-7 | `LDCD` | `3` = colour applied |
| `0xF4` | 0-1 | `LEDS` state | `0` off, `1` on, `3` blink |
| `0xF4` | 2-7 | `LEDF` blink flags | `3` when blinking |

So, by hand:

```
echo '\_SB.WMIV.SALS b01020200' | sudo tee /proc/acpi/call   # blink red
```

The colours match HP's user guide: white = internet connected / quiz answer A / group
white, green = test mode / B, red = C, blinking red = help request or battery <= 20%.

## Other models

The `0xF3`/`0xF4` offsets and the `WMIV.SALS` path come from this laptop's firmware. To
check another HP that ships "HP Interactive Light":

1. Dump your ACPI tables and decompile them (`acpica-tools`):
   ```
   sudo cat /sys/firmware/acpi/tables/DSDT > DSDT.aml && iasl -d DSDT.aml
   ```
2. In the resulting `DSDT.dsl`, search for `Case (0x4E)` and follow it to the get/set methods
   (here `GALS`/`SALS`), then read which EC fields they touch.
3. Check the payload layout and colour/state numbering above still match before
   sending anything.

## Disclaimer

Not affiliated with HP. This repo contains no HP code or firmware; it only sends the
same firmware call HP's own app does. All modes have been tested on one
machine only, so use it elsewhere at your own risk.

## License

MIT, see [LICENSE](LICENSE).
