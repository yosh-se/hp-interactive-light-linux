#!/bin/bash
# Install or remove the hpledd systemd service.
# Usage: sudo ./install.sh install | uninstall
set -eu
cd "$(dirname "$0")"

[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }

case "${1:-}" in
    install)
        install -Dm755 hpledd.py /usr/local/bin/hpledd
        install -Dm755 hpledctl.py /usr/local/bin/hpledctl
        install -Dm755 hpledgui.py /usr/local/bin/hpledgui
        install -Dm644 hpledgui.desktop /usr/share/applications/hpledgui.desktop
        install -Dm644 icons/hpledgui.svg /usr/share/icons/hicolor/scalable/apps/hpledgui.svg
        gtk-update-icon-cache -qf /usr/share/icons/hicolor 2>/dev/null || true
        install -Dm644 hpledd.service /etc/systemd/system/hpledd.service
        systemctl daemon-reload
        systemctl enable --now hpledd.service
        echo "installed; check with: hpledctl status"
        ;;
    uninstall)
        systemctl disable --now hpledd.service 2>/dev/null || true
        rm -f /usr/local/bin/hpledd /usr/local/bin/hpledctl /usr/local/bin/hpledgui \
            /usr/share/applications/hpledgui.desktop /usr/share/icons/hicolor/scalable/apps/hpledgui.svg \
            /etc/systemd/system/hpledd.service
        gtk-update-icon-cache -qf /usr/share/icons/hicolor 2>/dev/null || true
        systemctl daemon-reload
        echo "removed"
        ;;
    *)
        echo "usage: sudo ./install.sh install | uninstall" >&2
        exit 1
        ;;
esac
