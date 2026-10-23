# -*- coding: utf-8 -*-
"""Non-destructive display color correction.

A small grade (exposure, contrast, saturation, temperature) applied to the
*displayed* pixmap only -- it never touches decoded cache frames, exported
media or annotation data. Grading runs on the already-downscaled display
pixmap (<= the cache's decode width), so a few milliseconds of NumPy per frame
keeps playback smooth. When NumPy is unavailable the grade is disabled and the
image passes through untouched.
"""
from __future__ import absolute_import

from .qtcompat import QtCore, QtGui, QtWidgets, Signal

try:
    import numpy as np
    HAVE_NUMPY = True
except Exception:
    np = None
    HAVE_NUMPY = False

# Keys and their neutral (no-op) values.
NEUTRAL = {'exposure': 0.0, 'contrast': 0.0, 'saturation': 0.0, 'temperature': 0.0}


def is_neutral(params):
    if not params:
        return True
    return all(abs(float(params.get(key, 0.0))) < 1e-6 for key in NEUTRAL)


def _qimage_to_array(image):
    """RGB888 QImage -> contiguous HxWx3 uint8 array (handles row padding)."""
    image = image.convertToFormat(QtGui.QImage.Format_RGB888)
    width, height, stride = image.width(), image.height(), image.bytesPerLine()
    ptr = image.constBits()
    try:
        ptr.setsize(height * stride)        # PySide2 returns a sip.voidptr
    except AttributeError:
        pass                                # PySide6 returns a memoryview
    flat = np.frombuffer(ptr, np.uint8, count=height * stride)
    rows = flat.reshape(height, stride)
    return rows[:, :width * 3].reshape(height, width, 3)


def grade_pixmap(pixmap, params):
    """Return a graded copy of ``pixmap``; the original when neutral/unavailable."""
    if pixmap is None or pixmap.isNull() or not HAVE_NUMPY or is_neutral(params):
        return pixmap
    try:
        source = _qimage_to_array(pixmap.toImage()).astype(np.float32) / 255.0
    except Exception:
        return pixmap
    exposure = float(params.get('exposure', 0.0))        # stops
    contrast = float(params.get('contrast', 0.0)) / 100.0
    saturation = 1.0 + float(params.get('saturation', 0.0)) / 100.0
    temperature = float(params.get('temperature', 0.0)) / 100.0

    out = source * (2.0 ** exposure)
    if abs(contrast) > 1e-6:
        out = (out - 0.5) * (1.0 + contrast) + 0.5
    if abs(temperature) > 1e-6:
        # Warm shifts red up and blue down; cool does the reverse.
        out[..., 0] *= (1.0 + 0.2 * temperature)
        out[..., 2] *= (1.0 - 0.2 * temperature)
    if abs(saturation - 1.0) > 1e-6:
        luma = (0.2126 * out[..., 0] + 0.7152 * out[..., 1] +
                0.0722 * out[..., 2])[..., None]
        out = luma + (out - luma) * saturation
    out = np.clip(out * 255.0, 0, 255).astype(np.uint8)
    out = np.ascontiguousarray(out)
    height, width = out.shape[:2]
    image = QtGui.QImage(out.data, width, height, width * 3,
                         QtGui.QImage.Format_RGB888)
    return QtGui.QPixmap.fromImage(image.copy())


class ColorCorrectionDialog(QtWidgets.QDialog):
    """Live exposure/contrast/saturation/temperature sliders."""

    valuesChanged = Signal(object, bool)        # params dict, enabled

    SLIDERS = (
        ('exposure', 'Exposure', -300, 300, 0, 100.0),      # stops *100
        ('contrast', 'Contrast', -100, 100, 0, 1.0),
        ('saturation', 'Saturation', -100, 100, 0, 1.0),
        ('temperature', 'Temperature', -100, 100, 0, 1.0),
    )

    def __init__(self, parent=None):
        super(ColorCorrectionDialog, self).__init__(parent)
        self.setWindowTitle('Color Correction')
        self._sliders = {}
        self._scales = {}
        self._value_labels = {}
        layout = QtWidgets.QVBoxLayout(self)

        self.enable_check = QtWidgets.QCheckBox('Enable color correction')
        layout.addWidget(self.enable_check)

        grid = QtWidgets.QGridLayout()
        for row, (key, label, lo, hi, default, scale) in enumerate(self.SLIDERS):
            slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
            slider.setRange(lo, hi)
            slider.setValue(default)
            value_label = QtWidgets.QLabel('0')
            value_label.setMinimumWidth(40)
            value_label.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
            grid.addWidget(QtWidgets.QLabel(label), row, 0)
            grid.addWidget(slider, row, 1)
            grid.addWidget(value_label, row, 2)
            self._sliders[key] = slider
            self._scales[key] = scale
            self._value_labels[key] = value_label
            slider.valueChanged.connect(self._emit)
        layout.addLayout(grid)

        buttons = QtWidgets.QHBoxLayout()
        reset = QtWidgets.QPushButton('Reset')
        reset.clicked.connect(self.reset_values)
        buttons.addStretch(1)
        buttons.addWidget(reset)
        layout.addLayout(buttons)

        if not HAVE_NUMPY:
            self.enable_check.setChecked(False)
            self.enable_check.setEnabled(False)
            note = QtWidgets.QLabel(
                'NumPy is required in Maya Python for color correction.')
            note.setWordWrap(True)
            layout.addWidget(note)

        self.enable_check.toggled.connect(self._emit)

    def values(self):
        return {key: self._sliders[key].value() / self._scales[key]
                for key in self._sliders}

    def reset_values(self):
        for key, slider in self._sliders.items():
            slider.blockSignals(True)
            slider.setValue(0)
            slider.blockSignals(False)
        self._emit()

    def _emit(self, *args):
        params = self.values()
        for key, label in self._value_labels.items():
            value = params[key]
            label.setText('{:+.2f}'.format(value) if key == 'exposure'
                          else '{:+d}'.format(int(round(value))))
        enabled = bool(self.enable_check.isChecked()) and HAVE_NUMPY
        self.valuesChanged.emit(params, enabled)
