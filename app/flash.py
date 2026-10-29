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
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import serial
from serial.tools import list_ports
from uf2 import HIL_MARKER

BOOTSEL_LABEL = "RPI-RP2"
BAUD_RATE = 115200

# Firmware 2.1 and later writes one JSON record per line and says what each one is in
# "type". Only a reading carries sensor values: the "boot" snapshot written once per
# run does not, and record kinds added later are not this check's business.
READING_RECORD_TYPE = "reading"

# The version, and one key per sensor the check has always covered, named as
# record_json.c in the firmware repo renders them. A reading carries every one, null
# when a sensor did not answer, so a missing key means a different record format
# rather than a dead sensor.
REQUIRED_KEYS = ("firmware_version", "temperature", "ph", "uv_present")

# A record with no "type" is held to what this check required before 2.1, so a board
# flashed with 1.2.0, which the published list still offers, is judged as it was. No
# 2.0 build writes parseable JSON to serial, so a 2.0 board never passed and cannot.
LEGACY_REQUIRED_KEYS = ("firmware_version", "water_temperature", "ph", "uv_banks")

# Before its first record the board asks for the time, and a production image waits
# up to 180 s for the drone's Pi to answer. No Pi is attached here, so the check
# answers from this computer's clock. The wire format is boot_time.h's.
TIME_REQUEST = "TIME?"

# What the board prints when it accepts an answer. A board that printed it and then
# asked again reset in between; it did not refuse this computer's clock.
TIME_ACCEPTED = "Boot time synced"

# A Debug image writes the same records as a Release one and is told apart only by
# the file its banner names, debug_logN.txt rather than dataN.txt. The banner can
# go by before the port opens, so this catches a Debug image only when it is seen.
DEBUG_IMAGE_BANNER = "logging to debug_log"

# Serial timeout, per byte when reading and per call when writing. Short, so a quiet
# port hands control back to the deadline in read_telemetry_lines() every second.
_PORT_TIMEOUT_S = 1.0

PICO_USB_VID = 0x2E8A  # Raspberry Pi Trading Ltd — same VID whether in BOOTSEL or running firmware

# Firmware artifacts are named for the version inside them (AquaD_Pico_v1.0.3.uf2)
# by CMakeLists.txt's OUTPUT_NAME. This glob has to stay in step with that naming:
# if the two ever drift, the app silently finds no firmware and every tech has to
# browse for the file by hand. tests/test_release_consistency.py pins them together.
#
# Deliberately NOT the same constant as build_manifest.PUBLISHABLE_GLOB, which happens
# to have the same value today. That one decides what CI is allowed to publish; this
# one decides what the app auto-selects off a local disk. Two decisions, and a future
# reason to change one of them should not silently change the other.
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
    warning: str | None = None


def _parse_record(text: str) -> dict[str, Any] | None:
    """The line as a JSON object, or None for anything else the board prints.

    The banner, the SD card status lines and TIME? share the port with the records,
    and a line that happens to parse as a bare number is not a record either.
    """
    try:
        record = json.loads(text)
    # ValueError covers JSONDecodeError and the integer digit limit; a garbled line
    # of nested brackets raises RecursionError instead.
    except (ValueError, RecursionError):
        return None
    return record if isinstance(record, dict) else None


def _required_keys(record: dict[str, Any]) -> tuple[str, ...] | None:
    """The keys this record must carry, or None for one that holds no readings."""
    record_type = record.get("type")
    if record_type is None:
        return LEGACY_REQUIRED_KEYS
    if record_type == READING_RECORD_TYPE:
        return REQUIRED_KEYS
    return None


def _carries_readings(text: str) -> bool:
    record = _parse_record(text)
    return record is not None and _required_keys(record) is not None


