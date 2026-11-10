# nura-test

A touchscreen hardware test tool for the **OnePlus 8 Pro (`instantnoodlep`) running Nura Linux**. It runs directly on the phone's framebuffer, presents one check at a time, and saves your Pass / Fail / Skip decisions with logs and captured evidence.

This is a device-specific tool for port development and regression testing. The current profile expects the phone's 1440×3168 portrait framebuffer, `s6sy761` touchscreen, and the audio, camera and sensor interfaces used by the [unofficial OnePlus 8 Pro Nura port](https://github.com/naorunaoru/oneplus-instantnoodlep-nura-unofficial). Installing this app does not install that port's kernel or firmware.

## Install and run

Run these commands **as root on the phone**, from a console or SSH session:

```sh
apk add git
git clone https://github.com/naorunaoru/nura-test.git
cd nura-test
sh install.sh
nura-tests
```

The installer checks the device model, installs its APK dependencies, copies the app to `/opt/nura-tests`, and installs `nura-tests.service` plus the `nura-tests` launcher. It uses systemd and does not enable automatic startup at boot. The app takes over VT8 and grabs the internal touchscreen; closing it returns to the previous console.

The target system needs working framebuffer/DRM, evdev and hardware drivers. Use a console session with any graphical compositor stopped: external-display tests need exclusive DRM master access. The installer adds Python, Pillow, evdev, Droid fonts, libssc, libdrm, NetworkManager CLI and BlueZ. Individual checks also use tools and services supplied by the port: `monitor-sensor` with `iio-sensor-proxy`, ALSA's `amixer`/`aplay`/`arecord`, libcamera's `cam`, and `neard` for NFC. A missing interface or tool is reported by the affected check.

To update from the cloned directory:

```sh
git pull --ff-only
sh install.sh
nura-tests
```

Reinstalling stops a running app and preserves its saved state and reports. To stop it from SSH, run `systemctl stop nura-tests.service`.

## Using the suite

Tap **Start** or **Resume** to walk through the checks. **IMU**, **Compass** and **Display** jump directly to those pages; earlier unanswered checks remain pending. After all checks have an answer, the app shows a summary. **New run** starts another run; reopening a completed run shows its existing summary.

| Control | Effect |
|---|---|
| **Pass / Fail / Skip** | Save your verdict and evidence, then advance. Use Skip for unavailable equipment or a check you cannot perform. |
| **Back** | Return to the previous check. A replacement verdict becomes the current answer; earlier verdicts remain in history. |
| **Pause** | Stop the current action and return to the start screen without recording a verdict. |
| **Close** | Exit the app and restore the console. Available on the start and results screens. |
| **Run / Stop / Retry** | Trigger, stop or repeat an action that needs an explicit start. Retry also restarts a live check that ended. |

Sensor readings, touchscreen capture, screen patterns, connection status and radio scans start when you enter their pages. There is **no minimum test duration**: choose a verdict as soon as you have enough evidence. Leaving a live check stops its worker before advancing.

Tones, microphone playback, vibration, LEDs, brightness/display cycles and cameras use **Run**. Their **Pass** button becomes available after the action completes successfully; **Fail** and **Skip** remain available. A successful command only means the action ran—you still judge the hardware result.

Controls sit at the bottom, with the top 192 physical pixels reserved for the camera cutout. Use stationary taps for buttons: swiping across a verdict button during the touch check does not activate it.

## Checks and expected results

There are 32 checks in [steps.json](steps.json). Each page states its criterion.

