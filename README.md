# Aquadrone Setup

[![Download DroneSetup.exe](https://img.shields.io/badge/Download-DroneSetup.exe-2ea44f?style=for-the-badge)](https://github.com/caddis-tech/aquadrone-setup/releases/latest/download/DroneSetup.exe)

`DroneSetup.exe` — the Windows field tool for bringing up an Aquadrone unit:
flash a Pico with published firmware, and generate the drone's BlueOS Extension
settings. No terminal, no Python install, no repo clone.

Field techs want the badge above. Everything below is for people working on the
tool itself.

## Layout

| Path | What it is |
| --- | --- |
| `app/` | The Tkinter application. [`app/README.md`](app/README.md) is the detailed guide — UI walkthrough, firmware channels, and what gets verified before anything is flashed. |
| `tests/` | `pytest` suite. Pure logic only; no network, no hardware. |
| `DroneSetup.spec` | PyInstaller build definition. |
| `VERSION` | The exe's version. Bumping it on `main` mints a new GitHub Release. |
| `docs/` | Operational notes for the drone setup process. |
| `manta-link/Dockerfile` | **Vendored copy, see below.** Not buildable here. |

### The vendored `manta-link/Dockerfile`

`app/extension_settings.py` builds the BlueOS install settings by parsing two
`LABEL`s straight out of MANTA Link's Dockerfile rather than duplicating them as
constants: `permissions` (the container's access) and `version` (the Docker tag).

Generating the permissions block is the point of the tool. **Kraken does not fall
back to the image's own `permissions` LABEL**: installing with Custom settings
left empty stores `{}`, and MANTA Link then starts with no `/dev` bind, no host
networking, and no persistent volume. It reports "no API token configured",
"connection refused", and "no Pico present", three symptoms that look like
unrelated bugs and name nothing.

The tag is read from the `version` LABEL and **not** from the root `VERSION`,
which is the exe's own version and moves independently. This tool was at 1.2.0
while MANTA Link was at 0.9.0, so using `VERSION` as the tag sends a tech to
install an image that was never published.

MANTA Link lives in the `manta-link` repo, so this is a **byte-identical vendored
copy**, kept unannotated so checking it is a plain `diff`. It cannot be built
here: its build context is that repo's root and it `COPY`s sources that do not
exist in this repo. `DroneSetup.spec` bundles it into the exe.

**If MANTA Link's `permissions` or `version` LABEL changes, this copy must be
updated.** Nothing currently enforces that. The intended fix is a drift check in
the `manta-link` repo (which can read both) asserting its `Dockerfile` matches
this one.

## Developing

```
pip install -r app/requirements.txt
python app/main.py
```

```
pip install pytest
pytest
```

## Releases

[`.github/workflows/drone-setup-release.yml`](.github/workflows/drone-setup-release.yml)
builds the exe on every PR touching `app/`, `DroneSetup.spec`, or `VERSION`, so a
broken PyInstaller build is caught in review. It publishes only from `main`,
naming the release after `VERSION` — so an ordinary merge re-uploads the asset
and only a `VERSION` bump creates a new release.

## Related repositories

| Repo | Role |
| --- | --- |
| [`aquadrone-firmware-releases`](https://github.com/caddis-tech/aquadrone-firmware-releases) | Public distribution endpoint. Compiled `.uf2` builds plus a signed `manifest.json`. This is what the app downloads from. Not source. |
| `AquadronePicoFirmware` | Private. The Pico firmware source. |
| `manta-link` | The Pi-side BlueOS Extension this tool provisions. Source of the vendored `Dockerfile`. |
| `AquadronePicoFirmwareExperimental` | Private, frozen. Where this tool used to live, alongside the old Pi bridge that `manta-link` replaced. |

**No credential ships in the exe.** It carries only the public half of the
Ed25519 key used to verify the firmware manifest; the private half exists solely
as an Actions secret on the firmware repo. Firmware downloads are anonymous over
HTTPS, and every build is checked against the SHA-256 in the signed manifest
before it reaches a Pico. See [`app/README.md`](app/README.md) for the full chain.
