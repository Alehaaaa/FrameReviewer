# -*- coding: utf-8 -*-
"""Portable, conservative per-occurrence editorial sound choices.

Saved intent is *not* proof of usable sound. The existing v4 export plan must
independently freeze and verify every embedded/external source on every use.
This model intentionally contains no waveform samples, credentials or audio
device settings and cannot instruct an unconsented remote or output write.
"""
from __future__ import absolute_import

import os


def edit_identity(composition):
    """Strict JSON-stable edit fingerprint, independent of media file paths."""
    if composition is None or composition.mode != 'sequence':
        return None
    members = composition.visible_members
    if not 2 <= len(members) <= 8:
        return None
    return [[m.source_id, m.name, m.source_in, m.frame_count,
             m.session_offset, m.trim_in, m.trim_out, str(m.rate), bool(m.hold)]
            for m in members]


def _one_route(raw):
    if not isinstance(raw, dict):
        raise ValueError('Record audio route must be a typed object.')
    kind = raw.get('kind')
    if kind == 'silence':
        if set(raw) != {'kind'}:
            raise ValueError('Silent edit contains unexpected audio routing data.')
        return {'kind': kind}
    if kind == 'embedded':
        if (set(raw) != {'kind', 'stream_index'} or
                type(raw['stream_index']) is not int or
                not 0 <= raw['stream_index'] <= 3):
            raise ValueError('Embedded edit needs a bounded audio ordinal.')
        return dict(raw)
    if kind == 'external':
        if (set(raw) != {'kind', 'external_path', 'stream_index'} or
                type(raw['stream_index']) is not int or
                raw['stream_index'] != 0):
            raise ValueError('External edit requires explicit first PCM stream.')
        path = raw['external_path']
        if (not isinstance(path, str) or not 1 <= len(path) <= 4096 or
                not os.path.isabs(path) or os.path.normpath(path) != path or
                not path.lower().endswith('.wav') or
                any(ord(ch) < 32 or ord(ch) == 127 for ch in path)):
            raise ValueError('External record audio intent needs an absolute canonical WAV path.')
        return dict(raw)
    raise ValueError('Unknown record audio edit kind.')


def freeze_intent(composition, overrides):
    """Return safe JSON intent; a missing override means Source route."""
    key = edit_identity(composition)
    if key is None:
        raise ValueError('Record audio persistence needs a bounded SEQUENCE edit list.')
    if not isinstance(overrides, dict) or any(
            type(index) is not int or index < 0 or index >= len(key)
            for index in overrides):
        raise ValueError('Record audio overrides do not match edit occurrences.')
    return {'version': 1, 'edit_key': key,
            'routes': [_one_route(overrides[i]) if i in overrides else None
                       for i in range(len(key))]}


def restore_intent(composition, intent):
    """Fail closed on mismatched edits or malformed saved routing metadata.

    Does not query files, construct export proofs or mark a Session dirty.
    """
    key = edit_identity(composition)
    if key is None or intent is None:
        return {}
    if (not isinstance(intent, dict) or
            set(intent) != {'version', 'edit_key', 'routes'} or
            type(intent['version']) is not int or intent['version'] != 1 or
            intent['edit_key'] != key or
            not isinstance(intent['routes'], list) or
            len(intent['routes']) != len(key)):
        raise ValueError('Saved record audio intent does not match exact edit occurrences.')
    return {index: _one_route(value)
            for index, value in enumerate(intent['routes'])
            if value is not None}
