"""Anki add-on: Bing image picker for the current note.

A 🖼 editor button searches Bing Images for a note's source field and shows a 3×3
grid of thumbnails; clicking one downloads the full image, stores it in the
collection media, and writes an <img> tag to the target field.

Field mapping is per note type and configured lazily: the first time you press 🖼
on a note type, a small dialog asks which field to search from, which field to put
the image in, and whether to append or overwrite. Mappings are editable later via
Tools → Bing Image Picker (or the add-on's Config button).

Self-contained: bundled Python + urllib only, no extra deps. Results are scraped
from Bing's `/images/async` results fragment (each result is an `m=` attribute
holding JSON with the thumbnail `turl` + source `murl`). The async endpoint is
used, not the main search page: the latter returns the full grid only ~1 request
in 4 (otherwise a JS-streamed shell with a single result), while async returns a
full, stable batch every time. A desktop UA + SafeSearch-off cookie are needed for
real (e.g. Japanese) results.
"""
from __future__ import annotations

import html as _html
import json
import re
import time
import urllib.request
from dataclasses import dataclass
from urllib.parse import quote, urlparse

from aqt import gui_hooks, mw
from aqt.qt import (
    Qt, QAction, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGridLayout,
    QHBoxLayout, QIcon, QLabel, QPixmap, QPushButton, QSize, QVBoxLayout, sip,
)
from aqt.utils import showInfo

GRID = 9                       # 3×3
THUMB = 160

# A real desktop UA is REQUIRED: with a default/bot UA Bing serves an unrelated
# default feed for Japanese terms. The cookie turns SafeSearch off (single user —
# unfiltered results wanted; the adlt URL param alone is only advisory).
_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_HEADERS = {"User-Agent": _USER_AGENT, "Accept-Language": "ja,en;q=0.8",
            "Cookie": "SRCHHPGUSR=ADLT=OFF"}
_SEARCH_URL = ("https://www.bing.com/images/async?q={q}"
               "&first=0&count=35&adlt=off&mmasync=1")
# Each result is an <a class="iusc" ... m="{…json…}">; the JSON is HTML-escaped.
_M_ATTR = re.compile(r'\sm="(\{&quot;.*?&quot;\})"')


# --- Config (per note type) ----------------------------------------------------
#
# Stored via Anki's add-on config (config.json defaults + meta.json user values):
#   {"notetypes": {"<note type name>": {"source": "...", "target": "...",
#                                       "mode": "append"|"overwrite"}}}

def _config():
    cfg = mw.addonManager.getConfig(__name__) or {}
    cfg.setdefault("notetypes", {})
    return cfg


def _save_config(cfg):
    mw.addonManager.writeConfig(__name__, cfg)


def _field_names(notetype):
    return [f["name"] for f in notetype["flds"]]


# Likely field names, best-guessed when a note type is configured for the first
# time. Checked in order: an exact (case-insensitive) match wins over a substring.
_SOURCE_HINTS = ["expression", "word", "term", "vocab", "vocabulary", "kanji",
                 "headword", "japanese", "target word", "front", "question",
                 "reading", "sentence"]
_TARGET_HINTS = ["image", "picture", "img", "images", "photo", "pic"]


def _guess(fields, hints):
    """The field that best matches `hints` (exact match preferred over substring),
    or the empty string if nothing looks right."""
    lower = {f.lower(): f for f in fields}
    for h in hints:
        if h in lower:
            return lower[h]
    for h in hints:
        for f in fields:
            if h in f.lower():
                return f
    return ""


class _ConfigDialog(QDialog):
    """Per-note-type field mapping editor. Used both for first-use setup (opened
    on the note's own type) and from settings (pick any note type)."""

    def __init__(self, parent, select_name=None):
        super().__init__(parent)
        self.setWindowTitle("Bing Image Picker — field mapping")
        self.cfg = _config()

        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.nt = QComboBox()
        self.nt.addItems([m.name for m in mw.col.models.all_names_and_ids()])
        self.source = QComboBox()
        self.target = QComboBox()
        self.mode = QComboBox()
        self.mode.addItems(["Append", "Overwrite"])
        form.addRow("Note type", self.nt)
        form.addRow("Search term from", self.source)
        form.addRow("Put image in", self.target)
        form.addRow("When adding", self.mode)
        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.nt.currentTextChanged.connect(self._load_fields)
        if select_name and self.nt.findText(select_name) >= 0:
            self.nt.setCurrentText(select_name)
        self._load_fields(self.nt.currentText())

    def _load_fields(self, name):
        notetype = mw.col.models.by_name(name)
        fields = _field_names(notetype) if notetype else []
        for combo in (self.source, self.target):
            combo.clear()
            combo.addItems(fields)
        saved = self.cfg["notetypes"].get(name)
        if saved:
            if saved.get("source") in fields:
                self.source.setCurrentText(saved["source"])
            if saved.get("target") in fields:
                self.target.setCurrentText(saved["target"])
            self.mode.setCurrentText(
                "Overwrite" if saved.get("mode") == "overwrite" else "Append")
        else:
            # No mapping yet — pre-select likely fields so most note types are
            # one-click to confirm.
            self.source.setCurrentText(_guess(fields, _SOURCE_HINTS) or self.source.currentText())
            self.target.setCurrentText(_guess(fields, _TARGET_HINTS) or self.target.currentText())

    def _save(self):
        name = self.nt.currentText()
        if not self.source.currentText() or not self.target.currentText():
            showInfo("Pick a source and a target field.")
            return
        self.cfg["notetypes"][name] = {
            "source": self.source.currentText(),
            "target": self.target.currentText(),
            "mode": "overwrite" if self.mode.currentText() == "Overwrite" else "append",
        }
        _save_config(self.cfg)
        self.accept()


