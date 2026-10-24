# -*- coding: utf-8 -*-
"""Startup capability report.

Probes the optional and required runtime dependencies and reports, for each, a
status, a short detail, and an actionable remedy, so a reviewer (or a support
bundle) can see at a glance why a feature is unavailable. Dependency-light: each
probe imports or shells out lazily and defensively, so a broken optional
dependency never raises here. Probes are injectable, which keeps the report
deterministic under test and independent of the host.

Availability is ``True`` (present), ``False`` (absent), or ``None`` (unknown /
best effort, e.g. GPU).
"""
from __future__ import absolute_import

import os
import tempfile


OK = 'ok'
MISSING = 'missing'
UNKNOWN = 'unknown'


class Capability(object):
    __slots__ = ('key', 'label', 'required', 'available', 'detail', 'remedy')

    def __init__(self, key, label, required, available, detail='', remedy=''):
        self.key = key
        self.label = label
        self.required = bool(required)
        self.available = available
        self.detail = detail or ''
        self.remedy = remedy or ''

    @property
    def status(self):
        if self.available is None:
            return UNKNOWN
        return OK if self.available else MISSING

    def to_dict(self):
        return {'key': self.key, 'label': self.label, 'required': self.required,
                'available': self.available, 'status': self.status,
                'detail': self.detail, 'remedy': self.remedy}

    def __repr__(self):
        return '<Capability {} {}>'.format(self.key, self.status)


# -- default probes --------------------------------------------------------
# Each returns (available, detail, remedy). None means "unknown".

def _probe_python():
    import platform
    return True, 'Python {}'.format(platform.python_version()), ''


def _probe_qt():
    try:
        from .qtcompat import BINDING, QtCore
        return True, '{} (Qt {})'.format(BINDING, QtCore.qVersion()), ''
    except Exception:
        return False, 'No PySide2/PySide6', 'Run inside Maya, which provides PySide.'


def _probe_qt_multimedia():
    try:
        from .qtcompat import HAVE_MULTIMEDIA
        if HAVE_MULTIMEDIA:
            return True, 'QtMultimedia available', ''
        return False, 'QtMultimedia not importable', 'Audio playback is disabled.'
    except Exception:
        return False, 'Qt not available', 'Run inside Maya.'


def _probe_binary(name):
    try:
        from ..media.ffmpeg import find_binary
        return True, find_binary(name), ''
    except Exception as exc:
        return (False, 'not found',
                'Install {0} and ensure it is on PATH, or set '
                'FRAMEREVIEWER_{1}. ({2})'.format(name, name.upper(),
                                                  str(exc)[:120]))


def _probe_import(module, feature, remedy):
    try:
        mod = __import__(module)
        version = getattr(mod, '__version__', '')
        detail = '{} {}'.format(module, version).strip()
        return True, detail, ''
    except Exception:
        return False, '{} not importable'.format(module), remedy


def _probe_openexr():
    try:
        from ..media import exr
        if exr.available():
            return True, 'OpenEXR reader available', ''
    except Exception:
        pass
    return (False, 'Native OpenEXR reader unavailable (raster FFmpeg fallback may work)',
            'Install OpenEXR/Imath in Maya Python for exact EXR windows, layers and HALF/UINT dtype.')


def _probe_exr_ffmpeg():
    """Separately describe the raster FLOAT fallback; never claim native EXR."""
    try:
        from ..media import exr
        if exr.np is None:
            return False, 'NumPy missing', 'Install NumPy for float EXR pixels.'
        binaries = exr._ffmpeg_binaries()
        if binaries:
            return True, ('FFmpeg FLOAT RGB(A) raster fallback; lacks native EXR '
                          'data/display windows and arbitrary layers'), ''
    except Exception:
        pass
    return (False, 'FFmpeg/FFprobe EXR float fallback not available',
            'Install FFmpeg and FFprobe; native OpenEXR is preferred.')


def _probe_writable_cache():
    target = (os.environ.get('FRAMEREVIEWER_CACHE_DIR', '').strip()
              or tempfile.gettempdir())
    try:
        handle, path = tempfile.mkstemp(dir=target, prefix='frcache_')
        os.close(handle)
        os.remove(path)
        return True, target, ''
    except Exception as exc:
        return (False, '{} not writable'.format(target),
                'Set FRAMEREVIEWER_CACHE_DIR to a writable location. ({})'
                .format(str(exc)[:120]))


def _probe_gpu():
    # Best effort: a GPU viewer is a future capability, so this is informational.
    try:
        from .qtcompat import QtGui
        if hasattr(QtGui, 'QOpenGLContext'):
            return None, 'OpenGL context type present (not instantiated)', ''
    except Exception:
        pass
    return None, 'not probed', ''


# key -> (label, required, probe)
_REGISTRY = [
    ('python', 'Python', True, _probe_python),
    ('qt', 'Qt binding', True, _probe_qt),
    ('ffmpeg', 'FFmpeg', True, lambda: _probe_binary('ffmpeg')),
    ('ffprobe', 'FFprobe', True, lambda: _probe_binary('ffprobe')),
    ('qt_multimedia', 'QtMultimedia (audio)', False, _probe_qt_multimedia),
    ('numpy', 'NumPy', False, lambda: _probe_import(
        'numpy', 'tracking/EXR',
        'Install NumPy in Maya\'s Python for tracking and EXR.')),
    ('opencv', 'OpenCV', False, lambda: _probe_import(
        'cv2', 'tracking',
        'Install a Maya-compatible OpenCV (cv2) for crop tracking.')),
    ('openexr', 'OpenEXR', False, _probe_openexr),
    ('ffmpeg_exr', 'FFmpeg EXR float fallback', False, _probe_exr_ffmpeg),
    ('ocio', 'OpenColorIO', False, lambda: _probe_import(
        'PyOpenColorIO', 'color management',
        'Install PyOpenColorIO for OCIO color management.')),
    ('shotgrid', 'Flow/ShotGrid', False, lambda: _probe_import(
        'shotgun_api3', 'review publishing',
        'Install shotgun_api3 for Flow Production Tracking.')),
    ('cache', 'Writable cache', True, _probe_writable_cache),
    ('gpu', 'GPU viewer', False, _probe_gpu),
]


def probe_capabilities(probes=None, only=None):
    """Return a list of :class:`Capability`, one per registered dependency.

    ``probes`` overrides specific probe callables by key (for tests or custom
    environments). ``only`` restricts the report to the given keys.
    """
    probes = probes or {}
    report = []
    for key, label, required, default in _REGISTRY:
        if only is not None and key not in only:
            continue
        probe = probes.get(key, default)
        try:
            available, detail, remedy = probe()
        except Exception as exc:  # a probe must never break the report
            available, detail, remedy = (False, 'probe failed',
                                         str(exc)[:120])
        report.append(Capability(key, label, required, available, detail, remedy))
    return report


def missing_required(capabilities):
    """Required capabilities that are absent (``available is False``)."""
    return [c for c in capabilities if c.required and c.available is False]


def report_text(capabilities):
    """A human-readable multi-line report."""
    marks = {OK: '[ OK ]', MISSING: '[FAIL]', UNKNOWN: '[ -- ]'}
    lines = []
    for cap in capabilities:
        line = '{} {}'.format(marks[cap.status], cap.label)
        if cap.detail:
            line += ' — {}'.format(cap.detail)
        lines.append(line)
        if cap.status == MISSING and cap.remedy:
            lines.append('       remedy: {}'.format(cap.remedy))
    return '\n'.join(lines)
