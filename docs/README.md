# Drone Setup: Pico Firmware + Pi Bridge

End-to-end runbook for getting a `Drone-<Name>` unit (fleet naming convention,
e.g. `Drone-DBCooper`) onto the current Pico firmware and Pi bridge, including
registering it in caddis-api and wiring up its personal API token.

Applies to any current or future unit — swap in the real drone name and Pi IP
wherever you see `Drone-<Name>` / `<PI_IP>` below.

---

## Prerequisites

- Python 3 with `app/requirements.txt` installed (`pip install -r app/requirements.txt`) — runs the Drone Setup app that does the flashing and generates the extension settings below.
- Physical access to the unit's Pico, to put it into BOOTSEL mode.
- Access to the caddis-api Django admin.
- Access to the boat's BlueOS control panel (its web UI) — no SSH needed for anything in this runbook.
- A built firmware file (`build/AquaD_Pico_v<version>.uf2`) — see Step 1.

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
requirement) and produces `build/AquaD_Pico_v<version>.uf2` — the version comes
from the `VERSION` file, so the artifact name always names what's inside it.

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
   gets pasted straight into the app in Step 4, which folds it into the
   settings JSON you paste into BlueOS — never hand-edited into a file.

**Existing unit (re-flash / redeploy):** its token already exists — reuse the
value from BlueOS's Extensions Manager (the Env field of the installed
extension's settings) or look it up again in caddis-api admin → Devices. No
need to regenerate unless the old token was compromised.

---

## Step 3 — Flash the Pico

```bash
python app/main.py
```
In the **1. Flash Pico** section, get a `.uf2` one of two ways:

- **Download a published build.** Leave Channel on `stable`, click **Fetch
  versions**, pick one, click **Download**. The app checks the signature on the
  version list and the checksum of the file before it will flash anything. This
  needs no login. See [`app/README.md`](../app/README.md) for what it verifies.
- **Use a local file.** The field is pre-filled with the newest one it finds (in
  `build/` from source, or beside `DroneSetup.exe` when frozen), and
  **Load .uf2 file...** browses for another. This works with no network.

Then put the Pico in BOOTSEL mode (hold the white
button while
plugging it in), and click **Flash Pico**. The app copies the firmware over,
waits for the Pico to reboot, and reads back a few telemetry records over its
own USB serial to confirm it's actually logging valid data — a PASS/FAIL
shows in the log pane before you move on. Once it passes, reconnect the Pico
to the unit's Pi via USB.

See [`app/README.md`](../app/README.md) for details on what the check verifies.

**If a "Bench test firmware" warning appears, stop and pick a different file.** It
means the selected `.uf2` is the hardware test image, which can be commanded to fake
SD card writes: a drone running it streams normal-looking telemetry while recording
nothing to the card. The file you want has no `HIL` in its name. The app checks the
file's contents rather than its name, so the warning is right even if the filename
looks correct. Answering "yes" is only ever for a bench board.

**Power-cycle the Pico after flashing, before the unit goes out.** The first boot
after any flash holds the SD card off for 10 minutes; unplugging and replugging
clears it. See [`docs/firmware-build.md`](firmware-build.md) and entry seven in
[`docs/regressions.md`](regressions.md).

---

## Step 4 — Install the extension via BlueOS's control panel

The bridge ships as a BlueOS Extension: a Docker image that BlueOS's
Extensions Manager (Kraken) pulls, runs, restarts, and updates — see
[`bridge/BLUEOS_EXTENSION.md`](https://github.com/caddis-tech/AquadronePicoFirmwareExperimental/blob/main/bridge/BLUEOS_EXTENSION.md) for the full
rationale and reference. Provisioning it, including the token, happens
entirely through BlueOS's own web UI — no SSH, no sudo:

1. Same app, **2. BlueOS Extension Settings** section: paste in the token
   from Step 2 and click **Generate Settings JSON**. It's copied to the
   clipboard automatically (and shown in the box below, to eyeball before
   pasting). The generated JSON is built from the actual `bridge/Dockerfile`
   permissions and the current `VERSION`, so it can't drift from what the
   image really ships with, and the token is baked into its `Env` array —
   there's no way to point a drone at anything but prod
   (`CADDIS_API_URL=https://api.caddistech.com` is fixed in it too).
2. In BlueOS → Extensions → **INSTALLED** → the **+** button, fill in the
   Extension Identifier / Name / Docker image / Docker tag shown in the app,
   paste the generated JSON as the settings, and install. It will crash-loop
   immediately after install — that is expected: Kraken starts the container
   before anyone could possibly have supplied a token yet, and this JSON
   already has it, so the very next restart picks it up.
3. If BlueOS shows the container unhealthy, restart the extension from
   Extensions Manager — a fresh install occasionally needs one restart to
   pick up the just-provided Env.

This app never touches the Pi's filesystem — no `/opt/aquadrone`, no systemd,
no SSH keys. `bridge/deploy.sh` and the old systemd install remain only for a
boat not yet migrated to the extension; see
[`bridge/README.md`](https://github.com/caddis-tech/AquadronePicoFirmwareExperimental/blob/main/bridge/README.md).

---

## Step 5 — Verify

- **Logs** — BlueOS → Extensions → Aquadrone Bridge → Logs (legacy systemd
  install only: `ssh pi@<PI_IP> "journalctl -u aquadrone-bridge -f"`)
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
- Bridge architecture, env vars, hardware setup: [`bridge/README.md`](https://github.com/caddis-tech/AquadronePicoFirmwareExperimental/blob/main/bridge/README.md)
- BlueOS Extension install, token provisioning, versioning, rollback:
  [`bridge/BLUEOS_EXTENSION.md`](https://github.com/caddis-tech/AquadronePicoFirmwareExperimental/blob/main/bridge/BLUEOS_EXTENSION.md)
- Drone Setup app: [`app/README.md`](../app/README.md)

## Publishing a new firmware version

Run the **Release** workflow from the Actions tab: pick `patch`, `minor` or `major`,
tick **firmware**, press Run. It bumps `VERSION`, builds the production image, attaches
it to a release in the public `caddis-tech/aquadrone-pico-firmware` repo, and updates
the signed manifest the app reads. Nothing is published from a laptop, and merging a PR
publishes nothing at all.

Choose the `experimental` channel for a prerelease build. That is still a production
image; it just is not blessed for the fleet yet. The hardware test (HIL) image is never
published to either channel.

See [`.github/workflows/release.yml`](https://github.com/caddis-tech/AquadronePicoFirmwareExperimental/blob/main/.github/workflows/release.yml) and
[`docs/firmware-build.md`](firmware-build.md).
