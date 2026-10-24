# -*- coding: utf-8 -*-
"""Small pure helpers shared across the package. No Qt, no ffmpeg."""
from __future__ import absolute_import

import os
import re

IMAGE_EXTS = ('.jpg', '.jpeg', '.png', '.tga', '.bmp', '.tif', '.tiff', '.exr')
VIDEO_EXTS = ('.mp4', '.mov', '.mkv', '.webm', '.avi', '.m4v')


def clamp(value, low, high):
    return max(low, min(high, value))


def frame_to_ms(frame, fps):
    return 1000.0 * frame / max(1e-6, fps)


def ms_to_frame(ms, fps):
    return int(round(ms * max(1e-6, fps) / 1000.0))


def fps_from_text(text):
    """Parse ffprobe rate fields like '24000/1001' or '24'."""
    try:
        if text and '/' in text:
            num, den = text.split('/', 1)
            return float(num) / float(den)
        return float(text)
    except (ValueError, TypeError, ZeroDivisionError):
        return 24.0


def name_from_path(path):
    stem = os.path.splitext(os.path.basename(path or ''))[0]
    stem = re.sub(r'(?:[._-]?\d+|[.#%]+)$', '', stem)
    words = re.findall(r'[\w-]+', stem, re.UNICODE)
    return '_'.join(words[:3]) or 'media'


def valid_name(name):
    name = (name or '').strip()
    return bool(name and name not in ('.', '..') and not re.search(r'[/\\\x00-\x1f]', name))


def is_image(path):
    return os.path.splitext(path or '')[1].lower() in IMAGE_EXTS


def sequence_for(path):
    """Given one numbered image, return its sorted sibling sequence (or [path]).

    Delegates to :func:`framereviewer.media.sequence.discover_sequence`, which
    preserves holes and negative/non-zero frame numbers. The return value is the
    dense list of existing files in frame order, matching the historical
    contract callers rely on. Use :func:`sequence_info` for the real frame
    numbers, padding, and missing-frame detail.
    """
    info = sequence_info(path)
    return info.ordered_paths() or [path]


def sequence_info(path):
    """Return the :class:`~framereviewer.media.sequence.Sequence` for ``path``.

    Exposes real source frame numbers, padding, pattern, and holes rather than a
    bare file list. Imported lazily so this Qt-free helper module does not form
    an import cycle with :mod:`framereviewer.media.sequence`.
    """
    from ..media import sequence
    return sequence.discover_sequence(path)
