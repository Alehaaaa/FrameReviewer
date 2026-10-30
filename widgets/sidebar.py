# -*- coding: utf-8 -*-
"""Reusable construction primitives for the properties sidebar."""
from __future__ import absolute_import

from ..core.qtcompat import QtCore, QtWidgets


class SidebarBuilder(object):
    """Create consistent sidebar pages, cards, hints and numeric controls."""

    def __init__(self, stack, pages, settings):
        self.stack = stack
        self.pages = pages
        self.settings = settings

    def page(self, name, scrollable=True):
        widget = QtWidgets.QWidget()
        widget.setObjectName('vcPageBody')
        layout = QtWidgets.QVBoxLayout(widget)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)
        # Playlist passes scrollable=False: it owns an independently scrollable
        # media list, and a second QScrollArea would compress it and steal
        # wheel events.
        panel = widget
        if scrollable:
            panel = QtWidgets.QScrollArea()
            panel.setObjectName('vcPageScroll')
            panel.setWidgetResizable(True)
            panel.setFrameShape(QtWidgets.QFrame.NoFrame)
            panel.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
            panel.viewport().setAutoFillBackground(False)
            panel.setWidget(widget)
        self.stack.addWidget(panel)
        self.pages[name] = panel
        return layout

    def section(self, layout, title, collapsible=False, collapsed=False):
        """Build a distinct, breathable group with optional disclosure."""
        card = QtWidgets.QFrame()
        card.setObjectName('vcCard')
        card_layout = QtWidgets.QVBoxLayout(card)
        card_layout.setContentsMargins(12, 10, 12, 12)
        card_layout.setSpacing(9)
        key = None
        if collapsible:
            key = 'sidebar/sections/' + title.lower().replace(' ', '_')
            opened = self.settings.get_bool(key, not collapsed)
            heading = QtWidgets.QToolButton()
            heading.setObjectName('vcDisclosure')
            heading.setText(title)
            heading.setCheckable(True)
            heading.setChecked(opened)
            heading.setMinimumHeight(22)
            heading.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
            heading.setArrowType(
                QtCore.Qt.DownArrow if opened else QtCore.Qt.RightArrow)
        else:
            opened = True
            heading = QtWidgets.QLabel(title)
            heading.setObjectName('vcCardTitle')
        card_layout.addWidget(heading)

        content = QtWidgets.QWidget()
        inner = QtWidgets.QVBoxLayout(content)
        inner.setContentsMargins(0, 0, 0, 0)
        inner.setSpacing(8)
        inner.heading = heading
        inner.content = content
        inner.card = card
        card_layout.addWidget(content)
        content.setVisible(opened)
        if collapsible:
            heading.toggled.connect(
                lambda value, body=content, button=heading, setting=key:
                self._toggle_section(value, body, button, setting))
        layout.addWidget(card)
        return inner

    def _toggle_section(self, opened, body, button, key):
        body.setVisible(opened)
        button.setArrowType(
            QtCore.Qt.DownArrow if opened else QtCore.Qt.RightArrow)
        self.settings.set(key, bool(opened))

    @staticmethod
    def hint(text):
        """Dim, word-wrapped helper text that never clips at narrow widths."""
        label = QtWidgets.QLabel(text)
        label.setWordWrap(True)
        label.setProperty('dim', True)
        return label

    @staticmethod
    def numeric_control(layout, label, minimum, maximum, value, suffix):
        container = QtWidgets.QWidget()
        row = QtWidgets.QHBoxLayout(container)
        row.setContentsMargins(0, 0, 0, 0)
        caption = QtWidgets.QLabel(label)
        caption.setFixedWidth(64)
        slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        slider.setRange(minimum, maximum)
        slider.setValue(value)
        spin = QtWidgets.QSpinBox()
        spin.setRange(minimum, maximum)
        spin.setValue(value)
        spin.setSuffix(suffix)
        spin.setFixedWidth(72)
        slider.valueChanged.connect(spin.setValue)
        spin.valueChanged.connect(slider.setValue)
        row.addWidget(caption)
        row.addWidget(slider, 1)
        row.addWidget(spin)
        layout.addWidget(container)
        return slider, spin, caption, container


__all__ = ['SidebarBuilder']