def evaluate_telemetry(raw_lines: list[str]) -> LoggingCheckResult:
    """Decide pass/fail from collected serial lines. Pure (no I/O), so it's
    unit-testable without a physical Pico.

    A pass requires at least one record of readings carrying every key its format
    requires. The previous version passed as long as it had collected enough
    *lines* of any kind, so a Pico spewing boot noise or non-JSON garbage verified
    as "logging data" with firmware_version=None and a tech could ship a bad flash.
    A record of readings that is missing keys means the wrong firmware is on the
    board -> hard fail with the offending keys.
    """
    firmware_version = None
    card_warning = None
    saw_reading = False
    for text in raw_lines:
        record = _parse_record(text)
        if record is None:
            continue
        reported = str(record.get("firmware_version") or "")
        if HIL_MARKER in reported:
            # Checked first, and on every kind of record, because this is the more
            # serious answer and because a HIL board satisfies every other check here:
            # valid JSON, every required key, plausible readings. The suffix is the
            # only tell, and the boot snapshot carries it before any reading does.
            return LoggingCheckResult(
                False, reported, raw_lines,
                f"Test firmware on this board (firmware_version {reported}). This "
                "build can fake SD card writes, so a drone running it logs nothing "
                "to the card while looking completely healthy. Flash the image with "
                "no HIL in its filename, then power-cycle the board.",
            )
        required = _required_keys(record)
        if required is None:
            continue
        missing = [k for k in required if k not in record]
        if missing:
            return LoggingCheckResult(
                False, record.get("firmware_version"), raw_lines, f"Record missing keys: {missing}"
            )
        firmware_version = record.get("firmware_version")
        card_warning = _card_warning(record)
        saw_reading = True
    if not saw_reading:
        return _no_readings_result(raw_lines)
    # After the loop, so a HIL board is still reported as one whatever its banner says.
    if any(DEBUG_IMAGE_BANNER in text for text in raw_lines):
        return LoggingCheckResult(
            False, firmware_version, raw_lines,
            "Debug firmware on this board: its banner names debug_logN.txt, not "
            "dataN.txt. A Debug image waits only 15 s for the drone's Pi to send the "
            "time, so a drone running it can log a whole run with no dates. Flash the "
            "Release image, then power-cycle the board.",
        )
    return LoggingCheckResult(True, firmware_version, raw_lines, warning=card_warning)


def _card_warning(record: dict[str, Any]) -> str | None:
    """What a 2.1 reading says is wrong with the SD card, or None. Older records do
    not report the card, so they never warn.

    A warning rather than a failure: 2.1 keeps logging to serial without its card,
    and a bench flash may have no card in, but a drone must not leave without one.
    """
    if (
        record.get("sd_ready") != 0
        and not record.get("sd_writes_failed")
        and not record.get("sd_mounts_failed")
    ):
        return None
    return (
        f"the board reports SD card trouble (sd_ready {record.get('sd_ready')}, "
        f"sd_writes_failed {record.get('sd_writes_failed')}, sd_mounts_failed "
        f"{record.get('sd_mounts_failed')}). Put in an empty, freshly formatted card "
        "and check again before this board goes on a drone."
    )


def _no_readings_result(raw_lines: list[str]) -> LoggingCheckResult:
    # A board that took our answer asks once and says it synced. One still asking
    # without saying so did not accept it, which on a working board means this
    # computer's clock is outside 2025 to 2100.
    synced = any(text.startswith(TIME_ACCEPTED) for text in raw_lines)
    if raw_lines.count(TIME_REQUEST) > 1 and not synced:
        error = (
            "The board kept asking for the time (TIME?) and did not accept this "
            "computer's answer, so it had not started logging. Check this computer's "
            "date and time, then flash again."
        )
    else:
        error = "No telemetry readings seen, only startup output or lines that are not records"
    return LoggingCheckResult(False, None, raw_lines, error)


# Typing this is what confirms a board is a bench board. A yes/no dialog is one
# click, and this is exactly the kind of prompt a tech flashing all afternoon stops
# reading; the failure it guards against is a drone that logs nothing to its card
# while looking completely healthy, which nobody notices until the data is missing.
BENCH_CONFIRMATION = "BENCH"


def is_bench_confirmation(answer: str | None) -> bool:
    """True only if the tech actually typed the confirmation word.

    Takes the raw dialog result including None, because None is what Cancel returns
    and an empty string is what OK-on-an-empty-box returns. Treating either as
    consent would defeat the entire gate, and they are easy to conflate.

    Case and surrounding whitespace are forgiven. Typing the word at all is the
    deliberate act; demanding capitals adds friction without adding intent.
    """
    if answer is None:
        return False
    return answer.strip().casefold() == BENCH_CONFIRMATION.casefold()


