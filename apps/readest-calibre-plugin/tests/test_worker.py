import os
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

from worker import StatusWorker, _CloudWorker  # noqa: E402


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

    def get_identifiers(self):
        return {}

    def custom_field_keys(self):
        return []

    def get(self, key):
        return None


class FakeDB:
    def __init__(self, formats, paths):
        self._formats = formats
        self._paths = paths

    def get_metadata(self, book_id):
        return Metadata()

    def formats(self, book_id):
        return list(self._formats)

    def format_abspath(self, book_id, fmt):
        return self._paths.get(fmt.upper())

    def cover(self, book_id):
        return None


class FakeClient:
    def pull_books(self):
        return []

    def list_files_page(self, page):
        return [], 1


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

    def run_status_worker(self, formats, requested_format=None):
        worker = StatusWorker(
            None,
            FakeDB(formats, self.paths),
            [1],
            FakeClient(),
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

    def test_status_worker_has_no_format_when_none_is_chosen(self):
        rows, done = self.run_status_worker(['EPUB'], 'PDF')

        self.assertEqual(rows[0][:3], (1, 'failed', ''))
        self.assertIn('Requested format PDF', rows[0][3])
        self.assertEqual(done[0][0], False)


if __name__ == '__main__':
    unittest.main()
