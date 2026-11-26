"""
Screen language for the operator HMI.

English is the source language: every user-visible string in ui_pyside/ and
main.py is written in English and passed through tr(). Each other language is
one JSON file in ui_pyside/locales/ (<code>.json) that maps the English text
to its translation. Only the language in use is loaded.

    from ui_pyside.i18n import tr
    QLabel(tr("Pallet Records"))
    tr("Welcome, {name}", name=name)        # placeholders keep their names

A string missing from a catalog falls back to English, so a half-finished
catalog never breaks the screen.

Changing the language does not restart the app. set_language() loads the new
catalog and then re-translates the texts already on screen in one pass over
the widgets (labels, buttons, tooltips, placeholders, table headers). Texts
that are rebuilt all the time (status lines, counters) are simply set again
in the new language by their own code. Nothing runs between changes, so there
is no cost while the line is working.

Adding a language: copy ui_pyside/locales/_template.json to <code>.json, fill
in the values and add a line to LANGUAGES below.
"""

import json
import logging
from collections import OrderedDict
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal, QLocale

log = logging.getLogger("ui_pyside.i18n")

LOCALES_DIR = Path(__file__).parent / "locales"

# (code, name in the language itself, name in English, Qt writing system, right-to-left)
LANGUAGES = [
    ("en", "English",           "English",    "Latin",             False),
    ("es", "Español",           "Spanish",    "Latin",             False),
    ("pt", "Português",         "Portuguese", "Latin",             False),
    ("fr", "Français",          "French",     "Latin",             False),
    ("de", "Deutsch",           "German",     "Latin",             False),
    ("it", "Italiano",          "Italian",    "Latin",             False),
    ("nl", "Nederlands",        "Dutch",      "Latin",             False),
    ("pl", "Polski",            "Polish",     "Latin",             False),
    ("cs", "Čeština",           "Czech",      "Latin",             False),
    ("ro", "Română",            "Romanian",   "Latin",             False),
    ("tr", "Türkçe",            "Turkish",    "Latin",             False),
    ("ru", "Русский",           "Russian",    "Cyrillic",          False),
    ("uk", "Українська",        "Ukrainian",  "Cyrillic",          False),
    ("ar", "العربية",           "Arabic",     "Arabic",            True),
    ("hi", "हिन्दी",              "Hindi",      "Devanagari",        False),
    ("mr", "मराठी",              "Marathi",    "Devanagari",        False),
    ("bn", "বাংলা",              "Bengali",    "Bengali",           False),
    ("ta", "தமிழ்",              "Tamil",      "Tamil",             False),
    ("te", "తెలుగు",             "Telugu",     "Telugu",            False),
    ("zh", "简体中文",           "Chinese",    "SimplifiedChinese", False),
    ("ja", "日本語",             "Japanese",   "Japanese",          False),
    ("ko", "한국어",             "Korean",     "Korean",            False),
    ("vi", "Tiếng Việt",        "Vietnamese", "Vietnamese",        False),
    ("th", "ไทย",               "Thai",       "Thai",              False),
    ("id", "Bahasa Indonesia",  "Indonesian", "Latin",             False),
    ("sw", "Kiswahili",         "Swahili",    "Latin",             False),
]
_BY_CODE = {row[0]: row for row in LANGUAGES}
DEFAULT = "en"

_current = DEFAULT
_catalog = {}                # English -> current language
_reverse = {}                # current language (and its upper-case form) -> English
_recent = OrderedDict()      # recent formatted texts -> (English key, kwargs)
_RECENT_MAX = 400
_notifier = None


class _Notifier(QObject):
    changed = Signal(str)     # new language code


def notifier():
    """Object whose `changed(code)` signal fires after a language change.

    Connect code that builds text from data (status lines, counters) so it can
    set its text again at once instead of on its next update."""
    global _notifier
    if _notifier is None:
        _notifier = _Notifier()
    return _notifier


# Lookup
def current():
    return _current


def language(code=None):
    """(code, native name, English name, writing system, rtl) for `code`."""
    return _BY_CODE.get(code or _current, _BY_CODE[DEFAULT])


def native_name(code=None):
    return language(code)[1]


def is_rtl(code=None):
    return language(code)[4]


def qlocale(code=None):
    return QLocale(code or _current)


def _split(text):
    """('  ', 'core', '') so the padding before button labels survives."""
    core = text.strip()
    if not core:
        return "", text, ""
    start = text.find(core)
    return text[:start], core, text[start + len(core):]


