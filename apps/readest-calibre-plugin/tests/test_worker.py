import os
import json
import shutil
import sys
import tempfile
import types
import unittest

PLUGIN_DIR = os.path.join(os.path.dirname(__file__), '..')
sys.path.insert(0, PLUGIN_DIR)

import api as readest_api  # noqa: E402
import wire as readest_wire  # noqa: E402


class _BoundSignal:
    def __init__(self):
        self._slots = []

    def connect(self, slot):
        self._slots.append(slot)

    def emit(self, *args):
        for slot in self._slots:
            slot(*args)


class _Signal:
    def __init__(self, *args):
        self.name = None

    def __set_name__(self, owner, name):
        self.name = '_signal_' + name

    def __get__(self, instance, owner):
        if instance is None:
            return self
        if self.name not in instance.__dict__:
            instance.__dict__[self.name] = _BoundSignal()
        return instance.__dict__[self.name]


class _QThread:
    def __init__(self, *args):
        pass


qt_module = types.ModuleType('qt')
qt_core = types.ModuleType('qt.core')
qt_core.QThread = _QThread
qt_core.pyqtSignal = _Signal
sys.modules['qt'] = qt_module
sys.modules['qt.core'] = qt_core

calibre_plugins = types.ModuleType('calibre_plugins')
readest_package = types.ModuleType('calibre_plugins.readest')
sys.modules['calibre_plugins'] = calibre_plugins
sys.modules['calibre_plugins.readest'] = readest_package
sys.modules['calibre_plugins.readest.api'] = readest_api
sys.modules['calibre_plugins.readest.wire'] = readest_wire

import worker as readest_worker  # noqa: E402
from worker import PushWorker, StatusWorker, _CloudWorker  # noqa: E402


class Metadata:
    title = 'The Test Book'
    authors = ['Alice Author']
    languages = ['eng']
    publisher = None
    pubdate = None
    comments = None
    tags = []
    series = None
    series_index = None
    uuid = 'cafebabe-0000-0000-0000-000000000001'

    def __init__(self, uuid=None):
        if uuid is not None:
            self.uuid = uuid

    def get_identifiers(self):
        return {}

    def custom_field_keys(self):
        return []

    def get(self, key):
        return None


class FakeDB:
    def __init__(self, formats, paths, cover_bytes=None):
        self._formats = formats
        self._paths = paths
        self._cover_bytes = cover_bytes

    def get_metadata(self, book_id):
        return Metadata()

    def formats(self, book_id):
        return list(self._formats)

    def format_abspath(self, book_id, fmt):
        return self._paths.get(fmt.upper())

    def cover(self, book_id):
        return self._cover_bytes


class FakeMultiDB:
    def __init__(self, formats_by_book, paths_by_book, uuids_by_book):
        self.formats_by_book = formats_by_book
        self.paths_by_book = paths_by_book
        self.uuids_by_book = uuids_by_book

    def get_metadata(self, book_id):
        return Metadata(self.uuids_by_book[book_id])

    def formats(self, book_id):
        return list(self.formats_by_book[book_id])

    def format_abspath(self, book_id, fmt):
        return self.paths_by_book[book_id].get(fmt.upper())

    def cover(self, book_id):
        return None


class FakeClient:
    def __init__(self, rows=None, files=None):
        self.rows = list(rows or [])
        self.files = list(files or [])
        self.upload_requests = []
        self.uploads = []
        self.pushed_batches = []
        self.deleted_files = []

    def pull_books(self):
        return list(self.rows)

    def list_files_page(self, page):
        return list(self.files), 1

    def list_files(self, book_hash):
        marker = '/Readest/Books/%s/' % book_hash
        return [record for record in self.files if marker in record.get('file_key', '')]

    def get_upload_url(self, file_name, size, book_hash):
        self.upload_requests.append(
            {'fileName': file_name, 'size': size, 'bookHash': book_hash}
        )
        return {'uploadUrl': 'memory://' + file_name}

    def put_file(self, upload_url, fileobj, size):
        self.uploads.append(
            {'uploadUrl': upload_url, 'size': size, 'body': fileobj.read()}
        )

    def push_books(self, records):
        self.pushed_batches.append(list(records))

    def delete_file(self, file_key):
        self.deleted_files.append(file_key)


def cloud_file(book_hash, fmt):
    return {'file_key': 'user-1/' + readest_wire.book_file_name(book_hash, fmt)}


