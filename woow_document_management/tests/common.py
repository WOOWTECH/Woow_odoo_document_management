# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Helpers shared by several test modules."""
import string
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


# Names that look different but have the same numbered candidates ("name (2)", "name (3)", ...).
# Each is a ZIP segment of at most 255 bytes, in the order a folder lists them. Naming them one by
# one must cost a few fits of the name per name, whatever the names before it took.

def names_cut_alike(count):
    """``count`` different names, each twice, whose numbered candidates are the same text.

    124 two-byte letters, two characters that differ, and '.txt' are 254 bytes, so every name
    fits as it is. With a number added the stem is cut after 123 letters, which drops the two
    characters: every number gives the same candidate for every name.
    """
    letters = string.ascii_letters + string.digits
    pairs = [first + second for first in letters for second in letters]
    assert count <= len(pairs)
    return [name for pair in pairs[:count] for name in ['\u00e9' * 124 + pair + '.txt'] * 2]


def names_differing_in_case(count):
    """``count`` different long names that are the same name when case is ignored."""
    base = 'x' * 150 + 'abcdefghijkl'
    assert count <= 2 ** len(base[150:])
    return [''.join(letter.upper() if bits >> place & 1 else letter
                    for place, letter in enumerate(base)) + '.txt' for bits in range(count)]


def names_cut_alike_from_ten(classes, rounds):
    """``classes`` stems, each ``rounds`` times, in turns.

    The stems differ in one last byte that fits next to the numbers 2 to 9 but is cut next to
    10 and above (the number is one byte longer), so their candidates differ below 10 and are
    the same text from 10 on.
    """
    letters = string.ascii_letters + string.digits
    assert classes <= len(letters)
    stems = ['\u00e9' * 123 + letter for letter in letters[:classes]]
    return [stem + '.txt' for _round in range(rounds) for stem in stems]


def names_holding_the_numbers(count):
    """The numbered names of one name first (they are free), then that name ``count`` times."""
    return ['report (%d).txt' % number for number in range(2, count + 2)] + ['report.txt'] * count
