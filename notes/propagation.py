# -*- coding: utf-8 -*-
"""Copy, paste, and propagate notes across frames, ranges, and sources.

Pure Python, operating on the v2 note records produced by
:func:`framereviewer.notes.serialization.parse_notes`. Every duplicate gets a
fresh note id and fresh shape ids (so the copies have independent identity),
records where it came from (``derived_from``), and is marked ``copied`` in its
``provenance`` -- while author, category, body, and any unknown fields are
preserved. The display/source-frame split from the time model is respected: a
copy keeps the original's ``source_frame - frame`` offset, so a note pasted onto
a new display frame reports the correct real frame.
"""
from __future__ import absolute_import

import copy

from .serialization import new_id, _now, PROVENANCE_LOCAL


PROVENANCE_COPIED = 'copied'


def _source_offset(note):
    """``source_frame - frame`` for a note, or ``None`` when unknown."""
    if note.get('source_frame') is None or 'frame' not in note:
        return None
    return int(note['source_frame']) - int(note['frame'])


def duplicate_note(note, frame=None, source_frame=None, keep_shape_ids=False):
    """Return an independent copy of ``note`` with new identity and provenance.

    ``frame`` sets the copy's display frame (default: unchanged). ``source_frame``
    sets the real frame explicitly; when omitted it is derived from ``frame``
    plus the original's source/display offset so the copy stays frame-accurate.
    """
    clone = copy.deepcopy(note)
    origin = note.get('id')
    clone['id'] = new_id('note')
    if origin is not None:
        clone['derived_from'] = origin
    clone['provenance'] = PROVENANCE_COPIED
    now = _now()
    clone['created'] = now
    clone['modified'] = now

    if frame is not None:
        clone['frame'] = int(frame)
    if source_frame is not None:
        clone['source_frame'] = int(source_frame)
    elif frame is not None:
        offset = _source_offset(note)
        if offset is not None:
            clone['source_frame'] = int(frame) + offset

    if not keep_shape_ids:
        for shape in clone.get('strokes', []):
            if isinstance(shape, dict):
                shape['id'] = new_id('shape')
    return clone


def propagate_to_frames(note, frames):
    """Duplicate ``note`` onto each display frame in ``frames``."""
    return [duplicate_note(note, frame=int(f)) for f in frames]


def propagate_across_range(note, first, last, step=1):
    """Duplicate ``note`` across an inclusive display-frame range."""
    step = max(1, int(step))
    if int(last) < int(first):
        first, last = int(last), int(first)
    return propagate_to_frames(note, range(int(first), int(last) + 1, step))


def copy_notes_to_frame(notes, target_frame):
    """Paste a list of notes onto one display frame, keeping their order."""
    return [duplicate_note(n, frame=int(target_frame)) for n in notes]


def remap_note_to_source(note, from_mapping, to_mapping):
    """Re-express ``note`` on an aligned source via two frame mappings.

    The note's real frame is located as a display index in ``from_mapping`` and
    the same index is read back through ``to_mapping``, so a note on source A
    lands on the time-aligned frame of source B. A fresh copy is returned.
    """
    src_frame = note.get('source_frame')
    if src_frame is None:
        index = int(note.get('frame', 0))
    else:
        index = from_mapping.source_to_display(int(src_frame))
    target_source = to_mapping.display_to_source(index)
    return duplicate_note(note, frame=index, source_frame=target_source)