def describe_outcome(
    result: LoggingCheckResult, flashed_test_firmware: bool
) -> tuple[bool, str]:
    """How to report a finished flash: (it went as intended, what to tell the tech).

    evaluate_telemetry() above answers exactly one question, "is this drone ready for
    the field", and a board running the test build is not, however it got there. That
    verdict does not change here.

    What changes is who is being told. A tech who deliberately confirmed flashing the
    test image onto a bench board did precisely what they meant to, and reporting FAIL
    and telling them to go flash production firmware is both wrong and the opposite of
    what they want. The same result reached by accident still needs the original
    warning, so the two cases are distinguished by whether the tech was asked and
    said yes, not by anything the board reports.

    Pure, so the wording of both cases is testable without a Pico.
    """
    if result.ok:
        message = f"PASS - Pico is logging data. firmware_version: {result.firmware_version}"
        if result.warning:
            message += f". WARNING: {result.warning}"
        return True, message

    if flashed_test_firmware and HIL_MARKER in (result.firmware_version or ""):
        return True, (
            f"Bench test firmware is running and logging. firmware_version: "
            f"{result.firmware_version}. This board must NOT be deployed. Drive it "
            "with the hardware tests in tests/hil."
        )

    return False, f"FAIL - {result.error}"


def time_reply(epoch_s: float) -> bytes:
    """The answer to TIME?: this computer's clock in whole milliseconds. Pure."""
    return f"TIME {int(epoch_s * 1000)}\n".encode("ascii")


def read_telemetry_lines(
    ser: Any, timeout_s: float, clock: Callable[[], float] = time.time
) -> Iterator[str]:
    """Yield each non-empty serial line up to the first record of readings,
    answering the board's TIME? on the way.

    Takes an already-open port so the answering is testable with a stand-in, the
    same way evaluate_telemetry() is testable with no Pico.
    """
    deadline = time.monotonic() + timeout_s
    fragment = b""
    while time.monotonic() < deadline:
        chunk = ser.readline()
        if not chunk.endswith(b"\n"):
            # The timeout runs per byte, so a pause mid-line hands back only what has
            # arrived. Keep it for the rest: either half of a split record is not JSON.
            fragment += chunk
            continue
        text = (fragment + chunk).decode("utf-8", errors="replace").strip()
        fragment = b""
        if not text:
            continue
        # Yielded before answering, so the log keeps the request if the write fails.
        yield text
        if text == TIME_REQUEST:
            ser.write(time_reply(clock()))
        elif _carries_readings(text):
            return
    if fragment.strip():
        yield fragment.decode("utf-8", errors="replace").strip()


def verify_logging_data(port: str, timeout_s: float = 60.0) -> LoggingCheckResult:
    """Open the freshly-flashed Pico's serial port, read up to its first reading,
    and confirm it emits valid telemetry (see evaluate_telemetry).

    With its TIME? answered, 2.1 firmware's first reading comes roughly 10 to 20 s
    after boot: a 2 s banner delay, a 5 s pause before the SD card, card bring-up,
    then one measurement cycle, which also writes the boot snapshot. 60 s covers a
    board that missed the first answer and asks again, or one retrying its card.
    """
    # Collected one line at a time rather than with list(), so a port that drops
    # mid-read still reports what the board said before it went.
    raw_lines: list[str] = []
    try:
        with serial.Serial(
            port, BAUD_RATE, timeout=_PORT_TIMEOUT_S, write_timeout=_PORT_TIMEOUT_S
        ) as ser:
            for text in read_telemetry_lines(ser, timeout_s):
                raw_lines.append(text)
    except serial.SerialException as exc:
        return LoggingCheckResult(False, None, raw_lines, f"Serial error: {exc}")
    if not raw_lines:
        return LoggingCheckResult(False, None, raw_lines, "No serial output within timeout")
    return evaluate_telemetry(raw_lines)
