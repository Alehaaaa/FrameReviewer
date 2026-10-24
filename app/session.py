# -*- coding: utf-8 -*-
"""Portable source and session model for FrameReviewer.

Pure Python and Qt/Maya-free, so it can be unit tested off a host and persisted
to a portable ``.frsession`` JSON file (distinct from the per-scene snapshot in
``scene_state``, which stores machine- and window-specific state inside the Maya
file).

The model follows OpenRV's separation of concerns:

* A :class:`Source` has a stable ``id``, timing (start frame, rational FPS,
  frame numbers), one or more :class:`Representation` media (playblast, proxy,
  review movie, EXR, published), optional audio, a color interpretation,
  free-form metadata, and portable review state (in/out, markers, crop, flips).
  Switching the active representation relinks the media without disturbing the
  playhead, notes, crop, or comparison state.
* A :class:`Session` owns an ordered list of sources (the playlist order), the
  current source, and comparison/color view state. It deliberately does **not**
  store window geometry, volume, or Maya-sync wiring; those are host/user
  concerns kept in preferences and the per-scene snapshot.

Payloads are versioned and migrated purely, and unknown fields are preserved so
a newer writer's data survives a round trip through an older reader.
"""
from __future__ import absolute_import

import datetime
import uuid


FORMAT = 'frame-review-session'
VERSION = 1
SUPPORTED_VERSIONS = (1,)

# Conventional representation roles. Any string is allowed; these name the ones
# the pipeline understands so a relink can prefer a role.
ROLE_PLAYBLAST = 'playblast'
ROLE_PROXY = 'proxy'
ROLE_REVIEW = 'review'
ROLE_EXR = 'exr'
ROLE_PUBLISHED = 'published'
ROLE_ORIGINAL = 'original'

KIND_MOVIE = 'movie'
KIND_SEQUENCE = 'sequence'

_GENERATOR_APP = 'FrameReviewer'


def new_id(prefix):
    return '{}-{}'.format(prefix, uuid.uuid4().hex)


def _now():
    try:
        tz = datetime.timezone.utc
    except AttributeError:  # pragma: no cover
        tz = None
    now = datetime.datetime.now(tz) if tz else datetime.datetime.utcnow()
    return now.replace(microsecond=0).isoformat()


def _generator_version():
    try:
        from .. import __version__
        return str(__version__)
    except Exception:
        return '0'


def _known_extra(data, known):
    """Return the subset of ``data`` whose keys are not in ``known``."""
    return {k: v for k, v in data.items() if k not in known}


class Representation(object):
    """One concrete media for a source (a role plus a path/pattern)."""

    _KNOWN = frozenset(('id', 'role', 'path', 'kind'))

    def __init__(self, role, path, kind=KIND_MOVIE, id=None, extra=None):
        self.id = id or new_id('rep')
        self.role = str(role)
        self.path = str(path)
        self.kind = kind if kind in (KIND_MOVIE, KIND_SEQUENCE) else KIND_MOVIE
        self.extra = dict(extra or {})

    def to_dict(self):
        data = {'id': self.id, 'role': self.role, 'path': self.path,
                'kind': self.kind}
        data.update(self.extra)
        return data

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict) or 'path' not in data:
            raise ValueError('A representation needs a path.')
        return cls(role=data.get('role', ROLE_ORIGINAL), path=data['path'],
                   kind=data.get('kind', KIND_MOVIE), id=data.get('id'),
                   extra=_known_extra(data, cls._KNOWN))


