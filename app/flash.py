"""Flash a Pico with a .uf2 file and verify it's emitting telemetry.

Windows-only: BOOTSEL drive detection uses the Win32 GetVolumeInformationW
API directly (via ctypes) so the app has no extra dependency beyond pyserial.
"""
from __future__ import annotations

import ctypes
import json
import shutil
import string
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import serial
from serial.tools import list_ports

BOOTSEL_LABEL = "RPI-RP2"
BAUD_RATE = 115200
REQUIRED_KEYS = ("firmware_version", "water_temperature", "ph", "uv_banks")
PICO_USB_VID = 0x2E8A  # Raspberry Pi Trading Ltd — same VID whether in BOOTSEL or running firmware

# Firmware artifacts are named for the version inside them (AquaD_Pico_v1.0.3.uf2)
# by CMakeLists.txt's OUTPUT_NAME. This glob has to stay in step with that naming:
# if the two ever drift, the app silently finds no firmware and every tech has to
# browse for the file by hand. tests/test_release_consistency.py pins them together.
FIRMWARE_GLOB = "AquaD_Pico_v*.uf2"

DRIVE_REMOVABLE = 2
DRIVE_FIXED = 3

# Suppress the classic "insert disk" popup when probing empty removable drives.
SEM_FAILCRITICALERRORS = 0x0001
# BOOTSEL detection is Windows-only (Win32 API via ctypes). Guard the
# import-time call so this module still imports on Linux/CI for unit-testing
# the pure logic below (evaluate_telemetry); the flashing functions themselves
# stay Windows-only and will fail if actually invoked off-Windows.
if sys.platform == "win32":
    ctypes.windll.kernel32.SetErrorMode(SEM_FAILCRITICALERRORS)


def find_firmware(search_dir: Path) -> Path | None:
    """The newest firmware image in search_dir, or None if there isn't one.

    Deliberately NOT a bare *.uf2 glob. flash_nuke.uf2 — the utility that erases a
    Pico's flash — is a very ordinary thing to have sitting in the same folder as
    Pico tooling, and auto-selecting it would wipe the board instead of programming
    it. Only files that match our own versioned naming are ever offered up.

    Newest by mtime rather than by highest version string: a tech who just dropped a
    build into the folder means *that* build, even when it's a deliberate downgrade
    to chase a regression. Anything else, they pick by hand.
    """
    builds = sorted(
        search_dir.glob(FIRMWARE_GLOB),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return builds[0] if builds else None


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


def pico_serial_ports() -> set[str]:
    """Serial ports belonging to a Pico that is running firmware (USB Vendor ID match).

    A Pico held in BOOTSEL is a mass-storage device and exposes no serial port, so
    the board we're about to flash never shows up here. That's what makes a snapshot
    taken while it's in BOOTSEL a clean way to identify every *other* Pico attached.
    """
    return {str(p.device) for p in list_ports.comports() if p.vid == PICO_USB_VID}


def find_pico_serial_port(
    before: set[str], timeout_s: float = 40.0, poll_interval_s: float = 0.5
) -> str | None:
    """Wait for the freshly-flashed Pico to reappear as a serial port.

    `before` must be a pico_serial_ports() snapshot taken while the target board was
    still in BOOTSEL. Any Pico port appearing after that is the board we just flashed;
    anything already in `before` is a *different* Pico — a second unit in a batch, an
    old board, or a Debug Probe, all of which share VID 0x2E8A — and verifying that
    one instead would report PASS for firmware we never wrote.

    Filtering by VID rather than diffing the whole port list is the other half of this:
    Windows routinely hands the same COM number back to the same physical Pico after a
    reflash, so a naive "any new port" diff finds nothing even though it reconnected.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        new_ports = pico_serial_ports() - before
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


def evaluate_telemetry(raw_lines: list[str]) -> LoggingCheckResult:
    """Decide pass/fail from collected serial lines. Pure (no I/O), so it's
    unit-testable without a physical Pico.

    A pass requires at least one line that parses as JSON AND carries every
    REQUIRED_KEY. The previous version passed as long as it had collected
    enough *lines* of any kind, so a Pico spewing boot noise or non-JSON
    garbage verified as "logging data" with firmware_version=None and a tech
    could ship a bad flash. A record that parses but is missing keys means the
    wrong/old firmware is on the board -> hard fail with the offending keys.
    """
    firmware_version = None
    saw_valid_record = False
    for text in raw_lines:
        try:
            record = json.loads(text)
        except json.JSONDecodeError:
            continue
        missing = [k for k in REQUIRED_KEYS if k not in record]
        if missing:
            return LoggingCheckResult(
                False, record.get("firmware_version"), raw_lines, f"Record missing keys: {missing}"
            )
        firmware_version = record.get("firmware_version")
        saw_valid_record = True
    if not saw_valid_record:
        return LoggingCheckResult(
            False, None, raw_lines, "No valid telemetry JSON seen — only unparseable serial output"
        )
    return LoggingCheckResult(True, firmware_version, raw_lines)


def verify_logging_data(
    port: str, lines_to_check: int = 3, timeout_s: float = 40.0
) -> LoggingCheckResult:
    """Open the freshly-flashed Pico's serial port, sample a few lines, and
    confirm it emits valid telemetry JSON (see evaluate_telemetry).

    The firmware sleeps ~5s at boot (SD card init) before its first record, then
    emits one record every 5s (POLL_INTERVAL_US in my_project.c) — 3 records needs
    roughly 5 + 3*5 = 20s worst case; 40s leaves comfortable margin.
    """
    raw_lines: list[str] = []
    try:
        with serial.Serial(port, BAUD_RATE, timeout=timeout_s) as ser:
            deadline = time.monotonic() + timeout_s
            while len(raw_lines) < lines_to_check and time.monotonic() < deadline:
                raw = ser.readline()
                if not raw:
                    continue
                text = raw.decode("utf-8", errors="replace").strip()
                if text:
                    raw_lines.append(text)
    except serial.SerialException as exc:
        return LoggingCheckResult(False, None, raw_lines, f"Serial error: {exc}")
    if not raw_lines:
        return LoggingCheckResult(False, None, raw_lines, "No serial output within timeout")
    return evaluate_telemetry(raw_lines)
