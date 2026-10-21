# -*- coding: utf-8 -*-
"""Standalone launcher: python -m framereviewer (outside Maya, for UI testing)."""
from __future__ import absolute_import

import sys

from .qtcompat import QtWidgets
from . import show


def main():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    window = show()  # noqa: F841 (keep a reference alive)
    sys.exit(app.exec() if hasattr(app, 'exec') else app.exec_())


if __name__ == '__main__':
    main()
