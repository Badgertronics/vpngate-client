#!/bin/bash
# Install the VPN Gate GUI client and all of its dependencies.
#
#   sudo ./install.sh
#
# The application itself is installed into ~/VPNGate and can be started from
# the desktop menu ("VPN Gate Client") or with ~/VPNGate/vpngate_gui.py
set -euo pipefail

APP_DIR="${1:-}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# When run with sudo the invoking user's home, not root's, is the target.
REAL_HOME="$HOME"
INVOKER=""
if [ -n "${SUDO_USER:-}" ]; then
    INVOKER="$SUDO_USER"
    REAL_HOME="$(getent passwd "$INVOKER" | cut -d: -f6)"
fi
if [ -z "$APP_DIR" ]; then
    APP_DIR="$REAL_HOME/VPNGate"
fi

echo "==> Installing VPN Gate Client into $APP_DIR"
mkdir -p "$APP_DIR"
if [ -d "$SRC_DIR/app" ]; then
    cp -r "$SRC_DIR/app/." "$APP_DIR/"
else
    cp -r "$SRC_DIR/." "$APP_DIR/"
fi
chmod +x "$APP_DIR/vpngate_gui.py" "$APP_DIR/worker_main.py"
mkdir -p "$APP_DIR/state" "$APP_DIR/logs"

echo "==> Installing required packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends \
    python3 python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 \
    python3-cairo python3-gi-cairo \
    openvpn \
    strongswan strongswan-swanctl strongswan-pki \
    xl2tpd sstp-client ppp \
    python3-netfilterqueue \
    softether-vpnclient softether-vpncmd \
    iproute2 iputils-ping curl ca-certificates \
    | tail -3

# Optional: system tray icon (libayatana-appindicator is a GTK3 binding;
# the app runs it in a tiny helper process, so a failure here just
# disables the tray - never the client itself).
echo "==> Installing optional tray support"
apt-get install -y --no-install-recommends \
    libayatana-appindicator3-1 gir1.2-ayatanaappindicator3-0.1 >/dev/null 2>&1 \
    || echo "    (tray bindings unavailable; the tray icon will stay disabled)"

echo "==> Preparing PPP secrets"
if [ ! -f /etc/ppp/chap-secrets ]; then
    cat > /etc/ppp/chap-secrets <<'EOF'
# Secrets for authentication using CHAP
# client	server	secret			IP addresses
EOF
fi
if ! grep -q '^vpn ' /etc/ppp/chap-secrets 2>/dev/null; then
    printf 'vpn * vpn *\n' >> /etc/ppp/chap-secrets
fi
if [ ! -f /etc/ppp/pap-secrets ]; then
    cat > /etc/ppp/pap-secrets <<'EOF'
# Secrets for authentication using PAP
# client	server	secret			IP addresses
EOF
fi
if ! grep -q '^vpn ' /etc/ppp/pap-secrets 2>/dev/null; then
    printf 'vpn * vpn *\n' >> /etc/ppp/pap-secrets
fi
chmod 600 /etc/ppp/pap-secrets 2>/dev/null || true

if [ ! -f /etc/xl2tpd/xl2tpd.secrets ]; then
    printf 'vpn * vpn *\n' > /etc/xl2tpd/xl2tpd.secrets
fi
chmod 600 /etc/xl2tpd/xl2tpd.secrets

echo "==> Enabling the SoftEther client service"
/usr/libexec/softether/vpnclient/vpnclient start >/dev/null 2>&1 || true

echo "==> Installing the application icons"
mkdir -p /usr/share/icons/hicolor/scalable/apps
if [ -d "$APP_DIR/icons/hicolor/scalable/apps" ]; then
    cp -f "$APP_DIR"/icons/hicolor/scalable/apps/*.svg \
        /usr/share/icons/hicolor/scalable/apps/ || true
    gtk-update-icon-cache -f /usr/share/icons/hicolor >/dev/null 2>&1 || true
fi

echo "==> Registering the desktop entry"
mkdir -p /usr/share/applications
cat > /usr/share/applications/vpngate-gui.desktop <<EOF
[Desktop Entry]
Type=Application
Name=VPN Gate Client
GenericName=VPN Client
Comment=Connect to the public free VPN servers of VPN Gate
Exec=$APP_DIR/vpngate_gui.py
Icon=com.vpngate.client
Terminal=false
Categories=Network;Security;
StartupNotify=true
EOF
chmod 644 /usr/share/applications/vpngate-gui.desktop
update-desktop-database /usr/share/applications 2>/dev/null || true

if [ -n "$INVOKER" ] && [ -d "$REAL_HOME/.local/share/applications" ]; then
    cp -f /usr/share/applications/vpngate-gui.desktop "$REAL_HOME/.local/share/applications/"
    chown "$INVOKER:" "$REAL_HOME/.local/share/applications/vpngate-gui.desktop" 2>/dev/null || true
    update-desktop-database "$REAL_HOME/.local/share/applications" 2>/dev/null || true
fi

echo "==> Making sure the tun device is available"
modprobe tun 2>/dev/null || true
if ! grep -q '^tun' /etc/modules-load.d/*.conf 2>/dev/null; then
    echo tun > /etc/modules-load.d/vpngate.conf 2>/dev/null || true
fi

if [ -n "$INVOKER" ]; then
    echo "==> Fixing ownership for $INVOKER"
    chown -R "$INVOKER:" "$APP_DIR"
fi

echo
echo "VPN Gate Client has been installed."
echo "  Start it from the application menu or run:"
echo "      $APP_DIR/vpngate_gui.py"
