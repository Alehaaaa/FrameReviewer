# -*- coding: utf-8 -*-
"""Media playlist as an embeddable sidebar panel, with composition controls.

The window owns no playback: the panel is an ordered list of media paths. Any
change -- selecting clips, changing the mode, or editing the list -- emits
:attr:`compositionChanged` with the effective clips (the selection, or all when
nothing is selected) and the current composition mode (sequence, grid,
difference, or inverted difference). The window applies it live; there is no
Compose button. The window keeps the current file in the list.

``PlaylistPanel`` is a plain ``QWidget`` so it docks inside the Properties
sidebar as its own tool.
"""
from __future__ import absolute_import

import os

from ..core.qtcompat import QtCore, QtGui, QtWidgets, Signal
from ..playback import composition as comp
from ..app.session import Source, Representation, KIND_SEQUENCE, ROLE_ORIGINAL
from .. import style


# Mode selector rows: (label, key). Sequence is the default; there is no "off".
_MODE_ROWS = (
    ('Sequence', comp.SEQUENCE),
    ('Grid', comp.GRID),
    ('Difference', comp.DIFFERENCE),
    ('Invert difference', comp.INVERTED_DIFFERENCE),
    ('A / B', 'a_b'),
    ('Flicker', 'flicker'),
    ('Blend', 'blend'),
    ('Wipe', 'wipe'),
    ('Split', 'split'),
    ('Checker', 'checker'),
    ('Over', 'over'),
)


def _color(name, fallback):
    """Use the window's design tokens, without requiring a new style.py API."""
    return getattr(style, name, fallback)


_TITLE_ROLE = int(QtCore.Qt.UserRole) + 1
_DETAIL_ROLE = int(QtCore.Qt.UserRole) + 2
_OFFLINE_ROLE = int(QtCore.Qt.UserRole) + 3
_ACTIVE_ROLE = int(QtCore.Qt.UserRole) + 4


class _SourceDelegate(QtWidgets.QStyledItemDelegate):
    """Two-line selectable cards without embedded widgets or broken drag/drop.

    The standard QListWidget selection model, row dragging and keyboard focus
    stay authoritative. Data is drawn, not duplicated in child widgets; large
    playlists remain cheap to scroll and edit.
    """

    def sizeHint(self, option, index):
        return QtCore.QSize(180, 57)

    def paint(self, painter, option, index):
        rect = option.rect.adjusted(4, 2, -4, -3)
        if rect.width() <= 0 or rect.height() <= 0:
            return
        selected = bool(option.state & QtWidgets.QStyle.State_Selected)
        hovered = bool(option.state & QtWidgets.QStyle.State_MouseOver)
        active = bool(index.data(_ACTIVE_ROLE))
        offline = bool(index.data(_OFFLINE_ROLE))
        accent = QtGui.QColor(_color('SIDE_ACCENT', '#83b8df'))
        painter.save()
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
        background = QtGui.QColor(_color('SIDE_CARD', '#343a41'))
        if hovered:
            background = QtGui.QColor(_color('SIDE_HOVER', '#3d4650'))
        if selected:
            background = QtGui.QColor(_color('SIDE_ACTIVE', '#364c5e'))
        border = QtGui.QColor(_color('SIDE_BORDER', '#454d55'))
        if selected:
            border = accent
        painter.setPen(QtGui.QPen(border, 1))
        painter.setBrush(background)
        painter.drawRoundedRect(rect, 5, 5)
        if active:
            painter.setPen(QtCore.Qt.NoPen)
            painter.setBrush(accent)
            painter.drawRoundedRect(QtCore.QRect(rect.left(), rect.top() + 5,
                                                 3, rect.height() - 10), 1, 1)

        left = rect.left() + 12
        right = rect.right() - 11
        status = 'OFFLINE' if offline else 'READY'
        status_color = QtGui.QColor(_color('WARNING', '#ca9674') if offline
                                    else _color('SIDE_SUBTLE', '#788592'))
        small_font = QtGui.QFont(option.font)
        small_font.setPointSizeF(max(7.5, option.font.pointSizeF() - 1.5))
        small_font.setWeight(QtGui.QFont.Medium)
        painter.setFont(small_font)
        status_width = QtGui.QFontMetrics(small_font).horizontalAdvance(status)
        painter.setPen(status_color)
        painter.drawText(QtCore.QRect(left, rect.top() + 9,
                                      max(0, right - left), 16),
                         QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter, status)
        title_font = QtGui.QFont(option.font)
        title_font.setBold(active)
        title_font.setWeight(QtGui.QFont.DemiBold if active
                             else QtGui.QFont.Medium)
        painter.setFont(title_font)
        painter.setPen(QtGui.QColor(_color('SIDE_TEXT', '#e1e6ec')))
        title = str(index.data(_TITLE_ROLE) or index.data(QtCore.Qt.DisplayRole) or '')
        title_width = max(0, right - left - status_width - 12)
        title = QtGui.QFontMetrics(title_font).elidedText(
            title, QtCore.Qt.ElideMiddle, title_width)
        painter.drawText(QtCore.QRect(left, rect.top() + 7,
                                      title_width, 20),
                         QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft, title)
        painter.setFont(small_font)
        painter.setPen(QtGui.QColor(_color('SIDE_MUTED', '#a0aab5')))
        detail = str(index.data(_DETAIL_ROLE) or '')
        detail = QtGui.QFontMetrics(small_font).elidedText(
            detail, QtCore.Qt.ElideRight, max(0, right - left))
        painter.drawText(QtCore.QRect(left, rect.top() + 29,
                                      max(0, right - left), 17),
                         QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft, detail)
        painter.restore()


