"""Tests for the post-flash telemetry verification. The regression that matters:
a Pico emitting non-JSON boot noise (or nothing valid) must NOT be reported as
'logging data', which the previous line-count-only check allowed."""
import flash
import pytest


class _FakePort:
    def __init__(self, device, vid):
        self.device = device
        self.vid = vid


@pytest.fixture
def fake_comports(monkeypatch):
    """Swap out pyserial's port enumeration so port selection is testable without hardware."""
    def _set(ports):
        monkeypatch.setattr(flash.list_ports, "comports", lambda: ports)
    return _set


def _valid_record(fw="1.2.3"):
    return (
        f'{{"firmware_version":"{fw}","water_temperature":12.3,'
        '"ph":7.1,"uv_banks":[]}'
    )


def test_passes_on_a_valid_record():
    result = flash.evaluate_telemetry([_valid_record()])

    assert result.ok
    assert result.firmware_version == "1.2.3"


def test_fails_on_pure_garbage():
    # THE REGRESSION: unparseable serial noise must not verify as a good flash.
    result = flash.evaluate_telemetry(["boot", "USB init", "not json at all"])

    assert not result.ok
    assert result.firmware_version is None
    assert result.error


def test_fails_on_record_missing_required_keys():
    # Parses as JSON but lacks required keys -> wrong/old firmware on the board.
    result = flash.evaluate_telemetry(['{"firmware_version":"1.0"}'])

    assert not result.ok
    assert "missing keys" in (result.error or "").lower()


def test_passes_when_a_valid_record_appears_amid_noise():
    result = flash.evaluate_telemetry(["garbage", _valid_record(), "trailing noise"])

    assert result.ok
    assert result.firmware_version == "1.2.3"


def test_fails_on_empty_input():
    result = flash.evaluate_telemetry([])

    assert not result.ok


# -- Test firmware must never verify as a good flash ---------------------------
#
# The HIL build can be commanded to fake SD card success, so a drone carrying it
# emits telemetry that satisfies every check above: valid JSON, every required key,
# plausible values. The +HIL suffix in firmware_version is the only thing that gives
# it away, and this is the last gate before a tech is told the unit is good.

def test_fails_on_hil_test_firmware():
    result = flash.evaluate_telemetry([_valid_record(fw="1.2.3+HIL")])

    assert not result.ok, "the hardware test build verified as a good production flash"
    assert result.firmware_version == "1.2.3+HIL"
    # The tech reading this may have no idea what HIL is, so the error has to say
    # what to do rather than what the build is called.
    assert "test firmware" in (result.error or "").lower()


def test_fails_on_hil_firmware_even_when_a_clean_record_came_first():
    """One good-looking record must not launder the ones around it.

    Records are sampled over several seconds, and evaluate_telemetry used to let the
    last valid record win. A pass here would mean the suffix was seen and discarded.
    """
    result = flash.evaluate_telemetry([_valid_record(), _valid_record(fw="1.2.3+HIL")])

    assert not result.ok


def test_production_firmware_still_passes():
    # The guard above must not reject anything that merely mentions a suffix-free
    # version. Cheap to state, and it is the case every real flash takes.
    result = flash.evaluate_telemetry([_valid_record(fw="1.2.0")])

    assert result.ok
    assert result.firmware_version == "1.2.0"


# Identifying the test image *before* it is written lives in test_uf2.py, alongside
# the module that does it. The checks above are the ones that run after a flash.


# -- Confirming a board is a bench board ---------------------------------------
#
# Typed, not clicked. A yes/no dialog is one keystroke and this is exactly the prompt
# somebody flashing boards all afternoon stops reading. What it guards against is a
# drone that logs nothing to its card while reporting perfectly healthy telemetry.

def test_typing_the_word_confirms():
    assert flash.is_bench_confirmation(flash.BENCH_CONFIRMATION)


def test_case_and_stray_whitespace_are_forgiven():
    # Typing it at all is the deliberate act. Demanding capitals adds friction to a
    # workflow that is supposed to happen regularly, without adding any intent.
    assert flash.is_bench_confirmation("  bench  ")


@pytest.mark.parametrize(
    "answer, why",
    [
        (None, "Cancel, or the dialog being closed with the X"),
        ("", "OK pressed with the box left empty"),
        ("   ", "whitespace only"),
        ("y", "the habit from a yes/no prompt"),
        ("yes", "the habit from a yes/no prompt"),
        ("BENCHMARK", "a word that merely starts the same way"),
        ("not bench", "the word appearing inside something else"),
    ],
)
def test_anything_else_is_a_refusal(answer, why):
    # None and "" are the two that matter: they come back from Cancel and from an
    # empty OK, they are easy to conflate, and treating either as consent would
    # defeat the entire gate.
    assert not flash.is_bench_confirmation(answer), why


# -- Reporting the outcome to the tech -----------------------------------------
#
# evaluate_telemetry() answers "is this drone ready for the field", and a HIL board
# never is. But the same verdict has to read two completely different ways depending
# on whether the tech meant to put that image there. Getting this wrong told someone
# who had just deliberately confirmed flashing the test build that they had FAILED
# and should go flash production firmware instead.

def test_a_deliberate_bench_flash_is_not_reported_as_a_failure():
    result = flash.evaluate_telemetry([_valid_record(fw="1.2.3+HIL")])

    went_as_intended, message = flash.describe_outcome(result, flashed_test_firmware=True)

    assert went_as_intended, "a confirmed bench flash was reported as a failure"
    assert "FAIL" not in message
    assert "1.2.3+HIL" in message


def test_a_deliberate_bench_flash_still_says_not_to_deploy_it():
    result = flash.evaluate_telemetry([_valid_record(fw="1.2.3+HIL")])

    _, message = flash.describe_outcome(result, flashed_test_firmware=True)

    assert "not be deployed" in message.lower()


