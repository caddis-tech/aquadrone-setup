# aquadrone-setup

This repository follows the Caddis engineering standards. Read them before your first edit,
commit, or pull request: `../../../AGENTS.md` when this repository is cloned into caddis-hq's
`repos/`, as bootstrap does, and https://github.com/caddis-tech/caddis-hq/blob/main/AGENTS.md
otherwise. What follows is only what is specific to this repository.

## Before you push

Run what CI runs:

```bash
pip install -r app/requirements.txt
pip install pytest
python -m pytest
```

The tests are pure logic: no network, no hardware. A pull request touching `app/`,
`DroneSetup.spec`, or `VERSION` also builds `DroneSetup.exe` with PyInstaller on Windows, so a
broken build shows in review.

## How it ships

The released exe is the product: technicians download `/releases/latest` and run it. Releases
publish from `main` and are named from `VERSION`. A merge to `main` touching `app/`,
`DroneSetup.spec`, or `VERSION` re-uploads the exe to the existing release, so technicians get
it at once; only a `VERSION` bump creates a new release.

`manta-link/Dockerfile` is a byte-identical copy of the file in the `manta-link` repo. Update it
only by copying that file over, and never annotate it: checking it is a plain `diff`.
