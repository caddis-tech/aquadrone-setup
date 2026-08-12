# Drone Setup App

[![Download DroneSetup.exe](https://img.shields.io/badge/Download-DroneSetup.exe-2ea44f?style=for-the-badge)](https://github.com/caddis-tech/aquadrone-setup/releases/latest/download/DroneSetup.exe)

A small local window for flashing a Pico and provisioning a drone's BlueOS
Extension. No terminal required. Windows only.

## Running it

Download [`DroneSetup.exe`](https://github.com/caddis-tech/aquadrone-setup/releases/latest/download/DroneSetup.exe)
above and run it — no Python required. That link always resolves to the
latest build, published automatically by
[`.github/workflows/drone-setup-release.yml`](../.github/workflows/drone-setup-release.yml)
whenever `app/`, `DroneSetup.spec`, or `VERSION` changes on `main`. The release
is named from `VERSION`, so bumping that file is what mints a new one.

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
2. **BlueOS Extension (MANTA Link)**: check a boat, and fix it. MANTA Link
   ships as a BlueOS Extension: a Docker image that BlueOS's Extensions Manager
   (Kraken) pulls, runs, restarts, and updates. No SSH, no sudo, and nothing
   here touches the Pi's filesystem.

   - **Audit vehicle** reads the boat and changes nothing, so it is safe on a
     boat in service and is the right first move on any boat. Leave the address
     blank to search `blueos.local` and `192.168.2.2`, or type one in. It
     reports five things separately (installed, image, permissions, version,
     enabled) and states each one even when it passes.
   - **Install / Repair** audits first, shows exactly what it intends to do,
     asks, and then does it. It always sends the complete permissions block:
     Kraken has no partial update, so anything left out is access the container
     loses. Anything a boat already had in its `Env`, including its token, is
     carried back over unread.
   - **Generate Settings JSON** is the manual path, and still the only way to
     deliver a token: paste the unit's caddis-api token (from caddis-api admin
     → Devices), click the button, and paste the result with the
     Identifier/Name/image/tag into BlueOS → Extensions → INSTALLED → **+**.

   **Why the audit matters.** Kraken does not fall back to the image's own
   `permissions` LABEL. A boat installed with Custom settings left empty stores
   `{}` and comes up with no `/dev` bind, no host networking, and no persistent
   volume, while still looking installed, enabled, and on the right tag. MANTA
   Link then reports `no Pico present`, `Connection refused`, and `no API token
   configured` at once, and none of those names the cause. Empty permissions is
   its own reported state for exactly that reason.

## Downloading firmware

Published firmware lives in a separate public repo,
[`caddis-tech/aquadrone-firmware-releases`](https://github.com/caddis-tech/aquadrone-firmware-releases).
Only compiled `.uf2` files and a manifest listing them go there — it is a
distribution endpoint, not source. The firmware source itself is in the private
`caddis-tech/AquadronePicoFirmware`.

It has to be public because GitHub has no per-asset visibility, and a tech holding
nothing but `DroneSetup.exe` has no credential to authenticate with. **No token or
key that could grant access ships inside the exe**, and none is needed: the download
is anonymous.

**Two channels.** `stable` is a released build. `experimental` is a prerelease
*production* build — the same firmware, published before it is blessed for the
fleet. Neither channel ever carries the hardware test (HIL) image; that is not
published anywhere. See [`docs/firmware-build.md`][firmware-build-docs] in the
internal firmware repo.

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

The private signing key exists only as an Actions secret on the private firmware
repo — never here, and never in the exe. Publishing is done by
[`.github/workflows/firmware-publish.yml`][firmware-publish] there, so a released
build always comes from a clean checkout rather than someone's laptop.

[firmware-build-docs]: https://github.com/caddis-tech/AquadronePicoFirmwareExperimental/blob/main/docs/firmware-build.md
[firmware-publish]: https://github.com/caddis-tech/AquadronePicoFirmwareExperimental/blob/main/.github/workflows/firmware-publish.yml
