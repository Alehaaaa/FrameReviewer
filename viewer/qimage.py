# -*- coding: utf-8 -*-
"""Canonical owned conversions between QImage and NumPy arrays."""
from __future__ import absolute_import

from ..core.qtcompat import QtGui


def _buffer(image, channels, np):
    width, height = image.width(), image.height()
    stride = image.bytesPerLine()
    pointer = image.constBits()
    try:
        pointer.setsize(height * stride)
    except AttributeError:
        pass
    flat = np.frombuffer(pointer, dtype=np.uint8, count=height * stride)
    rows = flat.reshape(height, stride)
    return rows[:, :width * channels].reshape(height, width, channels).copy()


def qimage_to_rgb(image):
    import numpy as np
    converted = image.convertToFormat(QtGui.QImage.Format_RGB888)
    return _buffer(converted, 3, np)


def qimage_to_rgba(image):
    import numpy as np
    converted = image.convertToFormat(QtGui.QImage.Format_RGBA8888)
    return _buffer(converted, 4, np)


def array_to_qimage(array):
    import numpy as np
    owned = np.ascontiguousarray(array, dtype=np.uint8)
    height, width = owned.shape[:2]
    return QtGui.QImage(owned.data, width, height, width * 4,
                        QtGui.QImage.Format_RGBA8888).copy()


__all__ = ['array_to_qimage', 'qimage_to_rgb', 'qimage_to_rgba']
