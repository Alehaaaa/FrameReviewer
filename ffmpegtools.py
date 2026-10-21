# -*- coding: utf-8 -*-
"""ffmpeg / ffprobe discovery, a blocking runner, and media probing.

Cross-platform: binaries are located on PATH plus the usual install dirs for
macOS, Linux and Windows. Nothing is bundled.
"""
from __future__ import absolute_import

import os
import shutil
import subprocess
import sys

from . import utils

# Common install locations per platform, searched after PATH.
_EXTRA_DIRS = (
    '/usr/bin', '/usr/local/bin', '/opt/homebrew/bin', '/opt/local/bin',
    r'C:\ffmpeg\bin', r'C:\Program Files\ffmpeg\bin',
)


# DCCs prepend incompatible OpenEXR/GCC/Qt libraries to these paths.
# FFmpeg/FFprobe are separate programs and should load their own libraries.
_MEDIA_ENV_KEYS = ('LD_LIBRARY_PATH', 'LD_PRELOAD', 'LD_AUDIT',
                   'DYLD_LIBRARY_PATH', 'DYLD_FALLBACK_LIBRARY_PATH',
                   'DYLD_INSERT_LIBRARIES', 'QT_PLUGIN_PATH',
                   'QT_QPA_PLATFORM_PLUGIN_PATH', 'PYTHONHOME', 'PYTHONPATH')


def media_environment():
    """A copy of the environment suitable for standalone media utilities."""
    env = os.environ.copy()
    for key in _MEDIA_ENV_KEYS:
        env.pop(key, None)
    return env


def configure_process(proc):
    """Apply the same isolation to Qt QProcess as to Python subprocess."""
    from .qtcompat import QtCore
    env = QtCore.QProcessEnvironment.systemEnvironment()
    for key in _MEDIA_ENV_KEYS:
        env.remove(key)
    proc.setProcessEnvironment(env)
    return proc


def _no_window_kwargs():
    """Keep ffmpeg from flashing a console window on Windows."""
    if sys.platform == 'win32':
        return {'creationflags': getattr(subprocess, 'CREATE_NO_WINDOW', 0)}
    return {}


def find_binary(name):
    """Prefer a working binary over broken studio PATH entries."""
    exe = name + ('.exe' if sys.platform == 'win32' else '')
    # Preserve old per-machine settings after the FrameReviewer rename, while
    # letting the new package-specific override take precedence.
    override = (os.environ.get('FRAMEREVIEWER_' + name.upper(), '').strip() or
                os.environ.get('VIDEOCROPPER_' + name.upper(), '').strip())
    candidates = []
    if override:
        candidates.append(os.path.join(override, exe) if os.path.isdir(override) else override)
    else:
        found = shutil.which(name) or shutil.which(exe)
        if found:
            candidates.append(found)
        for directory in _EXTRA_DIRS:
            candidates.append(os.path.join(directory, exe))
    tried = []
    for candidate in dict.fromkeys(candidates):
        if not os.path.isfile(candidate) or not os.access(candidate, os.X_OK):
            continue
        try:
            kwargs = dict(stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                          env=media_environment())
            kwargs.update(_no_window_kwargs())
            proc = subprocess.Popen([candidate, '-version'], **kwargs)
            try:
                _, err = proc.communicate(timeout=4)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                raise RuntimeError('startup timed out')
            if proc.returncode == 0:
                return candidate
            tried.append('{}: {}'.format(candidate, err.decode('utf-8', 'replace')[-500:]))
        except (OSError, RuntimeError) as exc:
            tried.append('{}: {}'.format(candidate, exc))
    hint = 'Set FRAMEREVIEWER_{} to a compatible executable.'.format(name.upper())
    raise RuntimeError('No working {} found. {}\n{}'.format(name, hint, '\n'.join(tried)))


def run(args):
    """Run to completion, returning decoded stdout; raise on failure."""
    kwargs = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=media_environment())
    kwargs.update(_no_window_kwargs())
    proc = subprocess.Popen(args, **kwargs)
    out, err = proc.communicate()
    if proc.returncode:
        tail = err.decode('utf-8', 'replace')[-2000:]
        raise RuntimeError('{} failed ({}):\n{}\nCheck runtime libraries or configure FRAMEREVIEWER_FFMPEG / FRAMEREVIEWER_FFPROBE.'.format(os.path.basename(args[0]), proc.returncode, tail))
    return out.decode('utf-8', 'replace')


class MediaInfo(object):
    """Immutable description of imported media."""

    def __init__(self, path, width, height, fps, frame_count, sequence_files=None):
        self.path = path
        self.width = int(width)
        self.height = int(height)
        self.fps = max(0.001, float(fps))
        self.frame_count = max(1, int(frame_count))
        self.sequence_files = list(sequence_files or [])

    @property
    def is_sequence(self):
        return bool(self.sequence_files)

    @property
    def duration(self):
        return self.frame_count / self.fps


def probe(ffprobe, qimage_reader, path):
    """Return MediaInfo for a video or image sequence.

    qimage_reader is QtGui.QImageReader, passed in so this module stays Qt-free.
    """
    if not os.path.isfile(path):
        raise RuntimeError('Missing input: {}'.format(path))

    if utils.is_image(path):
        files = utils.sequence_for(path)
        reader = qimage_reader(path)
        size = reader.size()
        if size.isValid():
            return MediaInfo(path, size.width(), size.height(), 24.0,
                             len(files), sequence_files=files)

    out = run([ffprobe, '-v', 'error', '-select_streams', 'v:0',
               '-show_entries', 'stream=width,height,avg_frame_rate,nb_frames,duration',
               '-show_entries', 'format=duration',
               '-of', 'default=noprint_wrappers=1', path])
    meta = {}
    for line in out.splitlines():
        if '=' in line:
            key, value = line.split('=', 1)
            meta[key] = value
    fps = utils.fps_from_text(meta.get('avg_frame_rate'))
    try:
        count = int(meta['nb_frames'])
    except (ValueError, TypeError, KeyError):
        count = max(1, int(round(float(meta.get('duration', 0) or 0) * fps)))
    return MediaInfo(path, int(meta['width']), int(meta['height']), fps, count)
