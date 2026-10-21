# -*- coding: utf-8 -*-
"""Flow / ShotGrid review panel for the FrameReviewer tool rail.

A compact Project -> Sequence -> Shot -> Version browser that lives in the side
pane and loads a chosen Version's movie straight into the player, plus a read
only Notes view. Script-key authenticated (see :mod:`shotgrid`).

Every ShotGrid call is a blocking network request, so each runs on a tracked
:class:`_Worker` thread and never on the UI thread -- Maya never freezes while a
query is in flight. The panel owns those threads and :meth:`ShotGridPanel.shutdown`
stops them with bounded waits, so the window's teardown stays bulletproof and a
hot reload can refuse cleanly if a query will not stop.
"""
from __future__ import absolute_import

import html

from .qtcompat import QtCore, QtGui, QtWidgets, Signal
from . import shotgrid, style

ENTITY_ROLE = QtCore.Qt.UserRole


def _exec(dialog):
    return dialog.exec() if hasattr(dialog, 'exec') else dialog.exec_()


class _Worker(QtCore.QThread):
    """Run one callable off the UI thread and deliver its result or error."""
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, fn, parent=None):
        super(_Worker, self).__init__(parent)
        self._fn = fn

    def run(self):
        try:
            result = self._fn()
        except Exception as exc:  # deliver, never crash the thread
            self.failed.emit(str(exc))
            return
        self.done.emit(result)


class ShotGridLoginDialog(QtWidgets.QDialog):
    """Collect site / script name / API key for a script-key connection."""

    def __init__(self, parent, site='', script_name='', api_key='', remember=False):
        super(ShotGridLoginDialog, self).__init__(parent)
        self.setWindowTitle('Connect to Flow / ShotGrid')
        self.setModal(True)
        form = QtWidgets.QFormLayout(self)

        self.site_le = QtWidgets.QLineEdit(site)
        self.site_le.setPlaceholderText('https://yourstudio.shotgrid.autodesk.com')
        self.script_le = QtWidgets.QLineEdit(script_name)
        self.script_le.setPlaceholderText('framereviewer')
        self.key_le = QtWidgets.QLineEdit(api_key)
        self.key_le.setEchoMode(QtWidgets.QLineEdit.Password)
        self.key_le.setPlaceholderText('script API key')
        self.remember_cb = QtWidgets.QCheckBox('Remember the API key on this machine')
        self.remember_cb.setChecked(remember)

        form.addRow('Site URL', self.site_le)
        form.addRow('Script name', self.script_le)
        form.addRow('API key', self.key_le)
        form.addRow('', self.remember_cb)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        buttons.button(QtWidgets.QDialogButtonBox.Ok).setText('Connect')
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self.site_le.setMinimumWidth(320)

    def values(self):
        return {
            'site': self.site_le.text().strip(),
            'script_name': self.script_le.text().strip(),
            'api_key': self.key_le.text().strip(),
            'remember': self.remember_cb.isChecked(),
        }


