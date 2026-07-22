# Drone Setup App

[![Download DroneSetup.exe](https://img.shields.io/badge/Download-DroneSetup.exe-2ea44f?style=for-the-badge)](https://github.com/caddis-tech/AquadronePicoFirmwareExperimental/releases/latest/download/DroneSetup.exe)

A small local window for flashing a Pico and generating a drone's BlueOS
Extension settings — no terminal required. Windows only.

## Running it

Download [`DroneSetup.exe`](https://github.com/caddis-tech/AquadronePicoFirmwareExperimental/releases/latest/download/DroneSetup.exe)
above and run it — no Python required. That link always resolves to the
latest build, published automatically by
[`.github/workflows/drone-setup-release.yml`](../.github/workflows/drone-setup-release.yml)
whenever `app/`, `DroneSetup.spec`, or `VERSION` changes on `main`.

To run from source instead:

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
2. **BlueOS Extension Settings** — enter the unit's caddis-api token (from
   caddis-api admin → Devices), then click **Generate Settings JSON**. The
   bridge ships as a BlueOS Extension (a Docker image installed via BlueOS's
   own control panel — see
   [`bridge/BLUEOS_EXTENSION.md`](../bridge/BLUEOS_EXTENSION.md)), so this app
   never touches the Pi directly: it builds the ready-to-paste settings JSON
   (permissions read straight from `bridge/Dockerfile`, plus an `Env` array
   with the token) and copies it to the clipboard. Paste it, along with the
   Extension Identifier/Name/image/tag shown above it, into BlueOS →
   Extensions → INSTALLED → **+**. No SSH, no sudo — Kraken persists the
   token across restarts and updates.

## Not built yet (tracked as GitHub issues)

- A firmware version picker backed by GitHub Releases (stable/experimental
  channels), proxied through caddis-api.
