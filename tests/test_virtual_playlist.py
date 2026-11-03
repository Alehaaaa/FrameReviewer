# -*- coding: utf-8 -*-
"""Qt-free contracts for virtual playlist timing, cache and row selection."""
import importlib.util
from pathlib import Path
import sys
import unittest
from fractions import Fraction

ROOT = Path(__file__).resolve().parents[1]
if 'framereviewer' not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        'framereviewer', ROOT / '__init__.py',
        submodule_search_locations=[str(ROOT)])
    package = importlib.util.module_from_spec(spec)
    sys.modules['framereviewer'] = package
    spec.loader.exec_module(package)

from framereviewer.playback.composition import (
    CompositionMember, cached_source_frame_span)


class CacheTimelineTests(unittest.TestCase):
    def test_one_to_one_cache_mapping(self):
        member = CompositionMember('a', 8)
        self.assertEqual(
            cached_source_frame_span(member, 3, 24, 24, 100, 8),
            (103, 103))

    def test_mixed_frame_rate_duplicated_source_frame(self):
        member = CompositionMember('a', 8)
        self.assertEqual(
            cached_source_frame_span(member, 0, 24, 12, 10, 8),
            (10, 11))
        self.assertEqual(
            cached_source_frame_span(member, 2, 24, 12, 10, 8),
            (14, 15))

    def test_source_frames_skipped_at_double_rate(self):
        member = CompositionMember('a', 8)
        self.assertIsNone(
            cached_source_frame_span(member, 1, 24, 48, 10, 8))
        self.assertEqual(
            cached_source_frame_span(member, 2, 24, 48, 10, 8),
            (11, 11))

    def test_held_frames_and_trimmed_source(self):
        member = CompositionMember('a', 6, trim_out=2)
        self.assertEqual(
            cached_source_frame_span(member, 2, 24, 24, 0, 6),
            (2, 5))
        member = CompositionMember('a', 6, trim_out=2, hold=False)
        self.assertEqual(
            cached_source_frame_span(member, 2, 24, 24, 0, 6),
            (2, 2))

    def test_nonzero_source_in(self):
        member = CompositionMember(
            'a', 4, source_in=5, trim_in=5, trim_out=8)
        self.assertEqual(
            cached_source_frame_span(member, 6, 24, 24, 50, 4),
            (51, 51))

    def test_fractional_rate_exactness(self):
        member = CompositionMember('a', 100)
        source_rate = Fraction(24000, 1001)
        for source_frame in range(20):
            span = cached_source_frame_span(
                member, source_frame, Fraction(24), source_rate, 0, 100)
            if span:
                for global_frame in range(span[0], span[1] + 1):
                    self.assertEqual(
                        member.map_frame(global_frame, 24, source_rate),
                        source_frame)


class ArchitectureContracts(unittest.TestCase):
    def test_selection_does_not_load_media(self):
        source = (ROOT / 'media' / 'playlist.py').read_text(encoding='utf-8')
        self.assertNotIn('itemSelectionChanged.connect(self._emit_composition)', source)
        self.assertIn('QKeySequence.Delete', source)
        self.assertIn("QPushButton('Apply Selection')", source)
        self.assertIn('ExtendedSelection', source)

    def test_virtual_playback_uses_reader_caches(self):
        source = (ROOT / 'playback' / 'composite_source.py').read_text(encoding='utf-8')
        self.assertIn('bisect.bisect_right', source)
        self.assertIn('_source_cache_coverage', source)
        self.assertIn('_preroll_index', source)
        self.assertNotIn('concat demuxer', source)

    def test_window_reuses_movie_probe_metadata(self):
        source = (ROOT / 'app' / 'window.py').read_text(encoding='utf-8')
        self.assertIn('self._probe_playback_media(path)', source)
        self.assertIn('self._scrub(composition.clip_range(index)[0])', source)
        self.assertIn("self.export_queue_list.setSelectionMode(", source)
        self.assertIn('self._delete_queue_shortcut', source)


if __name__ == '__main__':
    unittest.main()
