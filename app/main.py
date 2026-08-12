"""Drone Setup — flash a Pico and provision its Pi, no terminal required.

Run with: python app/main.py
"""
from __future__ import annotations

import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk

import extension_settings
import firmware_manifest
import flash
from uf2 import is_test_firmware

REPO_ROOT = Path(__file__).resolve().parent.parent

# Downloaded firmware lands in the tech's own profile rather than beside the exe.
# DroneSetup.exe often sits somewhere unwritable (Program Files, a read-only share),
# and "the download failed" is a confusing way to find that out mid-job.
DOWNLOAD_DIR = Path.home() / ".aquadrone" / "firmware"


def _default_uf2() -> Path | None:
    """Where to look for a firmware image by default.

    From source: the repo's build/ output. As a frozen exe: alongside the .exe, so a
    tech can drop the firmware next to Drone-Setup.exe and have it auto-selected —
    the source-tree build/ path points into PyInstaller's temp extraction dir when
    frozen and would never exist.

    The picking itself lives in flash.find_firmware() (and is tested there); this
    only decides *where* to look."""
    root = (
        Path(sys.executable).resolve().parent
        if getattr(sys, "frozen", False)
        else REPO_ROOT / "build"
    )
    return flash.find_firmware(root)


DEFAULT_UF2 = _default_uf2()


class DroneSetupApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Drone Setup")
        self.geometry("640x760")
        self.minsize(560, 620)

        self.uf2_path = tk.StringVar(value=str(DEFAULT_UF2) if DEFAULT_UF2 else "")
        self.channel = tk.StringVar(value=firmware_manifest.CHANNEL_STABLE)
        self.selected_version = tk.StringVar()
        self._builds: dict[str, firmware_manifest.Build] = {}
        self._progress_decile = -1

        self._build_flash_section()
        self._build_pi_section()
        self._build_log_section()

    # -- 1. Flash Pico ---------------------------------------------------
    def _build_flash_section(self):
        frame = ttk.LabelFrame(self, text="1. Flash Pico")
        frame.pack(fill="x", padx=10, pady=8)

        row = ttk.Frame(frame)
        row.pack(fill="x", padx=8, pady=(6, 2))
        ttk.Label(row, text="Channel:", width=10).pack(side="left")
        ttk.Combobox(
            row,
            textvariable=self.channel,
            values=list(firmware_manifest.CHANNELS),
            state="readonly",
            width=14,
        ).pack(side="left")
        self.fetch_button = ttk.Button(
            row, text="Fetch versions", command=self._start_fetch_versions
        )
        self.fetch_button.pack(side="left", padx=6)

        row = ttk.Frame(frame)
        row.pack(fill="x", padx=8, pady=2)
        ttk.Label(row, text="Version:", width=10).pack(side="left")
        self.version_box = ttk.Combobox(
            row, textvariable=self.selected_version, values=[], state="disabled", width=24
        )
        self.version_box.pack(side="left")
        self.download_button = ttk.Button(
            row, text="Download", command=self._start_download, state="disabled"
        )
        self.download_button.pack(side="left", padx=6)

        row = ttk.Frame(frame)
        row.pack(fill="x", padx=8, pady=(6, 6))
        ttk.Entry(row, textvariable=self.uf2_path).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Load .uf2 file...", command=self._pick_uf2).pack(side="left", padx=6)

        self.flash_button = ttk.Button(frame, text="Flash Pico", command=self._start_flash)
        self.flash_button.pack(padx=8, pady=(0, 8), anchor="w")

    def _pick_uf2(self):
        path = filedialog.askopenfilename(filetypes=[("Pico firmware", "*.uf2")])
        if path:
            self.uf2_path.set(path)

    # -- Published firmware ------------------------------------------------
    #
    # Everything below runs its network work on a worker thread and touches widgets
    # only via self.after(), the same way _log does. Tk is not thread-safe, and a
    # widget updated straight from a worker fails intermittently rather than loudly.

    def _start_fetch_versions(self):
        channel = self.channel.get()
        self.fetch_button.config(state="disabled")
        self._log(f"Fetching published {channel} firmware versions...")
        threading.Thread(target=self._fetch_versions, args=(channel,), daemon=True).start()

    def _fetch_versions(self, channel: str):
        try:
            builds = firmware_manifest.load_builds(channel)
        except firmware_manifest.ManifestError as exc:
            self._log(f"FAIL - {exc}")
            builds = None
        except Exception as exc:  # noqa: BLE001 — surface any failure to the log panel
            self._log(f"FAIL - unexpected error fetching versions: {exc}")
            builds = None
        self.after(0, self._show_versions, channel, builds)

    def _show_versions(self, channel, builds):
        self.fetch_button.config(state="normal")
        if builds is None:
            # The failure is already in the log with its reason. Saying anything else
            # here would only bury it.
            return

        self._builds = {build.label: build for build in builds}
        labels = list(self._builds)
        self.version_box.config(values=labels, state="readonly" if labels else "disabled")
        self.download_button.config(state="normal" if labels else "disabled")
        self.selected_version.set(labels[0] if labels else "")
        if labels:
            self._log(f"{len(labels)} {channel} build(s) published. Newest: {labels[0]}")
        else:
            self._log(f"No {channel} builds have been published yet.")

    def _start_download(self):
        build = self._builds.get(self.selected_version.get())
        if build is None:
            messagebox.showerror("Drone Setup", "Pick a version first.")
            return
        self.fetch_button.config(state="disabled")
        self.download_button.config(state="disabled")
        self._progress_decile = -1
        self._log(f"Downloading {build.version} ({build.channel}) from {build.url}")
        threading.Thread(target=self._download, args=(build,), daemon=True).start()

    def _log_progress(self, received: int, total: int):
        """Log at each 10% mark. A line per 64KB chunk would drown the panel."""
        if total <= 0:
            return
        decile = received * 10 // total
        if decile != self._progress_decile:
            self._progress_decile = decile
            self._log(f"  {received * 100 // total}% ({received} of {total} bytes)")

    def _download(self, build: firmware_manifest.Build):
        try:
            path = firmware_manifest.download_build(
                build, DOWNLOAD_DIR, progress_cb=self._log_progress
            )
        except firmware_manifest.ManifestError as exc:
            self._log(f"FAIL - {exc}")
            self.after(0, self._download_finished, None)
            return
        except Exception as exc:  # noqa: BLE001 — surface any failure to the log panel
            self._log(f"FAIL - unexpected error downloading firmware: {exc}")
            self.after(0, self._download_finished, None)
            return

        # The last publishing guardrail, and the only one that runs on the tech's
        # machine. Unlike a file they browsed to, this is not a question: the test
        # image is published to no channel, so one arriving here has been signed by
        # our own key, which means the publishing pipeline itself is wrong. Flashing
        # it would be the worst possible way to find that out.
        #
        # Renamed rather than deleted. This file is the evidence of a serious failure
        # and throwing it away would leave nothing to investigate, but leaving a
        # flashable .uf2 sitting there is its own hazard. The new extension keeps it
        # out of the file dialog's filter and out of every .uf2 glob we have. Getting
        # the test image on purpose is unaffected: it is not downloadable at all, so
        # this is never the path a bench flash takes.
        try:
            if is_test_firmware(path):
                quarantined = path.with_name(path.name + ".rejected")
                path.replace(quarantined)
                self._log(
                    "FAIL - the downloaded image is the hardware TEST build, which "
                    "fakes SD card writes. Nothing was flashed."
                )
                self._log(
                    f"  Kept for investigation at {quarantined}, renamed so it cannot "
                    "be flashed by accident."
                )
                self._log(
                    "  Report this: the release channel must never carry that image, "
                    "so something is wrong with publishing, not with this laptop."
                )
                self.after(0, self._download_finished, None)
                return
        except OSError as exc:
            self._log(f"FAIL - could not check what kind of firmware was downloaded: {exc}")
            self.after(0, self._download_finished, None)
            return

        self._log(f"Verified against the signed checksum. Saved to {path}")
        self.after(0, self._download_finished, path)

    def _download_finished(self, path):
        self.fetch_button.config(state="normal")
        self.download_button.config(state="normal")
        if path is not None:
            self.uf2_path.set(str(path))

    def _start_flash(self):
        uf2 = Path(self.uf2_path.get())
        if not uf2.exists():
            messagebox.showerror("Drone Setup", f"File not found: {uf2}")
            return

        try:
            flashing_test_firmware = is_test_firmware(uf2)
        except OSError as exc:
            # Could not run the check. Not the same as passing it, so stop rather than
            # flash something we were unable to identify.
            messagebox.showerror(
                "Drone Setup",
                f"Could not read {uf2.name} to check what kind of firmware it is:\n\n"
                f"{exc}\n\nNothing was flashed.",
            )
            return

        if flashing_test_firmware and not self._confirm_test_firmware(uf2):
            return

        self.flash_button.config(state="disabled")
        threading.Thread(
            target=self._run_flash, args=(uf2, flashing_test_firmware), daemon=True
        ).start()

    def _confirm_test_firmware(self, uf2: Path) -> bool:
        """Ask before writing the bench test image. True if flashing should proceed.

        Catching this before the flash rather than after is the point. The post-flash
        check still refuses to call it a good production flash, but by then the wrong
        firmware is already on the board and someone has to notice and redo it.

        Deliberately a question and not a refusal: flashing the HIL image onto a bench
        board is a normal, expected thing to do, and it is why the image exists. The
        answer is carried through to _run_flash so a confirmed flash is reported as
        the success it is rather than as a failure.

        Typed rather than clicked. Yes/no is one keystroke, and this is exactly the
        prompt somebody flashing boards all afternoon stops reading. What it guards
        against is a drone that logs nothing to its card while reporting perfectly
        healthy telemetry, which nobody catches until the data is missing.
        """
        self._log(f"WARNING: {uf2.name} is the hardware test (HIL) firmware.")
        answer = simpledialog.askstring(
            "Bench test firmware",
            f"{uf2.name} is the hardware TEST firmware, not drone firmware.\n\n"
            "It can be told to fake SD card writes. A drone carrying it sends "
            "telemetry that looks completely normal while nothing at all is recorded "
            "to the card, so the loss is invisible until someone goes looking for "
            "the data.\n\n"
            "Only flash this on a bench board you are testing. Never on a drone that "
            "is going out.\n\n"
            f"If this is a bench board, type {flash.BENCH_CONFIRMATION} to confirm:",
            parent=self,
        )
        if not flash.is_bench_confirmation(answer):
            self._log("Not confirmed. Nothing was flashed.")
            return False

        self._log(f"Confirmed as a bench board. Flashing {uf2.name}.")
        return True

    def _run_flash(self, uf2: Path, flashing_test_firmware: bool = False):
        try:
            self._log(
                "Looking for a Pico in BOOTSEL mode (hold the white button while plugging in)..."
            )
            drive = flash.find_bootsel_drive()
            if drive is None:
                self._log("FAIL: no RPI-RP2 drive found within 30s.")
                return
            # Snapshot now, not earlier: the target board is in BOOTSEL at this point
            # and so has no serial port, meaning every Pico port we can see belongs to
            # some *other* board. Whatever appears after the flash is ours.
            before_ports = flash.pico_serial_ports()
            self._log(f"Found BOOTSEL drive at {drive}. Copying firmware...")
            flash.flash_uf2(uf2, drive)
            self._log("Copied. Waiting for the Pico to reboot and reconnect...")
            port = flash.find_pico_serial_port(before_ports)
            if port is None:
                self._log("FAIL: Pico did not reappear as a serial port after flashing.")
                return
            self._log(f"Pico back on {port}. Reading telemetry to verify...")
            result = flash.verify_logging_data(port)
            for line in result.raw_lines:
                self._log(f"  {line}")
            went_as_intended, message = flash.describe_outcome(result, flashing_test_firmware)
            self._log(message)
            if went_as_intended and not flashing_test_firmware:
                # Only a production board goes on to step 2. A bench board stays on
                # the bench, so pointing it at a drone's Pi would be the wrong advice.
                self._log(
                    "Reconnect the Pico to the drone's Pi via USB, then continue below."
                )
        except Exception as exc:  # noqa: BLE001 — surface any failure to the log panel
            self._log(f"FAIL — unexpected error: {exc}")
        finally:
            self.flash_button.config(state="normal")

    # -- 2. BlueOS Extension Settings ---------------------------------------
    def _build_pi_section(self):
        frame = ttk.LabelFrame(self, text="2. BlueOS Extension Settings")
        frame.pack(fill="x", padx=10, pady=8)

        # Kraken (BlueOS's Extensions Manager) owns install, update, restart,
        # and persisting the token — all through BlueOS's own control panel.
        # Nothing here touches the Pi: these are read-only fields to copy
        # alongside the generated settings JSON below.
        for label, value in (
            ("Extension Identifier:", extension_settings.EXTENSION_IDENTIFIER),
            ("Extension Name:", extension_settings.EXTENSION_NAME),
            ("Docker image:", extension_settings.DOCKER_IMAGE),
            ("Docker tag:", extension_settings.read_extension_version()),
        ):
            row = ttk.Frame(frame)
            row.pack(fill="x", padx=8, pady=2)
            ttk.Label(row, text=label, width=26).pack(side="left")
            field = ttk.Entry(row)
            field.insert(0, value)
            field.config(state="readonly")
            field.pack(side="left", fill="x", expand=True)

        self.token = tk.StringVar()
        self._labeled_entry(frame, "Token:", self.token, show="*")

        ttk.Label(
            frame,
            text=(
                "Paste all of the above into BlueOS -> Extensions -> "
                "INSTALLED -> + . No SSH needed — Kraken persists the token "
                "across restarts and updates."
            ),
            wraplength=560,
            justify="left",
        ).pack(fill="x", padx=8, pady=(6, 4), anchor="w")

        ttk.Button(
            frame, text="Generate Settings JSON", command=self._generate_settings
        ).pack(padx=8, pady=(0, 4), anchor="w")

        self.settings_text = scrolledtext.ScrolledText(
            frame, height=4, state="disabled", wrap="word"
        )
        self.settings_text.pack(fill="x", padx=8, pady=(0, 8))

    def _labeled_entry(self, parent, label, var, show=None):
        row = ttk.Frame(parent)
        row.pack(fill="x", padx=8, pady=3)
        ttk.Label(row, text=label, width=26).pack(side="left")
        ttk.Entry(row, textvariable=var, show=show or "").pack(side="left", fill="x", expand=True)

    def _generate_settings(self):
        token = self.token.get().strip()
        if not token:
            messagebox.showerror("Drone Setup", "Token is required.")
            return
        try:
            settings_json = extension_settings.build_install_settings(token)
        except Exception as exc:  # noqa: BLE001 — a broken bundled Dockerfile, surface it plainly
            messagebox.showerror("Drone Setup", f"Could not build settings: {exc}")
            return

        self.settings_text.config(state="normal")
        self.settings_text.delete("1.0", "end")
        self.settings_text.insert("1.0", settings_json)
        self.settings_text.config(state="disabled")

        self.clipboard_clear()
        self.clipboard_append(settings_json)
        self._log("Settings JSON generated and copied to the clipboard.")

    # -- Log ---------------------------------------------------------------
    def _build_log_section(self):
        frame = ttk.LabelFrame(self, text="Log")
        frame.pack(fill="both", expand=True, padx=10, pady=8)
        self.log_text = scrolledtext.ScrolledText(frame, height=16, state="disabled")
        self.log_text.pack(fill="both", expand=True, padx=8, pady=8)

    def _log(self, message: str):
        def append():
            self.log_text.config(state="normal")
            self.log_text.insert("end", message + "\n")
            self.log_text.see("end")
            self.log_text.config(state="disabled")
        self.after(0, append)


if __name__ == "__main__":
    DroneSetupApp().mainloop()
