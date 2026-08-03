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
| `bridge/Dockerfile` | **Vendored copy — see below.** Not buildable here. |

### The vendored `bridge/Dockerfile`

`app/extension_settings.py` builds the BlueOS install settings by parsing the
`LABEL permissions=` block straight out of the bridge's Dockerfile, deliberately
rather than duplicating it as a constant — a malformed permissions block installs
cleanly and then looks like a hardware fault, so the app reads the real thing.
`DroneSetup.spec` bundles that file into the exe.

The bridge itself lives in the private `AquadronePicoFirmwareExperimental` repo,
so this is a **byte-identical vendored copy**, kept unannotated so checking it is
a plain `diff`. It cannot be built here — its build context is that repo's root
and it `COPY`s bridge sources that do not exist in this repo.

**If the bridge's permissions LABEL changes, this copy must be updated.** Nothing
currently enforces that. The intended fix is a drift check in the private repo
(which can read both) asserting its `bridge/Dockerfile` matches this one.

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
| `AquadronePicoFirmwareExperimental` | Private, frozen. Where this tool used to live, alongside the Pi bridge. |

**No credential ships in the exe.** It carries only the public half of the
Ed25519 key used to verify the firmware manifest; the private half exists solely
as an Actions secret on the firmware repo. Firmware downloads are anonymous over
HTTPS, and every build is checked against the SHA-256 in the signed manifest
before it reaches a Pico. See [`app/README.md`](app/README.md) for the full chain.
