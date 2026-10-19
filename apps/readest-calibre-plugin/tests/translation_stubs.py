import builtins


def install_translation_stubs():
    builtins._ = lambda text: text
    builtins.ngettext = lambda singular, plural, count: singular if count == 1 else plural
