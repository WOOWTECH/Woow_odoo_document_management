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


def png_with_chunks(count, kind=b'prVt'):
    """A valid PNG that carries ``count`` empty private chunks before its image data."""
    import io
    from PIL import Image

    def chunk(name, payload):
        body = name + payload
        return struct.pack('>I', len(payload)) + body + struct.pack('>I', zlib.crc32(body))

    stream = io.BytesIO()
    Image.new('RGB', (2, 2), 'red').save(stream, format='PNG')
    data = stream.getvalue()
    return data[:33] + chunk(kind, b'') * count + data[33:]


def jpeg_with_segments(count, filler=b'\xff\xe1\x00\x02'):
    """A valid JPEG that carries ``count`` empty application segments before its tables."""
    import io
    from PIL import Image
    stream = io.BytesIO()
    Image.new('RGB', (2, 2), 'red').save(stream, format='JPEG')
    data = stream.getvalue()
    return data[:2] + filler * count + data[2:]


def gif_with_comments(count):
    """A valid GIF that carries ``count`` empty comment extensions before its image."""
    import io
    from PIL import Image
    stream = io.BytesIO()
    Image.new('P', (2, 2), 0).save(stream, format='GIF')
    data = stream.getvalue()
    colours = 13 + (3 * (2 << (data[10] & 7)) if data[10] & 0x80 else 0)
    return data[:colours] + b'\x21\xfe\x00' * count + data[colours:]