class ShotGridPanel(QtWidgets.QWidget):
    """Side-pane browser. ``load_path(path)`` is called to load a resolved movie."""

    def __init__(self, load_path, settings, parent=None):
        super(ShotGridPanel, self).__init__(parent)
        self._load_path = load_path
        self._settings = settings  # the shared FrameReviewer Settings wrapper
        self.sg = None
        self._site = ''
        self._workers = set()
        self._req = {}          # per-slot request id, for stale-result guarding
        self._project = None
        self._shot = None
        self._version = None
        self._build_ui()
        self._refresh_connection_state()

    # --------------------------------------------------------------- UI
    def _build_ui(self):
        column = QtWidgets.QVBoxLayout(self)
        column.setContentsMargins(0, 0, 4, 0)
        column.setSpacing(10)

        def section(title):
            box = QtWidgets.QGroupBox(title)
            inner = QtWidgets.QVBoxLayout(box)
            inner.setContentsMargins(8, 12, 8, 8)
            inner.setSpacing(6)
            column.addWidget(box)
            return inner

        # Connection -----------------------------------------------------
        conn = section('Connection')
        self.status_lbl = QtWidgets.QLabel()
        self.status_lbl.setWordWrap(True)
        conn.addWidget(self.status_lbl)
        row = QtWidgets.QHBoxLayout()
        self.connect_btn = QtWidgets.QPushButton('Connect…')
        self.connect_btn.clicked.connect(self._on_connect_clicked)
        self.disconnect_btn = QtWidgets.QPushButton('Disconnect')
        self.disconnect_btn.clicked.connect(self._disconnect)
        row.addWidget(self.connect_btn, 1)
        row.addWidget(self.disconnect_btn)
        conn.addLayout(row)

        # Context --------------------------------------------------------
        ctx = section('Context')
        self.project_combo = QtWidgets.QComboBox()
        self.sequence_combo = QtWidgets.QComboBox()
        self.shot_combo = QtWidgets.QComboBox()
        for label, combo in (('Project', self.project_combo),
                             ('Sequence', self.sequence_combo),
                             ('Shot', self.shot_combo)):
            tag = QtWidgets.QLabel(label)
            tag.setProperty('dim', True)
            ctx.addWidget(tag)
            ctx.addWidget(combo)
        self.project_combo.currentIndexChanged.connect(self._on_project_changed)
        self.sequence_combo.currentIndexChanged.connect(self._on_sequence_changed)
        self.shot_combo.currentIndexChanged.connect(self._on_shot_changed)

        # Versions -------------------------------------------------------
        ver = section('Versions')
        self.version_list = QtWidgets.QListWidget()
        self.version_list.setFixedHeight(150)
        self.version_list.currentItemChanged.connect(self._on_version_changed)
        self.version_list.itemDoubleClicked.connect(lambda *_: self._load_version())
        ver.addWidget(self.version_list)

        detail = QtWidgets.QHBoxLayout()
        self.thumb_lbl = QtWidgets.QLabel()
        self.thumb_lbl.setFixedSize(96, 54)
        self.thumb_lbl.setAlignment(QtCore.Qt.AlignCenter)
        self.thumb_lbl.setStyleSheet(
            'border:1px solid %s; background:%s;' % (style.BORDER, style.FIELD))
        detail.addWidget(self.thumb_lbl)
        self.summary_lbl = QtWidgets.QLabel()
        self.summary_lbl.setWordWrap(True)
        self.summary_lbl.setAlignment(QtCore.Qt.AlignTop)
        self.summary_lbl.setTextFormat(QtCore.Qt.RichText)
        detail.addWidget(self.summary_lbl, 1)
        ver.addLayout(detail)

        self.load_btn = QtWidgets.QPushButton('Load into Player')
        self.load_btn.clicked.connect(self._load_version)
        ver.addWidget(self.load_btn)

        # Notes ----------------------------------------------------------
        notes = section('Notes')
        self.notes_view = QtWidgets.QTextBrowser()
        self.notes_view.setOpenExternalLinks(True)
        self.notes_view.setMinimumHeight(120)
        notes.addWidget(self.notes_view)

        column.addStretch(1)

    # ------------------------------------------------------- connection
    def _refresh_connection_state(self):
        available = shotgrid.AVAILABLE
        connected = self.sg is not None
        self.connect_btn.setVisible(not connected)
        self.disconnect_btn.setVisible(connected)
        self.connect_btn.setEnabled(available)
        for widget in (self.project_combo, self.sequence_combo, self.shot_combo,
                       self.version_list, self.load_btn):
            widget.setEnabled(connected)
        if not available:
            self.status_lbl.setText(
                "<span style='color:%s'>shotgun_api3 is not installed.</span><br>"
                'Install it into Maya, then reopen this panel.' % style.ANNOTATION)
        elif connected:
            self.status_lbl.setText(
                "<span style='color:%s'>● Connected</span><br>%s"
                % (style.ACCENT_HI, html.escape(self._site or '')))
        else:
            self.status_lbl.setText('Not connected.')

    def _on_connect_clicked(self):
        remember = self._settings.get_bool('shotgrid/remember', False)
        dialog = ShotGridLoginDialog(
            self,
            site=self._settings.get_str('shotgrid/site', ''),
            script_name=self._settings.get_str('shotgrid/script_name', ''),
            api_key=self._settings.get_str('shotgrid/api_key', '') if remember else '',
            remember=remember)
        if _exec(dialog) != QtWidgets.QDialog.Accepted:
            return
        values = dialog.values()
        self.status_lbl.setText('Connecting…')
        self.connect_btn.setEnabled(False)
        self._run('connect',
                  lambda: shotgrid.connect(values['site'], values['script_name'],
                                           values['api_key']),
                  lambda sg: self._on_connected(sg, values),
                  self._on_connect_failed)

    def _on_connected(self, sg, values):
        self.sg = sg
        self._site = shotgrid.normalize_site(values['site'])
        self._settings.set('shotgrid/site', values['site'])
        self._settings.set('shotgrid/script_name', values['script_name'])
        self._settings.set('shotgrid/remember', values['remember'])
        if values['remember']:
            self._settings.set('shotgrid/api_key', values['api_key'])
        else:
            self._settings.remove('shotgrid/api_key')
        self._refresh_connection_state()
        self._load_projects()

    def _on_connect_failed(self, message):
        self.connect_btn.setEnabled(shotgrid.AVAILABLE)
        self._refresh_connection_state()
        QtWidgets.QMessageBox.critical(self, 'ShotGrid connection failed', message)

    def _disconnect(self):
        self.sg = None
        self._site = ''
        self._project = self._shot = self._version = None
        for combo in (self.project_combo, self.sequence_combo, self.shot_combo):
            combo.blockSignals(True)
            combo.clear()
            combo.blockSignals(False)
        self.version_list.clear()
        self.notes_view.clear()
        self.summary_lbl.clear()
        self.thumb_lbl.clear()
        self._refresh_connection_state()

    # ----------------------------------------------------------- queries
    def _load_projects(self):
        self._fill_combo(self.project_combo, 'projects',
                         lambda: shotgrid.projects(self.sg),
                         lambda p: p.get('name') or 'Project %s' % p.get('id'))

    def _on_project_changed(self, _index):
        self._project = self.project_combo.currentData()
        self.sequence_combo.clear()
        self.shot_combo.clear()
        self.version_list.clear()
        if not self._project:
            return
        self._fill_combo(
            self.sequence_combo, 'sequences',
            lambda project=self._project: shotgrid.sequences(self.sg, project),
            lambda s: s.get('code') or 'Sequence %s' % s.get('id'),
            first=('All sequences', None))

    def _on_sequence_changed(self, _index):
        if not self._project:
            return
        sequence = self.sequence_combo.currentData()
        self.shot_combo.clear()
        self.version_list.clear()
        self._fill_combo(
            self.shot_combo, 'shots',
            lambda project=self._project, seq=sequence: shotgrid.shots(self.sg, project, seq),
            lambda s: s.get('code') or 'Shot %s' % s.get('id'))

    def _on_shot_changed(self, _index):
        self._shot = self.shot_combo.currentData()
        self.version_list.clear()
        self.notes_view.clear()
        self.summary_lbl.clear()
        self.thumb_lbl.clear()
        if not (self._project and self._shot):
            return
        token = self._bump('versions')
        self._run('versions',
                  lambda project=self._project, shot=self._shot:
                      shotgrid.versions(self.sg, project, shot),
                  lambda rows, token=token: self._populate_versions(rows, token))

    def _populate_versions(self, rows, token):
        if token != self._req.get('versions'):
            return
        self.version_list.clear()
        for version in rows:
            code = version.get('code') or 'v%s' % version.get('id')
            label = '%s   %s' % (code, _status(version))
            item = QtWidgets.QListWidgetItem(label.strip())
            item.setData(ENTITY_ROLE, version)
            self.version_list.addItem(item)
        if rows:
            self.version_list.setCurrentRow(0)

    def _on_version_changed(self, current, _previous):
        self._version = current.data(ENTITY_ROLE) if current is not None else None
        if not self._version:
            self.summary_lbl.clear()
            self.thumb_lbl.clear()
            self.notes_view.clear()
            return
        self.summary_lbl.setText(_version_summary(self._version))
        self._load_thumbnail(self._version)
        self._load_notes(self._version)

    def _load_thumbnail(self, version):
        self.thumb_lbl.setText('…')
        url = version.get('image')
        token = self._bump('thumb')
        if not url:
            self.thumb_lbl.clear()
            return
        self._run('thumb', lambda url=url: shotgrid.thumbnail_bytes(url),
                  lambda data, token=token: self._set_thumbnail(data, token),
                  lambda _m: self.thumb_lbl.clear())

    def _set_thumbnail(self, data, token):
        if token != self._req.get('thumb') or not data:
            return
        pixmap = QtGui.QPixmap()
        if pixmap.loadFromData(data):
            self.thumb_lbl.setPixmap(pixmap.scaled(
                self.thumb_lbl.size(), QtCore.Qt.KeepAspectRatio,
                QtCore.Qt.SmoothTransformation))
        else:
            self.thumb_lbl.clear()

    def _load_notes(self, version):
        self.notes_view.setHtml('<i>Loading notes…</i>')
        entities = [{'type': 'Version', 'id': version.get('id')}]
        shot = version.get('entity')
        if isinstance(shot, dict) and shot.get('id'):
            entities.append({'type': shot.get('type', 'Shot'), 'id': shot['id']})
        token = self._bump('notes')
        self._run('notes', lambda ents=entities: shotgrid.notes(self.sg, ents),
                  lambda rows, token=token: self._render_notes(rows, token),
                  lambda m: self.notes_view.setHtml(
                      '<span style="color:%s">%s</span>' % (style.ANNOTATION, html.escape(m))))

    def _render_notes(self, rows, token):
        if token != self._req.get('notes'):
            return
        if not rows:
            self.notes_view.setHtml('<i style="color:%s">No notes.</i>' % style.TEXT_DIM)
            return
        self.notes_view.setHtml(_notes_html(rows))

    # -------------------------------------------------------- load movie
    def _load_version(self):
        if self.sg is None or self._version is None:
            return
        version = self._version
        self.load_btn.setEnabled(False)
        self.load_btn.setText('Resolving…')
        token = self._bump('movie')
        self._run('movie',
                  lambda v=version: shotgrid.version_movie_path(self.sg, v),
                  lambda path, token=token: self._on_movie_resolved(path, token),
                  self._on_movie_failed)

    def _on_movie_resolved(self, path, token):
        self._reset_load_button()
        if token != self._req.get('movie'):
            return
        self._load_path(path)

    def _on_movie_failed(self, message):
        self._reset_load_button()
        QtWidgets.QMessageBox.warning(self, 'Could not load Version', message)

    def _reset_load_button(self):
        self.load_btn.setText('Load into Player')
        self.load_btn.setEnabled(self.sg is not None)

    # ----------------------------------------------------- worker plumbing
    def _bump(self, slot):
        self._req[slot] = self._req.get(slot, 0) + 1
        return self._req[slot]

    def _fill_combo(self, combo, slot, query, label_of, first=None):
        token = self._bump(slot)

        def done(rows, token=token, combo=combo, label_of=label_of, first=first):
            if token != self._req.get(slot):
                return
            combo.blockSignals(True)
            combo.clear()
            if first is not None:
                combo.addItem(first[0], first[1])
            for row in rows:
                combo.addItem(label_of(row), row)
            combo.blockSignals(False)
            combo.currentIndexChanged.emit(combo.currentIndex())

        self._run(slot, query, done,
                  lambda m: QtWidgets.QMessageBox.warning(self, 'ShotGrid query failed', m))

    def _run(self, slot, fn, on_ok, on_fail=None):
        worker = _Worker(fn, self)
        worker.done.connect(on_ok)
        if on_fail is not None:
            worker.failed.connect(on_fail)
        worker.finished.connect(lambda w=worker: self._reap(w))
        self._workers.add(worker)
        worker.start()

    def _reap(self, worker):
        self._workers.discard(worker)
        worker.deleteLater()

    def shutdown(self):
        """Stop every in-flight query with a bounded wait. Returns clean/not."""
        clean = True
        for worker in list(self._workers):
            try:
                if worker.isRunning():
                    worker.requestInterruption()
                    worker.quit()
                    if not worker.wait(2000):
                        clean = False  # network call did not return; leave it
            except RuntimeError:
                pass
        self.sg = None
        return clean


