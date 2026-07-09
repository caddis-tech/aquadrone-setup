# Drone Setup: Pico Firmware + Pi Bridge

End-to-end runbook for getting a `Drone-<Name>` unit (fleet naming convention,
e.g. `Drone-DBCooper`) onto the current Pico firmware and Pi bridge, including
registering it in caddis-api and wiring up its personal API token.

Applies to any current or future unit — swap in the real drone name and Pi IP
wherever you see `Drone-<Name>` / `<PI_IP>` below.

---

## Prerequisites

- Python 3 with `app/requirements.txt` installed (`pip install -r app/requirements.txt`) — runs the Drone Setup app that does the flashing and Pi provisioning below.
- SSH access to the unit's Raspberry Pi (BlueOS), key-based auth accepted, and an SSH client on PATH (Windows 10/11 ships OpenSSH by default).
- Physical access to the unit's Pico, to put it into BOOTSEL mode.
- Access to the caddis-api Django admin.
- A built firmware file (`build/my_project.uf2`) — see Step 1.

---

## Versioning

Pico firmware and the Pi bridge share **one version number**, read from the
[`VERSION`](../VERSION) file at the repo root. There's no separate constant to
hand-sync: `CMakeLists.txt` compiles it into the firmware as `FIRMWARE_VERSION`
(sent in every telemetry record), and `bridge/aquadrone_bridge.py` reads the
same file at startup (sent as `firmware_version` in the heartbeat). Bump
`VERSION`, commit, rebuild — both sides update together. Don't hand-edit the
version in caddis-api admin; the next heartbeat overwrites it anyway.

---

## Step 1 — Build the firmware

```bash
./build.sh
```
Auto-fetches the Pico SDK on first run (no more "checkout next to pico-sdk"
requirement) and produces `build/my_project.uf2`.

*Windows note:* the Pico SDK's build also needs a native host compiler (for
helper tools like `pioasm`/`picotool`) in addition to the ARM cross toolchain
— e.g. Visual Studio Build Tools with the "Desktop development with C++"
workload, or MinGW-w64. If `./build.sh` fails with `No CMAKE_C_COMPILER could
be found`, that's the missing piece.

---

## Step 2 — Register the unit in caddis-api and get its token

Every unit authenticates to caddis-api with its own personal token — this is
the `CADDIS_API_TOKEN` the bridge sends as `Authorization: Token <...>`.

**New unit:**
1. Open caddis-api admin → **Devices** → **Add device**.
2. Name it `Drone-<Name>` (e.g. `Drone-DBCooper`) to match the fleet
   convention. Save.
3. The admin auto-generates an Auth Token — copy it. This is the unit's
   personal token; treat it as a secret and never commit it to the repo. It
   gets pasted straight into the app in Step 4, not hand-edited into `.env`.

**Existing unit (re-flash / redeploy):** its token already exists — reuse the
value from its current `.env` on the Pi, or look it up again in caddis-api
admin → Devices. No need to regenerate unless the old token was compromised.

---

## Step 3 — Flash the Pico

```bash
python app/main.py
```
In the **1. Flash Pico** section: point it at `build/my_project.uf2` (pre-filled
if it exists), put the Pico in BOOTSEL mode (hold the white button while
plugging it in), and click **Flash Pico**. The app copies the firmware over,
waits for the Pico to reboot, and reads back a few telemetry records over its
own USB serial to confirm it's actually logging valid data — a PASS/FAIL
shows in the log pane before you move on. Once it passes, reconnect the Pico
to the unit's Pi via USB.

See [`app/README.md`](../app/README.md) for details on what the check verifies.

---

## Step 4 — Deploy or update the Pi bridge

Same app, **2. Setup Pi** section: enter the Pi's IP, the drone name
(`Drone-<Name>`), and the token from Step 2, then click **Deploy**. It
detects whether the bridge is already installed on that Pi and does a fresh
install or an update accordingly, then pushes a `.env` built from
`bridge/.env.example` with only the token filled in — every other default
(including `CADDIS_API_URL=https://api.caddistech.com`) passes through
untouched, so there's no way to point a drone at anything but prod. A fresh
install finishes with a reboot (required for the `dialout` group change
before `/dev/ttyACM0` is accessible); an update just restarts the service.

`bridge/deploy.sh` still works standalone if you'd rather script this from a
Mac/Linux machine — the app wraps the same steps plus the `.env` templating
that script previously left manual.

---

## Step 5 — Verify

- **Follow logs**: `ssh pi@<PI_IP> "journalctl -u aquadrone-bridge -f"`
- **Bridge manual run** — `python3 /opt/aquadrone/bridge/aquadrone_bridge.py`,
  watch stdout for `Posted: ts=... lat=... lon=...`
- **SD card contents**:
  ```bash
  python3 -c "import json; [json.loads(l) for l in open('/media/sensor_data/session_001.ndjson')]"
  ```
  Every line should parse without error.
- **API records** — caddis-api admin → Telemetry → confirm the unit's
  records show up with correct lat/lon, sensor values, and `firmware_version`.
- **Heartbeat** — bridge logs should show a `Heartbeat OK` line every ~60s,
  or the unit's `last_seen` timestamp should update in caddis-api admin →
  Devices.
- **4G resilience** (optional) — block outbound traffic for 60s, restore,
  and confirm queued records appear in the API afterward.

---

## Reference

- Firmware build details: [`README.md`](../README.md)
- Bridge architecture, env vars, hardware setup: [`bridge/README.md`](../bridge/README.md)
- Install/update automation: [`bridge/deploy.sh`](../bridge/deploy.sh)
- Systemd unit: [`bridge/aquadrone-bridge.service`](../bridge/aquadrone-bridge.service)
- Drone Setup app: [`app/README.md`](../app/README.md)

## Not built yet

- A double-click `.exe` build of the app (currently run via `python app/main.py`).
- A GitHub-Releases-backed firmware version picker in the app (stable/experimental channels).
- A maintainer script to cut tagged GitHub Releases with the `.uf2` attached.

Tracked as GitHub issues in this repo.
