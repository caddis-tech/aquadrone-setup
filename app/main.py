"""Drone Setup — flash a Pico and provision its Pi, no terminal required.

Run with: python app/main.py
"""
from __future__ import annotations

import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

import flash
import pi_setup

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_UF2 = REPO_ROOT / "build" / "my_project.uf2"


class DroneSetupApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Drone Setup")
        self.geometry("640x680")
        self.minsize(560, 560)

        self.uf2_path = tk.StringVar(value=str(DEFAULT_UF2) if DEFAULT_UF2.exists() else "")

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
            self._log("Looking for a Pico in BOOTSEL mode (hold the white button while plugging in)...")
            before_ports = flash.current_serial_ports()
            drive = flash.find_bootsel_drive()
            if drive is None:
                self._log("FAIL: no RPI-RP2 drive found within 30s.")
                return
            self._log(f"Found BOOTSEL drive at {drive}. Copying firmware...")
            flash.flash_uf2(uf2, drive)
            self._log("Copied. Waiting for the Pico to reboot and reconnect...")
            port = flash.find_new_serial_port(before_ports)
            if port is None:
                self._log("FAIL: Pico did not reappear as a serial port after flashing.")
                return
            self._log(f"Pico back on {port}. Reading telemetry to verify...")
            result = flash.verify_logging_data(port)
            for line in result.raw_lines:
                self._log(f"  {line}")
            if result.ok:
                self._log(f"PASS — Pico is logging data. Reported firmware_version: {result.firmware_version}")
                self._log("Reconnect the Pico to the drone's Pi via USB, then continue below.")
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
        self.drone_name = tk.StringVar()
        self.token = tk.StringVar()

        self._labeled_entry(frame, "Pi IP:", self.pi_ip)
        self._labeled_entry(frame, "Pi user:", self.pi_user)
        self._labeled_entry(frame, "Drone name (Drone-<Name>):", self.drone_name)
        self._labeled_entry(frame, "Token:", self.token, show="*")

        self.deploy_button = ttk.Button(frame, text="Deploy", command=self._start_deploy)
        self.deploy_button.pack(padx=8, pady=8, anchor="w")

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
        self.deploy_button.config(state="disabled")
        threading.Thread(
            target=self._run_deploy,
            args=(pi_ip, self.pi_user.get().strip() or "pi", drone_name, token),
            daemon=True,
        ).start()

    def _run_deploy(self, pi_ip, pi_user, drone_name, token):
        try:
            pi_setup.deploy(pi_ip, pi_user, drone_name, token, log=self._log)
        except Exception as exc:  # noqa: BLE001
            self._log(f"FAIL — {exc}")
        finally:
            self.deploy_button.config(state="normal")

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
