# -*- coding: utf-8 -*-
"""About / info dialog -- logo, version badge, author links.

A self-contained port of the aleha_tools About panel: no dependency on that
package's QFlatDialog/DPI helpers, styled from this tool's own palette so it
works identically inside Maya and standalone.
"""

from __future__ import absolute_import

import os
import webbrowser

from .qtcompat import QtCore, QtGui, QtWidgets
from . import style

ICON_DIR = os.path.join(os.path.dirname(__file__), "icons")

AUTHOR = {
    "name": "Aleha",
    "links": {
        "Website": "https://aleha-animates.com/",
        "GitHub": "https://github.com/Alehaaaa",
        "Instagram": "https://www.instagram.com/alejandro_anim/",
    },
}


class AboutDialog(QtWidgets.QDialog):
    _instance = None

    @classmethod
    def show_about(cls, parent, version):
        if cls._instance is not None:
            try:
                cls._instance.close()
            except RuntimeError:
                pass
            cls._instance = None
        dialog = cls(parent, version)
        cls._instance = dialog
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        return dialog

    def __init__(self, parent=None, version="0.0.0"):
        QtWidgets.QDialog.__init__(self, parent)
        self.setObjectName("frameReviewerAbout")
        self.setWindowTitle("About FrameReviewer")
        self.setAttribute(QtCore.Qt.WA_DeleteOnClose, True)
        self.setStyleSheet("QDialog { background: %s; }" % style.BG)

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(26, 24, 26, 18)
        root.setSpacing(12)
        root.setAlignment(QtCore.Qt.AlignCenter)

        logo = QtWidgets.QLabel()
        logo.setAlignment(QtCore.Qt.AlignCenter)
        logo.setPixmap(QtGui.QIcon(os.path.join(ICON_DIR, "logo.svg")).pixmap(84, 84))
        root.addWidget(logo)

        name = QtWidgets.QLabel("FrameReviewer")
        name.setAlignment(QtCore.Qt.AlignCenter)
        name.setStyleSheet(
            "font-size: 19pt; font-weight: bold; color: %s;" % style.TEXT
        )
        root.addWidget(name)

        badge = QtWidgets.QPushButton("v%s" % version)
        badge.setCursor(QtCore.Qt.PointingHandCursor)
        badge.setToolTip("View the project online")
        badge.setStyleSheet(
            "QPushButton { background: rgba(82,133,166,0.18); border: 1px solid %s;"
            " color: %s; border-radius: 9px; padding: 3px 12px; font-weight: bold; }"
            " QPushButton:hover { background: %s; color: white; }"
            % (style.ACCENT, style.ACCENT_HI, style.ACCENT)
        )
        badge.clicked.connect(lambda: webbrowser.open(AUTHOR["links"]["GitHub"]))
        root.addWidget(badge, 0, QtCore.Qt.AlignCenter)

        links = " &nbsp;&middot;&nbsp; ".join(
            "<a href='%s' style='color:%s; text-decoration:none;'>%s</a>"
            % (url, style.ACCENT_HI, label)
            for label, url in AUTHOR["links"].items()
        )
        info = QtWidgets.QLabel(
            "<div style='text-align:center; color:%s; font-size:10pt;'>"
            "<p>&copy; %s &nbsp;&middot;&nbsp; All rights reserved.</p>"
            "<div style='margin-top:6px;'>%s</div>"
            "<p style='margin-top:12px; font-style:italic; color:%s;'>"
            "A Maya media-review &amp; crop player.</p></div>"
            % (style.TEXT_DIM, AUTHOR["name"], links, style.TEXT_DIM)
        )
        info.setAlignment(QtCore.Qt.AlignCenter)
        info.setTextFormat(QtCore.Qt.RichText)
        info.setTextInteractionFlags(QtCore.Qt.TextBrowserInteraction)
        info.setOpenExternalLinks(False)
        info.linkActivated.connect(webbrowser.open)
        root.addWidget(info)

        row = QtWidgets.QHBoxLayout()
        row.addStretch(1)
        close = QtWidgets.QPushButton("Close")
        close.setFixedWidth(96)
        close.clicked.connect(self.close)
        row.addWidget(close)
        row.addStretch(1)
        root.addLayout(row)
        self.setFixedWidth(360)
