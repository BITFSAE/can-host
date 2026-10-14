"""Bit placement of DBC signals inside a CAN payload.

A bit is addressed as ``byte * 8 + bit_in_byte`` (bit 0 is the least
significant bit of byte 0).  Intel (``@1``) signals start at their least
significant bit and run upwards; Motorola (``@0``) signals start at their most
significant bit and walk down inside a byte, then continue at bit 7 of the
next byte.  The mapping is verified against cantools' own encoder in the tests
instead of being trusted from memory.
"""

from __future__ import annotations

from typing import Iterable

INTEL = "intel"
MOTOROLA = "motorola"


def signal_bits(start: int, length: int, byte_order: str) -> list[int]:
    """Return the bit addresses of a signal, most significant bit first."""
    if length <= 0:
        return []
    if byte_order == INTEL:
        return list(range(start + length - 1, start - 1, -1))
    bits: list[int] = []
    bit = start
    for _ in range(length):
        bits.append(bit)
        bit = bit + 15 if bit % 8 == 0 else bit - 1
    return bits


def span_bytes(bits: Iterable[int]) -> int:
    """Number of payload bytes needed to hold every bit (0 for no bits)."""
    highest = max(bits, default=-1)
    return highest // 8 + 1 if highest >= 0 else 0
