# -*- coding: utf-8 -*-
"""A simple media playlist for stepping through shots during review.

The window owns no playback: it is an ordered list of media paths that emits
:attr:`loadRequested` when the user wants one loaded. The main window connects
that to its loader, marks the active row as media loads, and (optionally) asks
for the next entry when a clip finishes.
"""
from __future__ import absolute_import

import os

from .qtcompat import QtCore, QtWidgets, Signal
from . import style


class PlaylistWindow(QtWidgets.QDialog):
    loadRequested = Signal(str)

    def __init__(self, parent=None):
        super(PlaylistWindow, self).__init__(parent)
        self.setWindowTitle('Playlist')
        self.setObjectName('FrameReviewerPlaylist')
        self.resize(320, 420)
        self.setStyleSheet('QDialog { background: %s; }' % style.BG)

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        self.list = QtWidgets.QListWidget()
        self.list.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.list.itemActivated.connect(self._activate_item)
        self.list.itemDoubleClicked.connect(self._activate_item)
        root.addWidget(self.list, 1)

        controls = QtWidgets.QHBoxLayout()
        controls.setSpacing(4)
        add_btn = QtWidgets.QPushButton('Add Files…')
        add_btn.clicked.connect(self._add_files)
        self.add_current_btn = QtWidgets.QPushButton('Add Current')
        self.remove_btn = QtWidgets.QPushButton('Remove')
        self.remove_btn.clicked.connect(self._remove_selected)
        clear_btn = QtWidgets.QPushButton('Clear')
        clear_btn.clicked.connect(self.clear)
        controls.addWidget(add_btn)
        controls.addWidget(self.add_current_btn)
        controls.addWidget(self.remove_btn)
        controls.addWidget(clear_btn)
        root.addLayout(controls)

        nav = QtWidgets.QHBoxLayout()
        nav.setSpacing(4)
        self.auto_advance = QtWidgets.QCheckBox('Auto-advance at clip end')
        prev_btn = QtWidgets.QPushButton('◀ Prev')
        prev_btn.clicked.connect(self.previous)
        next_btn = QtWidgets.QPushButton('Next ▶')
        next_btn.clicked.connect(self.next)
        nav.addWidget(self.auto_advance, 1)
        nav.addWidget(prev_btn)
        nav.addWidget(next_btn)
        root.addLayout(nav)

        self._active_row = -1

    # -- model ------------------------------------------------------------
    def paths(self):
        return [self.list.item(row).data(QtCore.Qt.UserRole)
                for row in range(self.list.count())]

    def add_paths(self, paths):
        existing = set(self.paths())
        for path in paths:
            path = os.path.abspath(path)
            if path in existing or not os.path.exists(path):
                continue
            existing.add(path)
            item = QtWidgets.QListWidgetItem(os.path.basename(path) or path)
            item.setData(QtCore.Qt.UserRole, path)
            item.setToolTip(path)
            self.list.addItem(item)

    def clear(self):
        self.list.clear()
        self._active_row = -1

    def auto_advance_enabled(self):
        return self.auto_advance.isChecked()

    def set_active_path(self, path):
        """Highlight the row whose media is now loaded, if present."""
        if not path:
            return
        target = os.path.normpath(path)
        for row in range(self.list.count()):
            item = self.list.item(row)
            if os.path.normpath(item.data(QtCore.Qt.UserRole)) == target:
                self._active_row = row
                self.list.setCurrentRow(row)
                return
        self._active_row = -1

    # -- navigation -------------------------------------------------------
    def next(self):
        self._load_row(self._active_row + 1)

    def previous(self):
        self._load_row(self._active_row - 1)

    def _load_row(self, row):
        if 0 <= row < self.list.count():
            self._active_row = row
            self.list.setCurrentRow(row)
            self.loadRequested.emit(self.list.item(row).data(QtCore.Qt.UserRole))

    def _activate_item(self, item):
        self._load_row(self.list.row(item))

    # -- editing ----------------------------------------------------------
    def _add_files(self):
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self, 'Add media to playlist', os.path.expanduser('~'),
            'Media (*.mp4 *.mov *.mkv *.webm *.avi *.m4v '
            '*.jpg *.jpeg *.png *.tga *.exr);;All files (*)')
        if paths:
            self.add_paths(paths)

    def _remove_selected(self):
        for item in self.list.selectedItems():
            self.list.takeItem(self.list.row(item))
        self._active_row = self.list.currentRow()
