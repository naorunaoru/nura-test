#!/bin/sh
set -eu
cd "$(dirname "$0")"
[ "$(id -u)" = 0 ] || { echo 'Run this installer as root.' >&2; exit 1; }
tr -d '\000' < /proc/device-tree/model | grep -q 'OnePlus 8 Pro' || {
    echo 'This hardware profile supports the OnePlus 8 Pro only.' >&2; exit 1;
}
apk add python3 py3-pillow py3-evdev font-droid libssc networkmanager-cli bluez libdrm
systemctl stop nura-tests.service 2>/dev/null || true
install -d -m 755 /opt/nura-tests
for file in app.py actions.py core.py reports.py sensor_live.py sensor_views.py radio_scan.py touch_trace.py drm_display.py external_display.py display_pattern.py steps.json; do
    install -m 644 "$file" /opt/nura-tests/
done
install -m 644 nura-tests.service /etc/systemd/system/nura-tests.service
install -d -m 755 /usr/local/bin
cat > /usr/local/bin/nura-tests <<'EOF'
#!/bin/sh
exec systemctl start nura-tests.service
EOF
chmod 755 /usr/local/bin/nura-tests
systemctl daemon-reload
echo 'Installed. Run nura-tests as root to open the suite on the phone.'