def server_row(book_hash, fmt, metadata=None, **overrides):
    row = {
        'book_hash': book_hash,
        'meta_hash': 'm' * 32,
        'format': fmt,
        'title': 'The Test Book',
        'author': 'Alice Author',
        'tags': [],
        'metadata': json.dumps(metadata) if metadata is not None else None,
        'created_at': '2024-01-01T00:00:00.000Z',
        'updated_at': '2024-01-02T00:00:00.000Z',
        'deleted_at': None,
        'uploaded_at': '2024-01-01T12:00:00.000Z',
    }
    row.update(overrides)
    return row


def calibre_metadata(source_hash, uuid=Metadata.uuid):
    return {
        'title': 'The Test Book',
        'author': 'Alice Author',
        'language': 'eng',
        'identifier': 'urn:uuid:%s' % uuid,
        'calibreSourceHash': source_hash,
    }


class WorkerFormatPlanningTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = {}
        for fmt, body in (('EPUB', b'epub bytes'), ('PDF', b'pdf bytes')):
            path = os.path.join(self.tmp.name, 'book.%s' % fmt.lower())
            with open(path, 'wb') as handle:
                handle.write(body)
            self.paths[fmt] = path

    def make_worker(self, formats, requested_format=None):
        worker = _CloudWorker(
            None,
            FakeDB(formats, self.paths),
            [1],
            object(),
            False,
            requested_format,
        )
        worker.by_hash = {}
        worker.by_uuid = {}
        worker.cloud_hashes = set()
        return worker

    def test_default_uses_priority_format(self):
        ctx, reason = self.make_worker(['PDF', 'EPUB'])._plan_for(1)
        self.assertEqual(reason, '')
        self.assertEqual(ctx.fmt, 'EPUB')
        self.assertEqual(ctx.path, self.paths['EPUB'])
        self.assertEqual(ctx.plan['action'], 'new')

    def test_requested_format_overrides_priority(self):
        ctx, reason = self.make_worker(['EPUB', 'PDF'], 'PDF')._plan_for(1)
        self.assertEqual(reason, '')
        self.assertEqual(ctx.fmt, 'PDF')
        self.assertEqual(ctx.path, self.paths['PDF'])
        self.assertEqual(ctx.plan['action'], 'new')

    def test_requested_format_is_case_insensitive(self):
        ctx, reason = self.make_worker(['EPUB', 'PDF'], 'pdf')._plan_for(1)
        self.assertEqual(reason, '')
        self.assertEqual(ctx.fmt, 'PDF')

    def test_requested_format_must_be_available(self):
        ctx, reason = self.make_worker(['EPUB'], 'PDF')._plan_for(1)
        self.assertIsNone(ctx)
        self.assertIn('Requested format PDF', reason)

    def test_requested_format_must_be_supported(self):
        ctx, reason = self.make_worker(['DOCX'], 'DOCX')._plan_for(1)
        self.assertIsNone(ctx)
        self.assertIn('Requested format DOCX', reason)


class WorkerStatusFormatReportingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = {}
        for fmt, body in (('EPUB', b'epub bytes'), ('PDF', b'pdf bytes')):
            path = os.path.join(self.tmp.name, 'book.%s' % fmt.lower())
            with open(path, 'wb') as handle:
                handle.write(body)
            self.paths[fmt] = path
        self.epub_hash = readest_api.partial_md5(self.paths['EPUB'])
        self.pdf_hash = readest_api.partial_md5(self.paths['PDF'])

    def run_status_worker(self, formats, requested_format=None, client=None):
        worker = StatusWorker(
            None,
            FakeDB(formats, self.paths),
            [1],
            client or FakeClient(),
            False,
            requested_format,
        )
        rows = []
        done = []
        worker.book_status.connect(lambda *args: rows.append(args))
        worker.done.connect(lambda *args: done.append(args))
        worker.run()
        return rows, done

    def test_status_worker_reports_default_chosen_format(self):
        rows, done = self.run_status_worker(['PDF', 'EPUB'])

        self.assertEqual(rows, [(1, 'new', 'EPUB', '')])
        self.assertEqual(done[0][0], True)

    def test_status_worker_reports_requested_chosen_format(self):
        rows, done = self.run_status_worker(['PDF', 'EPUB'], 'PDF')

        self.assertEqual(rows, [(1, 'new', 'PDF', '')])
        self.assertEqual(done[0][0], True)

    def test_status_worker_defaults_to_live_bound_format(self):
        bound = server_row(self.pdf_hash, 'PDF', calibre_metadata(self.pdf_hash))
        old_epub_peer = server_row(
            'e' * 32,
            'EPUB',
            calibre_metadata(self.epub_hash),
            deleted_at='2024-05-01T00:00:00.000Z',
        )
        client = FakeClient([old_epub_peer, bound], [cloud_file(self.pdf_hash, 'PDF')])

        rows, done = self.run_status_worker(['EPUB', 'PDF'], client=client)

        self.assertEqual(rows, [(1, 'skip', 'PDF', '')])
        self.assertEqual(done[0][0], True)

    def test_status_worker_requested_format_overrides_bound_format(self):
        bound = server_row(self.pdf_hash, 'PDF', calibre_metadata(self.pdf_hash))
        client = FakeClient([bound], [cloud_file(self.pdf_hash, 'PDF')])

        rows, done = self.run_status_worker(
            ['EPUB', 'PDF'],
            requested_format='EPUB',
            client=client,
        )

        self.assertEqual(rows, [(1, 'replace', 'EPUB', '')])
        self.assertEqual(done[0][0], True)

    def test_status_worker_reports_missing_bound_format(self):
        bound = server_row(self.pdf_hash, 'PDF', calibre_metadata(self.pdf_hash))
        client = FakeClient([bound], [cloud_file(self.pdf_hash, 'PDF')])

        rows, done = self.run_status_worker(['EPUB'], client=client)

        self.assertEqual(rows[0][:3], (1, 'failed', ''))
        self.assertIn('Bound format PDF', rows[0][3])
        self.assertEqual(done[0][0], False)

    def test_status_worker_has_no_format_when_none_is_chosen(self):
        rows, done = self.run_status_worker(['EPUB'], 'PDF')

        self.assertEqual(rows[0][:3], (1, 'failed', ''))
        self.assertIn('Requested format PDF', rows[0][3])
        self.assertEqual(done[0][0], False)


class WorkerPushFormatBindingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = {}
        for fmt, body in (('EPUB', b'epub bytes'), ('PDF', b'pdf bytes')):
            path = os.path.join(self.tmp.name, 'book.%s' % fmt.lower())
            with open(path, 'wb') as handle:
                handle.write(body)
            self.paths[fmt] = path
        self.epub_hash = readest_api.partial_md5(self.paths['EPUB'])
        self.pdf_hash = readest_api.partial_md5(self.paths['PDF'])
        self.original_embed = readest_worker._embed_metadata_copy
        readest_worker._embed_metadata_copy = self.embed_metadata_copy
        self.addCleanup(self.restore_embed_metadata_copy)

    def restore_embed_metadata_copy(self):
        readest_worker._embed_metadata_copy = self.original_embed

    def embed_metadata_copy(self, path, mi, fmt):
        fd, tmp = tempfile.mkstemp(
            suffix='.' + fmt.lower(), prefix='embedded-', dir=self.tmp.name
        )
        os.close(fd)
        shutil.copyfile(path, tmp)
        return tmp

    def run_push(self, client, requested_format=None):
        return self.run_push_with_db(
            FakeDB(['EPUB', 'PDF'], self.paths),
            [1],
            client,
            requested_format,
        )

    def run_push_with_db(self, db, book_ids, client, requested_format=None):
        worker = PushWorker(
            None,
            db,
            book_ids,
            client,
            False,
            requested_format,
        )
        rows = []
        done = []
        worker.book_status.connect(lambda *args: rows.append(args))
        worker.done.connect(lambda *args: done.append(args))
        worker.run()
        return rows, done

    def add_book_files(self, book_id):
        paths = {}
        for fmt in ('EPUB', 'PDF'):
            path = os.path.join(self.tmp.name, 'book-%s.%s' % (book_id, fmt.lower()))
            with open(path, 'wb') as handle:
                handle.write(('%s %s bytes' % (book_id, fmt)).encode('ascii'))
            paths[fmt] = path
        return paths

    def direct_pdf_row(self):
        return server_row(
            self.pdf_hash,
            'PDF',
            progress=[42, 100],
            reading_status='reading',
            reading_status_updated_at='2024-03-01T00:00:00.000Z',
            group_id='pdf-group',
            group_name='PDF Group',
        )

    def assert_pdf_engagement_preserved(self, record):
        self.assertEqual(record['bookHash'], self.pdf_hash)
        self.assertEqual(record['format'], 'PDF')
        self.assertEqual(record['progress'], [42, 100])
        self.assertEqual(record['readingStatus'], 'reading')
        self.assertEqual(record['groupId'], 'pdf-group')
        self.assertEqual(record['groupName'], 'PDF Group')
        self.assertEqual(
            record['metadata']['identifier'],
            'urn:uuid:cafebabe-0000-0000-0000-000000000001',
        )

    def test_requested_format_replaces_existing_uuid_peer_without_duplicate(self):
        old_hash = 'e' * 32
        existing = server_row(old_hash, 'EPUB', calibre_metadata(self.epub_hash))
        client = FakeClient([existing], [cloud_file(old_hash, 'EPUB')])

        rows, done = self.run_push(client, 'PDF')

        self.assertEqual(rows, [(1, 'replaced', 'PDF', '')])
        self.assertEqual(done[0][0], True)
        self.assertEqual(
            client.upload_requests[0]['fileName'],
            readest_wire.book_file_name(self.pdf_hash, 'PDF'),
        )
        self.assertEqual(len(client.pushed_batches), 1)
        live = [record for record in client.pushed_batches[0] if not record.get('deletedAt')]
        tombstones = [record for record in client.pushed_batches[0] if record.get('deletedAt')]
        self.assertEqual(len(live), 1)
        self.assertEqual(len(tombstones), 1)
        self.assertEqual(live[0]['bookHash'], self.pdf_hash)
        self.assertEqual(live[0]['format'], 'PDF')
        self.assertEqual(
            live[0]['metadata']['identifier'],
            'urn:uuid:cafebabe-0000-0000-0000-000000000001',
        )
        self.assertEqual(tombstones[0]['bookHash'], old_hash)
        self.assertIn(old_hash, ''.join(client.deleted_files))

    def test_requested_format_preserves_live_hash_row_over_tombstoned_uuid_peer(self):
        old_hash = 'e' * 32
        direct_pdf = self.direct_pdf_row()
        deleted_epub_peer = server_row(
            old_hash,
            'EPUB',
            calibre_metadata(self.epub_hash),
            deleted_at='2024-05-01T00:00:00.000Z',
        )
        client = FakeClient(
            [direct_pdf, deleted_epub_peer],
            [cloud_file(self.pdf_hash, 'PDF'), cloud_file(old_hash, 'EPUB')],
        )

        rows, done = self.run_push(client, 'PDF')

        self.assertEqual(rows, [(1, 'updated', 'PDF', '')])
        self.assertEqual(done[0][0], True)
        self.assertEqual(client.upload_requests, [])
        self.assertEqual(len(client.pushed_batches), 1)
        self.assertEqual(len(client.pushed_batches[0]), 1)
        self.assert_pdf_engagement_preserved(client.pushed_batches[0][0])
        self.assertIsNone(client.pushed_batches[0][0]['deletedAt'])

    def test_requested_format_preserves_live_hash_row_over_live_uuid_peer(self):
        old_hash = 'e' * 32
        direct_pdf = self.direct_pdf_row()
        epub_peer = server_row(old_hash, 'EPUB', calibre_metadata(self.epub_hash))
        client = FakeClient(
            [direct_pdf, epub_peer],
            [cloud_file(self.pdf_hash, 'PDF'), cloud_file(old_hash, 'EPUB')],
        )

        rows, done = self.run_push(client, 'PDF')

        self.assertEqual(rows, [(1, 'updated', 'PDF', '')])
        self.assertEqual(done[0][0], True)
        self.assertEqual(client.upload_requests, [])
        self.assertEqual(len(client.pushed_batches), 1)
        live = [record for record in client.pushed_batches[0] if not record.get('deletedAt')]
        tombstones = [record for record in client.pushed_batches[0] if record.get('deletedAt')]
        self.assertEqual(len(live), 1)
        self.assertEqual(len(tombstones), 1)
        self.assert_pdf_engagement_preserved(live[0])
        self.assertEqual(tombstones[0]['bookHash'], old_hash)
        self.assertIn(old_hash, ''.join(client.deleted_files))

    def test_first_push_binds_existing_hash_match_without_uploading_duplicate(self):
        existing = server_row(self.pdf_hash, 'PDF')
        client = FakeClient([existing], [cloud_file(self.pdf_hash, 'PDF')])

        rows, done = self.run_push(client, 'PDF')

        self.assertEqual(rows, [(1, 'updated', 'PDF', '')])
        self.assertEqual(done[0][0], True)
        self.assertEqual(client.upload_requests, [])
        self.assertEqual(client.uploads, [])
        self.assertEqual(len(client.pushed_batches), 1)
        self.assertEqual(len(client.pushed_batches[0]), 1)
        record = client.pushed_batches[0][0]
        self.assertEqual(record['bookHash'], self.pdf_hash)
        self.assertEqual(record['format'], 'PDF')
        self.assertEqual(
            record['metadata']['identifier'],
            'urn:uuid:cafebabe-0000-0000-0000-000000000001',
        )
        self.assertIsNone(record['deletedAt'])

    def test_push_repairs_missing_cloud_cover_object(self):
        cover_bytes = b'calibre cover bytes'
        cover_hash = readest_api.partial_md5_bytes(cover_bytes)
        existing = server_row(
            self.pdf_hash,
            'PDF',
            calibre_metadata(self.pdf_hash),
            cover_hash=cover_hash,
            cover_updated_at='2024-01-01T00:00:00.000Z',
        )
        client = FakeClient([existing], [cloud_file(self.pdf_hash, 'PDF')])
        db = FakeDB(['PDF'], self.paths, cover_bytes)

        rows, done = self.run_push_with_db(db, [1], client, 'PDF')

        self.assertEqual(rows, [(1, 'updated', 'PDF', '')])
        self.assertEqual(done[0][0], True)
        self.assertEqual(len(client.upload_requests), 1)
        self.assertEqual(
            client.upload_requests[0]['fileName'],
            readest_wire.cover_file_name(self.pdf_hash),
        )
        self.assertEqual(client.uploads[0]['body'], cover_bytes)
        self.assertEqual(client.pushed_batches[0][0]['coverHash'], cover_hash)

    def test_replace_without_local_cover_does_not_carry_orphaned_cover_hash(self):
        old_hash = 'e' * 32
        existing = server_row(
            old_hash,
            'EPUB',
            calibre_metadata(self.epub_hash),
            cover_hash='c' * 32,
            cover_updated_at='2024-01-01T00:00:00.000Z',
        )
        client = FakeClient([existing], [cloud_file(old_hash, 'EPUB')])

        rows, done = self.run_push(client, 'PDF')

        self.assertEqual(rows, [(1, 'replaced', 'PDF', '')])
        self.assertEqual(done[0][0], True)
        live = [record for record in client.pushed_batches[0] if not record.get('deletedAt')]
        self.assertEqual(len(live), 1)
        self.assertNotIn('coverHash', live[0])
        self.assertNotIn('coverUpdatedAt', live[0])

    def test_default_push_respects_bound_formats_for_bulk_selection(self):
        book_ids = [1, 2, 3, 4, 5]
        uuids = {
            book_id: 'cafebabe-0000-0000-0000-%012d' % book_id
            for book_id in book_ids
        }
        formats_by_book = {book_id: ['EPUB', 'PDF'] for book_id in book_ids}
        paths_by_book = {book_id: self.add_book_files(book_id) for book_id in book_ids}
        bound_formats = {
            1: 'PDF',
            2: 'EPUB',
            3: 'PDF',
            4: 'EPUB',
            5: 'PDF',
        }
        rows = []
        files = []
        for book_id in book_ids:
            fmt = bound_formats[book_id]
            book_hash = readest_api.partial_md5(paths_by_book[book_id][fmt])
            rows.append(server_row(book_hash, fmt, calibre_metadata(book_hash, uuids[book_id])))
            files.append(cloud_file(book_hash, fmt))
        client = FakeClient(rows, files)
        db = FakeMultiDB(formats_by_book, paths_by_book, uuids)

        status_rows, done = self.run_push_with_db(db, book_ids, client)

        self.assertEqual(done[0][0], True)
        self.assertEqual(
            [(book_id, status, fmt) for book_id, status, fmt, detail in status_rows],
            [(book_id, 'skipped', bound_formats[book_id]) for book_id in book_ids],
        )
        self.assertEqual(client.upload_requests, [])
        self.assertEqual(client.pushed_batches, [])

    def test_default_push_fails_when_bound_format_is_missing_locally(self):
        existing = server_row(self.pdf_hash, 'PDF', calibre_metadata(self.pdf_hash))
        client = FakeClient([existing], [cloud_file(self.pdf_hash, 'PDF')])

        rows, done = self.run_push_with_db(FakeDB(['EPUB'], self.paths), [1], client)

        self.assertEqual(rows[0][:3], (1, 'failed', ''))
        self.assertIn('Bound format PDF', rows[0][3])
        self.assertEqual(done[0][0], False)
        self.assertEqual(client.upload_requests, [])
        self.assertEqual(client.pushed_batches, [])


if __name__ == '__main__':
    unittest.main()
