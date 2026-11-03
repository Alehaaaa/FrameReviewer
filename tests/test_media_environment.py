# -*- coding: utf-8 -*-
"""Regression coverage for Maya/DCC environment leakage into media subprocesses.

These smoke tests are Qt-free and use a tiny executable ffprobe substitute.
They verify subprocess isolation, not codec correctness or Maya integration.
"""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock
import wave


ROOT = Path(__file__).resolve().parents[1]
if 'framereviewer' not in sys.modules:
    package_spec = importlib.util.spec_from_file_location(
        'framereviewer', ROOT / '__init__.py',
        submodule_search_locations=[str(ROOT)])
    package = importlib.util.module_from_spec(package_spec)
    sys.modules['framereviewer'] = package
    package_spec.loader.exec_module(package)

from framereviewer.media.ffmpeg import media_environment
from framereviewer.exporting.spec import verify_output
from framereviewer.exporting.external_audio_proof import (
    freeze_external_record_audio_proof,
)


# An executable stand-in lets us assert the *actual* subprocess environment.
PROBE = """#!/usr/bin/env python3
import json
import os
import sys

blocked = ('LD_LIBRARY_PATH', 'LD_PRELOAD', 'LD_AUDIT',
           'PYTHONHOME', 'PYTHONPATH', 'QT_PLUGIN_PATH')
if any(name in os.environ for name in blocked):
    print('DCC environment leaked into ffprobe', file=sys.stderr)
    sys.exit(91)
if '-show_frames' in sys.argv:
    print(json.dumps({'frames': [{'pts': 0, 'nb_samples': 16}]}))
elif any('codec_type,codec_name,sample_fmt' in item for item in sys.argv):
    print(json.dumps({'streams': [{
        'index': 0, 'codec_type': 'audio', 'codec_name': 'pcm_s16le',
        'sample_fmt': 's16', 'sample_rate': '48000', 'channels': 2,
        'time_base': '1/48000', 'start_pts': 0, 'start_time': '0',
        'duration_ts': 16
    }]}))
else:
    print(json.dumps({'streams': [{
        'width': 32, 'height': 18, 'codec_name': 'h264',
        'pix_fmt': 'yuv420p', 'avg_frame_rate': '24/1',
        'nb_read_frames': '4'
    }], 'format': {'duration': '0.166667'}}))
"""


class MediaEnvironmentRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.ffprobe = self.path / 'ffprobe'
        self.ffprobe.write_text(PROBE, encoding='utf-8')
        self.ffprobe.chmod(0o755)
        self.poison = mock.patch.dict(os.environ, {
            'LD_LIBRARY_PATH': '/broken/maya/libraries',
            'LD_PRELOAD': '/broken/maya/libstdc++.so',
            'PYTHONHOME': '/broken/maya/python',
            'PYTHONPATH': '/broken/maya/modules',
            'QT_PLUGIN_PATH': '/broken/maya/plugins',
            'UNRELATED_TEST_MARKER': 'preserved',
        })
        self.poison.start()
        self.addCleanup(self.poison.stop)

    def test_media_environment_removes_only_studio_library_paths(self):
        env = media_environment()
        for name in ('LD_LIBRARY_PATH', 'LD_PRELOAD', 'PYTHONHOME',
                     'PYTHONPATH', 'QT_PLUGIN_PATH'):
            self.assertNotIn(name, env)
        self.assertEqual(env['UNRELATED_TEST_MARKER'], 'preserved')

    def test_managed_video_verification_isolated_from_maya(self):
        movie = self.path / 'sample.mp4'
        movie.write_bytes(b'fake-mp4-for-environment-test')
        spec = types.SimpleNamespace(
            sequence=False, stage='working', path=str(movie), count=4,
            fps='24', output_size=(32, 18), to_context=lambda: {})
        result = verify_output(spec, ffprobe=str(self.ffprobe))
        self.assertEqual(result['status'], 'verified')
        self.assertEqual(result['frames_probed'], '4')

    def test_external_wav_ffprobe_isolated_from_maya(self):
        wav = self.path / 'source.wav'
        with wave.open(str(wav), 'wb') as writer:
            writer.setnchannels(2)
            writer.setsampwidth(2)
            writer.setframerate(48000)
            writer.writeframes(b'\\x00' * (16 * 4))
        proof = freeze_external_record_audio_proof(
            str(wav), ffprobe=str(self.ffprobe))
        self.assertEqual(proof['sample_frames'], 16)
        self.assertEqual(proof['sample_rate'], 48000)


if __name__ == '__main__':
    unittest.main()