class _PlaylistWidget(QtWidgets.QListWidget):
    """Extended-selection list where clicking empty space clears selection."""

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton and self.itemAt(event.pos()) is None:
            self.clearSelection()
            event.accept()
            return
        super(_PlaylistWidget, self).mousePressEvent(event)


class PlaylistPanel(QtWidgets.QWidget):
    """Live playlist: composition follows the selection (or all) and the mode.

    There is no Compose button -- any change (selection, mode, or list contents)
    emits :attr:`compositionChanged` with the *effective* clips (the selected
    ones, or every clip when nothing is selected) and the current mode. The
    window always keeps the current file in this list.
    """
    compositionChanged = Signal(list, str)   # (effective paths, mode key)
    sourcesChanged = Signal(list)              # ordered stable source IDs
    comparisonParametersChanged = Signal(dict)
    relinkRequested = Signal(str)               # source ID; window owns file dialog
    activateRequested = Signal(str)             # source ID; double-click opens
    roleRequested = Signal(str, str)             # source ID and representation role

    def __init__(self, parent=None):
        super(PlaylistPanel, self).__init__(parent)
        self.setObjectName('FrameReviewerPlaylist')
        self._suspend = False
        self._active_path = None
        self._controller = None

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(1, 2, 1, 2)
        root.setSpacing(10)
        media_card = QtWidgets.QFrame(self)
        media_card.setObjectName('playlistMediaCard')
        media_layout = QtWidgets.QVBoxLayout(media_card)
        media_layout.setContentsMargins(9, 9, 9, 9)
        media_layout.setSpacing(7)
        root.addWidget(media_card, 1)

        media_header = QtWidgets.QHBoxLayout()
        media_header.setSpacing(6)
        media_title = QtWidgets.QLabel('MEDIA')
        media_title.setObjectName('playlistSectionTitle')
        self.media_summary = QtWidgets.QLabel('0 clips')
        self.media_summary.setObjectName('playlistCount')
        self.media_summary.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        media_header.addWidget(media_title)
        media_header.addWidget(self.media_summary, 1)
        media_layout.addLayout(media_header)

        media_hint = QtWidgets.QLabel('Double-click to open  ·  Drag to reorder')
        media_hint.setObjectName('playlistHint')
        media_layout.addWidget(media_hint)

        filter_row = QtWidgets.QHBoxLayout()
        filter_row.setSpacing(6)
        self.search = QtWidgets.QLineEdit()
        self.search.setObjectName('playlistSearch')
        self.search.setPlaceholderText('Search clips, tags, versions…')
        self.search.setToolTip('Filter by clip name, path, role, tags, or version')
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._apply_filter)
        self.missing_only = QtWidgets.QCheckBox('Offline')
        self.missing_only.setObjectName('playlistOfflineFilter')
        self.missing_only.setToolTip('Show only missing or unlinked media; playback selection is unchanged')
        self.missing_only.toggled.connect(self._apply_filter)
        filter_row.addWidget(self.search, 1)
        filter_row.addWidget(self.missing_only)
        media_layout.addLayout(filter_row)

        self.list = _PlaylistWidget()
        self.list.setObjectName('playlistSourceList')
        self.list.setItemDelegate(_SourceDelegate(self.list))
        self.list.setMouseTracking(True)
        self.list.setSpacing(0)
        self.list.setUniformItemSizes(True)
        self.list.setAlternatingRowColors(False)
        self.list.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.list.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.list.setDragDropMode(QtWidgets.QAbstractItemView.InternalMove)
        self.list.setDefaultDropAction(QtCore.Qt.MoveAction)
        self.list.itemSelectionChanged.connect(self._emit_composition)
        self.list.itemSelectionChanged.connect(self._update_summary)
        self.list.itemDoubleClicked.connect(
            lambda item: self.activateRequested.emit(item.data(QtCore.Qt.UserRole)))
        self.list.model().rowsMoved.connect(self._on_rows_moved)
        media_layout.addWidget(self.list, 1)

        controls = QtWidgets.QHBoxLayout()
        controls.setSpacing(5)
        add_btn = QtWidgets.QPushButton('+ Add')
        add_btn.setObjectName('playlistPrimary')
        add_btn.setToolTip('Add media files to the review playlist')
        add_btn.clicked.connect(self._add_files)
        self.remove_btn = QtWidgets.QPushButton('Remove')
        self.remove_btn.setObjectName('playlistSecondary')
        self.remove_btn.setToolTip('Remove selected clips (keeps the currently open clip)')
        self.remove_btn.clicked.connect(self._remove_selected)
        relink_btn = QtWidgets.QPushButton('Relink')
        relink_btn.setObjectName('playlistSecondary')
        relink_btn.setToolTip('Choose a new media representation for the selected source')
        relink_btn.clicked.connect(self._relink_selected)
        clear_btn = QtWidgets.QPushButton('Clear')
        clear_btn.setObjectName('playlistQuiet')
        clear_btn.setToolTip('Clear the playlist except for the currently open clip')
        clear_btn.clicked.connect(self.clear)
        for button, stretch in ((add_btn, 1), (self.remove_btn, 0),
                                (relink_btn, 0), (clear_btn, 0)):
            button.setMinimumHeight(27)
            button.setCursor(QtCore.Qt.PointingHandCursor)
            controls.addWidget(button, stretch)
        media_layout.addLayout(controls)

        role_row = QtWidgets.QHBoxLayout()
        role_row.setSpacing(8)
        role_label = QtWidgets.QLabel('Version')
        role_label.setObjectName('playlistFieldLabel')
        role_row.addWidget(role_label)
        self.role_combo = QtWidgets.QComboBox()
        self.role_combo.setObjectName('playlistRoleCombo')
        self.role_combo.setToolTip('Choose a media representation for the active or selected clip')
        self.role_combo.addItem('Active / Automatic', 'automatic')
        for role in ('original', 'published', 'review', 'proxy', 'playblast', 'exr'):
            self.role_combo.addItem(role.title(), role)
        self.role_combo.currentIndexChanged.connect(self._request_role_change)
        role_row.addWidget(self.role_combo, 1)
        media_layout.addLayout(role_row)
        self.list.itemSelectionChanged.connect(self._sync_role_combo)

        # -- composition mode (applied live) ------------------------------
        compare_card = QtWidgets.QFrame(self)
        compare_card.setObjectName('playlistCompareCard')
        compare_layout = QtWidgets.QVBoxLayout(compare_card)
        compare_layout.setContentsMargins(10, 10, 10, 10)
        compare_layout.setSpacing(7)
        compare_header = QtWidgets.QHBoxLayout()
        compare_title = QtWidgets.QLabel('COMPARISON')
        compare_title.setObjectName('playlistSectionTitle')
        self.compare_summary = QtWidgets.QLabel('All clips in order')
        self.compare_summary.setObjectName('playlistCount')
        self.compare_summary.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        compare_header.addWidget(compare_title)
        compare_header.addWidget(self.compare_summary, 1)
        compare_layout.addLayout(compare_header)
        root.addWidget(compare_card)

        mode_row = QtWidgets.QHBoxLayout()
        mode_row.setSpacing(8)
        playback_label = QtWidgets.QLabel('Mode')
        playback_label.setObjectName('playlistFieldLabel')
        mode_row.addWidget(playback_label)
        self.mode_combo = QtWidgets.QComboBox()
        self.mode_combo.setObjectName('playlistModeCombo')
        self.mode_combo.setToolTip('Sequence uses all sources. Comparisons use selected sources, or all if none are selected.')
        for label, key in _MODE_ROWS:
            self.mode_combo.addItem(label, key)
        self.mode_combo.currentIndexChanged.connect(self._emit_composition)
        self.mode_combo.currentIndexChanged.connect(self._update_summary)
        mode_row.addWidget(self.mode_combo, 1)
        compare_layout.addLayout(mode_row)

        self.compare_hint = QtWidgets.QLabel()
        self.compare_hint.setObjectName('playlistHint')
        self.compare_hint.setWordWrap(True)
        compare_layout.addWidget(self.compare_hint)

        parameter_row = QtWidgets.QHBoxLayout()
        parameter_row.setSpacing(8)
        self.parameter_label = QtWidgets.QLabel('Blend')
        self.parameter_label.setObjectName('playlistFieldLabel')
        self.parameter_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.parameter_slider.setRange(0, 100)
        self.parameter_slider.setValue(50)
        self.parameter_slider.setToolTip('A/B blend or wipe amount')
        self.parameter_slider.valueChanged.connect(self._emit_parameters)
        parameter_row.addWidget(self.parameter_label)
        parameter_row.addWidget(self.parameter_slider, 1)
        compare_layout.addLayout(parameter_row)

        self.comparison_options = QtWidgets.QWidget()
        options_row = QtWidgets.QGridLayout(self.comparison_options)
        options_row.setContentsMargins(0, 0, 0, 0)
        options_row.setHorizontalSpacing(8)
        options_row.setVerticalSpacing(5)
        options_row.setColumnStretch(1, 1)
        self.active_label = QtWidgets.QLabel('Active')
        self.active_combo = QtWidgets.QComboBox()
        self.active_combo.addItem('A · first source', 0)
        self.active_combo.addItem('B · second source', 1)
        self.active_combo.currentIndexChanged.connect(self._emit_parameters)
        options_row.addWidget(self.active_label, 0, 0)
        options_row.addWidget(self.active_combo, 0, 1)
        self.orientation_label = QtWidgets.QLabel('Direction')
        self.orientation_combo = QtWidgets.QComboBox()
        self.orientation_combo.addItem('Vertical', 'vertical')
        self.orientation_combo.addItem('Horizontal', 'horizontal')
        self.orientation_combo.currentIndexChanged.connect(self._emit_parameters)
        options_row.addWidget(self.orientation_label, 1, 0)
        options_row.addWidget(self.orientation_combo, 1, 1)
        self.checker_label = QtWidgets.QLabel('Checker (px)')
        self.checker_size = QtWidgets.QSpinBox()
        self.checker_size.setRange(2, 512)
        self.checker_size.setValue(64)
        self.checker_size.valueChanged.connect(self._emit_parameters)
        options_row.addWidget(self.checker_label, 2, 0)
        options_row.addWidget(self.checker_size, 2, 1)
        self.flicker_label = QtWidgets.QLabel('Hold (frames)')
        self.flicker_frames = QtWidgets.QSpinBox()
        self.flicker_frames.setRange(1, 120)
        self.flicker_frames.setValue(4)
        self.flicker_frames.valueChanged.connect(self._emit_parameters)
        options_row.addWidget(self.flicker_label, 3, 0)
        options_row.addWidget(self.flicker_frames, 3, 1)
        compare_layout.addWidget(self.comparison_options)
        self.mode_combo.currentIndexChanged.connect(self._update_parameter_controls)
        self._update_parameter_controls()
        self._install_style()
        self._update_summary()

    def _install_style(self):
        """Scoped dark Qt5 stylesheet; never overrides Maya-wide controls."""
        card = _color('SIDE_CARD', '#343a41')
        surface = _color('SIDE_FIELD', '#252b31')
        border = _color('SIDE_BORDER', '#454d55')
        accent = _color('SIDE_ACCENT', '#83b8df')
        text = _color('SIDE_TEXT', '#e1e6ec')
        muted = _color('SIDE_MUTED', '#a0aab5')
        self.setStyleSheet('''
            QWidget#FrameReviewerPlaylist {{ background: transparent; }}
            QFrame#playlistMediaCard, QFrame#playlistCompareCard {{
                background: {card}; border: 1px solid {border};
                border-radius: 7px;
            }}
            QLabel#playlistSectionTitle {{ color: {text};
                font-weight: 700; font-size: 10px; letter-spacing: 1px;
                background: transparent; border: none; }}
            QLabel#playlistCount, QLabel#playlistHint,
            QLabel#playlistFieldLabel {{ color: {muted};
                background: transparent; border: none; }}
            QLabel#playlistCount {{ font-size: 10px; }}
            QLabel#playlistHint {{ font-size: 10px; }}
            QLabel#playlistFieldLabel {{ font-size: 11px; min-width: 48px; }}
            QListWidget#playlistSourceList {{ background: {surface};
                border: 1px solid {border}; border-radius: 5px;
                padding: 2px 1px; outline: 0; }}
            QListWidget#playlistSourceList::item {{ border: none;
                padding: 0; margin: 0; }}
            QLineEdit#playlistSearch, QComboBox#playlistRoleCombo,
            QComboBox#playlistModeCombo {{ background: {surface};
                color: {text}; border: 1px solid {border}; border-radius: 4px;
                padding: 4px 7px; min-height: 20px; }}
            QLineEdit#playlistSearch:focus, QComboBox#playlistRoleCombo:focus,
            QComboBox#playlistModeCombo:focus {{ border: 1px solid {accent}; }}
            QPushButton#playlistPrimary, QPushButton#playlistSecondary,
            QPushButton#playlistQuiet {{ background: transparent;
                color: {text}; border: 1px solid {border};
                border-radius: 4px; padding: 4px 7px; }}
            QPushButton#playlistPrimary {{ background: {accent};
                border-color: {accent}; color: {surface}; font-weight: 600; }}
            QPushButton#playlistPrimary:hover {{ background: {accent_hover}; }}
            QPushButton#playlistSecondary:hover, QPushButton#playlistQuiet:hover {{
                background: {surface}; border-color: {accent}; }}
            QPushButton#playlistQuiet {{ color: {muted}; border-color: transparent; }}
            QCheckBox#playlistOfflineFilter {{ color: {muted};
                background: transparent; spacing: 4px; }}
            QCheckBox#playlistOfflineFilter::indicator {{ width: 12px;
                height: 12px; }}
            QSlider::groove:horizontal {{ height: 4px;
                background: {surface}; border: 1px solid {border};
                border-radius: 2px; }}
            QSlider::handle:horizontal {{ background: {accent}; width: 11px;
                margin: -5px 0; border: 1px solid {border}; border-radius: 5px; }}
        '''.format(card=card, surface=surface, border=border, accent=accent,
                   text=text, muted=muted,
                   accent_hover=_color('SIDE_ACCENT', '#83b8df')))

    def _update_summary(self, *args):
        count = self.list.count()
        visible = sum(not item.isHidden() for item in self._items())
        offline = sum(bool(item.data(_OFFLINE_ROLE)) for item in self._items())
        if visible != count:
            summary = '{} of {} clips'.format(visible, count)
        else:
            summary = '{} clip{}'.format(count, '' if count == 1 else 's')
        if offline:
            summary += ' · {} offline'.format(offline)
        self.media_summary.setText(summary)
        if self.composition_mode() == comp.SEQUENCE:
            self.compare_summary.setText('All clips in order')
            self.compare_hint.setText('Sequence plays the full playlist, even when filtered.')
        else:
            selected = len(self.list.selectedItems())
            self.compare_summary.setText(
                '{} selected'.format(selected) if selected else 'All clips')
            if self.composition_mode() == comp.GRID:
                self.compare_hint.setText(
                    'Tile selected clips, or clear selection to include all.')
            else:
                self.compare_hint.setText(
                    'First two selected clips become A and B. '
                    'Clear selection to use the first two in the playlist.')

    @staticmethod
    def _format_rate(value):
        try:
            return '{:g}'.format(float(value))
        except (ValueError, TypeError):
            return '?'

    def _set_item_presentation(self, item, name, path, exists, source=None):
        """Keep rich row paint metadata in sync with stable Qt UserRole IDs."""
        metadata = (source.metadata if source is not None else {}) or {}
        timing = (source.timing if source is not None else {}) or {}
        role = (source.active_role or ROLE_ORIGINAL) if source else 'Original'
        details = [str(role).title()]
        if timing.get('frame_count'):
            frames = int(timing['frame_count'])
            details.append('{}f @ {}'.format(frames,
                                              self._format_rate(timing.get('fps'))))
        version = metadata.get('version') or metadata.get('version_name')
        if version:
            details.append(str(version))
        tags = metadata.get('tags') or []
        if isinstance(tags, str):
            tags = [tags]
        if tags:
            details.append(', '.join(str(tag) for tag in list(tags)[:2]))
        status = metadata.get('status')
        if status:
            details.append(str(status))
        active = bool(path and self._active_path and
                      os.path.normpath(os.path.abspath(path)) == self._active_path)
        item.setData(_TITLE_ROLE, name)
        item.setData(_DETAIL_ROLE, '  ·  '.join(details))
        item.setData(_OFFLINE_ROLE, not exists)
        item.setData(_ACTIVE_ROLE, active)
        # Preserve a useful accessible/plain-text label for search, tooling,
        # and the default widget behavior outside the custom paint delegate.
        item.setText(('▶ ' if active else '') + name +
                     ('  [missing]' if not exists else ''))
        item.setToolTip('\n'.join(filter(None, (
            name, path, 'Status: ' + ('Offline' if not exists else 'Ready'),
            'Role: {}'.format(role),
            'Source: {}'.format(source.id) if source else None,
            ' · '.join(details),
            'Tags: {}'.format(', '.join(map(str, tags))) if tags else None))))
        if source is not None:
            item.setForeground(QtGui.QBrush(QtGui.QColor(
                _color('WARNING', '#ca9674') if not exists else
                _color('SIDE_TEXT', '#e1e6ec'))))

    # -- model ------------------------------------------------------------
    def paths(self):
        if self._controller is not None:
            return [self._source_path(item.data(QtCore.Qt.UserRole))
                    for item in self._items()]
        return [self.list.item(row).data(QtCore.Qt.UserRole)
                for row in range(self.list.count())]

    def selected_paths(self):
        if self._controller is not None:
            return [self._source_path(item.data(QtCore.Qt.UserRole))
                    for item in self.list.selectedItems()]
        return [item.data(QtCore.Qt.UserRole) for item in self.list.selectedItems()]

    def _items(self):
        return [self.list.item(row) for row in range(self.list.count())]

    def source_ids(self):
        return [item.data(QtCore.Qt.UserRole) for item in self._items()]

    def effective_source_ids(self):
        ids = [item.data(QtCore.Qt.UserRole)
               for item in self.list.selectedItems()]
        return ids or self.source_ids()

    def _source_path(self, source_id):
        source = self._controller.session.source(source_id)
        return source.active_path() if source is not None else None

    def set_session(self, controller):
        """Project playlist rows from a live Session, preserving stable IDs."""
        selected = set(self.effective_source_ids()) if self._controller else set()
        suspended = self._suspend
        self._suspend = True
        try:
            self._controller = controller
            self.list.clear()
            for source in controller.session.sources:
                self._append_source(source)
            self._active_path = (controller.current.active_path()
                                 if controller.current else None)
            self.set_active_path(self._active_path)
            if selected:
                for item in self._items():
                    item.setSelected(item.data(QtCore.Qt.UserRole) in selected)
        finally:
            self._suspend = suspended
        self._apply_filter()
        self._sync_role_combo()

    def _append_source(self, source):
        path = source.active_path() or ''
        resolved = self._controller.resolved_path(source)
        exists = os.path.exists(resolved or path)
        name = source.name or os.path.basename(path) or source.id
        item = QtWidgets.QListWidgetItem()
        item.setData(QtCore.Qt.UserRole, source.id)
        self._set_item_presentation(item, name, resolved or path, exists, source)
        self.list.addItem(item)
        return item

    def _apply_filter(self, *args):
        wanted = self.search.text().strip().casefold()
        offline_only = self.missing_only.isChecked()
        for item in self._items():
            source = (self._controller.session.source(item.data(QtCore.Qt.UserRole))
                      if self._controller is not None else None)
            path = (self._controller.resolved_path(source) if source else
                    item.data(QtCore.Qt.UserRole))
            offline = not bool(path and os.path.exists(path))
            if offline != bool(item.data(_OFFLINE_ROLE)):
                name = (source.name or os.path.basename(path or '') or source.id
                        if source else os.path.basename(path or '') or path or '')
                self._set_item_presentation(item, name, path, not offline, source)
            text = item.text() + ' ' + item.toolTip()
            if source is not None:
                text += ' ' + ' '.join(str(tag) for tag in
                                       (source.metadata or {}).get('tags', []))
            item.setHidden((wanted and wanted not in text.casefold()) or
                           (offline_only and not offline))
        self._update_summary()

    def _sync_role_combo(self, *args):
        if self._controller is None:
            self.role_combo.setEnabled(False)
            return
        selected = self.list.selectedItems()
        source = self._controller.session.source(selected[0].data(QtCore.Qt.UserRole)) \
            if selected else self._controller.current
        role = source.active_role if source else None
        self.role_combo.blockSignals(True)
        try:
            idx = self.role_combo.findData(role)
            self.role_combo.setCurrentIndex(max(0, idx))
            self.role_combo.setEnabled(source is not None)
        finally:
            self.role_combo.blockSignals(False)

    def _request_role_change(self, *args):
        if self._suspend or self._controller is None:
            return
        selected = self.list.selectedItems()
        source = self._controller.session.source(selected[0].data(QtCore.Qt.UserRole)) \
            if selected else self._controller.current
        if source is None:
            return
        self.roleRequested.emit(source.id, self.role_combo.currentData())

    def _on_rows_moved(self, *args):
        if self._suspend or self._controller is None:
            return
        self._controller.reorder(self.source_ids())
        self.sourcesChanged.emit(self.source_ids())
        self._update_summary()
        self._emit_composition()

    def comparison_parameters(self):
        value = self.parameter_slider.value() / 100.0
        return {'blend': value, 'wipe': value,
                'active': self.active_combo.currentData(),
                'orientation': self.orientation_combo.currentData(),
                'checker_size': self.checker_size.value(),
                'flicker_frames': self.flicker_frames.value()}

    def set_comparison_parameters(self, parameters):
        """Restore comparison options from a portable session snapshot."""
        parameters = dict(parameters or {})
        blocked = self._suspend
        self._suspend = True
        try:
            mode = self.composition_mode()
            fraction = parameters.get('wipe' if mode == 'wipe' else 'blend', .5)
            self.parameter_slider.setValue(int(max(0, min(100, round(float(fraction) * 100)))))
            for combo, key in ((self.active_combo, 'active'),
                               (self.orientation_combo, 'orientation')):
                if key in parameters:
                    index = combo.findData(parameters[key])
                    if index >= 0:
                        combo.setCurrentIndex(index)
            for spin, key in ((self.checker_size, 'checker_size'),
                              (self.flicker_frames, 'flicker_frames')):
                if key in parameters:
                    spin.setValue(int(parameters[key]))
        finally:
            self._suspend = blocked

    def _update_parameter_controls(self, *args):
        mode = self.composition_mode()
        visible = mode in ('blend', 'wipe')
        self.parameter_label.setVisible(visible)
        self.parameter_slider.setVisible(visible)
        self.parameter_label.setText('Wipe' if mode == 'wipe' else 'Blend')
        for label, widget, enabled in (
                (self.active_label, self.active_combo, mode == 'a_b'),
                (self.orientation_label, self.orientation_combo,
                 mode in ('wipe', 'split')),
                (self.checker_label, self.checker_size, mode == 'checker'),
                (self.flicker_label, self.flicker_frames, mode == 'flicker')):
            label.setVisible(enabled)
            widget.setVisible(enabled)
        self.comparison_options.setVisible(mode in
                                           ('a_b', 'wipe', 'split', 'checker', 'flicker'))

    def _emit_parameters(self, *args):
        if not self._suspend:
            self.comparisonParametersChanged.emit(self.comparison_parameters())

    def effective_paths(self):
        """Sequence is the full playlist; comparisons honor selected clips."""
        if self.composition_mode() == comp.SEQUENCE:
            return self.paths()
        return self.selected_paths() or self.paths()

    def add_paths(self, paths, active=None, emit=True, select=None):
        """Add clips and optionally identify the currently loaded path.

        Active/current is visual state, not selection: selection is reserved
        exclusively for choosing composition members. ``select`` is retained
        as a compatibility alias for older callers and now means ``active``.
        """
        if active is None and select is not None:
            active = select
        suspended = self._suspend
        self._suspend = True
        try:
            existing = set(self.paths())
            existing_ids = set(self.source_ids()) if self._controller is not None else set()
            for path in paths:
                path = os.path.abspath(path)
                if path in existing:
                    continue
                existing.add(path)
                if self._controller is None:
                    if os.path.exists(path):
                        self._append_path(path)
                else:
                    source = self._controller.source_for_path(path)
                    if source is None:
                        kind = KIND_SEQUENCE if os.path.splitext(path)[1].lower() in (
                            '.exr', '.png', '.jpg', '.jpeg', '.tga') else 'movie'
                        source = self._controller.session.add_source(Source(
                            name=os.path.basename(path), representations=[
                                Representation(ROLE_ORIGINAL, path, kind)]))
                        self._controller.dirty = True
                    if source.id not in existing_ids:
                        self._append_source(source)
                        existing_ids.add(source.id)
            if active is not None:
                self.set_active_path(active)
        finally:
            self._suspend = suspended
        if emit:
            self._apply_filter()
            self._emit_composition()
        else:
            self._apply_filter()

    def clear(self):
        active = self._active_path
        suspended = self._suspend
        self._suspend = True
        try:
            if self._controller is not None:
                keep = self._controller.source_for_path(active) if active else None
                for source in list(self._controller.session.sources):
                    if source is not keep:
                        self._controller.remove_source(source.id)
            self.list.clear()
            if active and (os.path.exists(active) or self._controller is not None):
                if self._controller is not None and keep is not None:
                    self._append_source(keep)
                elif self._controller is None:
                    self._append_path(active)
                self.set_active_path(active)
        finally:
            self._suspend = suspended
        self._apply_filter()
        self._emit_composition()

    def composition_mode(self):
        return self.mode_combo.currentData()

    def set_active_path(self, path):
        """Mark the loaded row without changing composition selection."""
        if not path:
            return
        target = os.path.normpath(os.path.abspath(path))
        self._active_path = target
        suspended = self._suspend
        self._suspend = True
        try:
            for row in range(self.list.count()):
                item = self.list.item(row)
                item_id = item.data(QtCore.Qt.UserRole)
                source = (self._controller.session.source(item_id)
                          if self._controller is not None else None)
                raw = (self._controller.resolved_path(source)
                       if source is not None else item_id)
                item_path = os.path.normpath(raw or '')
                font = item.font()
                font.setBold(item_path == target)
                item.setFont(font)
                name = ((source.name if source is not None else
                         os.path.basename(item_path)) or item_path)
                self._set_item_presentation(item, name, raw,
                                            os.path.exists(raw or ''), source)
        finally:
            self._suspend = suspended
        self.list.viewport().update()

    def _append_path(self, path):
        item = QtWidgets.QListWidgetItem()
        item.setData(QtCore.Qt.UserRole, path)
        self._set_item_presentation(item, os.path.basename(path) or path,
                                    path, os.path.exists(path))
        self.list.addItem(item)
        return item

    def set_suspended(self, suspended):
        """Suppress compositionChanged while the window drives the list."""
        self._suspend = bool(suspended)

    # -- composition ------------------------------------------------------
    def _emit_composition(self, *args):
        if self._suspend:
            return
        if not self.paths():
            return
        self.compositionChanged.emit(self.effective_paths(),
                                     self.composition_mode())

    # -- editing ----------------------------------------------------------
    def _add_files(self):
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self, 'Add media to playlist', os.path.expanduser('~'),
            'Media (*.mp4 *.mov *.mkv *.webm *.avi *.m4v '
            '*.jpg *.jpeg *.png *.tga *.exr);;All files (*)')
        if paths:
            self.add_paths(paths)

    def _remove_selected(self):
        active = self._active_path
        for item in self.list.selectedItems():
            item_id = item.data(QtCore.Qt.UserRole)
            active_source = (self._controller.source_for_path(active)
                             if self._controller is not None and active else None)
            if active_source is not None and item_id == active_source.id:
                # The actively loaded source remains in the live playlist.
                item.setSelected(False)
                continue
            if self._controller is not None:
                self._controller.remove_source(item_id)
            self.list.takeItem(self.list.row(item))
        if active and active not in self.paths() and os.path.exists(active):
            if self._controller is None:
                self._append_path(active)
            else:
                self.add_paths([active], emit=False)
            self.set_active_path(active)
        if self._controller is not None:
            self.sourcesChanged.emit(self.source_ids())
        self._apply_filter()
        self._emit_composition()

    def _relink_selected(self):
        if self._controller is None:
            return
        items = self.list.selectedItems()
        if not items and self._controller.current is not None:
            self.relinkRequested.emit(self._controller.current.id)
        elif items:
            self.relinkRequested.emit(items[0].data(QtCore.Qt.UserRole))
