import os
import sys
import types
import unittest

PLUGIN_DIR = os.path.join(os.path.dirname(__file__), '..')
sys.path.insert(0, PLUGIN_DIR)

import api as readest_api  # noqa: E402
import wire as readest_wire  # noqa: E402


class _Signal:
    def __init__(self):
        self._slots = []

    def connect(self, slot):
        self._slots.append(slot)

    def emit(self, *args):
        for slot in self._slots:
            slot(*args)


class _Action:
    def __init__(self, text=''):
        self._text = text
        self.enabled = True
        self.visible = True
        self.menu = None
        self.triggered = _Signal()

    def setEnabled(self, value):
        self.enabled = value

    def setVisible(self, value):
        self.visible = value

    def setText(self, text):
        self._text = text

    def text(self):
        return self._text

    def setMenu(self, menu):
        self.menu = menu

    def setIcon(self, icon):
        self.icon = icon

    def trigger(self):
        self.triggered.emit()


class _QMenu:
    def __init__(self, *args):
        title = args[0] if args and isinstance(args[0], str) else ''
        self._action = _Action(title)
        self.actions = []
        self.aboutToShow = _Signal()

    def addAction(self, action_or_text):
        action = _Action(action_or_text) if isinstance(action_or_text, str) else action_or_text
        self.actions.append(action)
        return action

    def addMenu(self, menu):
        action = menu.menuAction()
        action.setMenu(menu)
        self.actions.append(action)
        return action

    def addSeparator(self):
        self.actions.append(None)

    def clear(self):
        self.actions = []

    def menuAction(self):
        return self._action


class _InterfaceAction:
    def create_action(self, spec, attr=None):
        return _Action(spec[0])


qt_module = types.ModuleType('qt')
qt_core = types.ModuleType('qt.core')
qt_core.QMenu = _QMenu
sys.modules['qt'] = qt_module
sys.modules['qt.core'] = qt_core

calibre_module = types.ModuleType('calibre')
calibre_gui2 = types.ModuleType('calibre.gui2')
calibre_actions = types.ModuleType('calibre.gui2.actions')
calibre_gui2.error_dialog = lambda *args, **kwargs: None
calibre_gui2.info_dialog = lambda *args, **kwargs: None
calibre_actions.InterfaceAction = _InterfaceAction
sys.modules['calibre'] = calibre_module
sys.modules['calibre.gui2'] = calibre_gui2
sys.modules['calibre.gui2.actions'] = calibre_actions

calibre_plugins = types.ModuleType('calibre_plugins')
readest_package = types.ModuleType('calibre_plugins.readest')
readest_config = types.ModuleType('calibre_plugins.readest.config')
readest_config.prefs = {
    'api_base': '',
    'supabase_url': '',
    'tokens': {'access_token': 'token'},
    'user_email': '',
    'include_custom_columns': False,
}
readest_config.save_tokens = lambda tokens: None
readest_dialogs = types.ModuleType('calibre_plugins.readest.dialogs')
readest_dialogs.LoginDialog = object
readest_dialogs.PushDialog = type('PushDialog', (), {})
readest_dialogs.StatusDialog = type('StatusDialog', (), {})
sys.modules['calibre_plugins'] = calibre_plugins
sys.modules['calibre_plugins.readest'] = readest_package
sys.modules['calibre_plugins.readest.api'] = readest_api
sys.modules['calibre_plugins.readest.config'] = readest_config
sys.modules['calibre_plugins.readest.dialogs'] = readest_dialogs
sys.modules['calibre_plugins.readest.wire'] = readest_wire

import ui as readest_ui  # noqa: E402

readest_ui.get_icons = lambda *args: object()


class _LibraryView:
    def __init__(self, selected_ids):
        self._selected_ids = selected_ids

    def get_selected_ids(self):
        return list(self._selected_ids)


class _DB:
    def __init__(self, formats_by_book):
        self._formats_by_book = formats_by_book

    def formats(self, book_id):
        return self._formats_by_book.get(book_id, [])


class _CurrentDB:
    def __init__(self, formats_by_book):
        self.new_api = _DB(formats_by_book)


class _GUI:
    def __init__(self, selected_ids, formats_by_book):
        self.library_view = _LibraryView(selected_ids)
        self.current_db = _CurrentDB(formats_by_book)


def make_plugin(selected_ids, formats_by_book):
    plugin = readest_ui.ReadestInterfacePlugin()
    plugin.gui = _GUI(selected_ids, formats_by_book)
    plugin.qaction = _Action('Readest')
    plugin.genesis()
    return plugin


def action_texts(menu):
    return [action.text() for action in menu.actions if action is not None]


class ReadestMenuTest(unittest.TestCase):
    def test_default_push_keeps_requested_format_unset(self):
        plugin = make_plugin([1], {1: ['EPUB', 'PDF']})
        calls = []
        plugin.run_on_selection = lambda dialog_class, message, requested_format=None: calls.append(
            requested_format
        )

        plugin.push_action.trigger()

        self.assertEqual(calls, [None])

    def test_shows_format_submenu_for_multiple_supported_formats(self):
        plugin = make_plugin([1], {1: ['PDF', 'DOCX', 'EPUB']})

        plugin.update_menu()

        self.assertTrue(plugin.push_format_action.visible)
        self.assertEqual(action_texts(plugin.push_format_menu), ['EPUB', 'PDF'])

    def test_hides_format_submenu_without_multiple_common_formats(self):
        plugin = make_plugin([1, 2], {1: ['EPUB', 'PDF'], 2: ['EPUB', 'MOBI']})

        plugin.update_menu()

        self.assertFalse(plugin.push_format_action.visible)
        self.assertEqual(action_texts(plugin.push_format_menu), [])

    def test_format_submenu_uses_common_formats_for_multi_selection(self):
        plugin = make_plugin(
            [1, 2],
            {1: ['EPUB', 'PDF', 'MOBI'], 2: ['PDF', 'MOBI', 'TXT']},
        )

        plugin.update_menu()

        self.assertTrue(plugin.push_format_action.visible)
        self.assertEqual(action_texts(plugin.push_format_menu), ['PDF', 'MOBI'])

    def test_format_action_pushes_requested_format(self):
        plugin = make_plugin([1], {1: ['EPUB', 'PDF']})
        calls = []
        plugin.run_on_selection = lambda dialog_class, message, requested_format=None: calls.append(
            (dialog_class, requested_format)
        )
        plugin.update_menu()

        plugin.push_format_menu.actions[1].trigger()

        self.assertEqual(calls, [(readest_ui.PushDialog, 'PDF')])


if __name__ == '__main__':
    unittest.main()
