import os
import sys
import types
import unittest

from translation_stubs import install_translation_stubs


install_translation_stubs()
PLUGIN_DIR = os.path.join(os.path.dirname(__file__), '..')
sys.path.insert(0, PLUGIN_DIR)


class _Signal:
    def __init__(self, *args):
        pass

    def connect(self, *args):
        pass


class _Widget:
    def __init__(self, *args):
        pass


class _QDialog(_Widget):
    pass


class _QDialogButtonBox(_Widget):
    class StandardButton:
        Cancel = object()
        Close = object()


class _QLineEdit(_Widget):
    class EchoMode:
        Password = object()


class _QTableWidget(_Widget):
    class EditTrigger:
        NoEditTriggers = object()


class _QTableWidgetItem:
    def __init__(self, text):
        self._text = text

    def text(self):
        return self._text


qt_module = types.ModuleType('qt')
qt_core = types.ModuleType('qt.core')
for name in (
    'QFormLayout',
    'QHBoxLayout',
    'QLabel',
    'QProgressBar',
    'QPushButton',
    'QThread',
    'QUrl',
    'QVBoxLayout',
):
    setattr(qt_core, name, _Widget)
qt_core.QDialog = _QDialog
qt_core.QDialogButtonBox = _QDialogButtonBox
qt_core.QLineEdit = _QLineEdit
qt_core.QTableWidget = _QTableWidget
qt_core.QTableWidgetItem = _QTableWidgetItem
qt_core.pyqtSignal = _Signal
sys.modules['qt'] = qt_module
sys.modules['qt.core'] = qt_core

calibre_module = types.ModuleType('calibre')
calibre_gui2 = types.ModuleType('calibre.gui2')
calibre_gui2.error_dialog = lambda *args, **kwargs: None
calibre_gui2.open_url = lambda *args, **kwargs: None
sys.modules['calibre'] = calibre_module
sys.modules['calibre.gui2'] = calibre_gui2

calibre_plugins = types.ModuleType('calibre_plugins')
readest_package = types.ModuleType('calibre_plugins.readest')
readest_api = types.ModuleType('calibre_plugins.readest.api')
readest_api.ReadestAPIError = type('ReadestAPIError', (Exception,), {})
readest_oauth = types.ModuleType('calibre_plugins.readest.oauth')
readest_oauth.PROVIDERS = []
readest_oauth.OAuthCallbackServer = object
readest_oauth.build_authorize_url = lambda *args: ''
readest_worker = types.ModuleType('calibre_plugins.readest.worker')
readest_worker.CHECK_LABELS = {'skip': 'In Readest'}
readest_worker.STATUS_LABELS = {}
readest_worker.PushWorker = object
readest_worker.StatusWorker = object
sys.modules['calibre_plugins'] = calibre_plugins
sys.modules['calibre_plugins.readest'] = readest_package
sys.modules['calibre_plugins.readest.api'] = readest_api
sys.modules['calibre_plugins.readest.oauth'] = readest_oauth
sys.modules['calibre_plugins.readest.worker'] = readest_worker

import dialogs as readest_dialogs  # noqa: E402


class _DB:
    def field_for(self, field, book_id):
        return 'The Test Book'


class _Table:
    def __init__(self):
        self.rows = []
        self.scrolled = False

    def rowCount(self):
        return len(self.rows)

    def insertRow(self, row):
        self.rows.insert(row, [None, None, None, None])

    def setItem(self, row, column, item):
        self.rows[row][column] = item.text()

    def scrollToBottom(self):
        self.scrolled = True


class RunDialogStatusTest(unittest.TestCase):
    def test_book_status_renders_format_column(self):
        dialog = object.__new__(readest_dialogs._RunDialog)
        dialog.db = _DB()
        dialog.labels = {'skip': 'In Readest'}
        dialog.table = _Table()

        readest_dialogs._RunDialog.on_book_status(dialog, 1, 'skip', 'PDF', '')

        self.assertEqual(dialog.table.rows, [['The Test Book', 'PDF', 'In Readest', '']])
        self.assertTrue(dialog.table.scrolled)


if __name__ == '__main__':
    unittest.main()
