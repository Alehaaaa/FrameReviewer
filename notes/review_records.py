# -*- coding: utf-8 -*-
"""Typed, Qt-free review records layered over the existing notes schema v2.

``ReviewDocument`` owns note identity and synchronization state.  The historical
``notes.serialization`` and ``notes.propagation`` APIs remain usable; projection
to ``{display_frame: strokes}`` is deliberately one-way presentation data.

All frame matching is in *source* frame coordinates, including negatives.
``frame`` remains the legacy display index for compatibility. A ``range`` is an
inclusive source-frame interval. Source IDs must originate from the session's
logical Source and survive relinking of media representations.

Local edits create dirty IDs; local deletions are kept as tombstones. Incoming
remote changes can never overwrite a dirty note or resurrect a tombstone; the
caller explicitly resolves a typed conflict instead. Unknown JSON properties
at the document, source, note, shape, reply, attachment and layer levels survive.
"""
from __future__ import absolute_import

import copy
from dataclasses import dataclass, field
import datetime
import hashlib
import json
import mimetypes
import os
import re
import stat
import tempfile
import uuid
from typing import Any, Dict, Optional, Tuple

from . import serialization
from . import model as note_model
from .merge import notes_differ
from .propagation import duplicate_note as _duplicate_note


DEFAULT_LAYER_ID = 'layer-default'
LOCAL_STATE_KEY = 'review_record_state'
SURFACE_RECORDS_KEY = 'review_records'
LOCAL_REPLY_UNSENT_KEY = 'fr_local_unsent'
LOCAL_ATTACHMENT_UNSENT_KEY = 'fr_local_unsent'
LOCAL_ATTACHMENT_MANAGED_KEY = 'fr_managed_asset'
LOCAL_ATTACHMENT_DIRECTORY = '.framereviewer-assets'
# Allow useful 4K OpenEXR stills while bounding local I/O and disk usage.
MAX_LOCAL_ATTACHMENT_BYTES = 128 * 1024 * 1024
PRIORITIES = ('low', 'normal', 'high', 'critical')
STATUSES = ('open', 'in_progress', 'resolved', 'closed')


def _secure_absolute_parts(path):
    """Accept explicit local paths only; reject schemes, UNC and traversal."""
    try:
        path = os.fspath(path)
    except TypeError:
        raise ValueError('A local filesystem path is required.')
    if not isinstance(path, str) or not path or '\x00' in path:
        raise ValueError('A valid local filesystem path is required.')
    if (not os.path.isabs(path) or path.startswith('//') or
            path.startswith('\\\\') or '://' in path or
            re.match(r'^[a-zA-Z][a-zA-Z0-9+.-]*:', path)):
        raise ValueError('Only absolute local paths are accepted (no URLs or shares).')
    parts = path.split('/')
    if '..' in parts or '.' in parts:
        raise ValueError('Path traversal and dot segments are not accepted.')
    return tuple(p for p in parts if p)


def _open_secure_directory(path, create=False):
    """Return fd opened from / with no symlinks in any directory component."""
    parts = _secure_absolute_parts(path)
    flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open('/', flags)
    try:
        for part in parts:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            next_fd = os.open(part, flags, dir_fd=fd)
            if not stat.S_ISDIR(os.fstat(next_fd).st_mode):
                os.close(next_fd)
                raise ValueError('Attachment directory is not a directory.')
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _still_mime(header):
    """Recognize common inert still-image containers, not text/SVG or URLs."""
    if header.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if header.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if header.startswith((b'GIF87a', b'GIF89a')):
        return 'image/gif'
    if header.startswith(b'RIFF') and header[8:12] == b'WEBP':
        return 'image/webp'
    if header.startswith((b'II*\x00', b'MM\x00*')):
        return 'image/tiff'
    if header.startswith(b'v/1\x01'):
        return 'image/x-exr'
    if header.startswith(b'BM'):
        return 'image/bmp'
    return None


def _extras(data, known):
    return copy.deepcopy({k: v for k, v in data.items() if k not in known})


def _identity(value, prefix):
    value = str(value) if value else serialization.new_id(prefix)
    if not value.strip():
        raise ValueError('A {} ID must not be blank.'.format(prefix))
    return value


def _timestamp(value=None):
    return str(value) if value else serialization._now()


def _migrated_id(prefix, *parts):
    """Deterministic UUID for repeated imports of the same legacy key."""
    key = 'frame-review-session-drawings:' + ':'.join(str(p) for p in parts)
    return '{}-{}'.format(prefix, uuid.uuid5(uuid.NAMESPACE_URL, key).hex)


def _as_dict(value, field_name):
    if not isinstance(value, dict):
        raise ValueError('{} must be an object.'.format(field_name))
    return value


def _range(value):
    if value is None:
        return None
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        raise ValueError('A note range must be [first, last].')
    first, last = int(value[0]), int(value[1])
    if first > last:
        raise ValueError('A note range must have first <= last.')
    return (first, last)


@dataclass(frozen=True)
class ReviewReply:
    id: str
    body: str = ''
    author: str = ''
    created: str = ''
    modified: str = ''
    extra: Dict[str, Any] = field(default_factory=dict, compare=True, repr=False)

    @classmethod
    def from_dict(cls, data):
        data = _as_dict(data, 'Reply')
        known = {'id', 'body', 'author', 'created', 'modified'}
        return cls(_identity(data.get('id'), 'reply'), str(data.get('body') or ''),
                   str(data.get('author') or ''), _timestamp(data.get('created')),
                   _timestamp(data.get('modified') or data.get('created')),
                   _extras(data, known))

    def to_dict(self):
        return dict(copy.deepcopy(self.extra), id=self.id, body=self.body,
                    author=self.author, created=self.created, modified=self.modified)


@dataclass(frozen=True)
class ReviewAttachment:
    id: str
    kind: str = 'file'
    uri: str = ''
    name: str = ''
    mime_type: str = ''
    extra: Dict[str, Any] = field(default_factory=dict, compare=True, repr=False)

    @classmethod
    def from_dict(cls, data):
        data = _as_dict(data, 'Attachment')
        known = {'id', 'kind', 'uri', 'name', 'mime_type'}
        return cls(_identity(data.get('id'), 'attachment'),
                   str(data.get('kind') or 'file'), str(data.get('uri') or ''),
                   str(data.get('name') or ''), str(data.get('mime_type') or ''),
                   _extras(data, known))

    def to_dict(self):
        return dict(copy.deepcopy(self.extra), id=self.id, kind=self.kind,
                    uri=self.uri, name=self.name, mime_type=self.mime_type)


