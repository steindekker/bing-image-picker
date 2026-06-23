"""Anki add-on: Bing image picker for the current note.

A 🖼 editor button searches Bing Images for a note's source field and shows a 3×3
grid of thumbnails; clicking one downloads the full image, stores it in the
collection media, and writes an <img> tag to the target field.

Field mapping is per note type and configured lazily: the first time you press 🖼
on a note type, a small dialog asks which field to search from, which field to put
the image in, and whether to append or overwrite. Mappings are editable later via
Tools → Bing Image Picker (or the add-on's Config button).

Self-contained: bundled Python + urllib only, no extra deps. Each search warms a
session on the main image-search page (to collect Bing's cookies), then scrapes
its `/images/async` results fragment (each result is an `m=` attribute holding
JSON with the thumbnail `turl` + source `murl`). The warm session + a full browser
header set + a Referer are what keep Bing from serving its bot-detection fallback
feed of unrelated images on lower-reputation networks; if it does anyway (that feed
ignores the query and is oversized), the picker says so instead of showing junk.
"""
from __future__ import annotations

import gzip
import html as _html
import json
import re
import threading
import time
import urllib.request
import zlib
from dataclasses import dataclass
from http.cookiejar import Cookie, CookieJar
from urllib.parse import quote, urlparse

from aqt import gui_hooks, mw
from aqt.qt import (
    Qt, QAction, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
    QGridLayout, QHBoxLayout, QIcon, QLabel, QPixmap, QPushButton, QSize,
    QVBoxLayout, sip,
)
from aqt.utils import showInfo

GRID = 9                       # 3×3
THUMB = 160

# Bing's /images/async has two response modes. For a request it trusts as a real
# browser it returns results for the query; for one it flags as a bot it silently
# returns a query-INDEPENDENT default feed (~100 unrelated images) instead of a
# 403. A desktop UA alone clears the bar on clean residential IPs, but on
# lower-reputation networks (VPN, datacenter, CGNAT, some regions) Bing escalates
# to checking browser-consistency signals — so we mimic the real flow: warm a
# session on the search page to collect cookies, then call async with those
# cookies + a Referer + a full browser header set.
_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_BROWSER_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "ja,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",          # browsers never send "identity"
    "Sec-CH-UA": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-CH-UA-Mobile": "?0",
    "Sec-CH-UA-Platform": '"Windows"',
}
_ASYNC_COUNT = 35
# {adlt} is the SafeSearch level (off|strict), filled per request from config.
_SEARCH_PAGE = "https://www.bing.com/images/search?q={q}&form=HDRSC2&adlt={adlt}"
_WARM_PAGE = "https://www.bing.com/images/"     # query-less; pre-collects cookies
_ASYNC_URL = ("https://www.bing.com/images/async?q={q}&first=0"
              f"&count={_ASYNC_COUNT}" "&adlt={adlt}&mmasync=1")

# The bot feed ignores `count` and returns far more than we asked for; treat a
# wildly oversized batch as "blocked" rather than showing unrelated images.
_BLOCKED_MIN = 60
_BLOCKED = object()        # sentinel _search returns when Bing served the bot feed

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


def _adlt():
    """Bing's SafeSearch level from config: "STRICT" when on, else "OFF". (The
    setting is forced non-null before any search via _mapping_for.) Read on the UI
    thread and threaded into the search so the worker never touches the manager."""
    return "STRICT" if _config().get("safe_search") else "OFF"


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
        self.safe = QCheckBox("Filter explicit results")   # global, not per note type
        self.safe.setChecked(self.cfg.get("safe_search") is not False)   # on unless opted out
        form.addRow("Note type", self.nt)
        form.addRow("Search term from", self.source)
        form.addRow("Put image in", self.target)
        form.addRow("When adding", self.mode)
        form.addRow("SafeSearch", self.safe)
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
        self.cfg["safe_search"] = self.safe.isChecked()   # global; null until first saved
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


def _cookie(name, value):
    """A minimal .bing.com cookie for seeding the jar (used to set SafeSearch)."""
    return Cookie(0, name, value, None, False, ".bing.com", True, True, "/", True,
                  False, None, False, None, None, {})


# A single warmed session (opener + its cookie jar) is reused across searches so
# the cookie-collecting page load is paid once — pre-warmed in the background when
# the editor first opens (see _add_button), not lazily on the first search. A
# blocked or failed search drops it so the next attempt re-warms from scratch.
_session = None
_session_lock = threading.Lock()


def _new_session(adlt="OFF"):
    cj = CookieJar()
    cj.set_cookie(_cookie("SRCHHPGUSR", f"ADLT={adlt}"))
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj)), cj


def _warm_session(term=None, adlt="OFF"):
    """A fresh session with Bing's cookies collected by loading a real page: the
    query-less landing page for a generic pre-warm, or the term's own search page
    (so a later async Referer matches a page we actually loaded)."""
    opener, cj = _new_session(adlt)
    page = _SEARCH_PAGE.format(q=quote(term), adlt=adlt.lower()) if term else _WARM_PAGE
    _get(opener, page)
    cj.set_cookie(_cookie("SRCHHPGUSR", f"ADLT={adlt}"))   # re-assert SafeSearch level
    return opener, cj


