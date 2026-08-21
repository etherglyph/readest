import os
import sys
import tempfile
import types
import unittest

PLUGIN_DIR = os.path.join(os.path.dirname(__file__), '..')
sys.path.insert(0, PLUGIN_DIR)

import api as readest_api  # noqa: E402
import wire as readest_wire  # noqa: E402


class _Signal:
    def __init__(self, *args):
        pass

    def connect(self, *args):
        pass

    def emit(self, *args):
        pass


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

from worker import _CloudWorker  # noqa: E402


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


if __name__ == '__main__':
    unittest.main()