@dataclass(frozen=True)
class ReviewNote:
    id: str
    source_id: str
    frame: int = 0
    source_frame: int = 0
    body: str = ''
    category: str = ''
    priority: str = 'normal'
    status: str = 'open'
    assignee: str = ''
    author: str = ''
    created: str = ''
    modified: str = ''
    replies: Tuple[ReviewReply, ...] = ()
    attachments: Tuple[ReviewAttachment, ...] = ()
    remote_ref: Any = None
    strokes: Tuple[Dict[str, Any], ...] = ()
    layer_id: str = DEFAULT_LAYER_ID
    source_range: Optional[Tuple[int, int]] = None
    space: str = 'display'
    provenance: str = 'local'
    derived_from: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict, compare=True, repr=False)

    _KNOWN = frozenset(('id', 'source_id', 'frame', 'source_frame', 'body',
                        'category', 'priority', 'status', 'assignee', 'author',
                        'created', 'modified', 'replies', 'attachments',
                        'remote_ref', 'strokes', 'layer_id', 'range', 'space',
                        'provenance', 'derived_from'))

    def __post_init__(self):
        if not self.id or not self.source_id:
            raise ValueError('Note ID and logical source ID are required.')
        if not self.layer_id:
            raise ValueError('A note must belong to a layer.')
        _range(self.source_range)

    @classmethod
    def from_dict(cls, data, source_id=None, start_frame=0):
        data = _as_dict(data, 'Review note')
        if 'frame' not in data:
            raise ValueError('A review note requires a display frame.')
        source_id = data.get('source_id') or source_id
        if not source_id:
            raise ValueError('A review note requires a logical source ID.')
        frame = int(data['frame'])
        source_frame = data.get('source_frame')
        if source_frame is None:
            source_frame = frame + int(start_frame)
        strokes = []
        for stroke in data.get('strokes', []):
            item = copy.deepcopy(_as_dict(stroke, 'Shape'))
            item['id'] = _identity(item.get('id'), 'shape')
            strokes.append(item)
        return cls(
            id=_identity(data.get('id'), 'note'), source_id=str(source_id),
            frame=frame, source_frame=int(source_frame),
            body=str(data.get('body') or ''), category=str(data.get('category') or ''),
            priority=str(data.get('priority') or 'normal'),
            status=str(data.get('status') or 'open'),
            assignee=str(data.get('assignee') or ''),
            author=str(data.get('author') or ''),
            created=_timestamp(data.get('created')),
            modified=_timestamp(data.get('modified') or data.get('created')),
            replies=tuple(ReviewReply.from_dict(r) for r in data.get('replies', [])),
            attachments=tuple(ReviewAttachment.from_dict(a)
                              for a in data.get('attachments', [])),
            remote_ref=copy.deepcopy(data.get('remote_ref')),
            strokes=tuple(strokes), layer_id=str(data.get('layer_id') or DEFAULT_LAYER_ID),
            source_range=_range(data.get('range')),
            space=str(data.get('space') or 'display'),
            provenance=str(data.get('provenance') or 'local'),
            derived_from=data.get('derived_from'),
            extra=_extras(data, cls._KNOWN))

    def to_dict(self):
        data = copy.deepcopy(self.extra)
        data.update({
            'id': self.id, 'source_id': self.source_id,
            'frame': self.frame, 'source_frame': self.source_frame,
            'body': self.body, 'category': self.category,
            'priority': self.priority, 'status': self.status,
            'assignee': self.assignee, 'author': self.author,
            'created': self.created, 'modified': self.modified,
            'replies': [r.to_dict() for r in self.replies],
            'attachments': [a.to_dict() for a in self.attachments],
            'strokes': copy.deepcopy(list(self.strokes)),
            'layer_id': self.layer_id, 'space': self.space,
            'provenance': self.provenance})
        if self.remote_ref is not None:
            data['remote_ref'] = copy.deepcopy(self.remote_ref)
        if self.source_range is not None:
            data['range'] = list(self.source_range)
        if self.derived_from is not None:
            data['derived_from'] = self.derived_from
        return data

    def includes_source_frame(self, value):
        if self.source_range is None:
            return self.source_frame == int(value)
        return self.source_range[0] <= int(value) <= self.source_range[1]

    def revised(self, **changes):
        """Return a modified record without changing its note/source identity."""
        if 'id' in changes or 'source_id' in changes or 'created' in changes:
            raise ValueError('Identity and creation timestamps cannot be edited.')
        data = self.to_dict()
        if 'source_range' in changes:
            changes['range'] = changes.pop('source_range')
        data.update(copy.deepcopy(changes))
        data['modified'] = _timestamp()
        return type(self).from_dict(data, source_id=self.source_id)


@dataclass(frozen=True)
class ReviewLayer:
    id: str
    name: str = 'Default'
    visible: bool = True
    locked: bool = False
    solo: bool = False
    filters: Dict[str, Any] = field(default_factory=dict, compare=True, repr=False)
    extra: Dict[str, Any] = field(default_factory=dict, compare=True, repr=False)

    _KNOWN = frozenset(('id', 'name', 'visible', 'locked', 'solo', 'filters'))

    @classmethod
    def from_dict(cls, data):
        data = _as_dict(data, 'Layer')
        filters = copy.deepcopy(data.get('filters') or {})
        _as_dict(filters, 'Layer filters')
        return cls(_identity(data.get('id'), 'layer'),
                   str(data.get('name') or 'Layer'),
                   bool(data.get('visible', True)),
                   bool(data.get('locked', False)),
                   bool(data.get('solo', False)), filters,
                   _extras(data, cls._KNOWN))

    def to_dict(self):
        return dict(copy.deepcopy(self.extra), id=self.id, name=self.name,
                    visible=self.visible, locked=self.locked, solo=self.solo,
                    filters=copy.deepcopy(self.filters))

    def revised(self, **changes):
        if 'id' in changes:
            raise ValueError('Layer IDs are stable.')
        data = self.to_dict()
        data.update(copy.deepcopy(changes))
        return type(self).from_dict(data)

    def allows(self, note):
        """Layer filters are conjunctions; each supplied value permits a set."""
        for name, attr in (('authors', 'author'), ('categories', 'category'),
                           ('statuses', 'status'), ('priorities', 'priority')):
            values = self.filters.get(name, self.filters.get(attr))
            if values is None or values == []:
                continue
            allowed = [values] if isinstance(values, str) else list(values)
            if getattr(note, attr) not in allowed:
                return False
        return True


@dataclass(frozen=True)
class ReviewConflict:
    id: str
    local: Optional[ReviewNote]
    remote: Optional[ReviewNote]
    reason: str  # changed, remote_deleted, locally_deleted


