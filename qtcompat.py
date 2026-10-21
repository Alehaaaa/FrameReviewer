# -*- coding: utf-8 -*-
"""Qt binding shim.

Resolves PySide6 (Maya 2025+) or PySide2 (Maya 2024) once, so the rest of the
package imports Qt from a single place and never branches on the binding again.
Multimedia is optional: HAVE_MULTIMEDIA tells the player whether the smooth
QMediaPlayer path is available or whether it must fall back to frame stepping.
"""
from __future__ import absolute_import

try:
    from PySide6 import QtCore, QtGui, QtWidgets
    from shiboken6 import wrapInstance
    BINDING = 'PySide6'
except ImportError:  # Maya 2024 and earlier
    from PySide2 import QtCore, QtGui, QtWidgets
    from shiboken2 import wrapInstance
    BINDING = 'PySide2'

Signal = QtCore.Signal
Slot = QtCore.Slot

# QAction/QActionGroup/QShortcut moved from QtWidgets (Qt5) to QtGui (Qt6).
QAction = QtGui.QAction if hasattr(QtGui, 'QAction') else QtWidgets.QAction
QActionGroup = QtGui.QActionGroup if hasattr(QtGui, 'QActionGroup') else QtWidgets.QActionGroup
QShortcut = QtGui.QShortcut if hasattr(QtGui, 'QShortcut') else QtWidgets.QShortcut

# Qt 5 and Qt 6 diverge across the whole multimedia API, so the player adapter
# keys every difference off this one flag.
IS_QT5 = QtCore.qVersion().startswith('5.')

HAVE_MULTIMEDIA = False
QMediaPlayer = QAudioOutput = QMediaContent = None
QVideoSink = QVideoFrame = QAbstractVideoSurface = QAbstractVideoBuffer = None
try:
    if BINDING == 'PySide6':
        from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput, QVideoSink, QVideoFrame
    else:
        from PySide2.QtMultimedia import (QMediaPlayer, QMediaContent, QVideoFrame,
                                          QAbstractVideoSurface, QAbstractVideoBuffer)
    HAVE_MULTIMEDIA = True
except Exception:
    HAVE_MULTIMEDIA = False

try:
    import maya.cmds as cmds
    import maya.OpenMayaUI as omui
    from maya.app.general.mayaMixin import MayaQWidgetDockableMixin
    HAVE_MAYA_DOCKING = True
except ImportError:
    cmds = None
    omui = None
    HAVE_MAYA_DOCKING = False

    class MayaQWidgetDockableMixin(object):
        """Standalone-compatible substitute preserving the Maya class MRO."""
        def __init__(self, parent=None, *args, **kwargs):
            super(MayaQWidgetDockableMixin, self).__init__(parent)


def maya_main_window():
    """Return Maya's main window as a QWidget, or None when standalone."""
    if omui is None:
        return None
    try:
        ptr = omui.MQtUtil.mainWindow()
        return wrapInstance(int(ptr), QtWidgets.QWidget) if ptr else None
    except Exception:
        return None


def add_to_current_maya_layout(widget):
    """Attach an existing mixin widget during workspace-control restoration."""
    if omui is None:
        return False
    try:
        pointer = omui.MQtUtil.findControl(widget.objectName())
        parent = omui.MQtUtil.getCurrentParent()
        if pointer and parent:
            omui.MQtUtil.addWidgetToMayaLayout(int(pointer), int(parent))
            return True
    except Exception:
        pass
    return False
