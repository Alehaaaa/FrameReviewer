# -*- coding: utf-8 -*-
"""Offscreen Qt smoke tests for the OpenRV-style playlist interaction."""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
ROOT = Path(__file__).resolve().parents[1]
if 'framereviewer' not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        'framereviewer', ROOT / '__init__.py',
        submodule_search_locations=[str(ROOT)])
    package = importlib.util.module_from_spec(spec)
    sys.modules['framereviewer'] = package
    spec.loader.exec_module(package)

try:
    from framereviewer.core.qtcompat import QtCore, QtGui, QtWidgets
    from framereviewer.media.playlist import PlaylistPanel
    from PySide6.QtTest import QTest
except ImportError:
    # A configured Qt CI environment must never silently skip GUI tests.
    if importlib.util.find_spec('PySide6') is not None:
        raise
    QtCore = QtGui = QtWidgets = PlaylistPanel = QTest = None


@unittest.skipUnless(QtWidgets is not None, 'PySide6 required')
class PlaylistInteractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = [str(Path(self.tmp.name) / 'clip_{:02}.mp4'.format(i))
                      for i in range(5)]
        for path in self.paths:
            Path(path).write_bytes(b'placeholder')
        self.panel = PlaylistPanel()
        self.panel.resize(440, 720)
        self.panel.show()
        self.application.processEvents()
        self.addCleanup(self.panel.close)
        self.panel.add_paths(self.paths, emit=False)
        self.application.processEvents()

    def test_shift_selection_never_rebuilds_composition(self):
        emissions = []
        self.panel.compositionChanged.connect(lambda *args: emissions.append(args))
        widget = self.panel.list
        QTest.mouseClick(widget.viewport(), QtCore.Qt.LeftButton,
                         pos=widget.visualItemRect(widget.item(0)).center())
        QTest.mouseClick(widget.viewport(), QtCore.Qt.LeftButton,
                         QtCore.Qt.ShiftModifier,
                         pos=widget.visualItemRect(widget.item(2)).center())
        self.application.processEvents()
        self.assertEqual(len(widget.selectedItems()), 3)
        self.assertEqual(emissions, [])

    def test_selection_does_not_rescan_all_playlist_rows(self):
        from unittest import mock
        widget = self.panel.list
        with mock.patch.object(self.panel, '_items',
                               side_effect=AssertionError('unexpected O(N) scan')):
            widget.item(1).setSelected(True)
            widget.item(3).setSelected(True)
            self.application.processEvents()
        self.assertEqual(len(widget.selectedItems()), 2)

    def test_delete_keeps_playing_clip_but_removes_other_selected_rows(self):
        self.panel.set_active_path(self.paths[0])
        list_widget = self.panel.list
        for i in range(3):
            list_widget.item(i).setSelected(True)
        list_widget.setFocus()
        self.application.processEvents()
        QTest.keyClick(list_widget, QtCore.Qt.Key_Delete)
        self.application.processEvents()
        self.assertEqual(self.panel.paths(), [self.paths[0]] + self.paths[3:])

    def test_filter_never_hits_filesystem_or_decoder(self):
        from unittest import mock
        with mock.patch('framereviewer.media.playlist.os.path.exists',
                        side_effect=AssertionError('filesystem scan')):
            self.panel.search.setText('clip_03')
            self.application.processEvents()
        visible = [item for item in self.panel._items() if not item.isHidden()]
        self.assertEqual(len(visible), 1)
        self.assertIn('clip_03', visible[0].text())


if __name__ == '__main__':
    unittest.main()