# --------------------------------------------------------------- helpers
def _status(version):
    status = version.get('sg_status_list')
    return '[%s]' % status if status else ''


def _date_text(value):
    try:
        return value.strftime('%Y-%m-%d %H:%M')
    except Exception:
        return html.escape(str(value)) if value else ''


def _version_summary(version):
    user = version.get('user') or {}
    parts = []
    if version.get('created_at'):
        parts.append("<span style='color:%s'>%s</span>"
                     % (style.TEXT_DIM, _date_text(version.get('created_at'))))
    if isinstance(user, dict) and user.get('name'):
        parts.append(html.escape(user['name']))
    description = version.get('description')
    if description:
        parts.append(html.escape(description))
    return '<br>'.join(parts)


def _notes_html(rows):
    blocks = []
    for note in rows:
        user = note.get('user') or {}
        name = html.escape(user['name']) if isinstance(user, dict) and user.get('name') else ''
        subject = html.escape(note.get('subject') or '')
        content = html.escape(note.get('content') or '').replace('\n', '<br>')
        header = "<b style='color:%s'>%s</b>" % (style.ACCENT_HI, subject or 'Note')
        meta = "<span style='color:%s'>%s &middot; %s</span>" % (
            style.TEXT_DIM, name, _date_text(note.get('created_at')))
        blocks.append('%s<br>%s<br>%s' % (header, meta, content))
    return "<div style='margin-bottom:8px'></div>".join(blocks)
