# -*- coding: utf-8 -*-
"""Small reusable Qt widgets shared by application panels."""
from __future__ import absolute_import

from ..core.qtcompat import QtCore, QtGui, QtWidgets, Signal


class VolumeButton(QtWidgets.QToolButton):
    singleClicked = Signal()
    doubleClicked = Signal()

    def __init__(self, parent=None):
        QtWidgets.QToolButton.__init__(self, parent)
        self._ignore_release = False
        self._click_timer = QtCore.QTimer(self)
        self._click_timer.setSingleShot(True)
        self._click_timer.timeout.connect(self.singleClicked.emit)

    def mouseReleaseEvent(self, event):
        QtWidgets.QToolButton.mouseReleaseEvent(self, event)
        if event.button() != QtCore.Qt.LeftButton:
            return
        if self._ignore_release:
            self._ignore_release = False
            return
        interval = QtWidgets.QApplication.instance().doubleClickInterval()
        self._click_timer.start(interval)

    def mouseDoubleClickEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton:
            self._click_timer.stop()
            self._ignore_release = True
            self.doubleClicked.emit()
            event.accept()
            return
        QtWidgets.QToolButton.mouseDoubleClickEvent(self, event)


class CompactStack(QtWidgets.QStackedWidget):
    """A stack whose layout follows the current page, not its largest page."""

    def __init__(self, parent=None):
        QtWidgets.QStackedWidget.__init__(self, parent)
        self.currentChanged.connect(lambda *_: self.updateGeometry())

    def sizeHint(self):
        page = self.currentWidget()
        return page.sizeHint() if page is not None else QtCore.QSize()

    def minimumSizeHint(self):
        page = self.currentWidget()
        return page.minimumSizeHint() if page is not None else QtCore.QSize()


def _menu_item_option(widget, text='', selected=False):
    option = QtWidgets.QStyleOptionMenuItem()
    option.initFrom(widget)
    option.rect = widget.rect()
    option.menuRect = widget.rect()
    option.menuItemType = QtWidgets.QStyleOptionMenuItem.Normal
    option.checkType = QtWidgets.QStyleOptionMenuItem.NotCheckable
    option.text = text
    option.menuHasCheckableItems = False
    option.maxIconWidth = 0
    try:
        option.reservedShortcutWidth = 0
    except AttributeError:
        option.tabWidth = 0
    option.state = QtWidgets.QStyle.State_Enabled
    if selected:
        option.state |= (QtWidgets.QStyle.State_Selected |
                         QtWidgets.QStyle.State_MouseOver)
    return option


class _OptionBoxRow(QtWidgets.QWidget):
    """Native-style menu row with a Maya option box at the right."""
    primaryClicked = Signal()
    optionsClicked = Signal()
    BOX_WIDTH = 30

    def __init__(self, text, parent=None):
        super(_OptionBoxRow, self).__init__(parent)
        self._text = text
        self._hover = False
        self.setMouseTracking(True)
        self.setSizePolicy(QtWidgets.QSizePolicy.MinimumExpanding,
                           QtWidgets.QSizePolicy.Fixed)

    def sizeHint(self):
        option = _menu_item_option(self, self._text)
        metrics = self.fontMetrics()
        content = QtCore.QSize(
            metrics.horizontalAdvance(self._text) + 24 + self.BOX_WIDTH,
            metrics.height())
        return self.style().sizeFromContents(
            QtWidgets.QStyle.CT_MenuItem, option, content, self)

    def _box_rect(self):
        return QtCore.QRect(self.width() - self.BOX_WIDTH, 0,
                            self.BOX_WIDTH, self.height())

    def enterEvent(self, event):
        self._hover = True
        self.update()

    def leaveEvent(self, event):
        self._hover = False
        self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton:
            if self._box_rect().contains(event.pos()):
                self.optionsClicked.emit()
            elif self.rect().contains(event.pos()):
                self.primaryClicked.emit()
        super(_OptionBoxRow, self).mouseReleaseEvent(event)

    def paintEvent(self, event):
        painter = QtWidgets.QStylePainter(self)
        option = _menu_item_option(self, self._text, self._hover)
        option.rect = self.rect().adjusted(0, 0, 2, 0)
        option.menuRect = option.rect
        painter.drawControl(QtWidgets.QStyle.CE_MenuItem, option)
        box = self._box_rect()
        color = (self.palette().highlightedText().color() if self._hover
                 else self.palette().text().color())
        pen = QtGui.QPen(color)
        pen.setWidth(1)
        painter.setPen(pen)
        center = box.center()
        painter.drawRect(center.x() - 5, center.y() - 5, 10, 10)
        painter.drawRect(center.x() - 2, center.y() - 2, 4, 4)


class OptionBoxAction(QtWidgets.QWidgetAction):
    """Menu action with a native-style Maya option box."""
    primaryTriggered = Signal()
    optionsTriggered = Signal()

    def __init__(self, text, parent=None):
        super(OptionBoxAction, self).__init__(parent)
        self._text = text
        # QWidgetAction still needs an accessible QAction label for keyboard
        # menus, action enumeration, and assistive technology.
        self.setText(text)

    def createWidget(self, parent):
        row = _OptionBoxRow(self._text, parent)
        row.primaryClicked.connect(lambda: self._fire(self.primaryTriggered))
        row.optionsClicked.connect(lambda: self._fire(self.optionsTriggered))
        return row

    def _fire(self, signal):
        popup = QtWidgets.QApplication.activePopupWidget()
        if popup is not None:
            popup.close()
        signal.emit()
