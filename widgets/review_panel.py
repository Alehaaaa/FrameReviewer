# -*- coding: utf-8 -*-
"""Compact native-Qt editor for typed, source-owned review records.

The widget does not own persistence or contact ShotGrid. It sends explicit
intent to a session/document controller, which is also responsible for undo,
dirty IDs, layer locks and syncing shape selection from PlaybackSurface.
"""
from __future__ import absolute_import

from ..core.qtcompat import QtCore, QtWidgets, Signal


class ReviewRecordsPanel(QtWidgets.QWidget):
    noteSelected = Signal(str)
    saveRequested = Signal(str, object)
    replyRequested = Signal(str, str)
    replyEditRequested = Signal(str, str, str, str, str)
    replyDeleteRequested = Signal(str, str, str, str)
    # Frozen source identity and document revision reject stale UI requests.
    attachmentAddRequested = Signal(str, str, str, str, int)
    attachmentRemoveRequested = Signal(str, str, str, str, int)
    createRequested = Signal()
    layerUpdated = Signal(str, object)

    def __init__(self, parent=None):
        super(ReviewRecordsPanel, self).__init__(parent)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.summary = QtWidgets.QLabel('No review source')
        self.summary.setProperty('dim', True)
        layout.addWidget(self.summary)
        self.list = QtWidgets.QListWidget()
        self.list.setObjectName('reviewRecordList')
        self.list.setMinimumHeight(108)
        self.list.setMaximumHeight(210)
        self.list.setAlternatingRowColors(True)
        self.list.currentItemChanged.connect(self._selected)
        layout.addWidget(self.list)

        self.body = QtWidgets.QPlainTextEdit()
        self.body.setPlaceholderText('Review comment…')
        self.body.setFixedHeight(72)
        layout.addWidget(self.body)
        for text, choices, name in (
                ('Category', ('General', 'Animation', 'Layout', 'Lighting',
                              'Comp', 'Technical'), 'category'),
                ('Priority', ('Normal', 'Low', 'High', 'Critical'), 'priority'),
                ('Status', ('Open', 'In Progress', 'Resolved', 'Approved'), 'status')):
            line = QtWidgets.QHBoxLayout()
            line.addWidget(QtWidgets.QLabel(text))
            field = QtWidgets.QComboBox()
            for choice in choices:
                field.addItem(choice, choice.lower().replace(' ', '_'))
            line.addWidget(field, 1)
            setattr(self, name, field)
            layout.addLayout(line)
        line = QtWidgets.QHBoxLayout()
        line.addWidget(QtWidgets.QLabel('Layer'))
        self.layer = QtWidgets.QComboBox()
        self.layer.currentIndexChanged.connect(self._update_layer_checks)
        line.addWidget(self.layer, 1)
        self.layer_visible = QtWidgets.QCheckBox('Visible')
        self.layer_visible.toggled.connect(self._change_layer)
        self.layer_lock = QtWidgets.QCheckBox('Lock')
        self.layer_lock.toggled.connect(self._change_layer)
        layout.addLayout(line)
        toggles = QtWidgets.QHBoxLayout()
        toggles.addWidget(self.layer_visible)
        toggles.addWidget(self.layer_lock)
        toggles.addStretch(1)
        layout.addLayout(toggles)
        actions = QtWidgets.QHBoxLayout()
        self.add_btn = QtWidgets.QPushButton('New Note')
        self.add_btn.clicked.connect(self.createRequested.emit)
        self.save_btn = QtWidgets.QPushButton('Apply Changes')
        self.save_btn.clicked.connect(self._save)
        actions.addWidget(self.add_btn)
        actions.addWidget(self.save_btn)
        layout.addLayout(actions)

        layout.addWidget(QtWidgets.QLabel('Attachments (local)'))
        attach_actions = QtWidgets.QHBoxLayout()
        self.attachment_choice = QtWidgets.QComboBox()
        self.attachment_choice.setObjectName('reviewAttachmentSelect')
        self.attachment_choice.setToolTip('Select an attachment by its stable ID.')
        self.attachment_choice.currentIndexChanged.connect(self._update_attachment_buttons)
        attach_actions.addWidget(self.attachment_choice, 1)
        self.attachment_remove_btn = QtWidgets.QPushButton('Remove')
        self.attachment_remove_btn.setObjectName('reviewAttachmentRemove')
        self.attachment_remove_btn.clicked.connect(self._remove_attachment)
        attach_actions.addWidget(self.attachment_remove_btn)
        layout.addLayout(attach_actions)
        attach_add = QtWidgets.QHBoxLayout()
        self.attachment_kind = QtWidgets.QComboBox()
        self.attachment_kind.setObjectName('reviewAttachmentKind')
        self.attachment_kind.addItem('File', 'file')
        self.attachment_kind.addItem('Still image', 'still')
        attach_add.addWidget(self.attachment_kind, 1)
        self.attachment_browse_btn = QtWidgets.QPushButton('Browse / Add')
        self.attachment_browse_btn.setObjectName('reviewAttachmentBrowse')
        self.attachment_browse_btn.clicked.connect(self._browse_attachment)
        attach_add.addWidget(self.attachment_browse_btn)
        layout.addLayout(attach_add)

        # Discussion stays within the existing collapsed Review Records card.
        # Display replies as plain text: imported notes are not trusted HTML.
        layout.addWidget(QtWidgets.QLabel('Discussion'))
        self.replies_view = QtWidgets.QTextBrowser()
        self.replies_view.setObjectName('reviewReplies')
        self.replies_view.setFixedHeight(94)
        self.replies_view.setOpenExternalLinks(False)
        layout.addWidget(self.replies_view)
        # Stable IDs, not QTextBrowser paragraphs/positions, identify the
        # existing reply. The browser remains plain text for untrusted imports.
        reply_actions = QtWidgets.QHBoxLayout()
        self.reply_choice = QtWidgets.QComboBox()
        self.reply_choice.setObjectName('reviewReplySelect')
        self.reply_choice.setToolTip('Select a typed discussion reply by stable ID.')
        self.reply_choice.currentIndexChanged.connect(self._selected_reply)
        reply_actions.addWidget(self.reply_choice, 1)
        self.reply_edit_btn = QtWidgets.QPushButton('Save Edit')
        self.reply_edit_btn.setObjectName('reviewReplyEdit')
        self.reply_edit_btn.clicked.connect(self._edit_reply)
        reply_actions.addWidget(self.reply_edit_btn)
        self.reply_reload_btn = QtWidgets.QPushButton('Reload')
        self.reply_reload_btn.setObjectName('reviewReplyReload')
        self.reply_reload_btn.setToolTip(
            'Discard the selected reply edit draft and reload its latest saved text.')
        self.reply_reload_btn.clicked.connect(self._reload_reply)
        reply_actions.addWidget(self.reply_reload_btn)
        self.reply_delete_btn = QtWidgets.QPushButton('Delete')
        self.reply_delete_btn.setObjectName('reviewReplyDelete')
        self.reply_delete_btn.clicked.connect(self._delete_reply)
        reply_actions.addWidget(self.reply_delete_btn)
        layout.addLayout(reply_actions)
        self.reply_edit = QtWidgets.QPlainTextEdit()
        self.reply_edit.setObjectName('reviewReplyEditDraft')
        self.reply_edit.setFixedHeight(55)
        self.reply_edit.setPlaceholderText('Select an unsent local reply to edit…')
        self.reply_edit.textChanged.connect(self._update_reply_edit_buttons)
        layout.addWidget(self.reply_edit)
        self.reply_draft = QtWidgets.QPlainTextEdit()
        self.reply_draft.setObjectName('reviewReplyDraft')
        self.reply_draft.setPlaceholderText('Write a local reply…')
        self.reply_draft.setFixedHeight(58)
        layout.addWidget(self.reply_draft)
        self.reply_btn = QtWidgets.QPushButton('Add Reply')
        self.reply_btn.setToolTip('Add a reply to this review record locally. Remote publishing is separate.')
        self.reply_btn.clicked.connect(self._add_reply)
        self.reply_draft.textChanged.connect(self._update_reply_button)
        layout.addWidget(self.reply_btn)
        self._busy = False
        self._notes = {}
        self._layers = {}
        self._editing_note_id = None
        self._reply_drafts = {}
        self._reply_edit_drafts = {}
        self._reply_edit_origins = {}
        self._reply_baselines = {}
        self._editing_reply_key = None
        self._restore_reply_key = None
        self._bound_source_id = None
        self._bound_revision = None
        self._attachment_addable = {}
        self._attachment_permissions = {}
        self._attachment_display_note_id = None
        self._set_editable(False)

    def _set_editable(self, enabled):
        for item in (self.body, self.category, self.priority, self.status,
                     self.layer, self.save_btn, self.layer_visible,
                     self.layer_lock, self.reply_draft):
            item.setEnabled(bool(enabled))
        self._update_reply_button()
        self._update_reply_edit_buttons()
        self._update_attachment_buttons()

    def selected_attachment_id(self):
        return self.attachment_choice.currentData()

    def select_attachment(self, attachment_id):
        index = self.attachment_choice.findData(attachment_id)
        if index < 0:
            return False
        self.attachment_choice.setCurrentIndex(index)
        return True

    def _update_attachment_buttons(self, *_):
        note_id = self.selected_id()
        add_allowed, add_reason = self._attachment_addable.get(
            note_id, (False, 'Select an editable review note.'))
        attachment_id = self.selected_attachment_id()
        remove_allowed, remove_reason = self._attachment_permissions.get(
            (note_id, attachment_id),
            (False, 'Only locally copied, unsent attachments can be removed.'))
        self.attachment_browse_btn.setEnabled(bool(add_allowed))
        self.attachment_kind.setEnabled(bool(add_allowed))
        self.attachment_remove_btn.setEnabled(bool(remove_allowed))
        self.attachment_browse_btn.setToolTip(
            add_reason or 'Copy a local file to portable session attachment storage.')
        self.attachment_remove_btn.setToolTip(
            remove_reason or 'Remove the unsent reference; keep its stored copy for undo.')

    def _browse_attachment(self):
        note_id = self.selected_id()
        if (not note_id or not self._attachment_addable.get(note_id, (False,))[0] or
                self._bound_revision is None or not self._bound_source_id):
            return
        kind = self.attachment_kind.currentData()
        file_filter = ('Still images (*.png *.jpg *.jpeg *.gif *.webp *.tif *.tiff '
                       '*.exr *.bmp)' if kind == 'still' else 'All files (*)')
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, 'Attach local {}'.format('still' if kind == 'still' else 'file'),
            '', file_filter)
        if path:
            self.attachmentAddRequested.emit(
                note_id, path, kind, self._bound_source_id, self._bound_revision)

    def _remove_attachment(self):
        note_id = self.selected_id()
        attachment_id = self.selected_attachment_id()
        if (not note_id or not attachment_id or
                not self._attachment_permissions.get((note_id, attachment_id),
                                                     (False,))[0] or
                self._bound_revision is None):
            return
        note = self._notes.get(note_id)
        attachment = next((a for a in note.attachments if a.id == attachment_id), None)
        if attachment is None:
            return
        answer = QtWidgets.QMessageBox.question(
            self, 'Remove unsent attachment',
            'Remove the local attachment "{}" from this review record? '
            'The copied file stays available for undo/recovery. The original '
            'file will not be changed.'.format(attachment.name),
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No)
        if answer == QtWidgets.QMessageBox.Yes:
            self.attachmentRemoveRequested.emit(
                note_id, attachment_id, attachment.uri,
                self._bound_source_id, self._bound_revision)

    def _save_reply_draft(self):
        if self._editing_note_id:
            text = self.reply_draft.toPlainText()
            if text.strip():
                self._reply_drafts[self._editing_note_id] = text
            else:
                self._reply_drafts.pop(self._editing_note_id, None)

    def _update_reply_button(self):
        self.reply_btn.setEnabled(bool(self.selected_id() and
                                       self.reply_draft.isEnabled() and
                                       self.reply_draft.toPlainText().strip()))

    def _add_reply(self):
        note_id = self.selected_id()
        body = self.reply_draft.toPlainText().strip()
        if note_id and note_id in self._notes and body:
            self.replyRequested.emit(note_id, body)

    def mark_reply_sent(self, note_id):
        """Clear a draft only after the controller actually persisted its reply."""
        self._reply_drafts.pop(note_id, None)
        if self._editing_note_id == note_id:
            self.reply_draft.clear()

    def selected_reply_id(self):
        return self.reply_choice.currentData()

    def select_reply(self, reply_id):
        for index in range(self.reply_choice.count()):
            if self.reply_choice.itemData(index) == reply_id:
                self.reply_choice.setCurrentIndex(index)
                return True
        return False

    def _save_reply_edit_draft(self):
        key = self._editing_reply_key
        if key is None:
            return
        value = self.reply_edit.toPlainText()
        baseline = self._reply_baselines.get(key)
        if baseline is not None and value != baseline[0]:
            self._reply_edit_drafts[key] = value
            self._reply_edit_origins.setdefault(key, baseline[:2])
        else:
            self._reply_edit_drafts.pop(key, None)
            self._reply_edit_origins.pop(key, None)

    def _selected_reply(self, *_):
        if self._busy:
            return
        self._display_reply_editor()

    def _display_reply_editor(self):
        self._save_reply_edit_draft()
        reply_id = self.selected_reply_id()
        note_id = self.selected_id()
        key = (note_id, reply_id) if note_id and reply_id else None
        self._editing_reply_key = key
        baseline = self._reply_baselines.get(key)
        self.reply_edit.blockSignals(True)
        self.reply_edit.setPlainText(
            self._reply_edit_drafts.get(key, baseline[0] if baseline else ''))
        self.reply_edit.blockSignals(False)
        self._update_reply_edit_buttons()

    def _update_reply_edit_buttons(self):
        key = self._editing_reply_key
        baseline = self._reply_baselines.get(key)
        permitted = bool(baseline and baseline[2])
        text = self.reply_edit.toPlainText()
        if baseline and key is not None:
            if text == baseline[0]:
                self._reply_edit_drafts.pop(key, None)
                self._reply_edit_origins.pop(key, None)
            elif key not in self._reply_edit_origins:
                self._reply_edit_origins[key] = baseline[:2]
        stale = bool(baseline and key in self._reply_edit_origins and
                     self._reply_edit_origins[key] != baseline[:2])
        self.reply_edit.setEnabled(permitted)
        self.reply_edit_btn.setEnabled(
            permitted and not stale and bool(text.strip()) and
            text != baseline[0])
        self.reply_delete_btn.setEnabled(permitted and not stale)
        self.reply_reload_btn.setEnabled(
            permitted and bool(text != baseline[0] or stale))
        reason = ('Reply changed during your edit. Draft preserved; use Reload '
                  'to discard it and accept the new version.' if stale else
                  baseline[3] if baseline and not permitted else
                  'Only unsent local replies can be edited or deleted.')
        for item in (self.reply_edit, self.reply_edit_btn,
                     self.reply_delete_btn):
            item.setToolTip(reason)
        self.reply_reload_btn.setToolTip(reason if stale else
            'Discard the selected reply edit draft and reload its latest saved text.')

    def _reload_reply(self):
        key = self._editing_reply_key
        baseline = self._reply_baselines.get(key)
        if key is None or baseline is None or not baseline[2]:
            return
        if self.reply_edit.toPlainText() != baseline[0]:
            answer = QtWidgets.QMessageBox.question(
                self, 'Discard reply draft',
                'Discard the unsent edits to this reply and reload the latest '
                'saved text? This does not affect your new-reply draft.',
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No)
            if answer != QtWidgets.QMessageBox.Yes:
                return
        self._reply_edit_drafts.pop(key, None)
        self._reply_edit_origins.pop(key, None)
        self.reply_edit.setPlainText(baseline[0])
        self._update_reply_edit_buttons()

    def _edit_reply(self):
        key = self._editing_reply_key
        baseline = self._reply_baselines.get(key)
        if (not key or not baseline or not baseline[2] or
                (key in self._reply_edit_origins and
                 self._reply_edit_origins[key] != baseline[:2])):
            return
        body = self.reply_edit.toPlainText()
        if body.strip() and body != baseline[0]:
            self.replyEditRequested.emit(
                key[0], key[1], body, baseline[0], baseline[1])

    def _delete_reply(self):
        key = self._editing_reply_key
        baseline = self._reply_baselines.get(key)
        if (not key or not baseline or not baseline[2] or
                (key in self._reply_edit_origins and
                 self._reply_edit_origins[key] != baseline[:2])):
            return
        question = QtWidgets.QMessageBox.question(
            self, 'Delete unsent reply',
            'Delete this unsent local reply? This does not delete the review '
            'note or send any remote changes.',
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No)
        if question == QtWidgets.QMessageBox.Yes:
            self.replyDeleteRequested.emit(
                key[0], key[1], baseline[0], baseline[1])

    def mark_reply_updated(self, note_id, reply_id, submitted_body):
        """After successful persistence only, discard the submitted edit."""
        key = (note_id, reply_id)
        if self._editing_reply_key == key and self.reply_edit.toPlainText() == submitted_body:
            self._editing_reply_key = None
            self._reply_edit_drafts.pop(key, None)
            self._reply_edit_origins.pop(key, None)
        elif self._reply_edit_drafts.get(key) == submitted_body:
            self._reply_edit_drafts.pop(key, None)
            self._reply_edit_origins.pop(key, None)

    def mark_reply_deleted(self, note_id, reply_id):
        """After successful persistence only; never clear new-reply drafts."""
        key = (note_id, reply_id)
        self._reply_edit_drafts.pop(key, None)
        self._reply_edit_origins.pop(key, None)
        if self._editing_reply_key == key:
            self._editing_reply_key = None

    def current_fields(self):
        """Read unapplied note edits without making the document dirty."""
        return {
            'body': self.body.toPlainText(),
            'category': self.category.currentData(),
            'priority': self.priority.currentData(),
            'status': self.status.currentData(),
            'layer_id': self.layer.currentData(),
        }

    def restore_fields(self, note_id, values):
        """Preserve unapplied edits when a reply refreshes the selected note."""
        if self.selected_id() != note_id:
            return
        self.body.setPlainText(values['body'])
        for key in ('category', 'priority', 'status', 'layer_id'):
            field = self.layer if key == 'layer_id' else getattr(self, key)
            index = field.findData(values[key])
            if index >= 0:
                field.blockSignals(True)
                field.setCurrentIndex(index)
                field.blockSignals(False)
        self._update_layer_checks()

    def bind(self, document, selected_id=None):
        """Refresh detached records without emitting a selection or mutation."""
        self._busy = True
        try:
            source_id = document.source_id if document else None
            self._restore_reply_key = (
                self._editing_reply_key
                if self._bound_source_id == source_id else None)
            if self._bound_source_id == source_id:
                self._save_reply_draft()
                self._save_reply_edit_draft()
            else:
                self._reply_drafts.clear()
                self._reply_edit_drafts.clear()
                self._reply_edit_origins.clear()
                self._reply_baselines.clear()
                self._editing_reply_key = None
                self._editing_note_id = None
            self._bound_source_id = source_id
            self._bound_revision = document.revision if document else None
            self.add_btn.setEnabled(document is not None)
            self._notes = {note.id: note for note in document.notes} if document else {}
            self._attachment_addable = {
                note.id: document.attachment_addability(note.id)
                for note in self._notes.values()
            } if document else {}
            self._attachment_permissions = {
                (note.id, attachment.id): document.attachment_editability(
                    note.id, attachment.id)
                for note in self._notes.values() for attachment in note.attachments
            } if document else {}
            self._reply_baselines = {
                (note.id, reply.id): (
                    reply.body, reply.modified,
                    *document.reply_editability(note.id, reply.id))
                for note in self._notes.values() for reply in note.replies
            } if document else {}
            self._reply_edit_drafts = {
                key: value for key, value in self._reply_edit_drafts.items()
                if key in self._reply_baselines
            }
            self._reply_edit_origins = {
                key: value for key, value in self._reply_edit_origins.items()
                if key in self._reply_baselines
            }
            self._layers = {layer.id: layer for layer in document.layers} if document else {}
            self.list.clear()
            for note in sorted(self._notes.values(),
                               key=lambda n: (n.source_frame, n.created, n.id)):
                caption = ('{}  ·  {}  ·  {}'.format(
                    note.source_frame, note.status.replace('_', ' ').title(),
                    (note.body.splitlines()[0][:65] if note.body else 'Drawing')))
                item = QtWidgets.QListWidgetItem(caption)
                item.setData(QtCore.Qt.UserRole, note.id)
                item.setToolTip('Note {} · Source frame {}'.format(
                    note.id, note.source_frame))
                self.list.addItem(item)
            self.layer.clear()
            for layer in self._layers.values():
                self.layer.addItem(layer.name, layer.id)
            self.summary.setText('{} review record{}  ·  {} local change{}'.format(
                len(self._notes), '' if len(self._notes) == 1 else 's',
                len(document.dirty_ids) if document else 0,
                '' if document and len(document.dirty_ids) == 1 else 's'))
            for row in range(self.list.count()):
                if self.list.item(row).data(QtCore.Qt.UserRole) == selected_id:
                    self.list.setCurrentRow(row)
                    break
            self._display_selected()
        finally:
            self._restore_reply_key = None
            self._busy = False

    def selected_id(self):
        item = self.list.currentItem()
        return item.data(QtCore.Qt.UserRole) if item is not None else None

    def select_note(self, note_id):
        for row in range(self.list.count()):
            if self.list.item(row).data(QtCore.Qt.UserRole) == note_id:
                self.list.setCurrentRow(row)
                return True
        return False

    def _selected(self, *_):
        self._display_selected()
        if not self._busy and self.selected_id():
            self.noteSelected.emit(self.selected_id())

    def _display_selected(self):
        note = self._notes.get(self.selected_id())
        self._save_reply_draft()
        self._save_reply_edit_draft()
        self._editing_note_id = note.id if note is not None else None
        self._set_editable(bool(note))
        if note is None:
            self.body.clear()
            self.replies_view.clear()
            self.attachment_choice.clear()
            self._attachment_display_note_id = None
            self.reply_draft.clear()
            self.reply_choice.clear()
            self._editing_reply_key = None
            self.reply_edit.clear()
            self._update_reply_edit_buttons()
            self._update_attachment_buttons()
            return
        self.body.setPlainText(note.body)
        previous_attachment = (self.selected_attachment_id()
                               if self._attachment_display_note_id == note.id else None)
        self.attachment_choice.blockSignals(True)
        self.attachment_choice.clear()
        for attachment in note.attachments:
            self.attachment_choice.addItem(
                '{} · {}'.format(attachment.kind.title(), attachment.name or attachment.id),
                attachment.id)
            allowed, reason = self._attachment_permissions.get(
                (note.id, attachment.id), (False, 'Not a local unsent attachment.'))
            self.attachment_choice.setItemData(
                self.attachment_choice.count() - 1,
                reason if not allowed else attachment.uri,
                QtCore.Qt.ToolTipRole)
        if previous_attachment is not None:
            match = self.attachment_choice.findData(previous_attachment)
            if match >= 0:
                self.attachment_choice.setCurrentIndex(match)
        self.attachment_choice.blockSignals(False)
        self._attachment_display_note_id = note.id
        self._update_attachment_buttons()
        self.replies_view.setPlainText('\n\n'.join(
            '{}{}\n{}'.format(reply.author or 'Reviewer',
                               ' · {}'.format(reply.created) if reply.created else '',
                               reply.body)
            for reply in note.replies) if note.replies else 'No replies yet')
        previous_key = self._restore_reply_key or self._editing_reply_key
        previous_reply = (previous_key[1] if previous_key and
                          previous_key[0] == note.id else None)
        self.reply_choice.blockSignals(True)
        self.reply_choice.clear()
        for reply in note.replies:
            label = '{} · {}'.format(reply.author or 'Reviewer',
                                     reply.body.replace('\n', ' ')[:55])
            self.reply_choice.addItem(label, reply.id)
            key = (note.id, reply.id)
            info = self._reply_baselines.get(key)
            if info and not info[2]:
                self.reply_choice.setItemData(
                    self.reply_choice.count() - 1, info[3], QtCore.Qt.ToolTipRole)
        if previous_reply:
            match = self.reply_choice.findData(previous_reply)
            if match >= 0:
                self.reply_choice.setCurrentIndex(match)
        self.reply_choice.blockSignals(False)
        self._display_reply_editor()
        self.reply_draft.setPlainText(self._reply_drafts.get(note.id, ''))
        self._update_reply_button()
        for key in ('category', 'priority', 'status', 'layer'):
            combo = getattr(self, key)
            value = (note.layer_id if key == 'layer' else getattr(note, key))
            index = combo.findData(value)
            combo.blockSignals(True)
            combo.setCurrentIndex(max(0, index))
            combo.blockSignals(False)
        self._update_layer_checks()

    def _update_layer_checks(self):
        layer = self._layers.get(self.layer.currentData())
        for toggle, value in ((self.layer_visible, bool(layer and layer.visible)),
                              (self.layer_lock, bool(layer and layer.locked))):
            toggle.blockSignals(True)
            toggle.setChecked(value)
            toggle.blockSignals(False)

    def _change_layer(self, *_):
        if self._busy:
            return
        layer = self._layers.get(self.layer.currentData())
        if layer:
            self.layerUpdated.emit(layer.id, {
                'visible': self.layer_visible.isChecked(),
                'locked': self.layer_lock.isChecked()})

    def _save(self):
        note_id = self.selected_id()
        if note_id:
            self.saveRequested.emit(note_id, self.current_fields())


__all__ = ['ReviewRecordsPanel']