class ReviewDocument(object):
    """Single-source note collection, editable locally and merge-safe remotely.

    Store a document by ``Source.notes_ref`` and connect ``source['id']`` to the
    stable ``app.session.Source.id``. The mutable store owns its copies; queries
    and accessors return independent records. Serializing never clears dirty IDs.
    """

    def __init__(self, source, notes=None, layers=None, metadata=None):
        self._source = copy.deepcopy(_as_dict(source, 'Source'))
        self._source['id'] = _identity(self._source.get('id'), 'source')
        self._notes = {}
        self._layers = {}
        self._dirty = set()
        self._deleted = set()
        self._created = set()
        self._conflicts = {}
        self._revision = 0
        self._surface_extension_extra = {}
        self._metadata = copy.deepcopy(metadata or {})
        self._metadata.setdefault('generator', {'app': serialization.GENERATOR_APP})
        self._metadata.setdefault('created', serialization._now())
        self._metadata.setdefault('modified', self._metadata['created'])
        for layer in layers or (ReviewLayer(DEFAULT_LAYER_ID),):
            self._add_layer_initial(layer)
        if DEFAULT_LAYER_ID not in self._layers:
            self._add_layer_initial(ReviewLayer(DEFAULT_LAYER_ID))
        for note in notes or ():
            self._add_initial(note)

    @property
    def source_id(self):
        return self._source['id']

    @property
    def revision(self):
        """In-memory change counter for rejecting stale surface snapshots."""
        return self._revision

    @property
    def source(self):
        """Read-only by value; callers cannot rebind the document source ID."""
        return copy.deepcopy(self._source)

    @property
    def notes(self):
        return tuple(copy.deepcopy(note) for note in self._notes.values())

    @property
    def layers(self):
        return tuple(copy.deepcopy(layer) for layer in self._layers.values())

    @property
    def dirty_ids(self):
        return frozenset(self._dirty)

    @property
    def deleted_ids(self):
        return frozenset(self._deleted)

    @property
    def conflicts(self):
        return tuple(copy.deepcopy(c) for c in self._conflicts.values())

    @classmethod
    def from_payload(cls, payload):
        """Pure v1/v2 migration; preserve every unknown JSON field."""
        upgraded = serialization.migrate_payload(copy.deepcopy(payload))
        # Optional bridge export has ONE flattened drawing entry per frame for
        # legacy parse_payload(), plus the authoritative rich records here.
        # Never interpret its flattened copies as new independent review notes.
        embedded = upgraded.pop(SURFACE_RECORDS_KEY, None)
        embedded_extra = {}
        if embedded is not None:
            _as_dict(embedded, SURFACE_RECORDS_KEY)
            if not isinstance(embedded.get('notes'), list):
                raise ValueError('Embedded rich review notes must be an array.')
            embedded_extra = _extras(embedded, ('notes', 'layers'))
            upgraded['notes'] = copy.deepcopy(embedded['notes'])
            if 'layers' in embedded:
                upgraded['layers'] = copy.deepcopy(embedded['layers'])
        source = upgraded['source']
        if not isinstance(source, dict):
            raise ValueError('Invalid notes source.')
        source_id = source['id']
        start = int(source.get('start_frame', 0) or 0)
        raw_notes = upgraded.get('notes', [])
        if not isinstance(raw_notes, list):
            raise ValueError('Notes must be an array.')
        raw_layers = upgraded.get('layers')
        if raw_layers is not None and not isinstance(raw_layers, list):
            raise ValueError('Layers must be an array.')
        layers = ([ReviewLayer.from_dict(layer) for layer in raw_layers]
                  if raw_layers is not None else [ReviewLayer(DEFAULT_LAYER_ID)])
        notes = [ReviewNote.from_dict(n, source_id=source_id, start_frame=start)
                 for n in raw_notes]
        metadata = {k: v for k, v in upgraded.items()
                    if k not in ('source', 'notes', 'layers')}
        doc = cls(source, notes, layers, metadata)
        doc._surface_extension_extra = embedded_extra
        sync = upgraded.get(LOCAL_STATE_KEY, {})
        if sync:
            _as_dict(sync, LOCAL_STATE_KEY)
            doc._dirty = set(sync.get('dirty_ids', ()))
            doc._deleted = set(sync.get('deleted_ids', ()))
            doc._created = set(sync.get('created_ids', ()))
            if doc._dirty - (set(doc._notes) | doc._deleted):
                raise ValueError('Dirty note ID has no record or deletion tombstone.')
            if doc._deleted & set(doc._notes):
                raise ValueError('Deleted notes cannot remain in the active collection.')
            for conflict in sync.get('conflicts', ()):
                conflict = _as_dict(conflict, 'Review conflict')
                note_id = str(conflict['id'])
                local = conflict.get('local')
                remote = conflict.get('remote')
                restored = ReviewConflict(
                    note_id,
                    (ReviewNote.from_dict(local, source_id=doc.source_id)
                     if local is not None else None),
                    (ReviewNote.from_dict(remote, source_id=doc.source_id)
                     if remote is not None else None),
                    str(conflict['reason']))
                for item in (restored.local, restored.remote):
                    if item is not None and item.source_id != doc.source_id:
                        raise ValueError('Review conflict has a different source ID.')
                if restored.local is not None and restored.local.id != note_id:
                    raise ValueError('Review conflict local ID differs from conflict key.')
                if restored.remote is not None and restored.remote.id != note_id:
                    raise ValueError('Review conflict remote ID differs from conflict key.')
                doc._conflicts[note_id] = restored
        return doc

    @classmethod
    def empty(cls, source):
        """Construct a fresh document from a Source object or source mapping."""
        if isinstance(source, dict):
            data = copy.deepcopy(source)
        else:
            data = {'id': str(source.id),
                    'start_frame': int(getattr(source, 'start_frame', 0))}
            active = getattr(source, 'active_path', None)
            if callable(active) and active():
                data['path'] = active()
        return cls.from_payload(serialization.build_notes_payload([], source=data))

    @classmethod
    def from_session_source(cls, source, frame_mapping=None):
        """One-time migration of ``Source.review['drawings']`` into v2 records.

        Legacy session drawing keys are *source* frames stored as JSON strings,
        not PlaybackSurface display indices. Preserve that real frame, including
        negatives and sparse holes, even when ``source_to_display`` is nearest-
        neighbor or two source frames collapse onto the same display key.

        This method does not mutate Source.review, its shapes, or Session. IDs
        for notes and missing/duplicate shapes are deterministic UUID5 values
        based on logical Source.id, source frame and stroke position; existing
        unique shape IDs remain unchanged. It produces a clean (not remotely
        dirty) migration. Store ``doc.to_payload()`` persistently thereafter;
        edit rich records instead of running migration after every UI change.
        """
        if isinstance(source, dict):
            source_id = source.get('id')
            review = source.get('review') or {}
            timing = source.get('timing') or {}
            start = int(timing.get('start_frame', source.get('start_frame', 0)) or 0)
            path = source.get('path')
        else:
            source_id = getattr(source, 'id', None)
            review = getattr(source, 'review', None) or {}
            start = int(getattr(source, 'start_frame', 0) or 0)
            path = source.active_path() if callable(getattr(source, 'active_path', None)) else None
            if frame_mapping is None and callable(getattr(source, 'frame_mapping', None)):
                frame_mapping = source.frame_mapping()
        if not source_id:
            raise ValueError('Legacy drawings need a stable logical Source.id.')
        if not isinstance(review, dict):
            raise ValueError('Legacy Source.review must be an object.')
        drawings = review.get('drawings') or {}
        if not isinstance(drawings, dict):
            raise ValueError('Legacy Source.review.drawings must be a mapping.')
        canonical_source = {'id': str(source_id), 'start_frame': start}
        if path:
            canonical_source['path'] = str(path)
        prepared = []
        seen_shapes = set()
        for source_frame, old_strokes in sorted(drawings.items(), key=lambda item: int(item[0])):
            source_frame = int(source_frame)
            if not isinstance(old_strokes, list):
                raise ValueError('Legacy drawing at {} must be a list.'.format(source_frame))
            if frame_mapping is None:
                display = source_frame - start
            else:
                display = int(frame_mapping.source_to_display(source_frame))
            shapes = []
            for index, original in enumerate(old_strokes):
                shape = copy.deepcopy(_as_dict(original, 'Legacy shape'))
                sid = shape.get('id')
                if not sid:
                    sid = _migrated_id('shape', source_id, source_frame, index)
                else:
                    sid = str(sid)
                    if sid in seen_shapes:
                        shape['derived_from'] = sid
                        sid = _migrated_id('shape', source_id, source_frame, index)
                if sid in seen_shapes:
                    raise ValueError('Duplicate migrated shape ID {}'.format(sid))
                shape['id'] = sid
                seen_shapes.add(sid)
                shapes.append(shape)
            prepared.append({'id': _migrated_id('note', source_id, source_frame),
                             'frame': display, 'source_frame': source_frame,
                             'source_id': str(source_id), 'strokes': shapes,
                             'provenance': 'migrated'})
        return cls(canonical_source, notes=prepared)

    def to_payload(self):
        payload = copy.deepcopy(self._metadata)
        payload.pop(SURFACE_RECORDS_KEY, None)
        payload.update({'format': serialization.FORMAT,
                        'version': serialization.VERSION,
                        'source': copy.deepcopy(self._source),
                        'layers': [layer.to_dict() for layer in self._layers.values()],
                        'notes': [note.to_dict() for note in self._notes.values()]})
        payload.setdefault('generator', {'app': serialization.GENERATOR_APP})
        payload.setdefault('created', serialization._now())
        payload.setdefault('modified', payload['created'])
        if self._surface_extension_extra:
            embedded = copy.deepcopy(self._surface_extension_extra)
            embedded.update({'notes': copy.deepcopy(payload['notes']),
                             'layers': copy.deepcopy(payload['layers'])})
            payload[SURFACE_RECORDS_KEY] = embedded
        if (self._dirty or self._deleted or self._created or self._conflicts or
                LOCAL_STATE_KEY in payload):
            state = copy.deepcopy(payload.get(LOCAL_STATE_KEY) or {})
            state.update({
                'dirty_ids': sorted(self._dirty),
                'deleted_ids': sorted(self._deleted),
                'created_ids': sorted(self._created),
                'conflicts': [
                    {'id': c.id, 'local': c.local.to_dict() if c.local else None,
                     'remote': c.remote.to_dict() if c.remote else None,
                     'reason': c.reason}
                    for c in self._conflicts.values()],
            })
            payload[LOCAL_STATE_KEY] = state
        return payload

    def _add_layer_initial(self, layer):
        if isinstance(layer, dict):
            layer = ReviewLayer.from_dict(layer)
        if not isinstance(layer, ReviewLayer) or layer.id in self._layers:
            raise ValueError('Duplicate or invalid layer ID.')
        self._layers[layer.id] = copy.deepcopy(layer)

    def _add_initial(self, note):
        if isinstance(note, dict):
            note = ReviewNote.from_dict(
                note, source_id=self.source_id,
                start_frame=int(self.source.get('start_frame', 0) or 0))
        if not isinstance(note, ReviewNote):
            raise TypeError('Expected ReviewNote or schema-v2 note dict.')
        if note.source_id != self.source_id:
            raise ValueError('Note belongs to a different logical source.')
        if note.layer_id not in self._layers:
            raise ValueError('Note references unknown layer {}'.format(note.layer_id))
        if note.id in self._notes:
            raise ValueError('Duplicate note ID {}'.format(note.id))
        self._notes[note.id] = copy.deepcopy(note)

    def get(self, note_id):
        note = self._notes.get(note_id)
        return copy.deepcopy(note) if note is not None else None

    def layer(self, layer_id):
        layer = self._layers.get(layer_id)
        return copy.deepcopy(layer) if layer is not None else None

    def _editable(self, layer_id):
        layer = self._layers.get(layer_id)
        if layer is None:
            raise ValueError('Unknown layer {}'.format(layer_id))
        if layer.locked:
            raise PermissionError('Layer {} is locked.'.format(layer_id))

    def _touched(self, note_id):
        self._dirty.add(note_id)
        self._metadata['modified'] = serialization._now()
        self._revision += 1

    def add(self, note):
        """Add a local note, requiring a free UUID and an editable layer."""
        if isinstance(note, dict):
            note = ReviewNote.from_dict(
                note, source_id=self.source_id,
                start_frame=int(self.source.get('start_frame', 0) or 0))
        self._editable(note.layer_id)
        if note.id in self._deleted:
            raise ValueError('Cannot reuse a locally deleted note ID.')
        self._add_initial(note)
        self._touched(note.id)
        self._created.add(note.id)
        return self.get(note.id)

    def update(self, note_id, **changes):
        note = self._notes[note_id]
        self._editable(note.layer_id)
        updated = note.revised(**changes)
        self._editable(updated.layer_id)
        self._notes[note_id] = updated
        self._touched(note_id)
        return self.get(note_id)

    def delete(self, note_id):
        note = self._notes[note_id]
        self._editable(note.layer_id)
        del self._notes[note_id]
        if note_id in self._created:
            # A never-published note does not need a remote delete request.
            self._created.discard(note_id)
            self._dirty.discard(note_id)
            self._metadata['modified'] = serialization._now()
            self._revision += 1
            return
        self._deleted.add(note_id)
        self._touched(note_id)

    def add_reply(self, note_id, body, author=''):
        """Append a typed reply and mark the parent review note dirty."""
        parent = self._notes[note_id]
        if not isinstance(body, str) or not body.strip():
            raise ValueError('Reply text must not be empty.')
        # The explicit local-only marker is persisted in the typed reply's
        # extensible fields. Parent note dirty status alone is NOT evidence
        # that an imported or previously published reply is safe to edit.
        reply = ReviewReply.from_dict({
            'body': body, 'author': author, LOCAL_REPLY_UNSENT_KEY: True})
        self.update(note_id, replies=[r.to_dict() for r in parent.replies] +
                    [reply.to_dict()])
        return reply

    @staticmethod
    def _reply_has_remote_state(reply):
        """Never infer local ownership from an imported/remote reply's body."""
        extra = reply.extra
        # Different providers embed different remote/publish identifiers.
        return any(extra.get(key) not in (None, False, '', 0) for key in (
            'remote_ref', 'remote_id', 'sg_id', 'shotgrid_id', 'shotgrid_ref',
            'published', 'synced', 'remote_status', 'sync_status',
            'publish_status', 'published_at'))

    def reply_editability(self, note_id, reply_id):
        """Return (allowed, reason) for a specific, stable reply identity."""
        note = self._notes.get(note_id)
        if note is None:
            return False, 'Review note no longer exists.'
        layer = self._layers.get(note.layer_id)
        if layer is None or layer.locked:
            return False, 'Review layer is locked or unavailable.'
        if note_id in self._conflicts:
            return False, 'Resolve the remote review conflict before changing replies.'
        matches = [r for r in note.replies if r.id == reply_id]
        if len(matches) != 1:
            return False, 'Reply ID is missing or ambiguous.'
        reply = matches[0]
        if self._reply_has_remote_state(reply):
            return False, 'Published or remotely linked replies are read-only.'
        if (note_id not in self._dirty or
                reply.extra.get(LOCAL_REPLY_UNSENT_KEY) is not True):
            return False, 'Synced/imported replies are read-only; only unsent local replies can change.'
        return True, ''

    def _reply_change_target(self, note_id, reply_id, expected_body,
                             expected_modified):
        if not isinstance(expected_body, str) or not isinstance(expected_modified, str):
            raise ValueError('Reply edits require a frozen body and modification timestamp.')
        allowed, reason = self.reply_editability(note_id, reply_id)
        if not allowed:
            raise PermissionError(reason)
        note = self._notes[note_id]
        index = next(i for i, r in enumerate(note.replies) if r.id == reply_id)
        original = note.replies[index]
        if (original.body != expected_body or
                original.modified != expected_modified):
            raise ValueError('Reply changed after selection; refresh before editing.')
        return note, index, original

    def update_reply(self, note_id, reply_id, body, *,
                     expected_body=None, expected_modified=None):
        """Edit ONLY an unsent local reply; one normal note revision for undo.

        A no-op has no document side effects. Expected values make a stale
        selection or concurrent document/remote refresh fail closed.
        """
        if not isinstance(body, str) or not body.strip():
            raise ValueError('Edited reply text must not be empty.')
        note, index, old = self._reply_change_target(
            note_id, reply_id, expected_body, expected_modified)
        if old.body == body:
            return copy.deepcopy(old)
        data = old.to_dict()
        data['body'] = body
        # _timestamp() uses whole seconds; an edit inside that second still
        # needs an independent revision for optimistic reply comparison.
        data['modified'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        changed = list(note.replies)
        changed[index] = ReviewReply.from_dict(data)
        self.update(note_id, replies=[r.to_dict() for r in changed])
        return copy.deepcopy(changed[index])

    def delete_reply(self, note_id, reply_id, *,
                     expected_body=None, expected_modified=None):
        """Remove an unsent reply without deleting the note or other replies."""
        note, index, original = self._reply_change_target(
            note_id, reply_id, expected_body, expected_modified)
        remainder = list(note.replies)
        del remainder[index]
        self.update(note_id, replies=[r.to_dict() for r in remainder])
        return copy.deepcopy(original)

    def add_attachment(self, note_id, attachment):
        """Append a vector/still/file attachment to a review note."""
        parent = self._notes[note_id]
        value = (attachment if isinstance(attachment, ReviewAttachment)
                 else ReviewAttachment.from_dict(attachment))
        self.update(note_id, attachments=[a.to_dict() for a in parent.attachments]
                    + [value.to_dict()])
        return value

    def _check_attachment_revision(self, note_id, expected_source_id,
                                   expected_revision):
        """Reject mutations made from a different source or stale panel bind."""
        if not expected_source_id or expected_revision is None:
            raise ValueError('Attachment changes require source and document revision.')
        if expected_source_id != self.source_id:
            raise ValueError('Review source changed; reload attachments.')
        if expected_revision != self.revision:
            raise ValueError('Review document changed; reload attachments.')
        note = self._notes.get(note_id)
        if note is None:
            raise PermissionError('Review note no longer exists.')
        self._editable(note.layer_id)
        if note_id in self._conflicts:
            raise PermissionError('Resolve the remote review conflict before editing attachments.')
        return note

    @staticmethod
    def _attachment_has_remote_state(attachment):
        return ReviewDocument._reply_has_remote_state(attachment)

    def attachment_addability(self, note_id):
        """Expose lock/conflict state for the panel before file browsing."""
        note = self._notes.get(note_id)
        if note is None:
            return False, 'Review note no longer exists.'
        layer = self._layers.get(note.layer_id)
        if layer is None or layer.locked:
            return False, 'Review layer is locked or unavailable.'
        if note_id in self._conflicts:
            return False, 'Resolve the remote review conflict before adding attachments.'
        return True, ''

    def attachment_editability(self, note_id, attachment_id):
        """Return whether an existing *unsent, managed, local* asset can be removed."""
        note = self._notes.get(note_id)
        if note is None:
            return False, 'Review note no longer exists.'
        layer = self._layers.get(note.layer_id)
        if layer is None or layer.locked:
            return False, 'Review layer is locked or unavailable.'
        if note_id in self._conflicts:
            return False, 'Resolve the remote review conflict before changing attachments.'
        matches = [a for a in note.attachments if a.id == attachment_id]
        if len(matches) != 1:
            return False, 'Attachment ID is missing or ambiguous.'
        attachment = matches[0]
        if self._attachment_has_remote_state(attachment):
            return False, 'Published or remotely linked attachments are read-only.'
        if (note_id not in self._dirty or
                attachment.extra.get(LOCAL_ATTACHMENT_UNSENT_KEY) is not True or
                attachment.extra.get(LOCAL_ATTACHMENT_MANAGED_KEY) is not True):
            return False, 'Only unsent locally copied attachments can be removed.'
        return True, ''

    def add_local_attachment(self, note_id, source_path, *, session_dir,
                             expected_source_id, expected_revision, kind='file'):
        """Copy a bounded regular local file into portable session-side storage.

        The URI is relative to the saved .frsession file's parent folder. The
        controller must provide that folder; unsaved sessions cannot ingest.
        Copying never writes to or removes the artist's original file. Atomic
        creation and directory-fd traversal prevent replacing existing assets
        or following symlinked paths. A failed copy never modifies the note.
        """
        parent = self._check_attachment_revision(
            note_id, expected_source_id, expected_revision)
        if kind not in ('file', 'still'):
            raise ValueError('Only local file and still attachments can be authored.')
        source_parts = _secure_absolute_parts(source_path)
        if not source_parts:
            raise ValueError('Select a regular local file.')
        _secure_absolute_parts(session_dir)
        original_name = source_parts[-1]
        clean_name = ''.join(c for c in original_name if ord(c) >= 32 and ord(c) != 127)
        clean_name = clean_name[:160].strip()
        if not clean_name:
            raise ValueError('Attachment needs a display filename.')
        ext = os.path.splitext(original_name)[1].lower()
        if not re.fullmatch(r'\.[a-z0-9]{1,12}', ext or ''):
            ext = ''
        src_parent = _open_secure_directory(os.path.dirname(source_path))
        src_fd = None
        dest_parent = None
        asset_name = None
        committed = False
        try:
            src_fd = os.open(original_name, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) |
                             getattr(os, 'O_NONBLOCK', 0), dir_fd=src_parent)
            original = os.fstat(src_fd)
            if not stat.S_ISREG(original.st_mode):
                raise ValueError('Attachments must be regular files.')
            if original.st_size > MAX_LOCAL_ATTACHMENT_BYTES:
                raise ValueError('Attachment exceeds the 128 MB local file limit.')
            header = os.read(src_fd, 32)
            image_type = _still_mime(header)
            if kind == 'still' and image_type is None:
                raise ValueError('Still attachment must be PNG, JPEG, GIF, WEBP, TIFF, EXR or BMP.')
            if kind == 'still':
                # Never preserve an executable/SVG extension on an image copy.
                # The copied asset's suffix reflects the verified container.
                ext = {'image/png': '.png', 'image/jpeg': '.jpg',
                       'image/gif': '.gif', 'image/webp': '.webp',
                       'image/tiff': '.tif', 'image/x-exr': '.exr',
                       'image/bmp': '.bmp'}[image_type]
            os.lseek(src_fd, 0, os.SEEK_SET)
            attachment_id = serialization.new_id('attachment')
            source_hash = hashlib.sha256(self.source_id.encode('utf-8')).hexdigest()[:24]
            asset_name = attachment_id + ext
            asset_parts = (LOCAL_ATTACHMENT_DIRECTORY, source_hash, asset_name)
            asset_uri = '/'.join(asset_parts)
            dest_parent = _open_secure_directory(
                os.path.join(session_dir, *asset_parts[:-1]), create=True)
            dest_fd = os.open(asset_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                              getattr(os, 'O_NOFOLLOW', 0), 0o600, dir_fd=dest_parent)
            digest = hashlib.sha256()
            copied = 0
            try:
                with os.fdopen(dest_fd, 'wb') as destination:
                    while True:
                        block = os.read(src_fd, min(1024 * 1024,
                                                    MAX_LOCAL_ATTACHMENT_BYTES + 1 - copied))
                        if not block:
                            break
                        copied += len(block)
                        if copied > MAX_LOCAL_ATTACHMENT_BYTES:
                            raise ValueError('Attachment exceeds the 128 MB local file limit.')
                        digest.update(block)
                        destination.write(block)
                    destination.flush()
                    os.fsync(destination.fileno())
                updated_stat = os.fstat(src_fd)
                if (copied != original.st_size or
                        updated_stat.st_size != original.st_size or
                        updated_stat.st_mtime_ns != original.st_mtime_ns):
                    raise ValueError('Source file changed during attachment copy.')
                # A concurrent local edit/sync between selection and final
                # write cannot silently attach an asset to a changed note.
                self._check_attachment_revision(
                    note_id, expected_source_id, expected_revision)
                fingerprint = digest.hexdigest()
                # Reopening the file chooser on the same unmodified file is
                # not a second review decision or another autosave. The
                # temporary exclusive copy is discarded in the finally below.
                matching = next((a for a in parent.attachments
                                 if (a.kind == kind and a.name == clean_name and
                                     a.extra.get('sha256') == fingerprint and
                                     a.extra.get('size_bytes') == copied and
                                     a.extra.get(LOCAL_ATTACHMENT_UNSENT_KEY) is True and
                                     a.extra.get(LOCAL_ATTACHMENT_MANAGED_KEY) is True)), None)
                if matching is not None:
                    return copy.deepcopy(matching)
                attachment = ReviewAttachment.from_dict({
                    'id': attachment_id, 'kind': kind, 'uri': asset_uri,
                    'name': clean_name,
                    'mime_type': image_type if kind == 'still' else
                                 (mimetypes.guess_type(clean_name)[0] or 'application/octet-stream'),
                    'size_bytes': copied, 'sha256': fingerprint,
                    LOCAL_ATTACHMENT_UNSENT_KEY: True,
                    LOCAL_ATTACHMENT_MANAGED_KEY: True})
                self.update(note_id, attachments=[a.to_dict() for a in parent.attachments]
                            + [attachment.to_dict()])
                committed = True
                return attachment
            finally:
                if not committed:
                    os.unlink(asset_name, dir_fd=dest_parent)
        finally:
            if dest_parent is not None:
                os.close(dest_parent)
            if src_fd is not None:
                os.close(src_fd)
            os.close(src_parent)

    def remove_local_attachment(self, note_id, attachment_id, *, expected_uri,
                                expected_source_id, expected_revision):
        """Unlink only the typed review reference, preserving managed copy for undo.

        Retained copies remain available to undo/recovery snapshots. Deliberate
        asset garbage collection is a separate operation and must never touch
        the artist's source files or remote attachment URIs.
        """
        note = self._check_attachment_revision(
            note_id, expected_source_id, expected_revision)
        allowed, reason = self.attachment_editability(note_id, attachment_id)
        if not allowed:
            raise PermissionError(reason)
        index = next(i for i, a in enumerate(note.attachments) if a.id == attachment_id)
        attachment = note.attachments[index]
        if not isinstance(expected_uri, str) or attachment.uri != expected_uri:
            raise ValueError('Attachment changed after selection; reload attachments.')
        remaining = list(note.attachments)
        del remaining[index]
        self.update(note_id, attachments=[a.to_dict() for a in remaining])
        return copy.deepcopy(attachment)

    def duplicate(self, note_id, frame=None, source_frame=None,
                  target_layer_id=None):
        note = self._notes[note_id]
        layer_id = target_layer_id or note.layer_id
        self._editable(layer_id)
        data = _duplicate_note(note.to_dict(), frame=frame,
                               source_frame=source_frame)
        data['layer_id'] = layer_id
        for parent_shape, new_shape in zip(note.strokes, data.get('strokes', [])):
            new_shape['derived_from'] = parent_shape.get('id')
        # Replies/attachments and remote IDs belong to the original record.
        for reply in data.get('replies', []):
            reply['id'] = serialization.new_id('reply')
        for attachment in data.get('attachments', []):
            attachment['id'] = serialization.new_id('attachment')
        data.pop('remote_ref', None)
        return self.add(data)

    def add_layer(self, layer):
        layer = layer if isinstance(layer, ReviewLayer) else ReviewLayer.from_dict(layer)
        self._add_layer_initial(layer)
        self._metadata['modified'] = serialization._now()
        self._revision += 1
        return self.layer(layer.id)

    def update_layer(self, layer_id, **changes):
        layer = self._layers[layer_id].revised(**changes)
        self._layers[layer_id] = copy.deepcopy(layer)
        self._metadata['modified'] = serialization._now()
        self._revision += 1
        return self.layer(layer_id)

    def remove_layer(self, layer_id):
        if layer_id == DEFAULT_LAYER_ID:
            raise ValueError('Cannot remove the default layer.')
        if layer_id not in self._layers:
            raise KeyError(layer_id)
        if any(note.layer_id == layer_id for note in self._notes.values()):
            raise ValueError('Move or delete notes before removing their layer.')
        del self._layers[layer_id]
        self._metadata['modified'] = serialization._now()
        self._revision += 1

    def visible_notes(self, source_frame=None, author=None, category=None,
                      status=None, priority=None, include_hidden=False,
                      include_locked=True):
        """Source-frame projection with layer visibility, solo, lock and filters."""
        solo = {layer.id for layer in self._layers.values()
                if layer.solo and (layer.visible or include_hidden)}
        out = []
        for note in self._notes.values():
            layer = self._layers[note.layer_id]
            if not include_hidden and not layer.visible:
                continue
            if solo and layer.id not in solo:
                continue
            if not include_locked and layer.locked:
                continue
            if not layer.allows(note):
                continue
            if source_frame is not None and not note.includes_source_frame(source_frame):
                continue
            if any(value is not None and getattr(note, key) != value for key, value in (
                    ('author', author), ('category', category),
                    ('status', status), ('priority', priority))):
                continue
            out.append(copy.deepcopy(note))
        return tuple(out)

    def project_drawings(self, source_frame=None, **filters):
        """Legacy ``{display_frame: strokes}`` projection, preserving blank keys."""
        drawings = {}
        for note in self.visible_notes(source_frame=source_frame, **filters):
            drawings.setdefault(note.frame, []).extend(copy.deepcopy(list(note.strokes)))
        return drawings

    def merge_remote(self, incoming):
        """Apply remote truth except dirty local edits/deletes; report conflicts.

        Accepts ReviewDocument, a notes-v2 payload, or a sequence of record
        dicts/ReviewNote instances. No local dirty state is implicitly cleared.
        Incoming order is preserved; surviving dirty local-only records append.
        """
        if isinstance(incoming, ReviewDocument):
            if incoming.source_id != self.source_id:
                raise ValueError('Cannot merge notes from a different source.')
            notes = incoming.notes
            remote_layers = incoming.layers
        elif isinstance(incoming, dict):
            notes = self.from_payload(incoming)
            if notes.source_id != self.source_id:
                raise ValueError('Cannot merge notes from a different source.')
            remote_layers = notes.layers
            notes = notes.notes
        else:
            remote_layers = ()
            notes = [n if isinstance(n, ReviewNote) else ReviewNote.from_dict(
                n, source_id=self.source_id,
                start_frame=int(self.source.get('start_frame', 0) or 0))
                     for n in incoming]
        added_layers = {layer.id: layer for layer in remote_layers
                        if layer.id not in self._layers}
        by_id = {}
        for note in notes:
            if note.source_id != self.source_id:
                raise ValueError('Remote note belongs to a different source.')
            if note.id in by_id:
                raise ValueError('Duplicate remote note ID.')
            if note.layer_id not in self._layers and note.layer_id not in added_layers:
                raise ValueError('Remote note references unknown layer.')
            by_id[note.id] = note
        merged, conflicts = {}, {}
        for nid, remote in by_id.items():
            local = self._notes.get(nid)
            if nid in self._deleted:
                conflicts[nid] = ReviewConflict(nid, None, remote, 'locally_deleted')
                continue
            if nid in self._dirty and local is not None:
                merged[nid] = local
                if notes_differ(local.to_dict(), remote.to_dict()):
                    conflicts[nid] = ReviewConflict(nid, local, remote, 'changed')
            else:
                merged[nid] = copy.deepcopy(remote)
        for nid, local in self._notes.items():
            if nid not in by_id and nid in self._dirty:
                merged[nid] = local
                if nid not in self._created:
                    conflicts[nid] = ReviewConflict(nid, local, None, 'remote_deleted')
        self._layers.update(copy.deepcopy(added_layers))
        self._notes = merged
        self._conflicts = conflicts
        self._revision += 1
        return self.conflicts

    def resolve_conflict(self, note_id, choice='local'):
        """Resolve one merge conflict explicitly: ``local`` or ``remote``."""
        conflict = self._conflicts[note_id]
        if choice not in ('local', 'remote'):
            raise ValueError('Choice must be local or remote.')
        if choice == 'remote':
            if conflict.remote is None:
                self._notes.pop(note_id, None)
            else:
                self._notes[note_id] = copy.deepcopy(conflict.remote)
            self._deleted.discard(note_id)
            self._dirty.discard(note_id)
            self._created.discard(note_id)
        else:
            # Local deletion intentionally remains a tombstone.
            if conflict.local is None:
                self._notes.pop(note_id, None)
            else:
                self._notes[note_id] = copy.deepcopy(conflict.local)
        del self._conflicts[note_id]
        self._revision += 1

    def mark_synced(self, ids=None):
        """Acknowledge successful publication/refresh; never silently sync conflicts."""
        ids = set(self._dirty if ids is None else ids)
        blocked = ids.intersection(self._conflicts)
        if blocked:
            raise ValueError('Resolve conflicts before marking notes synced.')
        # Local-only edit consent expires on remote acknowledgment. Otherwise
        # an imported/synced reply could retain a stale local marker and be
        # edited later when an unrelated field makes its parent note dirty.
        for note_id in ids:
            note = self._notes.get(note_id)
            if note is None:
                continue
            if (any(LOCAL_REPLY_UNSENT_KEY in r.extra for r in note.replies) or
                    any(LOCAL_ATTACHMENT_UNSENT_KEY in a.extra for a in note.attachments)):
                replies = []
                for reply in note.replies:
                    data = reply.to_dict()
                    data.pop(LOCAL_REPLY_UNSENT_KEY, None)
                    replies.append(data)
                attachments = []
                for attachment in note.attachments:
                    data = attachment.to_dict()
                    data.pop(LOCAL_ATTACHMENT_UNSENT_KEY, None)
                    attachments.append(data)
                data = note.to_dict()
                data['replies'] = replies
                data['attachments'] = attachments
                self._notes[note_id] = ReviewNote.from_dict(
                    data, source_id=self.source_id)
        self._dirty.difference_update(ids)
        self._deleted.difference_update(ids)
        self._created.difference_update(ids)
        if ids:
            self._revision += 1

    def to_surface_payload(self):
        """Schema-v2 bridge for old ``serialization.parse_payload`` readers.

        A legacy writer expects one drawing key per nonnegative display frame.
        The *authoritative* records (including same-frame notes, layers, remote
        links, dirty tombstones and negative source frames) live in the retained
        ``review_records`` extension. ``ReviewDocument.from_payload`` prefers
        that extension; normal ``serialization.parse_payload`` simply sees the
        flattened strokes and never raises for duplicate frame numbers.

        Do not edit/rebuild the flattened notes independently and assume those
        changes affect embedded records. UI edits must go through
        ``ReviewSurfaceAdapter.pull`` before saving this payload.
        """
        payload = self.to_payload()
        embedded = copy.deepcopy(self._surface_extension_extra)
        embedded.update({'notes': copy.deepcopy(payload['notes']),
                         'layers': copy.deepcopy(payload['layers'])})
        payload[SURFACE_RECORDS_KEY] = embedded
        drawings = ReviewSurfaceAdapter(self).project().drawings
        grouped = {}
        for note in self.notes:
            if note.frame >= 0:
                grouped.setdefault(note.frame, []).append(note)
        source_start = int(self.source.get('start_frame', 0) or 0)
        payload['notes'] = [{
            'id': grouped[frame][0].id if grouped.get(frame) else serialization.new_id('note'),
            'frame': frame,
            'source_frame': (grouped[frame][0].source_frame if grouped.get(frame)
                             else source_start + frame),
            'strokes': copy.deepcopy(strokes),
        } for frame, strokes in sorted(drawings.items())]
        return payload

    @classmethod
    def read(cls, path):
        return cls.from_payload(serialization.read_payload(path))

    def write_atomic(self, path):
        """Write complete schema-v2 JSON, fsync, then atomically replace target."""
        data = json.dumps(self.to_payload(), ensure_ascii=False, indent=2,
                          sort_keys=True) + '\n'
        binary = data.encode('utf-8')
        if len(binary) > serialization.MAX_BYTES:
            raise ValueError('Notes file exceeds the 20 MB size limit.')
        directory = os.path.dirname(os.path.abspath(path))
        fd, temporary = tempfile.mkstemp(prefix='.frnotes-', suffix='.tmp', dir=directory)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(binary)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return path


