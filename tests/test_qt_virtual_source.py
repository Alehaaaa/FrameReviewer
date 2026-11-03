# -*- coding: utf-8 -*-
"""Offscreen virtual-sequence playback/cache and timeline painting regressions."""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
ROOT = Path(__file__).resolve().parents[1]
if 'framereviewer' not in sys.modules:
    package_spec = importlib.util.spec_from_file_location(
        'framereviewer', ROOT / '__init__.py',
        submodule_search_locations=[str(ROOT)])
    package = importlib.util.module_from_spec(package_spec)
    sys.modules['framereviewer'] = package
    package_spec.loader.exec_module(package)

try:
    from framereviewer.core.qtcompat import QtCore, QtGui, QtWidgets
    from framereviewer.media.ffmpeg import MediaInfo
    from framereviewer.playback.composite_source import CompositeSource
    from framereviewer.playback.composition import SEQUENCE
    from framereviewer.playback.cache_policy import CachePolicy
    from framereviewer.viewer.timeline import MarkedTimelineSlider
except ImportError:
    if importlib.util.find_spec('PySide6') is not None:
        raise
    QtCore = QtGui = QtWidgets = None


@unittest.skipUnless(QtWidgets is not None, 'Qt required for virtual playback')
class VirtualSequenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_two_independent_sequences_and_global_cache_ranges(self):
        with tempfile.TemporaryDirectory() as folder:
            infos = []
            for clip, color in enumerate(('red', 'blue')):
                paths = []
                for index in range(2):
                    path = str(Path(folder) / 'clip{}_{}.png'.format(clip, index))
                    image = QtGui.QImage(32, 18, QtGui.QImage.Format_ARGB32)
                    image.fill(QtGui.QColor(color))
                    self.assertTrue(image.save(path))
                    paths.append(path)
                infos.append(MediaInfo(paths[0], 32, 18, 24.0, len(paths),
                                       sequence_files=paths, fps_rational=(24, 1)))
            source = CompositeSource(infos, SEQUENCE, cache_policy=CachePolicy(),
                                     session_fps=24)
            try:
                self.assertEqual(source._sequence_location(0), (0, 0))
                self.assertEqual(source._sequence_location(3), (1, 1))
                first = source.get(0)
                last = source.get(3)
                self.assertIsNotNone(first)
                self.assertIsNotNone(last)
                self.assertEqual(first.toImage().pixelColor(0, 0),
                                 QtGui.QColor('red'))
                self.assertEqual(last.toImage().pixelColor(0, 0),
                                 QtGui.QColor('blue'))
                self.assertEqual(source._cache_ranges(), [(0, 0), (3, 3)])
                self.assertEqual(len(source._sources), 2)
            finally:
                source.shutdown()

    def test_timeline_clip_colours_not_hidden_by_in_out(self):
        timeline = MarkedTimelineSlider()
        timeline.resize(400, timeline.height())
        timeline.set_frame_count(4, 24.0)
        timeline.set_segments([(0, 1, 0, '#806ea8'),
                               (2, 3, 1, '#a97952')])
        timeline.set_cache_ranges([(0, 0), (2, 2)])
        self.addCleanup(timeline.close)
        image = timeline.grab().toImage()
        track = timeline._lane_rects()[2]
        clip_y = track.top() + 2
        cache_y = track.bottom() - 1
        first_colour = image.pixelColor(70, clip_y)
        second_colour = image.pixelColor(320, clip_y)
        self.assertNotEqual(first_colour, second_colour)
        # The bottom cache indicator is independent from the clip colours.
        cached = image.pixelColor(70, cache_y)
        uncached = image.pixelColor(198, cache_y)
        self.assertNotEqual(cached, uncached)


if __name__ == '__main__':
    unittest.main()
