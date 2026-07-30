"""Reading a Pico .uf2 well enough to tell the production build from the test one.

Split out of flash.py so the publishing pipeline can run the same check without
dragging in pyserial and the Windows-only flashing code around it. What lives here
is one piece of knowledge: the UF2 container layout, plus the marker the HIL build
stamps into its version string. The app needs it before flashing and CI needs it
before publishing, and neither needs anything else from flash.py to ask.
"""
from __future__ import annotations

from pathlib import Path

# The HIL fault-injection build stamps this into its FIRMWARE_VERSION, so it shows up
# both inside the .uf2 and in every telemetry record the board emits. See hil/.
HIL_MARKER = "+HIL"

# UF2 layout, from the format spec: 512-byte blocks, a 32-byte header, and the
# payload length at offset 16.
_UF2_BLOCK_SIZE = 512
_UF2_HEADER_SIZE = 32
_UF2_PAYLOAD_SIZE_OFFSET = 16
_UF2_MAGIC_START0 = 0x0A324655


def is_test_firmware(uf2_path: Path) -> bool:
    """True if this .uf2 is the HIL build, which must never go on a production drone.

    Checks the file's *contents*, not its name. Naming is the first line of defence
    (flash.FIRMWARE_GLOB never offers the HIL image up automatically) but it is also
    the easiest thing to lose: rename the file, or copy it from a machine that
    renamed it, and a name-based check waves it through.

    Reassembles the UF2 payloads rather than scanning the raw file, because a UF2 is
    a sequence of blocks with 32-byte headers spliced in. A four-character marker
    that happens to straddle a block boundary is not contiguous on disk, so a naive
    byte search would miss it exactly when the answer matters most.

    Raises OSError if the file cannot be read. Callers must not treat an unreadable
    file as "not test firmware": failing to run the check is not the same as passing
    it.
    """
    raw = uf2_path.read_bytes()
    marker = HIL_MARKER.encode()

    payload = bytearray()
    for start in range(0, len(raw) - _UF2_BLOCK_SIZE + 1, _UF2_BLOCK_SIZE):
        block = raw[start:start + _UF2_BLOCK_SIZE]
        if int.from_bytes(block[0:4], "little") != _UF2_MAGIC_START0:
            # Not a UF2, or not one we understand. Fall back to searching the bytes
            # we were given rather than reporting a clean result we did not earn.
            return marker in raw
        size = int.from_bytes(
            block[_UF2_PAYLOAD_SIZE_OFFSET:_UF2_PAYLOAD_SIZE_OFFSET + 4], "little"
        )
        size = min(size, _UF2_BLOCK_SIZE - _UF2_HEADER_SIZE)
        payload += block[_UF2_HEADER_SIZE:_UF2_HEADER_SIZE + size]

    return marker in payload or marker in raw
