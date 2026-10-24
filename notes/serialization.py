# -*- coding: utf-8 -*-
"""Versioned FrameReviewer notes JSON persistence.

The schema is versioned and forward-migrated purely: reading an older file
upgrades it in memory without touching disk, and unknown fields are preserved
so a newer writer's data survives a round trip through an older one.

Version 2 adds the identity and provenance a precision review record needs:

* stable ``id`` on the file, each note, and each shape,
* a real ``source_frame`` (which may be negative or non-zero) and an optional
  inclusive ``range`` hold, kept separate from the zero-based ``frame`` display
  index the viewer addresses,
* a coordinate ``space`` plus source ``width`` / ``height`` / ``pixel_aspect``
  so annotation geometry can be interpreted precisely,
* ``author`` / ``created`` / ``modified`` timestamps, a ``generator`` stamp, and
  ``provenance`` (local / imported / migrated / remote).

The historical ``build_payload`` / ``parse_payload`` pair keeps its exact
behavior (a ``{frame: strokes}`` mapping) so the existing viewer path is
unchanged; richer callers use :func:`parse_notes` / :func:`build_notes_payload`.
"""
from __future__ import absolute_import

import datetime
import json
import os
import uuid


FORMAT = 'frame-review-notes'
VERSION = 2
SUPPORTED_VERSIONS = (1, 2)
MAX_BYTES = 20 * 1024 * 1024

GENERATOR_APP = 'FrameReviewer'
DEFAULT_SPACE = 'display'
PROVENANCE_LOCAL = 'local'
PROVENANCE_MIGRATED = 'migrated'


def _generator_version():
    try:
        from .. import __version__
        return str(__version__)
    except Exception:
        return '0'


def _now():
    """UTC timestamp in ISO 8601 with an explicit offset."""
    return datetime.datetime.now(_UTC).replace(microsecond=0).isoformat()


try:
    _UTC = datetime.timezone.utc
except AttributeError:  # pragma: no cover - Python 2 fallback
    class _Utc(datetime.tzinfo):
        def utcoffset(self, _):
            return datetime.timedelta(0)

        def tzname(self, _):
            return 'UTC'

        def dst(self, _):
            return datetime.timedelta(0)
    _UTC = _Utc()


def new_id(prefix):
    """A stable, collision-resistant id (e.g. ``note-ab12...``)."""
    return '{}-{}'.format(prefix, uuid.uuid4().hex)


def _generator():
    return {'app': GENERATOR_APP, 'version': _generator_version()}


# -- migration --------------------------------------------------------------

def migrate_payload(payload):
    """Return ``payload`` upgraded to the current version.

    Pure: the input is never mutated, unknown fields are carried through
    unchanged, and existing ids/timestamps are preserved (so the migration is
    idempotent). Raises on a container that is not a notes file at all.
    """
    if not isinstance(payload, dict) or payload.get('format') != FORMAT:
        raise ValueError('This is not a FrameReviewer notes file.')
    version = int(payload.get('version', 0))
    if version not in SUPPORTED_VERSIONS:
        raise ValueError('Unsupported notes file version.')

    upgraded = dict(payload)  # shallow copy; nested objects copied as touched.
    migrated = version < VERSION
    upgraded['version'] = VERSION
    upgraded.setdefault('generator', _generator())
    upgraded.setdefault('created', _now())
    upgraded['modified'] = upgraded.get('modified') or _now()

    source = dict(upgraded.get('source') or {})
    source.setdefault('id', new_id('source'))
    upgraded['source'] = source

    note_provenance = PROVENANCE_MIGRATED if migrated else PROVENANCE_LOCAL
    notes = []
    for entry in upgraded.get('notes', []):
        if not isinstance(entry, dict):
            raise ValueError('Invalid annotation entry.')
        note = dict(entry)
        note.setdefault('id', new_id('note'))
        note.setdefault('space', DEFAULT_SPACE)
        note.setdefault('provenance', note_provenance)
        note.setdefault('created', upgraded['created'])
        note.setdefault('modified', upgraded['modified'])
        strokes = note.get('strokes', [])
        if not isinstance(strokes, list):
            raise ValueError('Invalid annotation strokes.')
        note['strokes'] = [_migrate_stroke(stroke) for stroke in strokes]
        notes.append(note)
    upgraded['notes'] = notes
    return upgraded


def _migrate_stroke(stroke):
    if not isinstance(stroke, dict):
        raise ValueError('Every annotation stroke must be an object.')
    item = dict(stroke)
    item.setdefault('id', new_id('shape'))
    return item


# -- compatibility bridge (unchanged external behavior) ---------------------

