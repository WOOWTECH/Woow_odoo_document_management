# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Helpers shared by several test modules."""
import string
import struct
import zlib


def png_chunk(kind, payload=b''):
    """One PNG chunk: length, type, payload and checksum."""
    body = kind + payload
    return struct.pack('>I', len(payload)) + body + struct.pack('>I', zlib.crc32(body))


def png_declaring(width, height):
    """A PNG whose header declares ``width`` x ``height`` pixels and that holds no image data.

    Image libraries read the declared size when they open a file, long before any
    pixel is decoded, so a few dozen bytes can declare billions of pixels.
    """
    return (b'\x89PNG\r\n\x1a\n'
            + png_chunk(b'IHDR', struct.pack('>2I5B', width, height, 8, 2, 0, 0, 0))
            + png_chunk(b'IEND', b''))


def _png_bytes(image):
    import io

    stream = io.BytesIO()
    image.save(stream, format='PNG')
    return stream.getvalue()


def png_with_chunk_list(chunks, *, after_image_data=False):
    """A valid PNG that carries the given chunks (bytes) before its image data, or after it.

    Opening an image reads the chunks up to the image data; loading it reads the ones after it.
    """
    from PIL import Image

    data = _png_bytes(Image.new('RGB', (2, 2), 'red'))
    assert data[-12:] == png_chunk(b'IEND')
    if after_image_data:
        return data[:-12] + b''.join(chunks) + data[-12:]
    return data[:33] + b''.join(chunks) + data[33:]


def png_with_chunks(count, kind=b'prVt', payload=b'', *, after_image_data=False):
    """A valid PNG with ``count`` chunks of one type (empty by default) around its image data."""
    return png_with_chunk_list([png_chunk(kind, payload)] * count,
                               after_image_data=after_image_data)


def png_with_image_chunks(count):
    """A valid PNG whose image data is in ``count`` IDAT chunks, all but the last of them empty."""
    from PIL import Image

    data = _png_bytes(Image.new('RGB', (2, 2), 'red'))
    length = struct.unpack('>I', data[33:37])[0]
    assert data[37:41] == b'IDAT' and data[41 + length + 4:] == png_chunk(b'IEND')
    return (data[:33] + png_chunk(b'IDAT') * (count - 1)
            + png_chunk(b'IDAT', data[41:41 + length]) + data[-12:])


def png_of_noise(side):
    """A valid PNG of ``side`` x ``side`` pixels of noise: large, in many IDAT chunks."""
    import random
    from PIL import Image

    pixels = random.Random(20261112).randbytes(side * side * 3)
    return _png_bytes(Image.frombytes('RGB', (side, side), pixels))


def apng_with_frames(count):
    """An animated PNG of ``count`` different frames: an acTL chunk, an fcTL and fdAT per frame."""
    import io
    from PIL import Image

    frames = [Image.new('RGB', (2, 2), (index % 256, index // 256, 0)) for index in range(count)]
    stream = io.BytesIO()
    frames[0].save(stream, format='PNG', save_all=True, append_images=frames[1:])
    return stream.getvalue()


def png_profile_payload(inflated=1 << 20):
    """The payload of an iCCP chunk: a profile of ``inflated`` zero bytes, 1 KB deflated."""
    return b'profile\x00\x00' + zlib.compress(b'\x00' * inflated, 9)


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