def _ensure_session():
    """The shared warmed session, warming it generically if not done yet. Network
    runs under the lock so a search started during the pre-warm waits for it."""
    global _session
    with _session_lock:
        if _session is None:
            _session = _warm_session()
        return _session


def _set_session(sess):
    global _session
    with _session_lock:
        _session = sess


def _reset_session():
    global _session
    with _session_lock:
        _session = None


def _get(opener, url, referer=None, timeout=10):
    """GET via the session opener with browser headers, gunzipping the response.
    With `referer` the request is shaped like the page's XHR (Sec-Fetch cors);
    without, like a top-level navigation."""
    headers = dict(_BROWSER_HEADERS)
    if referer:
        headers.update({"Referer": referer, "X-Requested-With": "XMLHttpRequest",
                        "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors",
                        "Sec-Fetch-Dest": "empty"})
    else:
        headers.update({"Sec-Fetch-Site": "none", "Sec-Fetch-Mode": "navigate",
                        "Sec-Fetch-Dest": "document"})
    with opener.open(urllib.request.Request(url, headers=headers), timeout=timeout) as r:
        raw, enc = r.read(), r.headers.get("Content-Encoding", "")
    if enc == "gzip":
        raw = gzip.decompress(raw)
    elif enc == "deflate":
        try:
            raw = zlib.decompress(raw)
        except zlib.error:
            raw = zlib.decompress(raw, -zlib.MAX_WBITS)   # raw (headerless) deflate
    return raw.decode("utf-8", "replace")


def _parse(page):
    """_Candidates from an async results fragment, deduped on the full URL."""
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


def _search(term, adlt="OFF"):
    """Image candidates from Bing's async results (one batch, ~35 — enough for
    several pages of GRID). `adlt` is the SafeSearch level ("OFF"|"STRICT").
    Reuses the shared pre-warmed session; on a block or error, retries once with
    a fresh term-specific warm. Returns a list of _Candidate, [] on
    nothing/failure, or the _BLOCKED sentinel if Bing served its bot-detection
    fallback feed (so the picker can say so, not show junk)."""
    q = quote(term)
    async_url = _ASYNC_URL.format(q=q, adlt=adlt.lower())
    referer = _SEARCH_PAGE.format(q=q, adlt=adlt.lower())
    blocked = False
    for fresh in (False, True):        # cached session first, then a fresh warm
        try:
            opener, cj = _warm_session(term, adlt) if fresh else _ensure_session()
            cj.set_cookie(_cookie("SRCHHPGUSR", f"ADLT={adlt}"))   # re-assert SafeSearch
            cands = _parse(_get(opener, async_url, referer=referer))
        except Exception:
            _reset_session()
            continue
        if cands and len(cands) < _BLOCKED_MIN:
            if fresh:
                _set_session((opener, cj))     # promote the working session for reuse
            return cands
        if cands:                              # oversized batch => the bot feed
            blocked = True
        _reset_session()                       # drop the stale/blocked session
    return _BLOCKED if blocked else []


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
        adlt = _adlt()         # read config here, on the UI thread
        _bg(lambda: _search(term, adlt), self._on_search)

    def done(self, r):  # marks the dialog closed so late thumbnail callbacks no-op
        self._closed = True
        super().done(r)

    @property
    def _pages(self):
        return max(1, -(-len(self.candidates) // GRID))   # ceil

    def _on_search(self, fut):
        result = fut.result()
        if result is _BLOCKED:
            self.info.setText("Bing blocked this search (bot check).\n"
                              "Wait a moment and try again.")
            return
        self.candidates = result
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
    # Also force the dialog while SafeSearch is unchosen (null) — new note types,
    # and existing users upgrading from before the setting existed, must pick once.
    stale = (not nt_cfg or nt_cfg.get("source") not in fields
             or nt_cfg.get("target") not in fields)
    if stale or cfg.get("safe_search") is None:
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


_prewarmed = False


def _prewarm():
    """Collect Bing's cookies in the background the first time an editor opens, so
    the latency is paid before — not during — the user's first search."""
    global _prewarmed
    if _prewarmed:
        return
    _prewarmed = True
    _bg(_ensure_session)


def _add_button(buttons, editor):
    _prewarm()
    buttons.append(editor.addButton(
        icon=None, cmd="bing_image_picker", func=_open_picker,
        tip="Bing image search (3×3 picker)", label="🖼"))
    return buttons


gui_hooks.editor_did_init_buttons.append(_add_button)

_settings_action = QAction("Bing Image Picker…", mw)
_settings_action.triggered.connect(_open_settings)
mw.form.menuTools.addAction(_settings_action)
mw.addonManager.setConfigAction(__name__, _open_settings)