def test_an_accidental_hil_flash_still_fails_with_the_original_warning():
    # THE REGRESSION this whole distinction exists to avoid softening. Nobody asked
    # for this image, so the tech needs to be told plainly and told what to do.
    result = flash.evaluate_telemetry([_valid_record(fw="1.2.3+HIL")])

    went_as_intended, message = flash.describe_outcome(result, flashed_test_firmware=False)

    assert not went_as_intended
    assert "FAIL" in message
    assert "no HIL in its filename" in message


def test_a_real_failure_on_a_bench_board_is_still_a_failure():
    """Confirming the test image excuses its version string, nothing else.

    A HIL board emitting garbage has genuinely failed, and swallowing that because
    "we expected test firmware" would make the bench flash the one path with no
    verification at all.
    """
    result = flash.evaluate_telemetry(["boot", "not json at all"])

    went_as_intended, message = flash.describe_outcome(result, flashed_test_firmware=True)

    assert not went_as_intended
    assert "FAIL" in message


def test_a_production_flash_still_passes():
    result = flash.evaluate_telemetry([_valid_record(fw="1.2.0")])

    went_as_intended, message = flash.describe_outcome(result, flashed_test_firmware=False)

    assert went_as_intended
    assert message.startswith("PASS")


def test_production_firmware_flashed_while_expecting_a_test_image_still_passes():
    # The tech changed their mind and picked a different file. Nothing is wrong.
    result = flash.evaluate_telemetry([_valid_record(fw="1.2.0")])

    went_as_intended, _ = flash.describe_outcome(result, flashed_test_firmware=True)

    assert went_as_intended


# -- Port selection ---------------------------------------------------------
# THE REGRESSION: a second Pico-VID device on the tech's laptop (another board, a
# Debug Probe) must never be handed to verify_logging_data() in place of the board
# we just flashed — that reports PASS for firmware we never wrote.

def test_pico_serial_ports_matches_only_pico_vid(fake_comports):
    fake_comports([
        _FakePort("COM3", 0x0A5C),            # Bluetooth
        _FakePort("COM5", flash.PICO_USB_VID),
        _FakePort("COM1", None),              # legacy port, no VID
    ])

    assert flash.pico_serial_ports() == {"COM5"}


def test_ignores_a_different_pico_that_was_already_attached(fake_comports):
    # COM5 is someone else's Pico, present before the flash. COM9 is ours, appearing after.
    before = {"COM5"}
    fake_comports([
        _FakePort("COM5", flash.PICO_USB_VID),
        _FakePort("COM9", flash.PICO_USB_VID),
    ])

    assert flash.find_pico_serial_port(before, timeout_s=0.5) == "COM9"


def test_times_out_when_only_the_other_pico_is_present(fake_comports):
    # Our board never came back. Returning the other Pico's port would be a false PASS.
    fake_comports([_FakePort("COM5", flash.PICO_USB_VID)])

    assert flash.find_pico_serial_port({"COM5"}, timeout_s=0.5, poll_interval_s=0.05) is None


def test_finds_our_pico_reusing_the_same_com_number(fake_comports):
    # Windows hands the same COM number back after a reflash. The board was in BOOTSEL
    # (no serial port) when the snapshot was taken, so its port still reads as new.
    fake_comports([_FakePort("COM9", flash.PICO_USB_VID)])

    assert flash.find_pico_serial_port(set(), timeout_s=0.5) == "COM9"


# -- firmware discovery ------------------------------------------------------
#
# The app auto-selects a .uf2 for the tech. Two ways that goes wrong, both here:
# it offers up a file that is not our firmware at all, or it silently offers up
# nothing because the artifact naming moved and the glob didn't follow.


def _uf2(dir_path, name, mtime):
    p = dir_path / name
    p.write_bytes(b"\x00")
    import os
    os.utime(p, (mtime, mtime))
    return p


def test_finds_the_versioned_firmware(tmp_path):
    _uf2(tmp_path, "AquaD_Pico_v1.0.3.uf2", 1000)

    assert flash.find_firmware(tmp_path).name == "AquaD_Pico_v1.0.3.uf2"


def test_never_offers_flash_nuke(tmp_path):
    # The one that actually matters. flash_nuke.uf2 ERASES the board. A bare *.uf2
    # glob would hand it to the tech pre-selected, and "Flash Pico" would wipe the
    # Pico instead of programming it.
    _uf2(tmp_path, "flash_nuke.uf2", 9999)  # newest by far — must still be ignored

    assert flash.find_firmware(tmp_path) is None


def test_ignores_unrelated_uf2_files(tmp_path):
    _uf2(tmp_path, "blink.uf2", 9999)
    _uf2(tmp_path, "picoprobe.uf2", 9998)
    _uf2(tmp_path, "AquaD_Pico_v1.0.3.uf2", 1)

    assert flash.find_firmware(tmp_path).name == "AquaD_Pico_v1.0.3.uf2"


def test_returns_none_when_no_firmware_present(tmp_path):
    # Must be None, not a phantom path: main.py leaves the field blank so the tech
    # browses, rather than pre-filling a file that doesn't exist and failing at flash.
    assert flash.find_firmware(tmp_path) is None


def test_picks_the_newest_build_not_the_highest_version(tmp_path):
    # A deliberate downgrade to chase a regression: the tech drops 1.0.2 in *after*
    # 1.0.3 is already there. They mean the one they just put in.
    _uf2(tmp_path, "AquaD_Pico_v1.0.3.uf2", 1000)
    _uf2(tmp_path, "AquaD_Pico_v1.0.2.uf2", 2000)

    assert flash.find_firmware(tmp_path).name == "AquaD_Pico_v1.0.2.uf2"
