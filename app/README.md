# Drone Setup App

A small local window for flashing a Pico and provisioning its drone's Pi — no
terminal required. Windows only.

## Running it (today)

```
pip install -r app/requirements.txt
python app/main.py
```

The window has two steps, top to bottom:

1. **Flash Pico** — pick a `.uf2` file (defaults to the newest
   `AquaD_Pico_v*.uf2` in `build/`, or sitting next to `Drone-Setup.exe`),
   then click **Flash Pico**. Put the Pico in BOOTSEL mode (hold
   the white button while plugging it in) when prompted in the log. The app
   copies the firmware over, waits for the Pico to reboot, and reads back a
   few telemetry records over serial to confirm it's actually logging data
   before calling it a pass.
2. **Setup Pi** — enter the target Pi's IP, the drone's name (`Drone-<Name>`,
   e.g. `Drone-DBCooper`), and its caddis-api token (from caddis-api admin →
   Devices), then click **Deploy**. The app detects whether the bridge is
   already installed on that Pi and either does a fresh install or an
   update, then pushes a `.env` built from `bridge/.env.example` with the
   token filled in — every other value (including `CADDIS_API_URL`) is left
   at its default, so there's no way to point a drone at anything but prod.
   Requires an SSH client on PATH (Windows 10/11 ships OpenSSH by default)
   and key-based SSH auth already set up to the Pi.

## Not built yet (tracked as GitHub issues)

- Packaging this into a double-click `.exe` (PyInstaller) so field techs
  don't need Python installed.
- A firmware version picker backed by GitHub Releases (stable/experimental
  channels), proxied through caddis-api.
