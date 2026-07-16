"""Drone Setup — flash a Pico and provision its Pi, no terminal required.

Run with: python app/main.py
"""
from __future__ import annotations

import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

import flash
import pi_setup

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

    # -- 2. Setup Pi -------------------------------------------------------
    def _build_pi_section(self):
        frame = ttk.LabelFrame(self, text="2. Setup Pi")
        frame.pack(fill="x", padx=10, pady=8)

        self.pi_ip = tk.StringVar()
        self.pi_user = tk.StringVar(value="pi")
        self.pi_password = tk.StringVar()
        self.drone_name = tk.StringVar()
        self.token = tk.StringVar()

        self._labeled_entry(frame, "Pi IP:", self.pi_ip)
        self._labeled_entry(frame, "Pi user:", self.pi_user)
        self._labeled_entry(frame, "Pi password (optional, one-time):", self.pi_password, show="*")
        self._labeled_entry(frame, "Drone name (Drone-<Name>):", self.drone_name)
        self._labeled_entry(frame, "Token:", self.token, show="*")

        btn_row = ttk.Frame(frame)
        btn_row.pack(fill="x", padx=8, pady=8, anchor="w")
        self.deploy_button = ttk.Button(btn_row, text="Deploy", command=self._start_deploy)
        self.deploy_button.pack(side="left")
        # The common field task is rotating just the token on a drone that's
        # already provisioned. A full Deploy (git pull + pip + restart) is
        # overkill for that and regenerates config; this does the minimal,
        # non-destructive token update instead (needs only Pi IP + token).
        self.token_button = ttk.Button(
            btn_row, text="Update Token Only", command=self._start_update_token
        )
        self.token_button.pack(side="left", padx=(8, 0))

    def _labeled_entry(self, parent, label, var, show=None):
        row = ttk.Frame(parent)
        row.pack(fill="x", padx=8, pady=3)
        ttk.Label(row, text=label, width=26).pack(side="left")
        ttk.Entry(row, textvariable=var, show=show or "").pack(side="left", fill="x", expand=True)

    def _start_deploy(self):
        pi_ip = self.pi_ip.get().strip()
        drone_name = self.drone_name.get().strip()
        token = self.token.get().strip()
        if not pi_ip or not drone_name or not token:
            messagebox.showerror("Drone Setup", "Pi IP, drone name, and token are all required.")
            return
        self._set_pi_buttons(state="disabled")
        threading.Thread(
            target=self._run_deploy,
            args=(pi_ip, self.pi_user.get().strip() or "pi", drone_name, token,
                  self.pi_password.get()),
            daemon=True,
        ).start()

    def _run_deploy(self, pi_ip, pi_user, drone_name, token, pi_password):
        try:
            pi_setup.deploy(
                pi_ip, pi_user, drone_name, token, log=self._log,
                pi_password=pi_password or None,
            )
        except Exception as exc:  # noqa: BLE001
            self._log(f"FAIL — {exc}")
        finally:
            self._set_pi_buttons(state="normal")

    def _start_update_token(self):
        pi_ip = self.pi_ip.get().strip()
        token = self.token.get().strip()
        # Token rotation needs only the target Pi and the new token — not the
        # drone name (that's cosmetic) — so validate just those two.
        if not pi_ip or not token:
            messagebox.showerror("Drone Setup", "Pi IP and token are required to update the token.")
            return
        self._set_pi_buttons(state="disabled")
        threading.Thread(
            target=self._run_update_token,
            args=(pi_ip, self.pi_user.get().strip() or "pi", token, self.pi_password.get()),
            daemon=True,
        ).start()

    def _run_update_token(self, pi_ip, pi_user, token, pi_password):
        try:
            pi_setup.update_token(
                pi_ip, pi_user, token, log=self._log, pi_password=pi_password or None
            )
        except Exception as exc:  # noqa: BLE001
            self._log(f"FAIL — {exc}")
        finally:
            self._set_pi_buttons(state="normal")

    def _set_pi_buttons(self, state):
        """Enable/disable both Pi-action buttons together so a second action
        can't start while one is mid-flight over SSH."""
        self.deploy_button.config(state=state)
        self.token_button.config(state=state)

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
