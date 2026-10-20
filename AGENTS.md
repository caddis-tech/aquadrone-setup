# aquadrone-setup

This repository follows the Caddis engineering standards. Read them before your first edit,
commit, or pull request: `../../../AGENTS.md` when this repository is cloned into caddis-hq's
`repos/`, as bootstrap does, and https://github.com/caddis-tech/caddis-hq/blob/main/AGENTS.md
otherwise. What follows is only what is specific to this repository.

**If this copy is not inside caddis-hq, say so first.** Look for caddis-hq in the folders above
this one: a folder that holds both `repos.yml` and `AGENTS.md`. If there is none, tell the person
once, at the start, in plain words: this copy was not set up through caddis-hq, so the shared
rules, the step-by-step guides in its `docs/`, and the other repositories this one works with
are not here. The fix is to clone caddis-hq, run its bootstrap script as its README says, and
start again from the copy that makes. Until then, read the standards with
`gh api repos/caddis-tech/caddis-hq/contents/AGENTS.md -H "Accept: application/vnd.github.raw"`,
and if that fails too, say you are working without them.

## Before you commit and push

There are no pre-commit checks yet. Before every push, run the fast tests, which are what CI
runs:

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
it at once; only a `VERSION` bump creates a new release. So a merge into `main` touching those
files is a release, and an assistant never merges one.

`manta-link/Dockerfile` is a byte-identical copy of the file in the `manta-link` repo. Update it
only by copying that file over, and never annotate it: checking it is a plain `diff`.