class Source(object):
    """A logical piece of review media with stable identity and review state."""

    _KNOWN = frozenset(('id', 'name', 'representations', 'active_role',
                        'timing', 'audio', 'color', 'metadata', 'review',
                        'notes_ref'))

    def __init__(self, name='', id=None, representations=None, active_role=None,
                 timing=None, audio=None, color=None, metadata=None,
                 review=None, notes_ref=None, extra=None):
        self.id = id or new_id('source')
        self.name = str(name)
        self.representations = list(representations or [])
        self.active_role = active_role
        self.timing = dict(timing or {})
        self.audio = dict(audio) if audio else None
        self.color = dict(color or {})
        self.metadata = dict(metadata or {})
        # Portable review state only (never window geometry). Open dict so new
        # review keys do not require a schema change.
        self.review = dict(review or {})
        self.notes_ref = notes_ref
        self.extra = dict(extra or {})
        if self.active_role is None and self.representations:
            self.active_role = self.representations[0].role

    # -- representations ---------------------------------------------------
    def add_representation(self, representation):
        self.representations.append(representation)
        if self.active_role is None:
            self.active_role = representation.role
        return representation

    def representation(self, role):
        for rep in self.representations:
            if rep.role == role:
                return rep
        return None

    def active_representation(self):
        rep = self.representation(self.active_role) if self.active_role else None
        if rep is None and self.representations:
            rep = self.representations[0]
        return rep

    def set_active_role(self, role):
        """Switch the active media. Review state and notes are untouched."""
        if self.representation(role) is None:
            raise ValueError('No representation with role {!r}.'.format(role))
        self.active_role = role

    def relink(self, role, path, kind=None):
        """Point a role at a new path (or add it), preserving review state."""
        rep = self.representation(role)
        if rep is None:
            rep = self.add_representation(
                Representation(role, path, kind or KIND_MOVIE))
        else:
            rep.path = str(path)
            if kind is not None:
                rep.kind = kind
        return rep

    def active_path(self):
        rep = self.active_representation()
        return rep.path if rep else None

    def remap_paths(self, mapper):
        """Rewrite every representation path through a ``PathMapper``.

        Review state, ids, and the active role are untouched, so a portable
        session relinks its media on a host with a different mount layout.
        """
        for rep in self.representations:
            rep.path = mapper.map(rep.path)
        return self

    # -- timing ------------------------------------------------------------
    @property
    def start_frame(self):
        return int(self.timing.get('start_frame', 0) or 0)

    def frame_mapping(self):
        """A :class:`~framereviewer.media.sequence.FrameMapping` for this source."""
        from ..media.sequence import FrameMapping
        frames = self.timing.get('frame_numbers')
        fps = float(self.timing.get('fps', 24.0) or 24.0)
        rational = self.timing.get('fps_rational')
        if frames:
            return FrameMapping(frames, fps=fps, fps_rational=rational)
        count = int(self.timing.get('frame_count', 1) or 1)
        return FrameMapping.for_movie(count, fps=fps, start_frame=self.start_frame,
                                      fps_rational=rational)

    # -- serialization -----------------------------------------------------
    def to_dict(self):
        data = {
            'id': self.id,
            'name': self.name,
            'representations': [r.to_dict() for r in self.representations],
            'active_role': self.active_role,
            'timing': dict(self.timing),
            'color': dict(self.color),
            'metadata': dict(self.metadata),
            'review': dict(self.review),
        }
        if self.audio is not None:
            data['audio'] = dict(self.audio)
        if self.notes_ref is not None:
            data['notes_ref'] = self.notes_ref
        data.update(self.extra)
        return data

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            raise ValueError('A source must be an object.')
        reps = [Representation.from_dict(r)
                for r in data.get('representations', [])]
        return cls(
            name=data.get('name', ''), id=data.get('id'),
            representations=reps, active_role=data.get('active_role'),
            timing=data.get('timing'), audio=data.get('audio'),
            color=data.get('color'), metadata=data.get('metadata'),
            review=data.get('review'), notes_ref=data.get('notes_ref'),
            extra=_known_extra(data, cls._KNOWN))

    @classmethod
    def from_media_info(cls, info, name=None, role=ROLE_ORIGINAL):
        """Build a source from a probed ``media.ffmpeg.MediaInfo``."""
        kind = KIND_SEQUENCE if getattr(info, 'is_sequence', False) else KIND_MOVIE
        rep = Representation(role, info.path, kind)
        timing = {
            'fps': float(getattr(info, 'fps', 24.0)),
            'frame_count': int(getattr(info, 'frame_count', 1)),
            'start_frame': int(getattr(info, 'start_frame', 0)),
        }
        frame_numbers = getattr(info, 'frame_numbers', None)
        if frame_numbers:
            timing['frame_numbers'] = list(frame_numbers)
        if getattr(info, 'fps_rational', None):
            timing['fps_rational'] = list(info.fps_rational)
        if getattr(info, 'declared_frames', None) is not None:
            # Authored timeline may begin/end on an absent image. The decoded
            # first image alone cannot recover that contract on session reopen.
            timing['declared_frames'] = list(info.declared_frames)
        metadata = {'width': int(getattr(info, 'width', 0)),
                    'height': int(getattr(info, 'height', 0))}
        metadata.update(dict(getattr(info, 'metadata', {}) or {}))
        audio = dict(info.audio) if getattr(info, 'audio', None) else None
        import os
        display = name or os.path.splitext(os.path.basename(info.path))[0]
        return cls(name=display, representations=[rep], active_role=role,
                   timing=timing, audio=audio, metadata=metadata)


