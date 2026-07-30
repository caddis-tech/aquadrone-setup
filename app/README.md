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

1. **Flash Pico** — get a `.uf2`, then click **Flash Pico**. Put the Pico in
   BOOTSEL mode (hold the white button while plugging it in) when prompted in
   the log. The app copies the firmware over, waits for the Pico to reboot, and
   reads back a few telemetry records over serial to confirm it's actually
   logging data before calling it a pass.

   Two ways to get the `.uf2`:

   - **Download a published version.** Pick a channel, click **Fetch
     versions**, pick a version, click **Download**. See below for what
     "channel" means and what gets checked.
   - **Use a file you already have.** **Load .uf2 file...** browses for one.
     The field starts pre-filled with the newest `AquaD_Pico_v*.uf2` in
     `build/`, or sitting next to `DroneSetup.exe`. This path needs no network,
     which is the point of keeping it.
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

## Downloading firmware

Published firmware lives in a second, public repo,
[`caddis-tech/aquadrone-pico-firmware`](https://github.com/caddis-tech/aquadrone-pico-firmware).
Only compiled `.uf2` files and a manifest listing them go there. Source, docs and
history stay in this repo.

It has to be public because GitHub has no per-asset visibility, and a tech holding
nothing but `DroneSetup.exe` has no credential to authenticate with. **No token or
key that could grant access ships inside the exe**, and none is needed: the download
is anonymous.

**Two channels.** `stable` is a released build. `experimental` is a prerelease
*production* build — the same firmware, published before it is blessed for the
fleet. Neither channel ever carries the hardware test (HIL) image; that is not
published anywhere. See [`docs/firmware-build.md`](../docs/firmware-build.md).

**What is checked before anything is flashed:**

1. The manifest is signed with an Ed25519 key whose public half is compiled into the
   app (`firmware_manifest.SIGNING_PUBLIC_KEY_HEX`). A manifest that fails that check
   is refused outright, and the app says so rather than falling back to it. HTTPS
   alone would only prove who answered the request, not who wrote the file.
2. Every field is validated after the signature passes — a valid signature proves the
   bytes are ours, not that whatever produced them was working. Download URLs must
   point at our own release storage, and versions must be a plain `x.y.z`, which means
   a `+HIL` version cannot be listed at all.
3. The downloaded `.uf2` is checked against the SHA-256 in the signed manifest. It is
   written to a `.part` file and only renamed once that matches, so the final path
   never holds unverified bytes.
4. The file's contents are read to confirm it is not the HIL image. Unlike a file you
   browsed to, this one is not a question: nothing published should ever be that
   image, so it is deleted and the flash refused.

Downloads land in `%USERPROFILE%\.aquadrone\firmware`, not beside the exe, which is
often somewhere unwritable.

The private signing key exists only as an Actions secret on this repo. Publishing is
done by [`.github/workflows/firmware-publish.yml`](../.github/workflows/firmware-publish.yml),
so a released build always comes from a clean checkout rather than someone's laptop.
