# -*- coding: utf-8 -*-
"""Offscreen cache-first timeline dragging and reader-cache contracts."""
import importlib.util
import os
from pathlib import Path
import sys
import unittest
from unittest import mock

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
    from framereviewer.viewer.timeline import MarkedTimelineSlider
    from framereviewer.playback.rolling_cache import VideoFrameSource
except ImportError:
    if importlib.util.find_spec('PySide6') is not None:
        raise
    QtCore = QtGui = QtWidgets = MarkedTimelineSlider = VideoFrameSource = None


class FakeMouse:
    def __init__(self, x, button=0, buttons=0):
        self._x, self._button, self._buttons = x, button, buttons
        self.accepted = False

    def pos(self):
        return QtCore.QPoint(self._x, 4)

    def button(self):
        return self._button

    def buttons(self):
        return self._buttons

    def accept(self):
        self.accepted = True


@unittest.skipUnless(QtWidgets is not None, 'Qt required')
class CacheFirstScrubTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_timeline_drag_emits_only_new_frames_and_finishes_last(self):
        timeline = MarkedTimelineSlider()
        self.addCleanup(timeline.close)
        timeline.resize(500, timeline.height())
        timeline.set_frame_count(120, 24)
        frames, started, finished = [], [], []
        timeline.scrubbed.connect(frames.append)
        timeline.scrubStarted.connect(lambda: started.append(True))
        timeline.scrubFinished.connect(finished.append)
        a, b = timeline._frame_for(110), timeline._frame_for(310)
        timeline.mousePressEvent(FakeMouse(110, QtCore.Qt.LeftButton))
        timeline.mouseMoveEvent(FakeMouse(110, buttons=QtCore.Qt.LeftButton))
        timeline.mouseMoveEvent(FakeMouse(310, buttons=QtCore.Qt.LeftButton))
        timeline.mouseReleaseEvent(FakeMouse(310, QtCore.Qt.LeftButton))
        self.assertEqual(started, [True])
        self.assertEqual(frames, [a, b])
        self.assertEqual(finished, [b])
        self.assertEqual(timeline._frame, b)
        self.assertFalse(timeline._dragging)

    def test_backward_cached_scrub_keeps_decoder_alive(self):
        source = VideoFrameSource(
            '/unused/ffmpeg', '/unused/sample.mov', 24, 100)
        self.addCleanup(source.deleteLater)
        source._current = 70
        source._cache[12] = QtGui.QPixmap(2, 2)
        source._worker = object()
        try:
            with mock.patch.object(source, '_cancel_worker',
                                   side_effect=AssertionError('cancelled cache hit')), \
                 mock.patch.object(source, '_move_window'), \
                 mock.patch.object(source, '_schedule'):
                source.ensure(12)
            self.assertEqual(source._direction, 1)
        finally:
            source._worker = None
            source._seek_timer.stop()

    def test_scrubbing_preserves_nonblocking_request_semantics(self):
        window = (ROOT / 'app' / 'window.py').read_text(encoding='utf-8')
        player = (ROOT / 'playback' / 'player.py').read_text(encoding='utf-8')
        self.assertIn('self.player.seek(frame, preview=preview)', window)
        self.assertIn('self.player.finish_scrub()', window)
        self.assertIn('if self._scrub_preview:', player)
        self.assertIn('previous_preview = self._scrub_preview', player)


if __name__ == '__main__':
    unittest.main()
