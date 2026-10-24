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
import hashlib
import os

from ...core.qtcompat import QtCore, QtGui, QtWidgets, Signal
from . import client as shotgrid
from .outbox import ShotGridOutbox, OutboxBusy, OutboxConflict
from ... import style

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
        self.key_le = QtWidgets.QLineEdit()
        self.key_le.setEchoMode(QtWidgets.QLineEdit.Password)
        self.key_le.setPlaceholderText('Optional with Toolkit or environment auth')
        hint = QtWidgets.QLabel('API keys are session-only and are never saved.')
        hint.setWordWrap(True)

        form.addRow('Site URL', self.site_le)
        form.addRow('Script name', self.script_le)
        form.addRow('API key', self.key_le)
        form.addRow('', hint)

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
            'remember': False,
        }


class ShotGridPanel(QtWidgets.QWidget):
    """Side-pane browser. ``load_path(path)`` is called to load a resolved movie."""

    def __init__(self, load_path, settings, parent=None, queue_playlist=None,
                 link_version=None, review_context=None, on_published=None):
        super(ShotGridPanel, self).__init__(parent)
        self._load_path = load_path
        self._queue_playlist = queue_playlist  # callback(list_of_paths) -> None
        self._link_version = link_version
        self._review_context = review_context
        self._on_published = on_published
        self._outbox = None
        self._publishing = False
        self._conflict_busy = False
        self._closed = False
        self._settings = settings  # the shared FrameReviewer Settings wrapper
        # Clean up plaintext keys stored by versions predating session auth.
        self._settings.remove('shotgrid/api_key')
        self._settings.remove('shotgrid/remember')
        self.sg = None
        self._site = ''
        self._workers = set()
        self._req = {}          # per-slot request id, for stale-result guarding
        self._connection_generation = 0  # invalidate every outstanding query on disconnect
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

        # Context playlist: queue every listed version of this shot into the
        # main Playlist so a reviewer can step through them in order.
        self.queue_btn = QtWidgets.QPushButton('Queue Versions → Playlist')
        self.queue_btn.clicked.connect(self._queue_context_playlist)
        self.queue_btn.setVisible(self._queue_playlist is not None)
        ver.addWidget(self.queue_btn)

        # Notes ----------------------------------------------------------
        notes = section('Notes')
        self.notes_view = QtWidgets.QTextBrowser()
        self.notes_view.setOpenExternalLinks(True)
        self.notes_view.setMinimumHeight(120)
        notes.addWidget(self.notes_view)

        # Explicit asynchronous publication: queue first; a second user
        # confirmation triggers network writes. Browsing never mutates Flow.
        outbox = section('Review Publishing')
        self.outbox_summary = QtWidgets.QLabel('Connect to access the offline outbox.')
        self.outbox_summary.setWordWrap(True)
        outbox.addWidget(self.outbox_summary)
        self.outbox_list = QtWidgets.QListWidget()
        self.outbox_list.setObjectName('frameReviewerShotGridOutbox')
        self.outbox_list.setMinimumHeight(75)
        self.outbox_list.setMaximumHeight(140)
        outbox.addWidget(self.outbox_list)
        self.enqueue_note_btn = QtWidgets.QPushButton('Queue Selected Review Note…')
        self.enqueue_note_btn.setToolTip(
            'Select a typed review note in Review; link the loaded Version first')
        self.enqueue_note_btn.clicked.connect(self._queue_selected_review_note)
        outbox.addWidget(self.enqueue_note_btn)
        status_row = QtWidgets.QHBoxLayout()
        self.outbox_status_code = QtWidgets.QLineEdit()
        self.outbox_status_code.setPlaceholderText('Version status code')
        self.outbox_status_code.setToolTip(
            'Studio-defined ShotGrid Version status code (e.g. rev or apr)')
        self.enqueue_status_btn = QtWidgets.QPushButton('Queue Status…')
        self.enqueue_status_btn.clicked.connect(self._queue_version_status)
        status_row.addWidget(self.outbox_status_code, 1)
        status_row.addWidget(self.enqueue_status_btn)
        outbox.addLayout(status_row)
        queue_actions = QtWidgets.QHBoxLayout()
        self.publish_btn = QtWidgets.QPushButton('Sync Queued Changes…')
        self.publish_btn.clicked.connect(self._publish_outbox)
        self.retry_outbox_btn = QtWidgets.QPushButton('Retry')
        self.retry_outbox_btn.clicked.connect(self._retry_outbox_item)
        self.inspect_conflict_btn = QtWidgets.QPushButton('Inspect Conflict…')
        self.inspect_conflict_btn.setToolTip(
            'Fetch the authoritative remote Version/Note and explicitly choose a local resolution')
        self.inspect_conflict_btn.clicked.connect(self._inspect_outbox_conflict)
        self.discard_outbox_btn = QtWidgets.QPushButton('Discard')
        self.discard_outbox_btn.clicked.connect(self._discard_outbox_item)
        for control in (self.publish_btn, self.retry_outbox_btn,
                        self.inspect_conflict_btn, self.discard_outbox_btn):
            queue_actions.addWidget(control)
        outbox.addLayout(queue_actions)
        self.outbox_list.currentItemChanged.connect(
            lambda *_: self._update_outbox_buttons())

        column.addStretch(1)

    # ------------------------------------------------------- connection
    def _refresh_connection_state(self):
        available = shotgrid.is_available()
        connected = self.sg is not None
        self.connect_btn.setVisible(not connected)
        self.disconnect_btn.setVisible(connected)
        self.connect_btn.setEnabled(available)
        for widget in (self.project_combo, self.sequence_combo, self.shot_combo,
                       self.version_list, self.load_btn, self.queue_btn):
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
        dialog = ShotGridLoginDialog(
            self,
            site=self._settings.get_str('shotgrid/site', ''),
            script_name=self._settings.get_str('shotgrid/script_name', ''),
            api_key='', remember=False)
        if _exec(dialog) != QtWidgets.QDialog.Accepted:
            return
        values = dialog.values()
        dialog.key_le.clear()
        self.status_lbl.setText('Connecting…')
        self.connect_btn.setEnabled(False)
        self._bump('connect')
        self._run('connect',
                  lambda: shotgrid.connect(values['site'], values['script_name'],
                                           values['api_key']),
                  lambda sg: self._on_connected(sg, values),
                  self._on_connect_failed)

    def _on_connected(self, sg, values):
        self.sg = sg
        self._site = shotgrid.normalize_site(
            values['site'] or os.environ.get('FRAMEREVIEWER_SHOTGRID_SITE')
            or getattr(sg, 'base_url', ''))
        self._settings.set('shotgrid/site', values['site'])
        self._settings.set('shotgrid/script_name', values['script_name'])
        self._settings.remove('shotgrid/api_key')
        self._settings.remove('shotgrid/remember')
        try:
            digest = hashlib.sha256(self._site.encode('utf-8')).hexdigest()[:20]
            path = os.path.join(os.path.expanduser('~'), '.framereviewer',
                                'shotgrid', 'outbox-' + digest + '.json')
            self._outbox = ShotGridOutbox(path, site=self._site)
        except (OSError, ValueError, TypeError) as exc:
            self._outbox = None
            self.outbox_summary.setText('Outbox unavailable: {}'.format(exc))
        self._refresh_outbox()
        self._refresh_connection_state()
        self._load_projects()

    def _on_connect_failed(self, message):
        self.connect_btn.setEnabled(shotgrid.is_available())
        self._refresh_connection_state()
        QtWidgets.QMessageBox.critical(self, 'ShotGrid connection failed', message)

    def _disconnect(self):
        # A late projects/versions/notes/thumbnail/connect response must never
        # repopulate a disconnected panel or resurrect an old SG connection.
        self._connection_generation += 1
        self._bump('publish')
        self._bump('conflict')
        self._publishing = False
        self._conflict_busy = False
        self.sg = None
        self._outbox = None
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
        self._refresh_outbox()

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
        # Clearing a Shot does not start another versions query, so invalidate
        # an already-running request explicitly (old results otherwise revive
        # the previous Shot's Version list after a clear/source switch).
        token = self._bump('versions')
        self._bump('notes')
        self._bump('thumb')
        self._bump('movie')
        self._shot = self.shot_combo.currentData()
        self._version = None
        self.version_list.clear()
        self.notes_view.clear()
        self.summary_lbl.clear()
        self.thumb_lbl.clear()
        if not (self._project and self._shot):
            return
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
        self._bump('movie')  # invalidate a prior Version resolution
        self._bump('notes')  # even when clearing the final selected Version
        self._bump('thumb')
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
                  lambda path, token=token, v=version:
                      self._on_movie_resolved(path, token, v),
                  self._on_movie_failed)

    def _on_movie_resolved(self, path, token, version=None):
        self._reset_load_button()
        if token != self._req.get('movie'):
            return
        self._load_path(path)
        if version is not None and self._link_version is not None:
            self._link_version(version, self._site, self._project, self._shot, path)

    def _on_movie_failed(self, message):
        self._reset_load_button()
        QtWidgets.QMessageBox.warning(self, 'Could not load Version', message)

    # ----------------------------------------------------- context playlist
    def _queue_context_playlist(self):
        """Resolve every listed version's movie and queue them in the Playlist."""
        if self.sg is None or self._queue_playlist is None:
            return
        versions = [self.version_list.item(row).data(ENTITY_ROLE)
                    for row in range(self.version_list.count())]
        versions = [v for v in versions if v]
        if not versions:
            return
        self.queue_btn.setEnabled(False)
        self.queue_btn.setText('Resolving…')
        token = self._bump('queue')

        def resolve():
            paths = []
            for version in versions:
                try:
                    path = shotgrid.version_movie_path(self.sg, version)
                except Exception:
                    continue
                if path:
                    paths.append(path)
            return paths

        self._run('queue', resolve,
                  lambda paths, token=token: self._on_context_resolved(paths, token),
                  self._reset_queue_button)

    def _on_context_resolved(self, paths, token):
        self._reset_queue_button()
        if token != self._req.get('queue'):
            return
        if paths:
            self._queue_playlist(paths)
        else:
            QtWidgets.QMessageBox.information(
                self, 'Playlist', 'None of these versions had a playable movie.')

    def _reset_queue_button(self, message=None):
        self.queue_btn.setText('Queue Versions → Playlist')
        self.queue_btn.setEnabled(self.sg is not None)
        if message:
            QtWidgets.QMessageBox.warning(self, 'Could not queue versions', message)

    def _reset_load_button(self):
        self.load_btn.setText('Load into Player')
        self.load_btn.setEnabled(self.sg is not None)

    # ----------------------------------------------------- offline publication
    def _selected_outbox_id(self):
        item = self.outbox_list.currentItem()
        return item.data(ENTITY_ROLE) if item is not None else None

    def _refresh_outbox(self):
        selected_id = self._selected_outbox_id()
        self.outbox_list.blockSignals(True)
        try:
            self.outbox_list.clear()
            entries = self._outbox.entries() if self._outbox is not None else ()
            for entry in reversed(entries):
                label = '{} · {} · Version {}{}'.format(
                    entry.kind.title(), entry.state.title(), entry.version_id,
                    ' · {}'.format(entry.code) if entry.code else '')
                item = QtWidgets.QListWidgetItem(label)
                item.setData(ENTITY_ROLE, entry.id)
                item.setToolTip('Source {} · Version {} · {} · Attempts {}{}'.format(
                    entry.source_id, entry.version_id, entry.state,
                    entry.attempts, ' · '+entry.code if entry.code else ''))
                self.outbox_list.addItem(item)
                if selected_id == entry.id:
                    self.outbox_list.setCurrentItem(item)
            if self._outbox is not None:
                waiting = len(self._outbox.pending())
                conflicts = sum(e.state == 'conflict' for e in entries)
                self.outbox_summary.setText(
                    '{} queued · {} conflict(s) requiring inspection · {} total'.format(
                        waiting, conflicts, len(entries)))
            elif self.sg is None:
                self.outbox_summary.setText('Connect to access the offline outbox.')
        finally:
            self.outbox_list.blockSignals(False)
        self._update_outbox_buttons()

    def _update_outbox_buttons(self):
        connected = self.sg is not None and self._outbox is not None
        self.enqueue_note_btn.setEnabled(connected)
        self.enqueue_status_btn.setEnabled(connected)
        self.outbox_status_code.setEnabled(connected)
        self.publish_btn.setEnabled(connected and not self._publishing and
                                    not self._conflict_busy
                                    and bool(self._outbox.pending()))
        entry_id = self._selected_outbox_id()
        entry = self._outbox.get(entry_id) if connected and entry_id else None
        self.retry_outbox_btn.setEnabled(bool(entry and entry.state == 'failed'
                                               and not self._conflict_busy
                                               and not self._publishing))
        self.inspect_conflict_btn.setEnabled(bool(entry and entry.state == 'conflict'
                                                   and not self._conflict_busy
                                                   and not self._publishing))
        self.discard_outbox_btn.setEnabled(bool(entry and entry.state != 'done'
                                                 and not self._conflict_busy
                                                 and not self._publishing))

    def _selected_local_review(self):
        if not (self._review_context and self._version and self._outbox):
            raise ValueError('Load a ShotGrid Version and select a review note first.')
        source, document, selected_id = self._review_context()
        if source is None or document is None:
            raise ValueError('Open media and review records before publishing.')
        remote = (source.metadata or {}).get('shotgrid') or {}
        if (remote.get('version_id') != self._version.get('id') or
                remote.get('site') != self._outbox.site):
            raise ValueError('The active media is not linked to this ShotGrid Version.')
        return source, document, selected_id

    def _queue_selected_review_note(self):
        try:
            source, document, selected_id = self._selected_local_review()
            note = document.get(selected_id) if selected_id else None
            if note is None or not note.body.strip():
                raise ValueError('Select a review note with a comment in Review.')
            if QtWidgets.QMessageBox.question(
                    self, 'Queue Review Note',
                    'Queue this note for publication to the selected Flow Version?\n'
                    'Nothing will be sent until you select Sync Queued Changes.',
                    QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                    QtWidgets.QMessageBox.No) != QtWidgets.QMessageBox.Yes:
                return
            self._outbox.enqueue_review_note(
                self._version['id'], note, subject='FrameReviewer',
                project_id=self._project['id'] if self._project else None,
                entity={'type': 'Shot', 'id': self._shot['id']}
                if self._shot else None)
            self._refresh_outbox()
        except (ValueError, TypeError, OSError, OutboxBusy, OutboxConflict) as exc:
            QtWidgets.QMessageBox.warning(self, 'Cannot queue review note', str(exc))

    def _queue_version_status(self):
        try:
            source, _doc, _selected_id = self._selected_local_review()
            code = self.outbox_status_code.text().strip()
            if not code:
                raise ValueError('Enter a studio-defined Version status code.')
            if QtWidgets.QMessageBox.question(
                    self, 'Queue Version Status',
                    'Queue a status change for the selected Flow Version?\n'
                    'The change will only be sent when you sync the outbox.',
                    QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                    QtWidgets.QMessageBox.No) != QtWidgets.QMessageBox.Yes:
                return
            self._outbox.enqueue_status(
                source.id, self._version['id'], code,
                expected_status=self._version.get('sg_status_list'),
                project_id=self._project['id'] if self._project else None,
                entity={'type': 'Shot', 'id': self._shot['id']}
                if self._shot else None)
            self._refresh_outbox()
        except (ValueError, TypeError, OSError, OutboxBusy, OutboxConflict) as exc:
            QtWidgets.QMessageBox.warning(self, 'Cannot queue Version status', str(exc))

    def _publish_outbox(self):
        if (self._closed or self._publishing or self._conflict_busy or self.sg is None or
                self._outbox is None):
            return
        pending = self._outbox.pending()
        if not pending:
            return
        confirmed_box, confirmed_sg = self._outbox, self.sg
        if QtWidgets.QMessageBox.question(
                self, 'Publish to Flow / ShotGrid',
                'Publish up to {} queued change(s) to the remote server?\n'
                'This may create Notes or change Version status.'.format(
                    min(len(pending), 10)),
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No) != QtWidgets.QMessageBox.Yes:
            return
        # A modal prompt runs its own event loop. Disconnect/shutdown may have
        # happened during confirmation, so never launch a worker against an
        # obsolete or now-None client/outbox.
        if (self._closed or self._outbox is not confirmed_box or
                self.sg is not confirmed_sg):
            return
        self._publishing = True
        self.publish_btn.setEnabled(False)
        self.outbox_summary.setText('Synchronizing queued review changes…')
        box, sg = self._outbox, self.sg
        generation = self._bump('publish')
        version_id = self._version.get('id') if self._version else None
        source_id = None
        if self._review_context is not None:
            try:
                source, _document, _selected = self._review_context()
                source_id = source.id if source is not None else None
            except (AttributeError, TypeError, ValueError):
                # The outbox can contain older sources, independent of current
                # scene state. Do not prevent a confirmed durable publication.
                pass
        def still_current():
            return (not self._closed and self._req.get('publish') == generation
                    and self._outbox is box and self.sg is sg)
        def same_view():
            if (self._version.get('id') if self._version else None) != version_id:
                return False
            if self._review_context is not None:
                try:
                    current, _document, _selected = self._review_context()
                    if (current.id if current is not None else None) != source_id:
                        return False
                except (AttributeError, TypeError, ValueError):
                    return False
            return True
        self._run('publish', lambda: box.publish(sg, limit=10),
                  lambda report: self._on_outbox_published(report,
                                                          refresh_notes=same_view())
                  if still_current() else None,
                  lambda message: self._on_outbox_publish_failed(message)
                  if still_current() else None)

    def _on_outbox_published(self, report, refresh_notes=True):
        self._publishing = False
        self._refresh_outbox()
        self.outbox_summary.setText(
            '{} published · {} retrying · {} conflict(s) · {} failed'.format(
                report.succeeded, report.retried, report.conflicts, report.failed))
        if refresh_notes and self._version is not None:
            self._load_notes(self._version)
        if self._on_published is not None and self._outbox is not None:
            self._on_published(report, self._outbox)

    def _on_outbox_publish_failed(self, message):
        self._publishing = False
        self._refresh_outbox()
        self.outbox_summary.setText('Sync not completed: {}'.format(
            html.escape(str(message))))

    def _retry_outbox_item(self):
        if self._closed or self._conflict_busy or self._publishing:
            return
        item_id = self._selected_outbox_id()
        if item_id and self._outbox is not None:
            try:
                self._outbox.retry_now(item_id)
                self._refresh_outbox()
            except (OSError, ValueError, OutboxBusy, OutboxConflict) as exc:
                self.outbox_summary.setText('Retry unavailable: {}'.format(exc))

    def _discard_outbox_item(self):
        if self._closed or self._conflict_busy or self._publishing:
            return
        item_id = self._selected_outbox_id()
        if item_id is None or self._outbox is None:
            return
        if QtWidgets.QMessageBox.question(
                self, 'Discard Queued Change',
                'Discard this unpublished change from the local outbox?',
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No) != QtWidgets.QMessageBox.Yes:
            return
        try:
            self._outbox.discard(item_id)
            self._refresh_outbox()
        except (OSError, ValueError, OutboxBusy) as exc:
            self.outbox_summary.setText('Could not discard: {}'.format(exc))

    def _inspect_outbox_conflict(self):
        """Read remote Version/Note on a worker; never publish on inspection."""
        if (self._closed or self._conflict_busy or self._publishing or
                self.sg is None or self._outbox is None):
            return
        job_id = self._selected_outbox_id()
        entry = self._outbox.get(job_id) if job_id else None
        if entry is None or entry.state != 'conflict':
            return
        box, sg = self._outbox, self.sg
        self._conflict_busy = True
        self._update_outbox_buttons()
        self.outbox_summary.setText('Checking the current remote Version/Note (read-only)…')
        token = self._bump('conflict')
        def current():
            return (not self._closed and self.sg is sg and self._outbox is box
                    and self._req.get('conflict') == token)
        def done(inspection):
            if not current():
                return
            self._conflict_busy = False
            self._update_outbox_buttons()
            if (self._selected_outbox_id() != job_id or
                    box.get(job_id) is None):
                self._refresh_outbox()
                return  # item or context switched while network was pending
            action = self._choose_conflict_resolution(inspection)
            if action is None or not current():
                self._refresh_outbox()
                return
            if QtWidgets.QMessageBox.question(
                    self, 'Confirm Local Conflict Resolution',
                    'Apply "{}" to local Version {} publication?\n\n'
                    'No remote write will happen now. Requeued changes only '
                    'publish later after a separate Sync confirmation.'.format(
                        action, inspection.version_id),
                    QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                    QtWidgets.QMessageBox.No) != QtWidgets.QMessageBox.Yes:
                return
            if (not current() or self._selected_outbox_id() != job_id or
                    self._publishing):
                return
            if action == 'discard':
                try:
                    box.discard(job_id)  # confirmed local-only discard
                    self._refresh_outbox()
                except (OutboxBusy, OutboxConflict, ValueError, OSError):
                    self.outbox_summary.setText('Outbox changed; inspect again.')
                return
            self._conflict_busy = True
            self._update_outbox_buttons()
            self.outbox_summary.setText('Rechecking remote state before local resolution…')
            self._run('conflict',
                      lambda: box.resolve_conflict(sg, inspection, action),
                      lambda result: self._resolved_outbox_conflict(result)
                      if current() else None,
                      lambda _error: self._conflict_recovery_failed()
                      if current() else None)
        self._run('conflict', lambda: box.inspect_conflict(sg, job_id), done,
                  lambda _error: self._conflict_recovery_failed()
                  if current() else None)

    def _choose_conflict_resolution(self, inspection):
        """Show actual fetched data; return no action unless user clicks one."""
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle('Inspect ShotGrid Conflict · Version {}'.format(
            inspection.version_id))
        dialog.resize(560, 380)
        layout = QtWidgets.QVBoxLayout(dialog)
        content = QtWidgets.QPlainTextEdit(dialog)
        content.setReadOnly(True)
        lines = ['Source: {}'.format(inspection.source_id),
                 'Remote Version: {}'.format(inspection.version_id),
                 'Remote status: {}'.format(inspection.remote_status or '(empty)'),
                 'Publication: {}'.format(inspection.kind)]
        if inspection.kind == 'status':
            lines += ['Queued expected status: {}'.format(
                inspection.expected_status or '(empty)'),
                'Queued desired status: {}'.format(inspection.local_status)]
        else:
            lines += ['Remote tagged Note: {}'.format(inspection.note_relation),
                      'Remote Note ID: {}'.format(inspection.remote_note_id or '(none)'),
                      '', 'LOCAL SUBJECT: {}'.format(inspection.local_subject),
                      'LOCAL COMMENT:', inspection.local_content[:1500],
                      '', 'REMOTE SUBJECT: {}'.format(inspection.remote_subject),
                      'REMOTE COMMENT:', inspection.remote_content[:1500]]
        lines += ['', 'Only a separate, confirmed Sync may write to ShotGrid.',
                  'The server will be checked again before any local resolution.']
        content.setPlainText('\n'.join(lines))
        layout.addWidget(content)
        choices = QtWidgets.QDialogButtonBox(dialog)
        choice = [None]
        labels = {'acknowledge': 'Acknowledge Already Applied',
                  'requeue': 'Requeue After Verification',
                  'rebase': 'Rebase Against Remote Status',
                  'discard': 'Discard Local Change'}
        for action in inspection.actions:
            button = choices.addButton(labels[action], QtWidgets.QDialogButtonBox.ActionRole)
            button.clicked.connect(lambda _checked=False, a=action:
                                   (choice.__setitem__(0, a), dialog.accept()))
        choices.addButton(QtWidgets.QDialogButtonBox.Cancel)
        choices.rejected.connect(dialog.reject)
        layout.addWidget(choices)
        return choice[0] if _exec(dialog) == QtWidgets.QDialog.Accepted else None

    def _resolved_outbox_conflict(self, _result):
        self._conflict_busy = False
        self._refresh_outbox()
        self.outbox_summary.setText(
            'Local conflict resolved. Remote unchanged; Sync needs separate confirmation.')

    def _conflict_recovery_failed(self):
        # Never present raw ShotGrid exception bodies (may contain API keys).
        self._conflict_busy = False
        self._update_outbox_buttons()
        self.outbox_summary.setText(
            'Conflict verification failed or remote state changed. '
            'Check connection, permissions and Version access; inspect again.')

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
        # Capturing the slot's token and the connection epoch also guards error
        # handlers, which formerly had no stale-result protection. A refresh or
        # disconnect must not mutate the UI from an old session's worker.
        token = self._req.get(slot, 0)
        epoch = self._connection_generation
        worker = _Worker(fn, self)
        def live():
            return (not self._closed and self._connection_generation == epoch
                    and self._req.get(slot, 0) == token)
        worker.done.connect(lambda value: on_ok(value) if live() else None)
        if on_fail is not None:
            worker.failed.connect(lambda message: on_fail(message) if live() else None)
        worker.finished.connect(lambda w=worker: self._reap(w))
        self._workers.add(worker)
        worker.start()

    def _reap(self, worker):
        self._workers.discard(worker)
        worker.deleteLater()

    def shutdown(self):
        """Stop every in-flight query with a bounded wait. Returns clean/not."""
        self._closed = True
        self._publishing = False
        self._conflict_busy = False
        self._connection_generation += 1
        self._bump('publish')
        self._bump('conflict')
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
    """Render notes as discrete cards: a titled, bordered block each."""
    cards = []
    for note in rows:
        user = note.get('user') or {}
        name = html.escape(user['name']) if isinstance(user, dict) and user.get('name') else ''
        subject = html.escape(note.get('subject') or '') or 'Note'
        content = html.escape(note.get('content') or '').replace('\n', '<br>')
        header = "<b style='color:%s'>%s</b>" % (style.ACCENT_HI, subject)
        meta = "<span style='color:%s; font-size:9pt'>%s &middot; %s</span>" % (
            style.TEXT_DIM, name, _date_text(note.get('created_at')))
        cards.append(
            "<table width='100%%' cellpadding='7' cellspacing='0' "
            "style='margin-bottom:7px; background:%s; border:1px solid %s;'>"
            "<tr><td>%s<br>%s<br><span style='color:%s'>%s</span></td></tr></table>"
            % (style.PANEL, style.BORDER, header, meta, style.TEXT, content))
    return ''.join(cards)