class ReviewProjectionError(ValueError):
    """Invalid/unsafe viewport edit; caller should restore the last projection."""


class ReviewProjectionStale(ReviewProjectionError):
    """Document changed since the surface was populated; refresh before edit."""


@dataclass(frozen=True)
class ReviewSurfaceProjection:
    """Detached PlaybackSurface drawing keys with lossless shape ownership.

    ``drawings`` is a mapping from *display* index to normalized strokes;
    ``owners`` maps each visible shape ID to its owning review note ID.
    Neither is an authoritative store; only ``ReviewDocument`` owns notes.
    """
    drawings: Dict[int, list]
    owners: Dict[str, str]
    source_id: str
    revision: int


class ReviewSurfaceAdapter(object):
    """Safe bidirectional integration with ``PlaybackSurface`` without Qt.

    Prime/UI integration::

        bridge = ReviewSurfaceAdapter(document, frame_mapping=source.frame_mapping())
        bridge.push(surface)  # surface.set_drawing_data(flattened, replace=True)
        # Connect PlaybackSurface.edited(int) to:
        bridge.pull(surface, edited_frame=frame)
        # Following a remote merge, change of layer visibility or undo:
        bridge.push(surface)

    The adapter snapshots the projection, shape IDs and document revision.
    ``pull`` changes only the committed display key(s), preserving other note
    records at the same frame, their bodies and unknown metadata. Each shape's
    original ID determines its owner; new strokes attach to the sole existing
    editable note or create a separate local note if ownership is ambiguous.
    New notes use ``frame_mapping.display_to_source`` (sparse/negative frames)
    or logical ``source.start_frame + display_frame``. Layer-hidden records are
    untouched; editing locked-layer strokes fails closed. All updates are
    transactional; on a stale revision or invalid edit no state is changed.

    IMPORTANT: PlaybackSurface strips unknown stroke properties and does not
    assign shape IDs to newly drawn strokes. ``pull`` merges known edited
    fields back into the original raw shape and refreshes the surface via
    ``push`` so subsequent edits have assigned, stable shape IDs.
    """

    _EDIT_FIELDS = ('kind', 'text', 'pos', 'points', 'color', 'opacity', 'width',
                    'size', 'pressure_size', 'pressure_opacity')

    def __init__(self, document, frame_mapping=None, new_layer_id=DEFAULT_LAYER_ID):
        if not isinstance(document, ReviewDocument):
            raise TypeError('ReviewSurfaceAdapter requires a ReviewDocument.')
        self.document = document
        self.frame_mapping = frame_mapping
        self.new_layer_id = str(new_layer_id)
        self._snapshot = None
        self._notes_by_frame = {}
        self._rendered_shapes = {}
        self._mapping_snapshot = None

    def _mapping_signature(self):
        mapping = self.frame_mapping
        return (None if mapping is None else
                (id(mapping), mapping.count, mapping.first_source, mapping.last_source))

    def _display_frame(self, note):
        # A new representation can shift the dense display-index range. The
        # record's real source frame remains authoritative across relinks.
        if self.frame_mapping is not None:
            return int(self.frame_mapping.source_to_display(note.source_frame))
        return note.frame

    def _source_frame(self, frame):
        frame = int(frame)
        if frame < 0:
            raise ReviewProjectionError('PlaybackSurface uses nonnegative display indices.')
        if self.frame_mapping is not None:
            if frame >= self.frame_mapping.count:
                raise ReviewProjectionError('Display frame is beyond FrameMapping.')
            return int(self.frame_mapping.display_to_source(frame))
        return frame + int(self.document.source.get('start_frame', 0) or 0)

    @staticmethod
    def _has_drawable_key(note):
        if note.strokes:
            return True
        # A pure empty note from legacy drawings is an intentional OpenRV hold
        # stop. A discussion-only record is NOT a drawing key and must not
        # hide the preceding drawing when projected to PlaybackSurface.
        return (bool(note.extra.get('surface_blank_key')) or
                not any((note.body, note.category, note.replies,
                         note.attachments, note.remote_ref)))

    def project(self):
        """Build a new immutable-by-convention, detached display snapshot.

        The surface supports a single list of strokes per display frame; its
        flattened view is not suitable for authoring serialized notes directly.
        Existing source/display identities, shape IDs and unknown fields remain
        in the document regardless of viewport normalization.
        """
        mapping = {}
        owners = {}
        by_frame = {}
        original_shapes = {}
        all_shape_ids = set()
        for note in self.document.notes:
            for shape in note.strokes:
                sid = str(shape['id'])
                if sid in all_shape_ids:
                    raise ReviewProjectionError('Duplicate shape ID: {}.'.format(sid))
                all_shape_ids.add(sid)
                original_shapes[sid] = (note.id, copy.deepcopy(shape))
        for note in self.document.visible_notes():
            frame = self._display_frame(note)
            if frame < 0 or not self._has_drawable_key(note):
                continue
            by_frame.setdefault(frame, []).append(note.id)
            mapping.setdefault(frame, [])
            for shape in note.strokes:
                normalized = note_model.normalize_stroke(shape)
                if normalized is None:
                    continue  # unsupported shape remains intact in raw record
                sid = str(shape['id'])
                owners[sid] = note.id
                mapping[frame].append(normalized)
        self._notes_by_frame = by_frame
        self._rendered_shapes = original_shapes
        self._mapping_snapshot = self._mapping_signature()
        self._snapshot = ReviewSurfaceProjection(
            copy.deepcopy(mapping), dict(owners), self.document.source_id,
            self.document.revision)
        return ReviewSurfaceProjection(copy.deepcopy(mapping), dict(owners),
                                       self.document.source_id, self.document.revision)

    def push(self, surface):
        """Populate ``PlaybackSurface`` with the current visible projection."""
        projection = self.project()
        surface.set_drawing_data(copy.deepcopy(projection.drawings), replace=True)
        # PlaybackSurface normalizes geometry and reconstructs text corners on
        # import. Treat that actual displayed representation as the baseline,
        # rather than interpreting its own canonicalization as a user edit.
        displayed = note_model.normalize_drawings(surface.drawing_data())
        if set(displayed) != set(projection.drawings):
            raise ReviewProjectionError('Surface dropped drawing frames on import.')
        for frame, shapes in displayed.items():
            if [s.get('id') for s in shapes] != [
                    s.get('id') for s in projection.drawings[frame]]:
                raise ReviewProjectionError('Surface dropped or reordered shape identities.')
        self._snapshot = ReviewSurfaceProjection(
            copy.deepcopy(displayed), dict(projection.owners),
            projection.source_id, projection.revision)
        return self.last_projection

    @property
    def last_projection(self):
        snapshot = self._snapshot
        if snapshot is None:
            return None
        return ReviewSurfaceProjection(copy.deepcopy(snapshot.drawings),
                                       dict(snapshot.owners), snapshot.source_id,
                                       snapshot.revision)

    def note_id_for_shape(self, shape_id):
        """Identify the note owning a shape in the current surface snapshot."""
        if self._snapshot is None:
            return None
        return self._snapshot.owners.get(str(shape_id))

    def apply_drawings(self, drawings, edited_frame=None):
        """Commit detached ``surface.drawing_data()`` transactionally.

        Pass ``edited_frame`` from ``PlaybackSurface.edited(frame)`` to limit
        edits to that key, or omit to reconcile every *changed* projected key.
        Direct remote refresh/merge must happen on ReviewDocument, followed by
        ``push``; this method NEVER accepts a new remote document as viewport
        edits. Returns a tuple of updated/created/deleted note IDs.
        """
        if self._snapshot is None:
            raise ReviewProjectionError('Call project() or push() before pulling edits.')
        if (self._snapshot.revision != self.document.revision or
                self._snapshot.source_id != self.document.source_id or
                self._mapping_snapshot != self._mapping_signature()):
            raise ReviewProjectionStale('Document changed; refresh surface projection.')
        if not isinstance(drawings, dict):
            raise ReviewProjectionError('Surface drawing_data() must be a mapping.')
        if any(int(frame) < 0 for frame in drawings):
            raise ReviewProjectionError('Negative display indices are not supported.')
        normalized = note_model.normalize_drawings(drawings)
        if edited_frame is None:
            frames = sorted(set(normalized).union(self._snapshot.drawings))
            frames = [f for f in frames if normalized.get(f) != self._snapshot.drawings.get(f)]
        else:
            frame = int(edited_frame)
            self._source_frame(frame)
            frames = [frame] if normalized.get(frame) != self._snapshot.drawings.get(frame) else []
        if not frames:
            return ()

        # Validate every changed frame and shape BEFORE modifying any document
        # record. Duplicate/foreign IDs are rejected rather than silently
        # transferring a stroke between records or sources.
        visible_owner = self._snapshot.owners
        existing_shapes = set(self._rendered_shapes)
        baseline_shapes = {str(s['id']): s for all_shapes in self._snapshot.drawings.values()
                           for s in all_shapes}
        for frame in frames:
            self._source_frame(frame)
            seen = set()
            for shape in normalized.get(frame, []):
                sid = shape.get('id')
                if not sid:
                    continue
                sid = str(sid)
                if sid in seen:
                    raise ReviewProjectionError('Duplicate viewport shape ID {}'.format(sid))
                seen.add(sid)
                if sid in existing_shapes and visible_owner.get(sid) is None:
                    raise ReviewProjectionError('Cannot edit hidden or unsupported shape {}.'.format(sid))
                if sid in visible_owner:
                    owner_id = visible_owner[sid]
                    owner = self.document.get(owner_id)
                    if owner is None or self._display_frame(owner) != frame:
                        raise ReviewProjectionError('Cannot move a shape to another display key.')
        staged = ReviewDocument.from_payload(self.document.to_payload())
        changed_ids = []
        for frame in frames:
            current = normalized.get(frame, [])
            by_owner = {}
            additions = []
            for stroke in current:
                sid = stroke.get('id')
                if sid in visible_owner:
                    by_owner.setdefault(visible_owner[sid], []).append(stroke)
                else:
                    additions.append(stroke)
            parents = list(self._notes_by_frame.get(frame, ()))
            attach_owner = None
            editable_parents = [nid for nid in parents
                                if not staged.layer(staged.get(nid).layer_id).locked]
            if len(parents) == 1 and editable_parents:
                attach_owner = parents[0]
            if attach_owner is not None and additions:
                by_owner.setdefault(attach_owner, []).extend(additions)
                additions = []
            for nid in parents:
                old = staged.get(nid)
                if (frame not in normalized and not old.strokes and
                        self._has_drawable_key(old)):
                    # Removing an explicit empty key must restore the held
                    # annotation from the preceding key. A permanent empty
                    # note would otherwise continue suppressing that hold.
                    staged.delete(nid)
                    changed_ids.append(nid)
                    continue
                # Unknown/non-renderable shapes in the record are not part of
                # the surface projection and must survive unchanged.
                nonrendered = [copy.deepcopy(s) for s in old.strokes
                               if s['id'] not in visible_owner]
                replacement = []
                for stroke in by_owner.get(nid, []):
                    sid = stroke.get('id')
                    if sid in visible_owner:
                        source_shape = copy.deepcopy(self._rendered_shapes[sid][1])
                        initial = note_model.normalize_stroke(source_shape)
                        if baseline_shapes[sid] != stroke:
                            for field_name in self._EDIT_FIELDS:
                                if field_name in stroke and (field_name != 'kind' or
                                      source_shape.get('kind', 'brush') == initial.get('kind')):
                                    source_shape[field_name] = copy.deepcopy(stroke[field_name])
                        replacement.append(source_shape)
                    else:
                        item = copy.deepcopy(stroke)
                        item['id'] = serialization.new_id('shape')
                        replacement.append(item)
                replacement.extend(nonrendered)
                if list(old.strokes) == replacement:
                    continue
                if not replacement and not any((old.body, old.category, old.replies,
                                                 old.attachments, old.remote_ref)):
                    staged.delete(nid)
                else:
                    staged.update(nid, strokes=replacement)
                changed_ids.append(nid)
            if additions:
                layer = staged.layer(self.new_layer_id)
                if layer is None or layer.locked:
                    raise PermissionError('New drawing layer is locked or unavailable.')
                strokes = []
                for stroke in additions:
                    item = copy.deepcopy(stroke)
                    item['id'] = serialization.new_id('shape')
                    strokes.append(item)
                created = staged.add({'frame': frame,
                                      'source_frame': self._source_frame(frame),
                                      'source_id': staged.source_id,
                                      'strokes': strokes,
                                      'layer_id': layer.id})
                changed_ids.append(created.id)
            elif frame in normalized and not current and frame not in self._snapshot.drawings:
                layer = staged.layer(self.new_layer_id)
                if layer is None or layer.locked:
                    raise PermissionError('New drawing layer is locked or unavailable.')
                created = staged.add({'frame': frame,
                                      'source_frame': self._source_frame(frame),
                                      'source_id': staged.source_id,
                                      'surface_blank_key': True,
                                      'strokes': [], 'layer_id': layer.id})
                changed_ids.append(created.id)
        if not changed_ids:
            # A raw surface changed a property that does not belong to the
            # review model. Re-project to discard that unsupported edit.
            self.project()
            return ()
        self.document._notes = staged._notes
        self.document._dirty = staged._dirty
        self.document._deleted = staged._deleted
        self.document._created = staged._created
        self.document._conflicts = staged._conflicts
        self.document._metadata = staged._metadata
        self.document._surface_extension_extra = staged._surface_extension_extra
        self.document._revision += max(1, staged.revision)
        self.project()
        return tuple(dict.fromkeys(changed_ids))

    def pull(self, surface, edited_frame=None):
        """Apply a committed surface edit and refresh assigned shape IDs.

        Connect to ``surface.edited`` (NOT ``drawingChanged``); the latter also
        fires on playback and would perform needless normalization work.
        """
        changes = self.apply_drawings(surface.drawing_data(), edited_frame)
        if changes:
            self.push(surface)
        return changes


__all__ = ['ReviewReply', 'ReviewAttachment', 'ReviewNote', 'ReviewLayer',
           'ReviewConflict', 'ReviewDocument', 'DEFAULT_LAYER_ID',
           'LOCAL_STATE_KEY', 'SURFACE_RECORDS_KEY', 'PRIORITIES', 'STATUSES',
           'ReviewSurfaceAdapter', 'ReviewSurfaceProjection',
           'ReviewProjectionError', 'ReviewProjectionStale']
