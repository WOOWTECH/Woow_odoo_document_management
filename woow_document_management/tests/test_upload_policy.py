# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Server-side upload validation, including content and suffix mismatch."""

import base64
import io
import struct
import time
import warnings
import zipfile
import zlib
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image
from odoo import Command
from odoo.tests import TransactionCase, tagged

from ..models import upload_policy
from ..models.upload_policy import WoowUploadRejected
from ..services.constants import PIXEL_LIMIT
from .common import (apng_with_frames, gif_with_comments, jpeg_with_scans, jpeg_with_segments,
                     png_chunk, png_chunks_of, png_declaring, png_of_noise, png_profile_payload,
                     png_with_chunk_list, png_with_chunks, png_with_image_chunks,
                     png_with_image_data_pieces)


@tagged('woow_docmgmt_files')
class TestRenamePolicy(TransactionCase):
    """renaming a document follows the upload filename policy."""

    def setUp(self):
        super().setUp()
        self.admin = self.env.ref('base.user_admin')
        self.folder = self.env['woow.document.folder'].sudo().create({
            'name': 'Rename folder', 'owner_id': self.admin.id})
        stream = io.BytesIO()
        Image.new('RGB', (2, 2), 'red').save(stream, format='PNG')
        self.png = base64.b64encode(stream.getvalue())
        documents = self.env['woow.document'].sudo()
        self.image = documents.create({
            'name': 'picture.png', 'folder_id': self.folder.id, 'datas': self.png})
        self.text = documents.create({
            'name': 'notes.txt', 'folder_id': self.folder.id,
            'datas': base64.b64encode(b'hello')})
        self.other = documents.create({
            'name': 'other.txt', 'folder_id': self.folder.id,
            'datas': base64.b64encode(b'world')})
        self.link = documents.create({
            'name': 'Site', 'folder_id': self.folder.id, 'document_type': 'url',
            'url': 'https://example.test/page'})

    def test_rename_rejects_blocked_unlisted_and_mismatched_extensions(self):
        image = self.image.with_user(self.admin)
        for name, code in (('page.html', 'extension'), ('picture.jpg', 'mime'),
                           ('picture', 'extension'), ('picture.unknownext', 'extension'),
                           ('...', 'filename')):
            with self.subTest(name=name), self.assertRaises(WoowUploadRejected) as caught:
                image.write({'name': name})
            self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.image.name, 'picture.png')
        self.assertEqual(self.image.mimetype, 'image/png')

    def test_rename_sanitizes_like_upload_and_keeps_matching_extensions(self):
        image = self.image.with_user(self.admin)
        image.write({'name': '\u202ephoto\u202c.png'})
        self.assertEqual(self.image.name, 'photo.png')
        image.write({'name': 'a' * 300 + '.png'})
        self.assertEqual(len(self.image.name), 200)
        self.assertTrue(self.image.name.endswith('.png'))
        image.write({'name': '../CON.png'})
        self.assertEqual(self.image.name, '_CON.png')
        # text/plain content may carry either listed extension of its type.
        self.text.with_user(self.admin).write({'name': 'notes.csv'})
        self.assertEqual(self.text.name, 'notes.csv')
        self.assertEqual(self.text.mimetype, 'text/plain')

    def test_link_rename_only_applies_the_name_rules(self):
        link = self.link.with_user(self.admin)
        link.write({'name': 'Company\x07 site'})
        self.assertEqual(self.link.name, 'Company site')
        link.write({'name': 'index.html'})
        self.assertEqual(self.link.name, 'index.html')
        with self.assertRaises(WoowUploadRejected) as caught:
            link.write({'name': '  '})
        self.assertEqual(caught.exception.code, 'filename')
        self.assertEqual(self.link.name, 'index.html')

    def test_batch_rename_and_unrelated_writes_keep_legacy_names(self):
        self.env.cr.execute('UPDATE woow_document SET name = %s WHERE id = %s',
                            ('legacy\u202e.html', self.text.id))
        self.env.invalidate_all()
        self.text.with_user(self.admin).write({'description': '<p>touched</p>'})
        self.text.with_user(self.admin).write({'name': 'legacy\u202e.html'})
        self.assertEqual(self.text.name, 'legacy\u202e.html')
        mixed = (self.image | self.text).with_user(self.admin)
        with self.assertRaises(WoowUploadRejected) as caught:
            mixed.write({'name': 'shared.png'})
        self.assertEqual(caught.exception.code, 'mime')
        self.assertEqual(self.image.name, 'picture.png')
        self.assertEqual(self.text.name, 'legacy\u202e.html')
        texts = (self.text | self.other).with_user(self.admin)
        texts.write({'name': ' renamed\u202e.txt'})
        self.assertEqual(set(texts.mapped('name')), {'renamed.txt'})

    def test_rename_with_new_content_keeps_the_upload_path(self):
        image = self.image.with_user(self.admin)
        with self.assertRaises(WoowUploadRejected) as caught:
            image.write({'name': 'replaced.html', 'datas': self.png})
        self.assertEqual(caught.exception.code, 'extension')
        image.write({'name': 'replaced.png', 'datas': self.png})
        self.assertEqual((self.image.name, self.image.mimetype), ('replaced.png', 'image/png'))
        image.write({'name': 'swapped.txt', 'datas': base64.b64encode(b'now text')})
        self.assertEqual((self.image.name, self.image.mimetype), ('swapped.txt', 'text/plain'))


