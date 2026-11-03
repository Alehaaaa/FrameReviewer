# -*- coding: utf-8 -*-
"""Offscreen Qt checks for editable playback-speed presets and wheel stepping."""
import importlib.util
import os
from pathlib import Path
import sys
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
    from framereviewer.widgets.speed_combo import PlaybackSpeedCombo
except ImportError:
    # A configured Qt CI environment must never silently skip GUI tests.
    if importlib.util.find_spec('PySide6') is not None:
        raise
    QtCore = QtGui = QtWidgets = PlaybackSpeedCombo = None


@unittest.skipUnless(QtWidgets is not None, 'Qt required for speed widget')
class SpeedComboTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.combo = PlaybackSpeedCombo((0.25, 0.5, 1., 1.5, 2., 3., 4.))
        self.addCleanup(self.combo.close)
        self.changes = []
        self.combo.speedChanged.connect(self.changes.append)

    def test_native_editable_combo_presets(self):
        self.assertTrue(self.combo.isEditable())
        self.assertEqual(
            [self.combo.itemData(i) for i in range(self.combo.count())],
            [0.25, 0.5, 1.0, 1.5, 2.0, 3.0, 4.0])
        self.assertEqual(self.combo.speed, 1.0)

    def test_custom_entry_is_not_rounded_to_nearest_preset(self):
        self.combo.lineEdit().setText('1.75')
        self.combo.lineEdit().editingFinished.emit()
        self.assertEqual(self.combo.speed, 1.75)
        self.assertEqual(self.changes, [1.75])
        self.assertEqual(self.combo.currentText(), '1.75')
        self.assertEqual(self.combo.currentIndex(), -1)

    def test_selecting_a_preset_then_custom_reverse(self):
        self.combo._choose_preset(self.combo.findData(3.0))
        self.assertEqual(self.combo.speed, 3.0)
        self.combo.lineEdit().setText('-1.25')
        self.combo.lineEdit().editingFinished.emit()
        self.assertEqual(self.combo.speed, -1.25)
        self.assertEqual(self.changes, [3.0, -1.25])

    def test_programmatic_updates_do_not_emit_or_insert_presets(self):
        self.combo.set_speed(3.125)
        self.assertEqual(self.combo.speed, 3.125)
        self.assertEqual(self.changes, [])
        self.assertEqual(self.combo.count(), 7)

    def test_invalid_input_reverts(self):
        self.combo.set_speed(1.25)
        for invalid in ('not a speed', '30', 'nan', 'inf'):
            self.combo.lineEdit().setText(invalid)
            self.combo.lineEdit().editingFinished.emit()
            self.assertEqual(self.combo.speed, 1.25)
            self.assertEqual(self.combo.currentText(), '1.25')
        self.assertEqual(self.changes, [])

    def test_wheel_increments_quarter_steps(self):
        def wheel(delta):
            event = QtGui.QWheelEvent(
                QtCore.QPointF(10, 10), QtCore.QPointF(10, 10),
                QtCore.QPoint(0, 0), QtCore.QPoint(0, delta),
                QtCore.Qt.NoButton, QtCore.Qt.NoModifier,
                QtCore.Qt.ScrollUpdate, False)
            QtWidgets.QApplication.sendEvent(self.combo, event)
        wheel(120)
        self.assertEqual(self.combo.speed, 1.25)
        wheel(-120)
        self.assertEqual(self.combo.speed, 1.0)
        wheel(40)
        wheel(40)
        self.assertEqual(self.combo.speed, 1.0)
        wheel(40)
        self.assertEqual(self.combo.speed, 1.25)
        self.assertEqual(self.changes, [1.25, 1.0, 1.25])

    def test_scene_speed_snapshot_preserves_custom_values(self):
        source = (ROOT / 'app' / 'scene_state.py').read_text(encoding='utf-8')
        self.assertIn("'speed': float(player.speed)", source)
        self.assertIn("window._set_speed(float(state['speed']))", source)
        self.assertNotIn("min(choices, key=", source)


if __name__ == '__main__':
    unittest.main()
