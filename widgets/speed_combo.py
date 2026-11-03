# -*- coding: utf-8 -*-
"""Editable playback-speed preset combo with quarter-step wheel control."""
import math

from ..core.qtcompat import QtCore, QtWidgets, Signal


class PlaybackSpeedCombo(QtWidgets.QComboBox):
    """One native combo: type a multiplier, choose a preset, or scroll by 0.25."""

    speedChanged = Signal(float)
    MIN_SPEED, MAX_SPEED = -8.0, 8.0
    STEP = 0.25

    def __init__(self, presets, parent=None):
        super(PlaybackSpeedCombo, self).__init__(parent)
        self._speed = 1.0
        self._wheel_delta = 0
        self.setEditable(True)
        self.setInsertPolicy(QtWidgets.QComboBox.NoInsert)
        self.setMaxVisibleItems(10)
        editor = self.lineEdit()
        editor.setAlignment(QtCore.Qt.AlignRight)
        editor.setPlaceholderText('1.25')
        # Native QLineEdit remains purely numeric; the fixed multiplier is
        # a non-selectable child label, not an editable suffix character.
        editor.setTextMargins(0, 0, 16, 0)
        self._multiplier = QtWidgets.QLabel('×', editor)
        self._multiplier.setObjectName('playbackSpeedMultiplier')
        self._multiplier.setAlignment(QtCore.Qt.AlignCenter)
        self._multiplier.setTextInteractionFlags(QtCore.Qt.NoTextInteraction)
        self._multiplier.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
        self._multiplier.setFixedWidth(13)
        editor.installEventFilter(self)
        self._position_multiplier()
        for value in presets:
            self.addItem('{:g}×'.format(value), float(value))
        self.activated[int].connect(self._choose_preset)
        self.lineEdit().editingFinished.connect(self._commit_text)
        self.set_speed(1.0)

    def _position_multiplier(self):
        editor = self.lineEdit()
        self._multiplier.setGeometry(
            max(0, editor.width() - 16), 0, 13, editor.height())

    def eventFilter(self, watched, event):
        if watched is self.lineEdit() and event.type() == QtCore.QEvent.Resize:
            self._position_multiplier()
        return super(PlaybackSpeedCombo, self).eventFilter(watched, event)

    @property
    def speed(self):
        return self._speed

    def set_speed(self, value):
        """Reflect player speed without recursively emitting speedChanged."""
        value = float(value)
        if not math.isfinite(value):
            value = 1.0
        value = min(self.MAX_SPEED, max(self.MIN_SPEED, value))
        self._speed = value
        blocked = self.blockSignals(True)
        try:
            self.setCurrentIndex(-1)  # custom values are not new presets
            self.setEditText('{:g}'.format(value))
        finally:
            self.blockSignals(blocked)

    def _apply_speed(self, value):
        old = self._speed
        self.set_speed(value)
        if self._speed != old:
            self.speedChanged.emit(self._speed)

    def _choose_preset(self, index):
        value = self.itemData(index)
        if value is not None:
            self._apply_speed(float(value))

    def _commit_text(self):
        text = self.currentText().strip().replace('×', '').strip()
        if text.endswith(('x', 'X')):
            text = text[:-1].strip()
        try:
            value = float(text.replace(',', '.'))
        except ValueError:
            self.set_speed(self._speed)
            return
        if not math.isfinite(value) or not self.MIN_SPEED <= value <= self.MAX_SPEED:
            self.set_speed(self._speed)
            return
        self._apply_speed(value)

    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        if not delta and not event.pixelDelta().isNull():
            delta = event.pixelDelta().y() * 3
        self._wheel_delta += delta
        steps = int(self._wheel_delta / 120)
        if steps:
            self._wheel_delta -= steps * 120
            self._apply_speed(round(self._speed + self.STEP * steps, 4))
        event.accept()
