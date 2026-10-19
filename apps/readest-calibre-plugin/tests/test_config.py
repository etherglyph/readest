import importlib.util
import os
import sys
import types
import unittest
from unittest.mock import patch

from translation_stubs import install_translation_stubs


PLUGIN_DIR = os.path.join(os.path.dirname(__file__), '..')
install_translation_stubs()


class _JSONConfig(dict):
    def __init__(self, name):
        super().__init__()
        self.name = name
        self.defaults = {}

    def __getitem__(self, key):
        return self.get(key, self.defaults[key])


class _Widget:
    def __init__(self, *args):
        self.layout = None

    def setLayout(self, layout):
        self.layout = layout


class _CheckBox(_Widget):
    def __init__(self, text):
        super().__init__()
        self.text = text
        self.checked = False
        self.tooltip = ''

    def setChecked(self, checked):
        self.checked = checked

    def isChecked(self):
        return self.checked

    def setToolTip(self, tooltip):
        self.tooltip = tooltip


class _LineEdit(_Widget):
    def __init__(self, *args):
        super().__init__(*args)
        self.value = ''

    def setText(self, value):
        self.value = value

    def text(self):
        return self.value

    def setToolTip(self, tooltip):
        pass


class _Layout:
    def __init__(self):
        self.widgets = []

    def addWidget(self, widget):
        self.widgets.append(widget)

    def addStretch(self):
        pass


def load_config_module():
    calibre = types.ModuleType('calibre')
    calibre_utils = types.ModuleType('calibre.utils')
    calibre_config = types.ModuleType('calibre.utils.config')
    calibre_config.JSONConfig = _JSONConfig

    qt = types.ModuleType('qt')
    qt_core = types.ModuleType('qt.core')
    qt_core.QCheckBox = _CheckBox
    qt_core.QLabel = _Widget
    qt_core.QLineEdit = _LineEdit
    qt_core.QVBoxLayout = _Layout
    qt_core.QWidget = _Widget

    calibre_plugins = types.ModuleType('calibre_plugins')
    readest = types.ModuleType('calibre_plugins.readest')
    readest_api = types.ModuleType('calibre_plugins.readest.api')
    readest_api.DEFAULT_API_BASE = 'https://example.test/api'
    readest_api.DEFAULT_SUPABASE_URL = 'https://example.test/supabase'

    modules = {
        'calibre': calibre,
        'calibre.utils': calibre_utils,
        'calibre.utils.config': calibre_config,
        'qt': qt,
        'qt.core': qt_core,
        'calibre_plugins': calibre_plugins,
        'calibre_plugins.readest': readest,
        'calibre_plugins.readest.api': readest_api,
    }
    path = os.path.join(PLUGIN_DIR, 'config.py')
    spec = importlib.util.spec_from_file_location('readest_config_under_test', path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.config = load_config_module()

    def test_raw_file_mode_is_enabled_by_default(self):
        self.assertTrue(self.config.prefs['do_not_embed_metadata'])

    def test_widget_saves_raw_file_mode(self):
        widget = self.config.ConfigWidget()
        self.assertTrue(widget.do_not_embed_metadata_checkbox.isChecked())

        widget.do_not_embed_metadata_checkbox.setChecked(False)
        widget.save_settings()

        self.assertFalse(self.config.prefs['do_not_embed_metadata'])


if __name__ == '__main__':
    unittest.main()