class Session(object):
    """An ordered playlist of sources plus comparison/color view state."""

    _KNOWN = frozenset(('id', 'name', 'sources', 'current_source_id',
                        'view', 'color', 'metadata'))

    def __init__(self, name='', id=None, sources=None, current_source_id=None,
                 view=None, color=None, metadata=None, extra=None):
        self.id = id or new_id('session')
        self.name = str(name)
        self.sources = list(sources or [])
        self.current_source_id = current_source_id
        self.view = dict(view or {})      # comparison mode, A/B ids, wipe, etc.
        self.color = dict(color or {})    # session-wide color view state
        self.metadata = dict(metadata or {})
        self.extra = dict(extra or {})
        if self.current_source_id is None and self.sources:
            self.current_source_id = self.sources[0].id

    # -- playlist ----------------------------------------------------------
    def add_source(self, source):
        self.sources.append(source)
        if self.current_source_id is None:
            self.current_source_id = source.id
        return source

    def remove_source(self, source_id):
        self.sources = [s for s in self.sources if s.id != source_id]
        if self.current_source_id == source_id:
            self.current_source_id = self.sources[0].id if self.sources else None

    def source(self, source_id):
        for src in self.sources:
            if src.id == source_id:
                return src
        return None

    def current_source(self):
        return self.source(self.current_source_id) if self.current_source_id else None

    def set_current(self, source_id):
        if self.source(source_id) is None:
            raise ValueError('No source with id {!r}.'.format(source_id))
        self.current_source_id = source_id

    def index_of(self, source_id):
        for i, src in enumerate(self.sources):
            if src.id == source_id:
                return i
        return -1

    def move_source(self, source_id, new_index):
        """Reorder the playlist, keeping the source identity stable."""
        idx = self.index_of(source_id)
        if idx < 0:
            raise ValueError('No source with id {!r}.'.format(source_id))
        src = self.sources.pop(idx)
        new_index = max(0, min(len(self.sources), int(new_index)))
        self.sources.insert(new_index, src)

    def remap_paths(self, mapper):
        """Relink every source's media through a ``PathMapper``."""
        for src in self.sources:
            src.remap_paths(mapper)
        return self

    # -- serialization -----------------------------------------------------
    def to_dict(self):
        data = {
            'id': self.id,
            'name': self.name,
            'sources': [s.to_dict() for s in self.sources],
            'current_source_id': self.current_source_id,
            'view': dict(self.view),
            'color': dict(self.color),
            'metadata': dict(self.metadata),
        }
        data.update(self.extra)
        return data

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            raise ValueError('A session must be an object.')
        sources = [Source.from_dict(s) for s in data.get('sources', [])]
        return cls(
            name=data.get('name', ''), id=data.get('id'), sources=sources,
            current_source_id=data.get('current_source_id'),
            view=data.get('view'), color=data.get('color'),
            metadata=data.get('metadata'),
            extra=_known_extra(data, cls._KNOWN))


# -- payload (versioned, migratable) ---------------------------------------

def build_payload(session):
    """Wrap a :class:`Session` in a versioned, stamped container."""
    now = _now()
    return {
        'format': FORMAT,
        'version': VERSION,
        'generator': {'app': _GENERATOR_APP, 'version': _generator_version()},
        'created': now,
        'modified': now,
        'session': session.to_dict(),
    }


def migrate_payload(payload):
    """Upgrade a session payload to the current version (pure)."""
    if not isinstance(payload, dict) or payload.get('format') != FORMAT:
        raise ValueError('This is not a FrameReviewer session file.')
    version = int(payload.get('version', 0))
    if version not in SUPPORTED_VERSIONS:
        raise ValueError('Unsupported session file version.')
    upgraded = dict(payload)
    upgraded['version'] = VERSION
    return upgraded


def parse_payload(payload):
    """Validate and return a :class:`Session` from a payload."""
    payload = migrate_payload(payload)
    data = payload.get('session')
    if not isinstance(data, dict):
        raise ValueError('The session payload is missing its session object.')
    return Session.from_dict(data)
