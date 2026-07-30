"""Telling the production image from the hardware test image, by reading its contents.

Moved out of test_flash.py alongside the code itself, so the publishing pipeline can
run this check without importing the Windows-only flashing module. The post-flash
telemetry checks stay in test_flash.py; these are about the file, before it is written.
"""
import pytest
import uf2


def _uf2_bytes(payloads: list[bytes]) -> bytes:
    """Build a minimal but structurally real UF2 from the given payload chunks.

    Real structure matters here: the marker this detector looks for is four bytes, and
    a UF2 splices a 32-byte header in every 512 bytes. A test built from a flat buffer
    would pass against a detector that cannot handle block boundaries.
    """
    out = bytearray()
    for i, payload in enumerate(payloads):
        block = bytearray(512)
        block[0:4] = (0x0A324655).to_bytes(4, "little")   # magicStart0
        block[4:8] = (0x9E5D5157).to_bytes(4, "little")   # magicStart1
        block[12:16] = (0x10000000 + i * 256).to_bytes(4, "little")  # targetAddr
        block[16:20] = len(payload).to_bytes(4, "little")  # payloadSize
        block[20:24] = i.to_bytes(4, "little")             # blockNo
        block[24:28] = len(payloads).to_bytes(4, "little")  # numBlocks
        block[32:32 + len(payload)] = payload
        block[508:512] = (0x0AB16F30).to_bytes(4, "little")  # magicEnd
        out += block
    return bytes(out)


def test_identifies_the_hil_image(tmp_path):
    image = tmp_path / "AquaD_Pico_HIL_v1.2.0.uf2"
    image.write_bytes(_uf2_bytes([b"boot", b"firmware 1.2.0+HIL here", b"tail"]))

    assert uf2.is_test_firmware(image)


def test_does_not_flag_the_production_image(tmp_path):
    image = tmp_path / "AquaD_Pico_v1.2.0.uf2"
    image.write_bytes(_uf2_bytes([b"boot", b"firmware 1.2.0 here", b"tail"]))

    assert not uf2.is_test_firmware(image)


def test_a_renamed_hil_image_is_still_identified(tmp_path):
    """The case a filename check cannot catch, and the reason this reads contents.

    Someone copies the test build and it picks up a production-looking name, by hand
    or by a script. The bytes inside are what decide.
    """
    image = tmp_path / "AquaD_Pico_v1.2.0.uf2"
    image.write_bytes(_uf2_bytes([b"firmware 1.2.0+HIL here"]))

    assert uf2.is_test_firmware(image)


def test_finds_a_marker_split_across_two_blocks(tmp_path):
    """A four-byte marker landing on a 512-byte boundary is not contiguous on disk.

    This is the case a plain byte scan of the file gets wrong, and it fails silently
    by reporting the image as safe.
    """
    image = tmp_path / "AquaD_Pico_v1.2.0.uf2"
    image.write_bytes(_uf2_bytes([b"version 1.2.0+H", b"IL and onwards"]))

    assert uf2.is_test_firmware(image)


def test_a_non_uf2_file_still_gets_searched(tmp_path):
    # Rather than reporting a clean result we did not earn.
    blob = tmp_path / "mystery.uf2"
    blob.write_bytes(b"this is not a uf2 but it mentions 1.2.0+HIL")

    assert uf2.is_test_firmware(blob)


def test_an_unreadable_file_raises_rather_than_reporting_clean(tmp_path):
    missing = tmp_path / "nope.uf2"

    with pytest.raises(OSError):
        uf2.is_test_firmware(missing)