| Area | What to do and expect |
|---|---|
| Internal display and touch | Inspect colour blocks and text for corruption or flicker. Sweep edges and corners; yellow dots should follow contacts and stop when fingers lift. |
| Brightness and display power | Run cycles three brightness levels, or blanks the display for two seconds. The original brightness/picture should return. |
| Haptics and LEDs | Expect one brief vibration or a short pulse from each rear LED. |
| Phone audio | Check earpiece and bottom-speaker tones. The microphone check records three seconds, plays them back, then deletes the recording. |
| IMU and tilt | The bubble moves toward the raised edge and centers when level. Turn the phone screen-up, upright, then screen-down: labels should say face-up, vertical, then face-down. |
| Compass | Keep both phones flat with their top edges aligned; compare against a reference set to **magnetic north** in several directions. Animation buffers about 220 ms to interpolate 5 Hz readings; saved samples are unfiltered. |
| Light and proximity | Cover/uncover the top sensor area. Light readings should change; proximity should switch near/far. |
| External display | Check the pattern and modes described below, then plug orientation, monitor audio, and phone-powered operation. |
| USB peripherals | A USB touchscreen should increment its coordinate-event count. With the monitor detached, a USB 3 drive/hub should report at least 5000 Mb/s. USB networking checks whether the computer sees an interface, not transfer speed. |
| Charging | Test USB-C PD and USB-A chargers separately. Expect battery status **Charging**; skip these checks above 90% charge. |
| Bluetooth | Put an accessory in discovery/pairing mode. The list shows observed names, addresses and RSSI; the app does not pair. If it enables Bluetooth, it restores the previous off state on exit. |
| Wi-Fi | Expect nearby SSIDs, BSSIDs, channels, signal percentages and security. Enable Wi-Fi before testing; scanning does not change connections. **Next page** cycles radio lists longer than six rows. |
| NFC | Present a tag and expect detection. The app does not read or write tag contents. |
| Cameras | Run captures from the main, ultrawide, front, telephoto and colour-filter sensor—the last is labelled **Fifth camera** in the app. Expect a recognisable preview; use a bright, distant scene for telephoto. Captures are saved, including retries. |

Accessory checks need the corresponding monitor, USB devices, chargers or NFC tag; use Skip when you do not have them. This suite does not cover every subsystem: for example, cellular calls/data, GNSS and video codecs have no checks here.

Camera checks cover capture with optical image stabilisation (OIS) disabled. They refuse to run while the `lc898128` OIS module is loaded, and the app does not unload it for you. If the page reports OIS active, choose **Skip**; testing capture with OIS enabled is outside this suite's current coverage.

## External display modes

Enter **Display** from the start screen, or reach **External display modes** in the suite. When a monitor is connected, the app gives it a separate DRM framebuffer and immediately draws a test card, initially trying its preferred mode. The phone keeps its existing internal mode.

1. Use **< Res / Res >** to select an advertised resolution.
2. Use **Hz** to cycle that resolution's timings.
3. Tap **Apply**, then allow the monitor to settle before judging the picture.

**Preferred** selects the monitor's EDID preferred timing; press **Apply** to use it. Preferred does not necessarily mean the highest refresh rate. The selector includes advertised modes from 640×480 upward, including native resolutions larger than the phone's framebuffer. It does not invent modes absent from the connector's mode list.

The card is regenerated from vector primitives at each output size: 75% RGB bars, a circle/grid, a gray ramp, near-black/white patches, one-pixel lines and overscan borders. Look for a round circle, visible borders, distinct patches and a stable picture. The SVG saved with each accepted mode uses the same primitives as the displayed card.

A kernel-rejected mode change leaves the previous mode active and shows the error. A kernel-accepted change still needs visual verification: the app's mode label is not proof that the monitor locked to that timing. Use the monitor's input-information menu where available. The pattern stays until another mode is applied or you leave the external-display checks; there is no timed reversion.

The pattern also runs during plug-orientation, USB-touchscreen, monitor-audio and phone-powered-monitor checks. If you unplug the display, the app waits for it to reconnect and probes its modes again. Pause, Close or leaving these checks restores the previous external configuration. Service cleanup or the next app start uses a recovery journal to restore interrupted changes.

**HDR is not implemented:** the tested MSM DisplayPort driver exposes no `HDR_OUTPUT_METADATA` property. On the tested setup, the VGA pattern was visually confirmed, as was a stable **2880×1800 at 120 Hz** input. Other modes depend on the display, cable and driver; physical unplug/replug still needs validation beyond the software tests.

