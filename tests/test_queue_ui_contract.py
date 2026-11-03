# -*- coding: utf-8 -*-
"""Qt-free contracts for native checkbox styling and durable queue removal."""
import importlib.util
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if 'framereviewer' not in sys.modules:
    package_spec = importlib.util.spec_from_file_location(
        'framereviewer', ROOT / '__init__.py',
        submodule_search_locations=[str(ROOT)])
    package = importlib.util.module_from_spec(package_spec)
    sys.modules['framereviewer'] = package
    package_spec.loader.exec_module(package)

from framereviewer.exporting.queue_store import ExportQueueStore
from framereviewer.exporting.queue_runner import ExportQueueRunner


def _store(state, recovery=None):
    store = ExportQueueStore.__new__(ExportQueueStore)
    store._data = {'tasks': [
        {'id': 'test-id', 'state': state, 'recovery': recovery}
    ]}
    store._save = lambda: None
    return store


class NativeCheckboxTests(unittest.TestCase):
    def test_sidebar_has_no_qcheckbox_styles(self):
        source = (ROOT / 'widgets' / 'sidebar_style.py').read_text(encoding='utf-8')
        self.assertNotIn('QCheckBox', source)

    def test_playlist_keeps_native_checkbox(self):
        code = (ROOT / 'media' / 'playlist.py').read_text(encoding='utf-8')
        self.assertIn("QtWidgets.QCheckBox('Offline')", code)
        self.assertNotIn('QCheckBox#playlistOfflineFilter {', code)
        self.assertNotIn('QCheckBox#playlistOfflineFilter::indicator', code)

    def test_queue_controls_are_simple_and_explicit(self):
        code = (ROOT / 'app' / 'window.py').read_text(encoding='utf-8')
        self.assertIn("section(export_page, 'Managed Queue')", code)
        for label in ('Start', 'Cancel', 'Reveal', 'Remove'):
            self.assertIn("QPushButton('{}')".format(label), code)
        self.assertNotIn("QPushButton('Retry')", code)
        self.assertIn("self.export_queue_start_btn.setProperty('role', 'primary')", code)
        self.assertIn('style.QUEUE_FAILED_BG', code)
        self.assertIn('style.QUEUE_SUCCESS_BG', code)


class QueueRemoveTests(unittest.TestCase):
    def test_idle_history_can_be_removed(self):
        for state in ('pending', 'succeeded', 'failed', 'crashed', 'cancelled'):
            with self.subTest(state=state):
                store = _store(state)
                self.assertTrue(store.can_remove('test-id'))
                self.assertTrue(store.remove('test-id'))
                self.assertEqual(store._data['tasks'], [])

    def test_active_or_recoverable_history_is_protected(self):
        for state in ('running', 'committing', 'cancelling'):
            with self.subTest(state=state):
                store = _store(state)
                self.assertFalse(store.can_remove('test-id'))
                with self.assertRaises(ValueError):
                    store.remove('test-id')
        for state in ('failed', 'crashed'):
            with self.subTest(state=state):
                store = _store(state, {'phase': 'committing'})
                self.assertFalse(store.can_remove('test-id'))
                with self.assertRaises(ValueError):
                    store.remove('test-id')

    def test_existing_backup_prevents_removal(self):
        with tempfile.TemporaryDirectory() as folder:
            backup = Path(folder) / 'important-original.mov'
            backup.write_bytes(b'original')
            store = _store('succeeded', {'backup': str(backup)})
            self.assertFalse(store.can_remove('test-id'))
            self.assertTrue(backup.exists())

    def test_failed_journal_save_restores_entry(self):
        store = _store('succeeded')
        store._save = mock.Mock(side_effect=OSError('disk full'))
        with self.assertRaises(OSError):
            store.remove('test-id')
        self.assertEqual(store._data['tasks'][0]['id'], 'test-id')

    def test_runner_removes_only_private_stage(self):
        with tempfile.TemporaryDirectory() as folder:
            final = Path(folder) / 'published.mp4'
            final.write_bytes(b'keep')
            stage = Path(folder) / '.frqueue-stage'
            stage.mkdir()
            (stage / 'preview.mp4').write_bytes(b'temp')
            task = types.SimpleNamespace(
                id='test-id', spec=types.SimpleNamespace(path=str(final)),
                recovery={'stage': str(stage)})
            class FakeStore:
                def __init__(self):
                    self.removed = False

                def can_remove(self, task_id):
                    return task_id == 'test-id'

                def get(self, task_id):
                    return task

                def remove(self, task_id):
                    self.removed = True
                    return True

            runner = ExportQueueRunner.__new__(ExportQueueRunner)
            runner.store = FakeStore()
            runner.active_id = None
            with mock.patch('framereviewer.exporting.queue_runner._safe_stage', return_value=True):
                self.assertTrue(runner.remove('test-id'))
            self.assertFalse(stage.exists())
            self.assertEqual(final.read_bytes(), b'keep')
            self.assertTrue(runner.store.removed)


if __name__ == '__main__':
    unittest.main()