def _open_settings():
    if not mw.col:
        return
    _ConfigDialog(mw).exec()


# --- Bing search ---------------------------------------------------------------

@dataclass(frozen=True)
class _Candidate:
    thumb_url: str
    full_url: str


def _search(term):
    """Image candidates scraped from Bing's async results, deduped (one full batch,
    ~35 — enough for several pages of GRID in the picker)."""
    try:
        req = urllib.request.Request(_SEARCH_URL.format(q=quote(term)), headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=10) as r:
            page = r.read().decode("utf-8", "replace")
    except Exception:
        return []
    out, seen = [], set()
    for raw in _M_ATTR.findall(page):
        try:
            d = json.loads(_html.unescape(raw))
        except (ValueError, TypeError):
            continue
        thumb, full = d.get("turl", ""), d.get("murl", "")
        if not thumb or not full or full in seen:
            continue
        seen.add(full)
        out.append(_Candidate(thumb_url=thumb, full_url=full))
    return out


def _fetch(url, timeout=10):
    """Download bytes with the desktop UA; None on any failure."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception:
        return None


def _ext(url):
    suffix = urlparse(url).path.rsplit(".", 1)
    if len(suffix) == 2 and 1 <= len(suffix[1]) <= 4 and suffix[1].isalnum():
        return suffix[1].lower()
    return "jpg"


def _bg(task, on_done=None):
    """Run a network task off the UI thread. `uses_collection=False` is essential:
    the default routes to Anki's single-worker collection executor, which would
    serialize our thumbnail fetches. The flag is recent, so fall back gracefully."""
    try:
        mw.taskman.run_in_background(task, on_done, uses_collection=False)
    except TypeError:
        mw.taskman.run_in_background(task, on_done)


class _PickerDialog(QDialog):
    def __init__(self, parent, term, on_pick):
        super().__init__(parent)
        self.setWindowTitle(f"Bing images — {term}")
        self.on_pick = on_pick
        self._closed = False
        self.candidates = []
        self.page = 0
        self.gen = 0               # bumped each page render; stale thumb jobs no-op
        self.thumbs = {}           # thumb_url -> bytes|None (cache across pages)

        layout = QVBoxLayout(self)
        self.grid = QGridLayout()
        layout.addLayout(self.grid)

        nav = QHBoxLayout()
        self.prev_btn = QPushButton("◀ Previous")
        self.next_btn = QPushButton("Next ▶")
        self.info = QLabel(f"Searching images for {term}…")
        self.info.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.prev_btn.clicked.connect(lambda: self._turn(-1))
        self.next_btn.clicked.connect(lambda: self._turn(1))
        self.prev_btn.setVisible(False)        # hidden until the grid is ready
        self.next_btn.setVisible(False)
        nav.addWidget(self.prev_btn)
        nav.addStretch()
        nav.addWidget(self.info)
        nav.addStretch()
        nav.addWidget(self.next_btn)
        layout.addLayout(nav)

        # Fix the final size up front so the window is mapped at full size and the
        # compositor centers it. On Wayland a client can't position its own window
        # (move() is ignored) and a window that grows after mapping drifts toward a
        # corner — so we must NOT resize after show. 3 columns + margins/spacing.
        cell = THUMB + 10
        self.setFixedSize(3 * cell + 2 * layout.spacing() + 2 * layout.contentsMargins().left(),
                          self.prev_btn.sizeHint().height() + 3 * cell
                          + 3 * layout.spacing() + 2 * layout.contentsMargins().top())

        # Search runs first; thumbnails are then fetched in parallel and each slot
        # fills in as it arrives (rather than waiting for all of them serially).
        _bg(lambda: _search(term), self._on_search)

    def done(self, r):  # marks the dialog closed so late thumbnail callbacks no-op
        self._closed = True
        super().done(r)

    @property
    def _pages(self):
        return max(1, -(-len(self.candidates) // GRID))   # ceil

    def _on_search(self, fut):
        self.candidates = fut.result()
        if not self.candidates:
            self.info.setText("No images found.")
            return
        self._render_page()

    def _turn(self, delta):
        self.page = min(max(self.page + delta, 0), self._pages - 1)
        self._render_page()

    def _render_page(self):
        self.gen += 1
        gen = self.gen
        while self.grid.count():                  # clear the previous page
            w = self.grid.takeAt(0).widget()
            if w:
                w.deleteLater()
        page_cands = self.candidates[self.page * GRID:self.page * GRID + GRID]
        for i, cand in enumerate(page_cands):
            btn = QPushButton("…")                # placeholder until its thumb loads
            btn.setFixedSize(THUMB + 10, THUMB + 10)
            btn.clicked.connect(lambda _=False, c=cand: self._pick(c))
            self.grid.addWidget(btn, i // 3, i % 3)
            cached = self.thumbs.get(cand.thumb_url, ...)
            if cached is not ...:
                self._apply_thumb(btn, cached)
            else:
                _bg((lambda c=cand: _fetch(c.thumb_url)),
                    (lambda fut, b=btn, u=cand.thumb_url, g=gen: self._on_thumb(b, u, g, fut)))
        self.info.setText(f"{self.page + 1} / {self._pages}")
        self.prev_btn.setVisible(True)
        self.next_btn.setVisible(True)
        self.prev_btn.setEnabled(self.page > 0)
        self.next_btn.setEnabled(self.page < self._pages - 1)

    def _on_thumb(self, btn, url, gen, fut):
        if self._closed or sip.isdeleted(self):   # dialog gone before fetch landed
            return
        self.thumbs[url] = fut.result()
        if gen == self.gen and not sip.isdeleted(btn):   # still the current page
            self._apply_thumb(btn, self.thumbs[url])

    @staticmethod
    def _apply_thumb(btn, data):
        if not data:
            btn.setText("✕")
            return
        pm = QPixmap()
        pm.loadFromData(data)
        btn.setText("")
        btn.setIcon(QIcon(pm))
        btn.setIconSize(QSize(THUMB, THUMB))

    def _pick(self, cand):
        self.info.setText("Downloading…")
        self.setEnabled(False)

        def work():
            data = _fetch(cand.full_url, timeout=15) or _fetch(cand.thumb_url, 15)
            return (data, cand.full_url if data else "")

        def done(fut):
            data, url = fut.result()
            if data:
                self.on_pick(data, _ext(url))
            self.accept()

        _bg(work, done)


def _mapping_for(editor, note):
    """The (source, target, mode) for this note's type, configuring it lazily if
    unset or stale. Returns None if the user cancels setup."""
    name = note.note_type()["name"]
    cfg = _config()
    nt_cfg = cfg["notetypes"].get(name)
    fields = note.keys()
    if not nt_cfg or nt_cfg.get("source") not in fields or nt_cfg.get("target") not in fields:
        if not _ConfigDialog(editor.parentWindow, select_name=name).exec():
            return None
        nt_cfg = _config()["notetypes"].get(name)
        if not nt_cfg:
            return None
    return nt_cfg["source"], nt_cfg["target"], nt_cfg.get("mode", "append")


def _open_picker(editor):
    note = editor.note
    if not note:
        return
    mapping = _mapping_for(editor, note)
    if not mapping:
        return
    source, target, mode = mapping
    term = note[source].strip()
    if not term:
        showInfo(f"The '{source}' field is empty.")
        return

    def on_pick(data, ext):
        fname = mw.col.media.write_data(f"bing_{int(time.time() * 1000)}.{ext}", data)
        tag = f'<img src="{fname}">'
        if mode == "overwrite" or not note[target].strip():
            note[target] = tag
        else:
            note[target] = note[target] + tag
        editor.loadNote()

    _PickerDialog(editor.parentWindow, term, on_pick).exec()


def _add_button(buttons, editor):
    buttons.append(editor.addButton(
        icon=None, cmd="bing_image_picker", func=_open_picker,
        tip="Bing image search (3×3 picker)", label="🖼"))
    return buttons


gui_hooks.editor_did_init_buttons.append(_add_button)

_settings_action = QAction("Bing Image Picker…", mw)
_settings_action.triggered.connect(_open_settings)
mw.form.menuTools.addAction(_settings_action)
mw.addonManager.setConfigAction(__name__, _open_settings)