def build_payload(drawings, source=None):
    """Build a current-version payload from a ``{frame: strokes}`` mapping.

    Behaves like the historical builder for the viewer path, while stamping the
    new identity/provenance metadata. Each note and shape gets a stable id and a
    coordinate space; ``source_frame`` defaults to the display ``frame`` offset
    by ``source['start_frame']`` when that is known.
    """
    source = dict(source or {})
    source.setdefault('id', new_id('source'))
    start = int(source.get('start_frame', 0) or 0)
    now = _now()
    notes = []
    for frame, strokes in sorted(drawings.items()):
        frame = int(frame)
        notes.append({
            'id': new_id('note'),
            'frame': frame,
            'source_frame': start + frame,
            'space': source.get('space', DEFAULT_SPACE),
            'provenance': PROVENANCE_LOCAL,
            'created': now,
            'modified': now,
            'strokes': [_with_shape_id(stroke) for stroke in strokes],
        })
    return {
        'format': FORMAT,
        'version': VERSION,
        'generator': _generator(),
        'created': now,
        'modified': now,
        'source': source,
        'notes': notes,
    }


def _with_shape_id(stroke):
    if isinstance(stroke, dict) and 'id' not in stroke:
        stroke = dict(stroke)
        stroke['id'] = new_id('shape')
    return stroke


def parse_payload(payload):
    """Validate the container and return ``(source, {frame: strokes})``.

    Reads both v1 and v2 (migrating v1 in memory). The returned mapping keys on
    the zero-based display ``frame`` so the existing viewer path is unchanged;
    richer identity/time data is available through :func:`parse_notes`.
    """
    payload = migrate_payload(payload)
    source = payload.get('source', {})
    if not isinstance(source, dict):
        raise ValueError('The notes source must be an object.')
    entries = payload.get('notes', [])
    if not isinstance(entries, list):
        raise ValueError('The notes field must be a list.')
    drawings = {}
    for entry in entries:
        if not isinstance(entry, dict) or 'frame' not in entry:
            raise ValueError('Invalid annotation entry.')
        frame = int(entry['frame'])
        if frame < 0 or frame in drawings:
            raise ValueError('Invalid or duplicate frame.')
        strokes = entry.get('strokes', [])
        if not isinstance(strokes, list):
            raise ValueError('Invalid annotation strokes.')
        drawings[frame] = strokes
    return source, drawings


# -- richer record API ------------------------------------------------------

def parse_notes(payload):
    """Return ``(source, [note, ...])`` with full identity/time/provenance.

    Each note retains its ``id``, ``frame``, ``source_frame``, optional
    ``range``, ``space``, ``author``, timestamps, ``provenance``, its ``strokes``
    (each with an ``id``), and any unknown fields a future writer added.
    """
    payload = migrate_payload(payload)
    source = payload.get('source', {})
    if not isinstance(source, dict):
        raise ValueError('The notes source must be an object.')
    entries = payload.get('notes', [])
    if not isinstance(entries, list):
        raise ValueError('The notes field must be a list.')
    notes = []
    for entry in entries:
        if not isinstance(entry, dict) or 'frame' not in entry:
            raise ValueError('Invalid annotation entry.')
        note = dict(entry)
        note['frame'] = int(entry['frame'])
        if 'source_frame' in note and note['source_frame'] is not None:
            note['source_frame'] = int(note['source_frame'])
        rng = note.get('range')
        if rng is not None:
            if (not isinstance(rng, (list, tuple)) or len(rng) != 2):
                raise ValueError('A note range must be [first, last].')
            note['range'] = [int(rng[0]), int(rng[1])]
            if note['range'][0] > note['range'][1]:
                raise ValueError('A note range must have first <= last.')
        strokes = note.get('strokes', [])
        if not isinstance(strokes, list):
            raise ValueError('Invalid annotation strokes.')
        notes.append(note)
    return source, notes


def build_notes_payload(notes, source=None):
    """Build a current-version payload from full note records.

    Missing ids/timestamps/space/provenance are filled in; everything else,
    including unknown fields, is preserved so this is a lossless round trip with
    :func:`parse_notes`.
    """
    source = dict(source or {})
    source.setdefault('id', new_id('source'))
    now = _now()
    out = []
    for note in notes:
        if not isinstance(note, dict) or 'frame' not in note:
            raise ValueError('Invalid annotation entry.')
        record = dict(note)
        record['frame'] = int(note['frame'])
        record.setdefault('id', new_id('note'))
        record.setdefault('space', source.get('space', DEFAULT_SPACE))
        record.setdefault('provenance', PROVENANCE_LOCAL)
        record.setdefault('created', now)
        record.setdefault('modified', now)
        record['strokes'] = [_with_shape_id(s) for s in note.get('strokes', [])]
        out.append(record)
    return {
        'format': FORMAT,
        'version': VERSION,
        'generator': _generator(),
        'created': now,
        'modified': now,
        'source': source,
        'notes': out,
    }


# -- disk -------------------------------------------------------------------

def read_payload(path):
    if os.path.getsize(path) > MAX_BYTES:
        raise ValueError('The notes file is larger than 20 MB.')
    with open(path, 'r') as stream:
        return json.load(stream)


def write_payload(path, payload):
    with open(path, 'w') as stream:
        json.dump(payload, stream, indent=2, sort_keys=True)
        stream.write('\n')
