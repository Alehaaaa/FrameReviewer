# -*- coding: utf-8 -*-
"""Shared Qt-free path routing for sessions, notes, media and sequences."""
from __future__ import absolute_import

import os

from ..core.path_mapping import PathMapper
from ..core.utils import IMAGE_EXTS, VIDEO_EXTS
from .sequence import expand_pattern, split_sequence_name


AUDIO_EXTS = ('.wav', '.aiff', '.aif', '.flac', '.mp3', '.ogg', '.m4a')
COLOR_EXTS = ('.cube', '.3dl', '.cc', '.ccc', '.cdl', '.clf', '.spi1d', '.spi3d')


class Resolution(object):
    def __init__(self, kind, authored_path, local_path, exists=True, candidates=None,
                 sequence_pattern=None):
        self.kind = kind
        self.authored_path = authored_path
        self.local_path = local_path
        self.exists = bool(exists)
        self.candidates = list(candidates or [])
        # An explicit Shake/frame-cookie path is a timeline declaration. Keep
        # it independently of the first actual image used for decoding.
        self.sequence_pattern = sequence_pattern


class SourceResolver(object):
    """Return a consistent dispatch result for every media-open entry point."""

    def __init__(self, mapper=None):
        self.mapper = mapper or PathMapper.from_env()

    def resolve(self, authored_path, relative_to=None):
        authored = str(authored_path or '').strip()
        if not authored:
            raise ValueError('A source path is required.')
        local = self.mapper.map_existing(authored)
        if relative_to and not os.path.isabs(local):
            local = os.path.join(relative_to, local)
        local = os.path.abspath(os.path.expanduser(local))
        suffix = os.path.splitext(local)[1].lower()
        if suffix == '.frsession':
            return Resolution('session', authored, local, os.path.isfile(local))
        if suffix == '.json':
            return Resolution('notes', authored, local, os.path.isfile(local))
        if suffix in COLOR_EXTS:
            return Resolution('color', authored, local, os.path.isfile(local))
        if suffix in AUDIO_EXTS:
            return Resolution('audio', authored, local, os.path.isfile(local))
        if os.path.isdir(local):
            files = [os.path.join(local, name) for name in sorted(os.listdir(local))
                     if os.path.splitext(name)[1].lower() in IMAGE_EXTS + VIDEO_EXTS]
            return Resolution('folder', authored, local, candidates=files)
        name = os.path.basename(local)
        time_range, pattern = split_sequence_name(name)
        if time_range is not None or '%' in pattern and 'd' in pattern:
            seq = expand_pattern(local)
            files = seq.ordered_paths()
            return Resolution('sequence', authored, files[0] if files else local,
                              exists=bool(files), candidates=files,
                              sequence_pattern=local)
        return Resolution('media', authored, local, os.path.isfile(local))