## Results and touch traces

The run directory is shown in the app and saved in `/var/lib/nura-tests/current.json` as `output_directory`. It has the form `/tmp/mqa/YYYY-MM-DD/NNN/`, with a new number for each run.

| Artifact | Contents |
|---|---|
| `results.json` | Verdicts, criteria, answer history, kernel/app version, snapshots and evidence paths. |
| `photos/*.png` | Camera captures, including retries. The check requests 1280×960; these files retain the capture rather than the smaller UI preview. |
| `dmesg-start.log`, `dmesg.log` | Kernel log at run start and its latest collected copy. |
| `logs/services.log`, `logs/*.log` | Service logs and raw action output. |
| `logs/imu-samples-*.jsonl`, `logs/compass-samples-*.jsonl` | Sensor samples for the live views. |
| `logs/external-modes-*.jsonl` | Connector modes, accepted/rejected mode changes and errors. Operator verdicts remain in `results.json`. |
| `logs/external-pattern-*.svg` | Vector card for each accepted mode. |
| `logs/touchscreen-*.jsonl` | Internal touch contacts recorded throughout app use, including controls and other test pages. |
| `logs/touch-coverage-*.png` | Coverage dots saved when answering the internal touch check. |

**Copy the whole run directory before rebooting: `/tmp` is temporary.** From your computer, substitute the phone address and the run path shown in the app:

```sh
scp -r root@PHONE_IP:/tmp/mqa/YYYY-MM-DD/NNN ./nura-test-run
```

Current state and completed JSON reports also live under `/var/lib/nura-tests/`, but they do not preserve photos or other artifacts from `/tmp`. Reopening resumes saved answers; it cannot recover deleted artifacts. Reports can include camera images, nearby network/device identifiers and system logs, so choose the relevant files when sharing a bug report.

Touch traces have no point-count limit. Each JSONL starts with device name, raw axis ranges, screen dimensions, top-left origin and clock metadata. Contact records include sequence number, event/elapsed time, slot, tracking ID, down/move/up phase, raw/normalized/screen coordinates, page/test and the control under the contact. Dropped input frames have explicit records. Each answer references its trace file and sequence interval; reopening the app creates another trace without overwriting earlier ones.

Use those contacts to reconstruct paths or plot coverage later. Coverage PNGs mark actual samples without drawing lines across gaps. **An empty area means untested**, unless you know you swiped through it and no contacts were received; coverage alone cannot establish a dead region.

## Troubleshooting and development

For startup or cleanup errors, inspect the service log from SSH:

```sh
systemctl status nura-tests.service --no-pager
journalctl -b -u nura-tests.service --no-pager -n 100
```

The app requires the internal framebuffer in portrait 1440×3168 with the expected 32-bit pixel layout. For an external-display permission or busy error, check that a graphical compositor is not holding DRM master access. A missing sensor stream, Wi-Fi-off message, unavailable camera or other action error should be investigated in that action's log; **Retry** repeats the check after the cause is fixed.

The 43 software tests exercise state persistence, worker cleanup, radio parsing, sensor transforms, touch evidence, framebuffer updates and DRM selection/restoration with hardware mocked. Run them on Linux or macOS without a phone or root access:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
python test_app.py
```

GitHub Actions runs the same suite. Passing it does not establish hardware functionality: new driver, timing or device-profile changes still need on-device checks.

The main entry point is `app.py`; `actions.py` runs hardware workers, `core.py` stores answers, and `reports.py` manages artifacts. Sensor views live in `sensor_live.py` / `sensor_views.py`, radio parsing in `radio_scan.py`, and touch capture in `touch_trace.py`. External output uses `drm_display.py`, `external_display.py` and `display_pattern.py`. Add or revise the test catalogue in `steps.json`; porting to another phone also requires changing the device-specific interfaces in the code and installer.
