"""Drone Setup — flash a Pico and provision its Pi, no terminal required.

Run with: python app/main.py
"""
from __future__ import annotations

import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

import extension_settings
import flash

REPO_ROOT = Path(__file__).resolve().parent.parent


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
        self.geometry("640x680")
        self.minsize(560, 560)

        self.uf2_path = tk.StringVar(value=str(DEFAULT_UF2) if DEFAULT_UF2 else "")

        self._build_flash_section()
        self._build_pi_section()
        self._build_log_section()

    # -- 1. Flash Pico ---------------------------------------------------
    def _build_flash_section(self):
        frame = ttk.LabelFrame(self, text="1. Flash Pico")
        frame.pack(fill="x", padx=10, pady=8)

        row = ttk.Frame(frame)
        row.pack(fill="x", padx=8, pady=6)
        ttk.Entry(row, textvariable=self.uf2_path).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Load .uf2 file...", command=self._pick_uf2).pack(side="left", padx=6)

        self.flash_button = ttk.Button(frame, text="Flash Pico", command=self._start_flash)
        self.flash_button.pack(padx=8, pady=(0, 8), anchor="w")

    def _pick_uf2(self):
        path = filedialog.askopenfilename(filetypes=[("Pico firmware", "*.uf2")])
        if path:
            self.uf2_path.set(path)

    def _start_flash(self):
        uf2 = Path(self.uf2_path.get())
        if not uf2.exists():
            messagebox.showerror("Drone Setup", f"File not found: {uf2}")
            return
        self.flash_button.config(state="disabled")
        threading.Thread(target=self._run_flash, args=(uf2,), daemon=True).start()

    def _run_flash(self, uf2: Path):
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
            if result.ok:
                self._log(
                    f"PASS — Pico is logging data. firmware_version: {result.firmware_version}"
                )
                self._log(
                    "Reconnect the Pico to the drone's Pi via USB, then continue below."
                )
            else:
                self._log(f"FAIL — {result.error}")
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
            ("Docker tag:", extension_settings.read_version()),
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
