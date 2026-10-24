# -*- coding: utf-8 -*-
"""Maya-compatible, scoped stylesheet for the FrameReviewer tool workspace.

Keep the review surface, timeline, menu bar and Maya's own widgets outside
these rules.  Object names and role properties encode semantic hierarchy; do
not set per-widget styles that shadow disabled/focus/hover states.
"""
from __future__ import absolute_import

from .. import style


def sidebar_stylesheet():
    return '''
        QFrame#vcToolRail {{ background: {rail}; border-right: 1px solid {edge}; }}
        QFrame#vcRailDivider {{ background: {edge}; border: 0; }}
        QToolButton#vcToolButton {{ background: transparent; color: {text};
            border: 1px solid transparent; border-radius: 3px; padding: 0px; }}
        QToolButton#vcToolButton:hover {{ background: {hover}; }}
        QToolButton#vcToolButton:checked {{ background: {active};
            border: 1px solid {accent}; color: {text}; }}
        QToolButton#vcToolButton:pressed {{ background: {active}; }}

        QWidget#vcSide {{ background: {bg}; border-left: 1px solid {edge}; }}
        QFrame#vcSideHeader {{ background: {header};
            border-bottom: 1px solid {edge}; border-left: 0px; }}
        QLabel#vcSideEyebrow {{ color: {subtle}; font-size: 10px;
            font-weight: 700; }}
        QLabel#vcSideTitle {{ color: {text}; font-size: 14px;
            font-weight: 600; }}
        QLabel#vcToolSubtitle {{ color: {muted}; font-size: 11px; }}
        QLabel#vcSideIcon {{ background: transparent; border: 0; }}
        QToolButton#vcSideClose {{ background: transparent; color: {muted};
            border: 1px solid transparent; border-radius: 5px;
            font-size: 18px; }}
        QToolButton#vcSideClose:hover {{ color: {text}; background: {hover}; }}
        QFrame#vcSideFooter {{ background: {bg};
            border-top: 1px solid {edge}; }}
        QLabel#vcSideFooterLabel {{ color: {subtle}; font-size: 10px; }}
        QLabel#vcSideShortcut {{ color: {muted}; background: transparent;
            border: 1px solid {edge}; border-radius: 2px;
            padding: 2px 6px; font-size: 10px; font-weight: 600; }}

        QWidget#vcSide QLabel {{ color: {text}; background: transparent; }}
        QWidget#vcSide QLabel[dim="true"] {{ color: {muted}; }}
        QWidget#vcSide QCheckBox {{ color: {text}; spacing: 6px;
            background: transparent; }}
        QWidget#vcSide QCheckBox:disabled {{ color: {subtle}; }}
        QWidget#vcSide QListWidget, QWidget#vcSide QPlainTextEdit,
        QWidget#vcSide QTextBrowser {{ background: {field}; color: {text};
            selection-background-color: {active}; border: 1px solid {edge}; }}
        QWidget#vcSide QStackedWidget, QWidget#vcSide QScrollArea,
        QWidget#vcSide QWidget#vcPageBody {{ background: transparent;
            border: 0; }}
        QWidget#vcSide QFrame#vcCard {{ background: {card};
            border: 1px solid {edge}; border-radius: 3px; }}
        QWidget#vcSide QLabel#vcCardTitle {{ color: {muted};
            font-size: 11px; font-weight: 700; }}
        QWidget#vcSide QToolButton#vcDisclosure {{ color: {muted};
            font-size: 11px; font-weight: 700; text-align: left;
            border: 0; border-radius: 4px; background: transparent;
            padding: 2px 0px; }}
        QWidget#vcSide QToolButton#vcDisclosure:hover {{ color: {text};
            background: transparent; }}
        QWidget#vcSide QToolButton#vcDisclosure:checked {{ color: {muted};
            background: transparent; }}

        QWidget#vcSide QLineEdit, QWidget#vcSide QSpinBox,
        QWidget#vcSide QDoubleSpinBox, QWidget#vcSide QComboBox {{
            background: {field}; color: {text};
            border: 1px solid {edge}; border-radius: 2px;
            padding: 4px 6px; min-height: 22px; selection-background-color: {active}; }}
        QWidget#vcSide QLineEdit:focus, QWidget#vcSide QSpinBox:focus,
        QWidget#vcSide QDoubleSpinBox:focus, QWidget#vcSide QComboBox:focus {{
            border: 1px solid {accent}; }}
        QWidget#vcSide QLineEdit:disabled, QWidget#vcSide QSpinBox:disabled,
        QWidget#vcSide QComboBox:disabled {{ color: {subtle};
            background: {bg}; border-color: {edge}; }}
        QWidget#vcSide QToolButton {{ color: {text};
            border: 1px solid transparent; border-radius: 3px;
            background: transparent; }}
        QWidget#vcSide QToolButton:hover {{ background: {hover}; }}
        QWidget#vcSide QToolButton:disabled {{ color: {subtle}; }}
        QWidget#vcSide QToolButton[toolMode="true"] {{ background: {field};
            border: 1px solid {edge}; border-radius: 3px; }}
        QWidget#vcSide QToolButton[toolMode="true"]:hover {{
            background: {hover}; }}
        QWidget#vcSide QToolButton[toolMode="true"]:checked {{
            background: {active}; border: 1px solid {accent}; }}
        QWidget#vcSide QPushButton {{ background: {control}; color: {text};
            border: 1px solid {edge}; border-radius: 3px;
            min-height: 27px; padding: 3px 9px; }}
        QWidget#vcSide QPushButton:hover {{ background: {hover};
            border-color: {muted}; }}
        QWidget#vcSide QPushButton:pressed {{ background: {active}; }}
        QWidget#vcSide QPushButton:disabled {{ color: {subtle};
            border-color: {edge}; background: {bg}; }}
        QWidget#vcSide QPushButton[role="primary"] {{
            background: {primary}; color: {text}; border-color: {edge};
            font-weight: 600; min-height: 30px; }}
        QWidget#vcSide QPushButton[role="primary"]:hover {{
            background: {primaryhover}; }}
        QWidget#vcSide QPushButton[role="danger"] {{
            background: {danger}; color: {dangertext}; border-color: {dangeredge}; }}
        QWidget#vcSide QPushButton[role="danger"]:hover {{
            background: {dangerhover}; }}
        QWidget#vcSide QPushButton[role="utility"] {{ background: transparent;
            color: {muted}; border: 1px solid transparent;
            min-height: 24px; padding: 2px 7px; }}
        QWidget#vcSide QPushButton[role="utility"]:hover {{
            color: {text}; background: {hover}; border-color: {edge}; }}
        QWidget#vcSide QPushButton[role="utility"]:disabled {{
            color: {subtle}; background: transparent; border-color: transparent; }}
        QWidget#vcSide QSlider::groove:horizontal {{ background: {edge};
            border: 0; border-radius: 2px; height: 4px; }}
        QWidget#vcSide QSlider::handle:horizontal {{ background: {accent};
            border: 1px solid {rail}; width: 12px; margin: -5px 0;
            border-radius: 6px; }}
        QWidget#vcSide QSlider::handle:horizontal:hover {{ background: {text}; }}
        QWidget#vcSide QProgressBar {{ background: {field};
            border: 1px solid {edge}; border-radius: 4px; }}
        QWidget#vcSide QProgressBar::chunk {{ background: {accent}; }}
        QWidget#vcSide QScrollBar:vertical {{ background: {bg};
            width: 9px; border: 0; margin: 2px 1px; }}
        QWidget#vcSide QScrollBar::handle:vertical {{ background: {edge};
            min-height: 30px; border: 0; border-radius: 3px; }}
        QWidget#vcSide QScrollBar::handle:vertical:hover {{
            background: {muted}; }}
        QWidget#vcSide QScrollBar::add-line:vertical,
        QWidget#vcSide QScrollBar::sub-line:vertical {{
            background: transparent; border: 0; height: 0px; }}
        QWidget#vcSide QScrollBar::add-page:vertical,
        QWidget#vcSide QScrollBar::sub-page:vertical {{
            background: transparent; }}
    '''.format(
        rail=style.SIDE_RAIL, edge=style.SIDE_BORDER, text=style.SIDE_TEXT,
        hover=style.SIDE_HOVER, active=style.SIDE_ACTIVE,
        accent=style.SIDE_ACCENT, bg=style.SIDE_BG,
        header=style.SIDE_HEADER, muted=style.SIDE_MUTED,
        subtle=style.SIDE_SUBTLE, field=style.SIDE_FIELD,
        card=style.SIDE_CARD, control=style.CONTROL,
        primary=style.PRIMARY, primaryhover=style.PRIMARY_HOVER,
        danger=style.DANGER, dangertext=style.DANGER_TEXT,
        dangeredge=style.DANGER_BORDER, dangerhover=style.DANGER_HOVER)


__all__ = ['sidebar_stylesheet']
