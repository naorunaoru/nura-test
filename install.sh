#!/bin/sh
set -eu
cd "$(dirname "$0")"
[ "$(id -u)" = 0 ] || { echo 'Run this installer as root.' >&2; exit 1; }
tr -d '\000' < /proc/device-tree/model | grep -q 'OnePlus 8 Pro' || {
    echo 'This hardware profile supports the OnePlus 8 Pro only.' >&2; exit 1;
}
apk add python3 py3-pillow py3-evdev font-droid libssc networkmanager-cli bluez libdrm
# Migrate installations made before the foreground launcher.
if [ -f /etc/systemd/system/nura-tests.service ]; then
    if command -v systemctl >/dev/null; then
        systemctl disable --now nura-tests.service
        rm /etc/systemd/system/nura-tests.service
        systemctl daemon-reload
    else
        rm /etc/systemd/system/nura-tests.service
    fi
fi
python3 - <<'PY'
from pathlib import Path
import shutil
from launcher import Instance, AlreadyRunning

try:
    with Instance():
        target=Path('/opt/nura-tests');target.mkdir(mode=0o755,parents=True,exist_ok=True)
        for name in ('app.py actions.py launcher.py core.py reports.py sensor_live.py sensor_views.py '
                     'camera_live.py camera_view.py radio_scan.py touch_trace.py drm_display.py external_display.py display_pattern.py steps.json').split():
            shutil.copyfile(name,target/name);(target/name).chmod(0o644)
        launcher=Path('/usr/local/bin/nura-tests');launcher.parent.mkdir(parents=True,exist_ok=True)
        launcher.write_text('#!/bin/sh\nexec python3 /opt/nura-tests/launcher.py "$@"\n')
        launcher.chmod(0o755)
except AlreadyRunning as error:
    raise SystemExit(str(error)+' Re-run the installer after it exits.')
PY
echo 'Installed. Run nura-tests as root to open the suite on the phone.'
