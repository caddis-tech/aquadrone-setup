"""Flash a Pico with a .uf2 file and verify it's emitting telemetry.

Windows-only: BOOTSEL drive detection uses the Win32 GetVolumeInformationW
API directly (via ctypes) so the app has no extra dependency beyond pyserial.
"""
from __future__ import annotations

import ctypes
import json
import shutil
import string
import time
from dataclasses import dataclass, field
from pathlib import Path

import serial
from serial.tools import list_ports

BOOTSEL_LABEL = "RPI-RP2"
BAUD_RATE = 115200
REQUIRED_KEYS = ("firmware_version", "water_temperature", "ph", "uv_banks")

DRIVE_REMOVABLE = 2
DRIVE_FIXED = 3

# Suppress the classic "insert disk" popup when probing empty removable drives.
SEM_FAILCRITICALERRORS = 0x0001
ctypes.windll.kernel32.SetErrorMode(SEM_FAILCRITICALERRORS)


def _volume_label(root: str) -> str | None:
    if ctypes.windll.kernel32.GetDriveTypeW(root) not in (DRIVE_REMOVABLE, DRIVE_FIXED):
        return None
    label_buf = ctypes.create_unicode_buffer(261)
    ok = ctypes.windll.kernel32.GetVolumeInformationW(
        ctypes.c_wchar_p(root), label_buf, ctypes.sizeof(label_buf),
        None, None, None, None, 0,
    )
    return label_buf.value if ok else None


def find_bootsel_drive(timeout_s: float = 30.0, poll_interval_s: float = 1.0) -> Path | None:
    """Poll drive letters for one labeled RPI-RP2 (the Pico's BOOTSEL mass-storage drive)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for letter in string.ascii_uppercase:
            root = f"{letter}:\\"
            if _volume_label(root) == BOOTSEL_LABEL:
                return Path(root)
        time.sleep(poll_interval_s)
    return None


def flash_uf2(uf2_path: Path, drive: Path) -> None:
    shutil.copy(uf2_path, drive / uf2_path.name)


def current_serial_ports() -> set[str]:
    return {p.device for p in list_ports.comports()}


def find_new_serial_port(before: set[str], timeout_s: float = 15.0, poll_interval_s: float = 0.5) -> str | None:
    """Diff the serial port list against a prior snapshot to find the Pico's port after reboot."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        new_ports = current_serial_ports() - before
        if new_ports:
            return sorted(new_ports)[0]
        time.sleep(poll_interval_s)
    return None


@dataclass
class LoggingCheckResult:
    ok: bool
    firmware_version: str | None
    raw_lines: list[str] = field(default_factory=list)
    error: str | None = None


def verify_logging_data(port: str, lines_to_check: int = 5, timeout_s: float = 20.0) -> LoggingCheckResult:
    """Open the freshly-flashed Pico's serial port and confirm it emits valid telemetry JSON."""
    raw_lines: list[str] = []
    try:
        with serial.Serial(port, BAUD_RATE, timeout=timeout_s) as ser:
            deadline = time.monotonic() + timeout_s
            firmware_version = None
            while len(raw_lines) < lines_to_check and time.monotonic() < deadline:
                raw = ser.readline()
                if not raw:
                    continue
                text = raw.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                raw_lines.append(text)
                try:
                    record = json.loads(text)
                except json.JSONDecodeError:
                    continue
                missing = [k for k in REQUIRED_KEYS if k not in record]
                if missing:
                    return LoggingCheckResult(False, None, raw_lines, f"Record missing keys: {missing}")
                firmware_version = record.get("firmware_version")
            if len(raw_lines) < lines_to_check:
                return LoggingCheckResult(False, firmware_version, raw_lines, "Timed out waiting for enough records")
            return LoggingCheckResult(True, firmware_version, raw_lines)
    except serial.SerialException as exc:
        return LoggingCheckResult(False, None, raw_lines, f"Serial error: {exc}")
