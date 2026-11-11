# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Helpers shared by several test modules."""
import struct
import zlib


def png_declaring(width, height):
    """A PNG whose header declares ``width`` x ``height`` pixels and that holds no image data.

    Image libraries read the declared size when they open a file, long before any
    pixel is decoded, so a few dozen bytes can declare billions of pixels.
    """
    def chunk(kind, payload):
        body = kind + payload
        return struct.pack('>I', len(payload)) + body + struct.pack('>I', zlib.crc32(body))

    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('>2I5B', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IEND', b''))
