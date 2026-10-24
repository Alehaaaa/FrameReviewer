# -*- coding: utf-8 -*-
"""Session-persisted, deliberately narrow audio-only overlay edit intent.

This is an editorial plan, not media proof. Every use must independently
revalidate original external WAV content/PTS via exporting.external_audio_proof.
"""
from __future__ import absolute_import

import os

from .record_audio_intent import edit_identity


def _choice(raw):
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) != {
            'external_path', 'record_start_frame', 'source_in_frame',
            'duration_frames', 'gain_num', 'gain_den'}:
        raise ValueError('An audio-only overlay requires exactly one explicit route and edit.')
    path = raw['external_path']
    if (not isinstance(path, str) or not 1 <= len(path) <= 4096 or
            not os.path.isabs(path) or os.path.normpath(path) != path or
            not path.lower().endswith('.wav') or
            any(ord(ch) < 32 or ord(ch) == 127 for ch in path)):
        raise ValueError('Audio-only overlay requires an absolute canonical WAV path.')
    for key in ('record_start_frame', 'source_in_frame', 'duration_frames',
                'gain_num', 'gain_den'):
        if type(raw[key]) is not int:
            raise ValueError('Audio-only overlay timing/gain must be exact integers.')
    if (not 0 <= raw['record_start_frame'] < 120 or
            not 0 <= raw['source_in_frame'] < 120 or
            not 1 <= raw['duration_frames'] <= 120 or
            raw['record_start_frame'] + raw['duration_frames'] > 120 or
            raw['gain_den'] != 2 or raw['gain_num'] not in (0, 1, 2)):
        raise ValueError('Audio-only overlay is bounded to 120 frames and gain 0, 1/2 or 1.')
    return dict(raw)


def freeze_overlay_intent(composition, choice):
    """Freeze legacy singleton v1 or ordered, independent 1..3-event v2.

    Each event is editorial intent only; physical PCM source proof belongs to
    the separately verified export/preview path.  Returning a new container
    prevents edits in a modal widget from mutating saved Session intent.
    """
    key = edit_identity(composition)
    if key is None:
        raise ValueError('Audio-only overlay requires 2–8 SEQUENCE edit occurrences.')
    if type(choice) is list:
        if not 1 <= len(choice) <= 3:
            raise ValueError('Ordered audio-only overlays require 1..3 independent events.')
        total = composition.total_frames()
        if not 1 <= total <= 120:
            raise ValueError('Ordered audio-only overlays require a bounded record timeline.')
        choices = [_choice(item) for item in choice]
        if any(item is None or
               item['record_start_frame'] + item['duration_frames'] > total
               for item in choices):
            raise ValueError('Audio-only overlay event exceeds actual record bounds.')
        return {'version': 2, 'edit_key': key, 'choices': choices}
    return {'version': 1, 'edit_key': key, 'choice': _choice(choice)}


def restore_overlay_intent(composition, frozen):
    if frozen is None:
        return None
    key = edit_identity(composition)
    if (key is None or not isinstance(frozen, dict) or
            type(frozen.get('version')) is not int or
            frozen.get('edit_key') != key):
        raise ValueError('Saved audio-only overlay does not match exact record edits.')
    if frozen['version'] == 1:
        if set(frozen) != {'version', 'edit_key', 'choice'}:
            raise ValueError('Saved audio-only overlay does not match exact record edits.')
        return _choice(frozen['choice'])
    if frozen['version'] == 2:
        if (set(frozen) != {'version', 'edit_key', 'choices'} or
                type(frozen['choices']) is not list or
                not 1 <= len(frozen['choices']) <= 3):
            raise ValueError('Saved ordered audio-only overlays are malformed.')
        # Repeat all bounds/shape validation on restore; never trust edited
        # JSON or stale per-occurrence names and trims from another record.
        return freeze_overlay_intent(composition, frozen['choices'])['choices']
    raise ValueError('Saved audio-only overlay intent version is unsupported.')


__all__ = ['freeze_overlay_intent', 'restore_overlay_intent']