def tr(text, **kwargs):
    """`text` in the screen language; placeholders are filled from kwargs."""
    if not text:
        return text
    lead, core, trail = _split(str(text))
    out = _catalog.get(core, core) if _catalog else core
    if kwargs:
        try:
            out = out.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            out = core.format(**kwargs)          # broken translation: use English
        _recent[out] = (core, kwargs)
        _recent.move_to_end(out)
        if len(_recent) > _RECENT_MAX:
            _recent.popitem(last=False)
    return lead + out + trail


def N_(text):
    """Mark a constant for translation without translating it yet.

    For module-level text, which is defined before the language is known; pass
    it through tr() where it is shown."""
    return text


# Loading
def _load_catalog(code):
    if code == DEFAULT:
        return {}
    path = LOCALES_DIR / f"{code}.json"
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        log.warning("No catalog for language %s (%s)", code, path.name)
        return {}
    except Exception as e:
        log.warning("Could not read %s: %s", path.name, e)
        return {}
    return {str(k).strip(): str(v) for k, v in data.items()
            if not str(k).startswith("_") and isinstance(v, str) and v.strip()}


def has_font(code):
    """True if this device has a font for the language's script."""
    from PySide6.QtGui import QFontDatabase
    script = language(code)[3]
    if script == "Latin":
        return True
    try:
        ws = getattr(QFontDatabase.WritingSystem, script)
        return bool(QFontDatabase.families(ws))
    except Exception:
        return True          # cannot tell: let the operator try it


def set_language(code):
    """Switch the screen language and re-translate what is on screen.

    Returns the code in use (unknown codes fall back to English)."""
    global _current, _catalog, _reverse
    if code not in _BY_CODE:
        code = DEFAULT
    if code == _current:
        return code
    old_reverse, old_recent = _reverse, dict(_recent)
    english_before = _current == DEFAULT
    new_catalog = _load_catalog(code)
    _current, _catalog = code, new_catalog
    _reverse = _variants((local, en) for en, local in new_catalog.items())
    _recent.clear()

    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is not None:
        app.setLayoutDirection(Qt.LayoutDirection.RightToLeft if is_rtl(code)
                               else Qt.LayoutDirection.LeftToRight)
        _retranslate_all(old_reverse, old_recent, english_before)
    notifier().changed.emit(code)
    return code


# Re-translating the widgets already on screen
def _cap(s):
    return s[:1].upper() + s[1:]


def _variants(pairs):
    """{text as it may appear on screen: (English key, how it was changed)}.

    Some helpers show a text upper-cased (field_label, StatCell) or with its
    first letter capitalised (header_button)."""
    out = {}
    for shown, en in pairs:
        out.setdefault(shown, (en, ""))
    for shown, en in list(out.items()):
        en = en[0]
        out.setdefault(shown.upper(), (en, "upper"))
        out.setdefault(_cap(shown), (en, "cap"))
    return out


def _swap(text, old_reverse, old_recent):
    """The new-language version of `text`, or None if it is not ours."""
    if not text:
        return None
    lead, core, trail = _split(text)
    if core in old_recent:
        key, kwargs = old_recent[core]
        return lead + tr(key, **kwargs) + trail
    hit = old_reverse.get(core)
    if hit is None:
        return None
    key, how = hit
    new = tr(key)
    new = new.upper() if how == "upper" else _cap(new) if how == "cap" else new
    return lead + new + trail


def _retranslate_all(old_reverse, old_recent, english_before):
    from PySide6.QtWidgets import (QApplication, QLabel, QAbstractButton, QLineEdit,
                                   QTableWidget)
    # Coming from English the screen holds the catalog keys themselves;
    # otherwise the old catalog's reverse map leads back to English.
    if english_before:
        old_reverse = _variants((k, k) for k in _catalog)

    def swap(text):
        return _swap(text, old_reverse, old_recent)

    for w in QApplication.allWidgets():
        try:
            if w.property("i18n_skip"):
                continue
            hook = getattr(w, "i18n_refresh", None)
            if callable(hook):
                hook(swap)
                continue
            if isinstance(w, (QLabel, QAbstractButton)):
                new = swap(w.text())
                if new is not None:
                    w.setText(new)
            elif isinstance(w, QLineEdit):
                new = swap(w.placeholderText())
                if new is not None:
                    w.setPlaceholderText(new)
            elif isinstance(w, QTableWidget):
                for c in range(w.columnCount()):
                    item = w.horizontalHeaderItem(c)
                    new = swap(item.text()) if item is not None else None
                    if new is not None:
                        item.setText(new)
            tip = w.toolTip()
            if tip:
                new = swap(tip)
                if new is not None:
                    w.setToolTip(new)
        except RuntimeError:
            continue           # widget deleted while we walked