@tagged('woow_docmgmt_files')
class TestUploadPolicy(TransactionCase):

    def setUp(self):
        super().setUp()
        self.policy = self.env['woow.document.upload.policy']

    def test_valid_image_and_text(self):
        stream = io.BytesIO()
        Image.new('RGB', (2, 2), 'red').save(stream, format='PNG')
        content = stream.getvalue()
        result = self.policy._validate('../image.png', content)
        self.assertEqual((result.name, result.mimetype, result.size),
                         ('image.png', 'image/png', len(content)))
        text = self.policy._validate('notes.txt', b'hello world\n')
        self.assertEqual(text.mimetype, 'text/plain')
        self.assertTrue(self.policy._inline_allowed('image/png; charset=binary'))

    def test_image_sizes_beyond_the_pixel_limit_are_rejected_as_pixels(self):
        """The size a header declares is judged before any pixel is decoded.

        Above twice its own limit Pillow refuses to open an image, with an exception
        that is neither an OSError nor a ValueError, and below that it only warns.
        Every declared size past the module's limit is the 'pixels' rejection, for
        every uploader and whatever Pillow's limit and warning filters are; an image
        Pillow cannot read at all is still 'content'.
        """
        portal = self.env['res.users'].create({
            'name': 'Pixel portal fixture', 'login': 'doc-pixel-portal-fixture',
            'groups_id': [Command.set([self.env.ref('base.group_portal').id])],
        })
        columns = 10000
        at_limit = png_declaring(columns, PIXEL_LIMIT // columns)
        self.assertEqual(self.policy._validate('limit.png', at_limit).mimetype, 'image/png')
        sizes = {'just above the limit': (columns, PIXEL_LIMIT // columns + 1),
                 'Pillow warns': (12000, 10000),
                 'Pillow refuses to open it': (200000, 200000),
                 'the largest the format can declare': (2**32 - 1, 2**32 - 1)}
        for label, (width, height) in sizes.items():
            for uploader, policy in (('internal', self.policy),
                                     ('portal', self.policy.with_user(portal))):
                with self.subTest(size=label, uploader=uploader), warnings.catch_warnings():
                    warnings.simplefilter('ignore', Image.DecompressionBombWarning)
                    with self.assertRaises(WoowUploadRejected) as caught:
                        policy._validate('big.png', png_declaring(width, height))
                    self.assertEqual(caught.exception.code, 'pixels')
        with self.subTest(setting='warnings are errors'), warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with self.assertRaises(WoowUploadRejected) as caught:
                self.policy._validate('warned.png', png_declaring(12000, 10000))
            self.assertEqual(caught.exception.code, 'pixels')
        with self.subTest(setting='Pillow limit disabled'), \
                patch.object(Image, 'MAX_IMAGE_PIXELS', None):
            with self.assertRaises(WoowUploadRejected) as caught:
                self.policy._validate('huge.png', png_declaring(200000, 200000))
            self.assertEqual(caught.exception.code, 'pixels')
        with self.subTest(image='cut inside the header'):
            with self.assertRaises(WoowUploadRejected) as caught:
                self.policy._validate('cut.png', png_declaring(10, 10)[:30])
            self.assertEqual(caught.exception.code, 'content')

    def test_creating_or_replacing_a_file_with_an_enormous_image_header_is_rejected(self):
        """A document write that carries such a header is refused like any bad upload."""
        admin = self.env.ref('base.user_admin')
        folder = self.env['woow.document.folder'].sudo().create({
            'name': 'Pixel folder', 'owner_id': admin.id})
        documents = self.env['woow.document'].sudo()
        header = base64.b64encode(png_declaring(200000, 200000))
        with self.assertRaises(WoowUploadRejected) as caught:
            documents.create({'name': 'huge.png', 'folder_id': folder.id, 'datas': header})
        self.assertEqual(caught.exception.code, 'pixels')
        self.assertFalse(documents.search([('folder_id', '=', folder.id)]))
        stream = io.BytesIO()
        Image.new('RGB', (2, 2), 'red').save(stream, format='PNG')
        small = documents.create({'name': 'small.png', 'folder_id': folder.id,
                                  'datas': base64.b64encode(stream.getvalue())})
        size = small.file_size
        with self.assertRaises(WoowUploadRejected) as caught:
            small.write({'datas': header})
        self.assertEqual(caught.exception.code, 'pixels')
        self.assertEqual((small.file_size, small.mimetype), (size, 'image/png'))

    def test_images_with_too_many_header_structures_are_refused_before_they_are_opened(self):
        """The headers of a PNG, JPEG or GIF are counted before the image library reads them.

        The library walks them in Python and some cost more than their size, so a few
        megabytes of empty structures would keep a worker busy for minutes. The check
        refuses them as unreadable content before ``Image.open`` is called.
        """
        too_many = upload_policy.IMAGE_MAX_STRUCTURES + 1
        cases = {
            'png.png': png_with_chunks(too_many),
            'jpeg.jpg': jpeg_with_segments(too_many),
            'gif.gif': gif_with_comments(too_many),
            'jpeg-fill.jpg': jpeg_with_segments(1, b'\xff' * too_many),
            'jpeg-junk.jpg': jpeg_with_segments(
                1, b'\xff\xe1\x00\x02' + b'\x00' * (upload_policy.IMAGE_MAX_JUNK + 1)),
            'gif-sub-blocks.gif': (gif_with_comments(0)[:13] + b'\x21\xfe' + b'\x01a' * too_many
                                   + b'\x00;'),
        }
        portal = self.env['res.users'].create({
            'name': 'Header portal fixture', 'login': 'doc-header-portal-fixture',
            'groups_id': [Command.set([self.env.ref('base.group_portal').id])]})
        for name, content in cases.items():
            for uploader, policy in (('internal', self.policy),
                                     ('portal', self.policy.with_user(portal))):
                with self.subTest(file=name, uploader=uploader), \
                        patch.object(upload_policy.Image, 'open',
                                     side_effect=AssertionError('the image was opened')):
                    with self.assertRaises(WoowUploadRejected) as caught:
                        policy._validate(name, content)
                    self.assertEqual(caught.exception.code, 'content')

    def test_a_jpeg_with_too_many_scans_is_refused_before_it_is_opened(self):
        """A JPEG has a few scans, not a few hundred.

        A progressive file may repeat a scan of ten bytes as often as it likes, and the decoder
        (Odoo decodes the pictures it stores, and every thumbnail of them) goes over the whole
        picture once for each, so a file of a few kilobytes keeps a worker busy for seconds. The
        scans are counted up to the end of the image, wherever the data of a scan is, and the file
        is refused as unreadable content before ``Image.open`` is called.
        """
        limit = upload_policy.JPEG_MAX_SCANS
        scan_data = b'\x12\xff\x00\x34\xff\xd0\x56\xff\x00\x78\xff\xff\xd1\x9a'
        # the bytes of an end marker inside an application segment end nothing
        behind_a_fake_end = jpeg_with_scans(limit + 1)
        behind_a_fake_end = (behind_a_fake_end[:2] + b'\xff\xe1\x00\x08\xff\xd9\xff\xd9\xff\xd9'
                             + behind_a_fake_end[2:])
        cases = {
            'one more than the limit': jpeg_with_scans(limit + 1),
            'a few thousand': jpeg_with_scans(4006),
            'with data in the scans': jpeg_with_scans(limit + 1, scan_data),
            'behind a fake end marker': behind_a_fake_end,
        }
        portal = self.env['res.users'].create({
            'name': 'Scan portal fixture', 'login': 'doc-scan-portal-fixture',
            'groups_id': [Command.set([self.env.ref('base.group_portal').id])]})
        for name, content in cases.items():
            for uploader, policy in (('internal', self.policy),
                                     ('portal', self.policy.with_user(portal))):
                with self.subTest(file=name, uploader=uploader), \
                        patch.object(upload_policy.Image, 'open',
                                     side_effect=AssertionError('the image was opened')):
                    with self.assertRaises(WoowUploadRejected) as caught:
                        policy._validate('scans.jpg', content)
                    self.assertEqual(caught.exception.code, 'content')

    def test_creating_or_replacing_a_file_with_too_many_scans_is_rejected(self):
        """A document create or write refuses it like any other upload."""
        admin = self.env.ref('base.user_admin')
        folder = self.env['woow.document.folder'].sudo().create({
            'name': 'Scan folder', 'owner_id': admin.id})
        documents = self.env['woow.document'].sudo()
        many = base64.b64encode(jpeg_with_scans(upload_policy.JPEG_MAX_SCANS + 1))
        with self.assertRaises(WoowUploadRejected) as caught:
            documents.create({'name': 'scans.jpg', 'folder_id': folder.id, 'datas': many})
        self.assertEqual(caught.exception.code, 'content')
        self.assertFalse(documents.search([('folder_id', '=', folder.id)]))
        few = jpeg_with_scans(3)
        document = documents.create({'name': 'few.jpg', 'folder_id': folder.id,
                                     'datas': base64.b64encode(few)})
        with self.assertRaises(WoowUploadRejected) as caught:
            document.write({'datas': many})
        self.assertEqual(caught.exception.code, 'content')
        self.assertEqual(base64.b64decode(document.datas), few)

    def test_a_jpeg_within_the_scan_limit_is_accepted(self):
        """What encoders write passes: one scan, six for grey, ten for colour, eighteen for four
        components; so does a file at the limit. What follows the end of the image is not read."""
        limit = upload_policy.JPEG_MAX_SCANS
        scan_data = b'\x12\xff\x00\x34\xff\xd0\x56\xff\x00\x78\xff\xff\xd1\x9a'
        many = jpeg_with_scans(100)
        after_the_end = many[many.index(b'\xff\xda'):-2]       # the headers of 100 scans, no picture

        def encoded(mode, **options):
            stream = io.BytesIO()
            Image.new(mode, (16, 16), 100).save(stream, format='JPEG', **options)
            return stream.getvalue()

        cases = {
            'baseline': encoded('L'),
            'progressive grey': encoded('L', progressive=True),
            'progressive colour': encoded('RGB', progressive=True),
            'progressive four components': encoded('CMYK', progressive=True),
            'at the limit': jpeg_with_scans(limit),
            'at the limit, with data': jpeg_with_scans(limit, scan_data),
            'a second picture after the end': jpeg_with_scans(1) + jpeg_with_scans(4 * limit),
            'scan headers after the end': jpeg_with_scans(1) + b'\x00\x00' + after_the_end,
        }
        for name, content in cases.items():
            with self.subTest(file=name):
                self.assertEqual(self.policy._validate('scans.jpg', content).mimetype, 'image/jpeg')

    def test_the_data_of_a_jpeg_scan_is_walked_in_one_pass(self):
        """A long run of 0xFF bytes in the data of a scan costs the walk its length, not its square.

        The next marker is found with one search that has no repeat to back out of; the bound is
        far above what that takes and far below what a quadratic pattern takes on a run of 0xFF
        that no marker ends (a stuffed zero, here), as well as on one that a marker ends.
        """
        for ending in (b'', b'\x00'):
            content = jpeg_with_scans(2, b'\xff' * (1 << 16) + ending)
            with self.subTest(run_ends_with=ending):
                started = time.perf_counter()
                self.assertTrue(upload_policy._jpeg_structure_ok(content))
                self.assertLess(time.perf_counter() - started, 5)

    @staticmethod
    def _png_chunks_of_unequal_cost():
        """Chunks that cost the library far more than their size, in a few megabytes at most.

        A profile (iCCP) or a compressed text (zTXt, a compressed iTXt) of about a kilobyte is
        inflated to 1 MiB when the file is read, so thousands of them keep a worker busy for
        seconds, and the whole file is smaller than every upload limit.
        """
        deflated = zlib.compress(b'\x00' * (1 << 20), 9)
        profile = png_chunk(b'iCCP', png_profile_payload())
        ztxt = png_chunk(b'zTXt', b'key\x00\x00' + deflated)
        itxt = png_chunk(b'iTXt', b'key\x00\x01\x00\x00\x00' + deflated)
        return {
            'profiles': [profile] * 4000,
            'two profiles': [profile] * 2,
            'zTXt': [ztxt] * 17,
            'iTXt': [itxt] * 17,
            'zTXt and iTXt': [ztxt] * 9 + [itxt] * 8,
            'a profile and texts': [profile, ztxt, itxt, profile],
            'private chunks': [png_chunk(b'prVt')] * 300,
        }

    def test_png_chunks_that_cost_more_than_their_size_are_refused_before_the_image_is_opened(self):
        """More than one profile, more than a handful of compressed texts, or hundreds of chunks.

        They are refused as 'content' and ``Image.open`` is never called (opening the 4000
        profiles takes the library seconds), wherever in the file they are: Odoo loads the PNGs
        it stores as attachments, which reads the chunks after the image data as well.
        """
        portal = self.env['res.users'].create({
            'name': 'Chunk portal fixture', 'login': 'doc-chunk-portal-fixture',
            'groups_id': [Command.set([self.env.ref('base.group_portal').id])]})
        for label, chunks in self._png_chunks_of_unequal_cost().items():
            for where, after in (('before', False), ('after', True)):
                content = png_with_chunk_list(chunks, after_image_data=after)
                for uploader, policy in (('internal', self.policy),
                                         ('portal', self.policy.with_user(portal))):
                    with self.subTest(chunks=label, image_data=where, uploader=uploader), \
                            patch.object(upload_policy.Image, 'open',
                                         side_effect=AssertionError('the image was opened')):
                        with self.assertRaises(WoowUploadRejected) as caught:
                            policy._validate('chunks.png', content)
                        self.assertEqual(caught.exception.code, 'content')

    def test_the_png_chunk_limits_are_exact(self):
        """A file at each limit is accepted and one chunk more is refused, wherever it is."""
        check = upload_policy._png_structure_ok
        texts, profiles = upload_policy.PNG_MAX_COMPRESSED_TEXTS, upload_policy.PNG_MAX_PROFILES
        deflated = zlib.compress(b'\x00' * 1024, 9)
        profile = png_chunk(b'iCCP', png_profile_payload(1024))
        ztxt = png_chunk(b'zTXt', b'key\x00\x00' + deflated)
        itxt = png_chunk(b'iTXt', b'key\x00\x01\x00\x00\x00' + deflated)
        # The end chunk counts, and the image data does not.
        at_the_limit = {
            'chunks': [png_chunk(b'prVt')] * (upload_policy.PNG_MAX_CHUNKS - 2),
            'profiles': [profile] * profiles,
            'zTXt': [ztxt] * texts,
            'iTXt': [itxt] * texts,
            'zTXt and iTXt': [ztxt] * (texts // 2) + [itxt] * (texts - texts // 2),
        }
        over_the_limit = {
            'chunks': [png_chunk(b'prVt')] * (upload_policy.PNG_MAX_CHUNKS - 1),
            'profiles': [profile] * (profiles + 1),
            'zTXt': [ztxt] * (texts + 1),
            'iTXt': [itxt] * (texts + 1),
            'zTXt and iTXt': [ztxt] * (texts // 2) + [itxt] * (texts - texts // 2 + 1),
        }
        for where, after in (('before', False), ('after', True)):
            for label, chunks in at_the_limit.items():
                with self.subTest(chunks=label, image_data=where, limit='at'):
                    self.assertTrue(check(png_with_chunk_list(chunks, after_image_data=after)))
                    self.assertEqual(
                        self.policy._validate('limit.png', png_with_chunk_list(
                            chunks, after_image_data=after)).mimetype, 'image/png')
            for label, chunks in over_the_limit.items():
                with self.subTest(chunks=label, image_data=where, limit='over'):
                    self.assertFalse(check(png_with_chunk_list(chunks, after_image_data=after)))

    def test_png_image_data_chunks_follow_the_size_of_the_file(self):
        """Image data may come in any number of chunks, but not in thousands of empty ones.

        The library reads each chunk in Python, so empty chunks cost it far more than their size,
        and the walk over them costs this check the same. Past a free allowance they must be
        as large as the encoders write them (kilobytes), or the file is refused. The allowance
        grows with the size of the file: one large picture cut into thousands of small chunks is
        accepted up to a bound and refused beyond it, where as many empty chunks in a tiny file
        are refused.
        """
        check = upload_policy._png_structure_ok
        free = upload_policy.PNG_FREE_IMAGE_CHUNKS
        self.assertTrue(check(png_with_image_chunks(free)))
        self.assertFalse(check(png_with_image_chunks(free + free // 4)))
        self.assertFalse(check(png_with_image_chunks(100000)))
        with patch.object(upload_policy.Image, 'open',
                          side_effect=AssertionError('the image was opened')):
            with self.assertRaises(WoowUploadRejected) as caught:
                self.policy._validate('split.png', png_with_image_chunks(100000))
        self.assertEqual(caught.exception.code, 'content')
        # An animation has an fcTL and an fdAT chunk for each of its frames.
        animation = apng_with_frames(upload_policy.PNG_MAX_CHUNKS + 44)
        self.assertGreater(animation.count(b'fcTL'), upload_policy.PNG_MAX_CHUNKS)
        self.assertTrue(check(animation))
        # The same large picture in more image data chunks than the free allowance: 1.6 times as
        # many, a valid file that is accepted, and twice as many, a valid file that is refused.
        # A flat limit of PNG_FREE_IMAGE_CHUNKS would refuse both.
        picture = png_of_noise(512)
        stream = sum(len(payload) for kind, payload in png_chunks_of(picture) if kind == b'IDAT')
        pixels = Image.open(io.BytesIO(picture)).tobytes()
        many = png_with_image_data_pieces(picture, stream // (free * 8 // 5))
        too_many = png_with_image_data_pieces(picture, stream // (free * 2))
        counts = {}
        for label, content in (('many', many), ('too many', too_many)):
            counts[label] = sum(kind == b'IDAT' for kind, _payload in png_chunks_of(content))
            self.assertEqual(Image.open(io.BytesIO(content)).tobytes(), pixels, label)
        self.assertGreater(counts['many'], free)
        self.assertGreater(counts['too many'], counts['many'])
        self.assertIs(check(many), True)
        self.assertEqual(self.policy._validate('many.png', many).mimetype, 'image/png')
        self.assertIs(check(too_many), False)
        with patch.object(upload_policy.Image, 'open',
                          side_effect=AssertionError('the image was opened')):
            with self.assertRaises(WoowUploadRejected) as caught:
                self.policy._validate('too-many.png', too_many)
        self.assertEqual(caught.exception.code, 'content')

    def test_images_with_ordinary_headers_are_still_accepted(self):
        within = upload_policy.IMAGE_MAX_STRUCTURES // 8
        phys = struct.pack('>2IB', 2835, 2835, 1)
        description = b'XML:com.adobe.xmp\x00\x00\x00\x00\x00<x/>'      # an uncompressed iTXt
        deflated = zlib.compress(b'\x00' * 1024, 9)
        ordinary = [png_chunk(b'iCCP', png_profile_payload(4096)), png_chunk(b'pHYs', phys),
                    png_chunk(b'tEXt', b'Software\x00test'), png_chunk(b'iTXt', description),
                    png_chunk(b'zTXt', b'key\x00\x00' + deflated)]
        for name, content, mimetype in (
                ('chunks.png', png_with_chunks(100), 'image/png'),
                ('segments.jpg', jpeg_with_segments(within), 'image/jpeg'),
                ('comments.gif', gif_with_comments(within), 'image/gif'),
                ('plain.png', png_with_chunks(0), 'image/png'),
                ('plain.jpg', jpeg_with_segments(0), 'image/jpeg'),
                ('plain.gif', gif_with_comments(0), 'image/gif'),
                # What editors and cameras write: a profile, the resolution, a few texts.
                ('profile.png', png_with_chunk_list(ordinary), 'image/png'),
                ('trailing.png', png_with_chunk_list(ordinary, after_image_data=True), 'image/png'),
                # A large picture as an encoder writes it: its image data is in chunks of 64 KB.
                # (The test of the image data chunks cuts such a picture into thousands of
                # small ones.)
                ('noise.png', png_of_noise(512), 'image/png'),
                ('split.png', png_with_image_chunks(50), 'image/png')):
            with self.subTest(file=name):
                self.assertEqual(self.policy._validate(name, content).mimetype, mimetype)

    def test_blocked_suffix_and_mime_disagreement(self):
        for name, content, expected in (
                ('test.svg', b'<svg/>', 'extension'),
                ('test.png', b'<html>payload</html>', 'mime'),
                ('../...', b'hello', 'filename')):
            with self.subTest(name=name), self.assertRaises(WoowUploadRejected) as caught:
                self.policy._validate(name, content)
            self.assertEqual(caught.exception.code, expected)

    def test_actual_length_obeys_typed_setting(self):
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_document_management.internal_max_upload_mb', '1')
        with self.assertRaises(WoowUploadRejected) as caught:
            self.policy._validate('large.txt', b'a' * (1024 * 1024 + 1))
        self.assertEqual(caught.exception.code, 'size')

    def test_served_type_never_reflects_unrecognised_mime(self):
        self.assertEqual(self.policy._served_mimetype(
            SimpleNamespace(mimetype='text/html')), 'application/octet-stream')
        self.assertEqual(self.policy._served_mimetype(
            SimpleNamespace(mimetype='text/plain')), 'text/plain; charset=utf-8')

    def test_portal_requires_libmagic_and_public_cannot_upload(self):
        portal = self.env['res.users'].create({
            'name': 'Upload portal fixture', 'login': 'doc-portal-fixture',
            'groups_id': [Command.set([self.env.ref('base.group_portal').id])],
        })
        with patch('odoo.tools.mimetypes.magic', None):
            with self.assertRaises(WoowUploadRejected) as caught:
                self.policy.with_user(portal)._validate('notes.txt', b'hello')
            self.assertEqual(caught.exception.code, 'no_libmagic')
        with self.assertRaises(WoowUploadRejected) as caught:
            self.policy.with_user(self.env.ref('base.public_user'))._validate('notes.txt', b'hello')
        self.assertEqual(caught.exception.code, 'channel')

    def test_ooxml_type_uses_complete_zip_directory(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as archive:
            archive.writestr('[Content_Types].xml', 'x' * 1200)
            archive.writestr('word/document.xml', '<document/>')
        result = self.policy._validate('report.docx', stream.getvalue())
        self.assertEqual(result.mimetype,
                         'application/vnd.openxmlformats-officedocument.wordprocessingml.document')

    def test_odf_mimetype_member_is_bounded(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('mimetype', 'application/vnd.oasis.opendocument.text')
            archive.writestr('content.xml', '<document/>')
        self.assertEqual(self.policy._validate('report.odt', stream.getvalue()).mimetype,
                         'application/vnd.oasis.opendocument.text')
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('mimetype', b'x' * (1024 * 1024))
        with self.assertRaises(WoowUploadRejected) as caught:
            self.policy._validate('bomb.odt', stream.getvalue())
        self.assertEqual(caught.exception.code, 'mime')

    ODF = {'odt': 'application/vnd.oasis.opendocument.text',
           'ods': 'application/vnd.oasis.opendocument.spreadsheet',
           'odp': 'application/vnd.oasis.opendocument.presentation'}

    def _odf(self, ext, compression):
        """An ODF file whose members (the ``mimetype`` one included) are written with ``compression``."""
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w', compression=compression) as archive:
            archive.writestr('mimetype', self.ODF[ext])
            archive.writestr('content.xml', '<document/>')
        return stream.getvalue()

    def test_a_stored_or_deflated_odf_mimetype_member_is_accepted(self):
        """The specification keeps the member stored; a writer that deflates it is accepted as before."""
        for ext, mimetype in self.ODF.items():
            for label, compression in (('stored', zipfile.ZIP_STORED),
                                       ('deflated', zipfile.ZIP_DEFLATED)):
                with self.subTest(ext=ext, member=label):
                    result = self.policy._validate('report.' + ext, self._odf(ext, compression))
                    self.assertEqual(result.mimetype, mimetype)

    def test_a_bzip2_or_lzma_odf_mimetype_member_is_refused_without_being_opened(self):
        """CPython unpacks a whole compressed block of such a member before it cuts the read."""
        for ext in self.ODF:
            for label, compression in (('bzip2', zipfile.ZIP_BZIP2), ('lzma', zipfile.ZIP_LZMA)):
                content = self._odf(ext, compression)
                info = zipfile.ZipFile(io.BytesIO(content)).getinfo('mimetype')
                self.assertEqual((info.compress_type, info.file_size <= 255, info.compress_size <= 1024),
                                 (compression, True, True))
                with self.subTest(ext=ext, member=label), \
                        patch.object(zipfile.ZipFile, 'open', side_effect=AssertionError('opened')):
                    with self.assertRaises(WoowUploadRejected) as caught:
                        self.policy._validate('report.' + ext, content)
                    self.assertEqual(caught.exception.code, 'mime')

    def test_an_odf_mimetype_member_that_declares_a_large_compressed_size_is_not_opened(self):
        """The compressed size is read from the directory of the archive, as the other sizes are."""
        for compression in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            content = bytearray(self._odf('odt', compression))
            first = content.index(b'PK\x01\x02')
            for size, opened in ((1024, True), (1025, False), (10 ** 6, False)):
                struct.pack_into('<I', content, first + 20, size)     # the compressed size
                with self.subTest(compression=compression, size=size), patch.object(
                        zipfile.ZipFile, 'open', return_value=io.BytesIO(self.ODF['odt'].encode())
                ) as open_member:
                    try:
                        result = self.policy._validate('report.odt', bytes(content))
                    except WoowUploadRejected as caught:
                        self.assertEqual(caught.code, 'mime')
                        result = None
                    self.assertEqual(result is not None, opened)
                    self.assertEqual(open_member.called, opened)

    def _zip_with_mimetype_member(self, ext, compression, size=64):
        """A ZIP of the kind ``ext`` that carries a member named ``mimetype`` of the given method."""
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_STORED) as archive:
            archive.writestr('[Content_Types].xml', '<Types/>')
            archive.writestr('word/document.xml', '<document/>')
            archive.writestr('xl/workbook.xml', '<workbook/>')
            archive.writestr('ppt/presentation.xml', '<presentation/>')
            archive.writestr(zipfile.ZipInfo('mimetype'), b'x' * size, compress_type=compression)
        return stream.getvalue()

    def test_a_mimetype_member_is_never_unpacked_by_the_storage_step_in_any_zip(self):
        """Without libmagic, ``ir.attachment`` falls back to Odoo's sniffer, which reads this member whole.

        The policy decides from the directory entry of the member, for every extension that is a ZIP,
        and the sniffer of core is never given a chance to read a bzip2 or LZMA member.
        """
        from odoo.tools import mimetypes as core_mimetypes
        for ext in ('zip', 'pptx', 'docx', 'xlsx', 'odt'):
            for label, compression in (('bzip2', zipfile.ZIP_BZIP2), ('lzma', zipfile.ZIP_LZMA)):
                content = self._zip_with_mimetype_member(ext, compression)
                with self.subTest(ext=ext, member=label), \
                        patch.object(zipfile.ZipFile, 'read', side_effect=AssertionError('read')), \
                        patch.object(zipfile.ZipFile, 'open', side_effect=AssertionError('opened')):
                    with self.assertRaises(WoowUploadRejected) as caught:
                        self.policy._validate('file.' + ext, content)
                    self.assertEqual(caught.exception.code, 'mime')
        # what the sniffer of core would have read, to show the member is what the test is about
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as archive:
            archive.writestr('data.txt', 'x')
            archive.writestr(zipfile.ZipInfo('mimetype'), b'x' * 64, compress_type=zipfile.ZIP_BZIP2)
        bomb = stream.getvalue()
        with patch.object(zipfile.ZipFile, 'read', autospec=True,
                          side_effect=zipfile.ZipFile.read) as spy:
            core_mimetypes._odoo_guess_mimetype(bomb)
        self.assertIn('mimetype', [call.args[1] for call in spy.call_args_list])

    def test_a_large_or_odd_mimetype_member_is_refused_in_any_zip_and_a_small_one_passes(self):
        for ext in ('zip', 'pptx'):
            for label, compression, size in (('large', zipfile.ZIP_STORED, 256),
                                             ('deflated large', zipfile.ZIP_DEFLATED, 4096)):
                with self.subTest(ext=ext, member=label):
                    with self.assertRaises(WoowUploadRejected) as caught:
                        self.policy._validate(
                            'file.' + ext, self._zip_with_mimetype_member(ext, compression, size))
                    self.assertEqual(caught.exception.code, 'mime')
            for label, compression in (('stored', zipfile.ZIP_STORED),
                                       ('deflated', zipfile.ZIP_DEFLATED)):
                with self.subTest(ext=ext, member=label, small=True):
                    result = self.policy._validate(
                        'file.' + ext, self._zip_with_mimetype_member(ext, compression))
                    self.assertEqual(result.mimetype, 'application/zip' if ext == 'zip' else
                                     'application/vnd.openxmlformats-officedocument.presentationml.presentation')

    def test_excessive_zip_entries_rejected_before_parser(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as archive:
            archive.writestr('[Content_Types].xml', '<Types/>')
            archive.writestr('word/document.xml', '<document/>')
            for number in range(4097):
                archive.writestr('empty/%04d' % number, b'')
        with patch.object(upload_policy.zipfile, 'ZipFile') as parser:
            with self.assertRaises(WoowUploadRejected) as caught:
                self.policy._validate('too-many.docx', stream.getvalue())
            self.assertEqual(caught.exception.code, 'mime')
            parser.assert_not_called()

    @staticmethod
    def _office_zip(entries, *, zip64=None, classic_total=None, classic_size=None):
        """A .docx-shaped ZIP whose end records can be made to disagree.

        ``zip64`` inserts a ZIP64 end record and locator declaring that many headers
        (they are all written); the classic end record keeps ``classic_total`` and
        ``classic_size`` when given.
        """
        names = [b'[Content_Types].xml', b'word/document.xml'] + [
            b'pad/%05d' % index for index in range(max(0, entries - 2))]
        local, headers = b'', b''
        for name in names:
            offset = len(local)
            local += struct.pack('<4s5H3I2H', b'PK\x03\x04', 20, 0, 0, 0, 0, 0, 0, 0,
                                 len(name), 0) + name
            headers += struct.pack('<4s6H3I5H2I', b'PK\x01\x02', 45, 20, 0, 0, 0, 0, 0, 0, 0,
                                   len(name), 0, 0, 0, 0, 0, offset) + name
        directory_at = len(local)
        tail = b''
        if zip64 is not None:
            record_at = directory_at + len(headers)
            tail = struct.pack('<4sQHHIIQQQQ', b'PK\x06\x06', 44, 45, 45, 0, 0, zip64, zip64,
                               len(headers), directory_at)
            tail += struct.pack('<4sIQI', b'PK\x06\x07', 0, record_at, 1)
        total = len(names) if classic_total is None else classic_total
        size = len(headers) if classic_size is None else classic_size
        end = struct.pack('<4s4H2IH', b'PK\x05\x06', 0, 0, total, total, size,
                          directory_at + len(headers) - size, 0)
        return local + headers + tail + end

    def test_zip64_end_record_cannot_lift_the_directory_bound(self):
        crafted = self._office_zip(5000, zip64=5000, classic_total=1, classic_size=46 + 19)
        self.assertEqual(len(zipfile.ZipFile(io.BytesIO(crafted)).infolist()), 5000)
        for name in ('crafted.docx', 'crafted.odt', 'crafted.pdf', 'crafted.txt'):
            with self.subTest(name=name), \
                    patch.object(upload_policy.zipfile, 'ZipFile') as parser, \
                    self.assertRaises(WoowUploadRejected) as caught:
                self.policy._validate(name, crafted)
            self.assertEqual(caught.exception.code, 'mime')
            parser.assert_not_called()
        # The classic record alone may not under-declare the headers ZipFile walks.
        walked = self._office_zip(5000, classic_total=1)
        self.assertEqual(len(zipfile.ZipFile(io.BytesIO(walked)).infolist()), 5000)
        with patch.object(upload_policy.zipfile, 'ZipFile') as parser, \
                self.assertRaises(WoowUploadRejected) as caught:
            self.policy._validate('walked.docx', walked)
        self.assertEqual(caught.exception.code, 'mime')
        parser.assert_not_called()

    def test_small_consistent_zip64_and_plain_archives_are_accepted(self):
        for archive in (self._office_zip(10, zip64=10), self._office_zip(10)):
            with self.subTest(zip64=b'PK\x06\x06' in archive):
                self.assertEqual(len(zipfile.ZipFile(io.BytesIO(archive)).infolist()), 10)
                self.assertEqual(
                    self.policy._validate('consistent.docx', archive).mimetype,
                    'application/vnd.openxmlformats-officedocument.wordprocessingml.document')
        dangling = self._office_zip(10)
        end = dangling.rfind(b'PK\x05\x06')
        dangling = dangling[:end] + struct.pack('<4sIQI', b'PK\x06\x07', 0, 0, 1) + dangling[end:]
        with self.assertRaises(WoowUploadRejected) as caught:
            self.policy._validate('dangling.docx', dangling)
        self.assertEqual(caught.exception.code, 'mime')
