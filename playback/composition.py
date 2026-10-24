# -*- coding: utf-8 -*-
"""Multi-source composition model: how several media play together.

Pure Python and Qt-free, so the timing, layout, and navigation logic is unit
testable off a Maya host. The viewer and player consume this model; they do not
re-derive it.

Four modes:

* ``SEQUENCE`` - the members play back to back on one concatenated timeline.
  :meth:`Composition.segments` gives each member's global frame span and an
  alternating color so the timeline can distinguish clips, :meth:`locate` maps a
  global frame to ``(member_index, local_frame)``, and :meth:`next_clip_range` /
  :meth:`prev_clip_range` drive an Up/Down "focus the adjacent clip" control.
* ``GRID`` - every member is shown at once, tiled and centered.
  :meth:`grid_cells` returns the normalized rectangles.
* ``DIFFERENCE`` / ``INVERTED_DIFFERENCE`` - members are overlaid at the same
  session time and combined as an absolute difference (optionally inverted).

The actual pixel compositing and simultaneous decode live in the viewer/player;
this module only describes *what* to show and *where*.
"""
from __future__ import absolute_import

import math
from fractions import Fraction


SEQUENCE = 'sequence'
GRID = 'grid'
DIFFERENCE = 'difference'
INVERTED_DIFFERENCE = 'inverted_difference'
A_B = 'a_b'
BLEND = 'blend'
WIPE = 'wipe'
SPLIT = 'split'
CHECKER = 'checker'
OVER = 'over'
FLICKER = 'flicker'
TWO_SOURCE_MODES = (DIFFERENCE, INVERTED_DIFFERENCE, A_B,
                    BLEND, WIPE, SPLIT, CHECKER, OVER, FLICKER)
MODES = (SEQUENCE, GRID) + TWO_SOURCE_MODES

# Alternating segment tints for the concatenated timeline (cycled per clip).
DEFAULT_SEGMENT_COLORS = ('#3a6ea5', '#a5623a')


def rational_fps(value):
    """Exact rational value for timebase or speed (including ``[num, den]``)."""
    if isinstance(value, (tuple, list)) and len(value) == 2:
        return Fraction(int(value[0]), int(value[1]))
    return Fraction(str(value))


def validate_mode(mode):
    if mode in ('ab', 'a/b', 'A/B'):
        return A_B
    return mode if mode in MODES else SEQUENCE


class CompositionMember(object):
    """One participant: a reference to a session source plus its length."""

    __slots__ = ('source_id', 'name', 'frame_count', 'source_in',
                 'session_offset', 'rate', 'trim_in', 'trim_out', 'hold')

    def __init__(self, source_id, frame_count, name='', source_in=0,
                 session_offset=0, rate=None, trim_in=0, trim_out=None,
                 hold=True):
        self.source_id = source_id
        self.frame_count = max(1, int(frame_count))
        self.name = name or str(source_id)
        self.source_in = int(source_in)
        self.session_offset = int(session_offset)
        self.rate = rational_fps(rate) if rate is not None else None
        if self.rate is not None and self.rate <= 0:
            raise ValueError('Member rate must be positive.')
        # frame_count is *record* duration for EDL/OTIO edits, not source
        # duration. A 2x speed clip can consume twice as many source frames;
        # never clamp explicitly supplied source trim bounds to record length.
        self.trim_in = max(0, int(trim_in))
        self.trim_out = (max(self.trim_in, self.frame_count - 1)
                         if trim_out is None else
                         max(self.trim_in, int(trim_out)))
        self.hold = bool(hold)

    def map_frame(self, session_frame, session_fps=None, source_fps=None):
        """Map one session frame into this source, returning None outside a non-hold trim.

        ``rate`` expresses source frames per session frame. By default the
        rational ratio of source/session FPS is used when both are supplied.
        ``source_in`` and trim coordinates refer to zero-based source indices.
        """
        rate = self.rate
        if rate is None:
            rate = (rational_fps(source_fps) / rational_fps(session_fps)
                    if session_fps and source_fps else Fraction(1))
        if int(session_frame) < self.session_offset:
            return (max(self.trim_in, min(self.source_in, self.trim_out))
                    if self.hold else None)
        mapped = self.source_in + ((int(session_frame) - self.session_offset) *
                                   rate).__floor__()
        if not self.hold and not self.trim_in <= mapped <= self.trim_out:
            return None
        return max(self.trim_in, min(mapped, self.trim_out))

    def to_dict(self):
        return {'source_id': self.source_id, 'frame_count': self.frame_count,
                'name': self.name, 'source_in': self.source_in,
                'session_offset': self.session_offset,
                'rate': str(self.rate) if self.rate is not None else None,
                'trim_in': self.trim_in, 'trim_out': self.trim_out,
                'hold': self.hold}

    @classmethod
    def from_dict(cls, data):
        return cls(data['source_id'], data.get('frame_count', 1),
                   data.get('name', ''), data.get('source_in', 0),
                   data.get('session_offset', 0), data.get('rate'),
                   data.get('trim_in', 0), data.get('trim_out'),
                   data.get('hold', True))


class Segment(object):
    """A member's inclusive span on the concatenated sequence timeline."""

    __slots__ = ('index', 'start', 'end', 'member', 'color')

    def __init__(self, index, start, end, member, color):
        self.index = index
        self.start = start
        self.end = end            # inclusive
        self.member = member
        self.color = color

    @property
    def count(self):
        return self.end - self.start + 1

    def to_tuple(self):
        return (self.start, self.end, self.index, self.color)


class Composition(object):
    """An ordered set of members shown together in one of :data:`MODES`."""

    def __init__(self, mode=SEQUENCE, members=None, colors=None,
                 parameters=None):
        self.mode = validate_mode(mode)
        self.members = list(members or [])
        self.colors = tuple(colors or DEFAULT_SEGMENT_COLORS)
        self.parameters = dict(parameters or {})

    # -- membership --------------------------------------------------------
    def add(self, member):
        self.members.append(member)
        return member

    @property
    def count(self):
        return len(self.members)

    def is_active(self):
        """A composition only means something with two or more members."""
        return len(self.members) >= 2

    @property
    def visible_members(self):
        """Extra selected sources are ignored in two-source comparison modes."""
        return self.members[:2] if self.mode in TWO_SOURCE_MODES else self.members

    @property
    def ignored_members(self):
        return self.members[2:] if self.mode in TWO_SOURCE_MODES else []

    def map_frame(self, member_index, session_frame, session_fps=None,
                  source_fps=None):
        return self.members[member_index].map_frame(session_frame,
                                                     session_fps, source_fps)

    @property
    def is_difference(self):
        return self.mode in (DIFFERENCE, INVERTED_DIFFERENCE)

    @property
    def inverted(self):
        return self.mode == INVERTED_DIFFERENCE

    @property
    def record_timeline(self):
        """Whether SEQUENCE members describe record positions, not raw clips.

        Legacy members have no explicit rate, offset or source trims and keep
        historical concatenation. An imported composition has at least one of
        these fields, and gaps/retimes are evaluated on a common playhead.
        ``parameters['record_timeline']`` can explicitly override detection.
        """
        if self.mode != SEQUENCE:
            return False
        if 'record_timeline' in self.parameters:
            return bool(self.parameters['record_timeline'])
        return any(m.session_offset != 0 or m.source_in != 0 or
                   m.rate is not None or m.trim_in != 0 or
                   m.trim_out != m.frame_count - 1
                   for m in self.members)

    # -- sequence timeline -------------------------------------------------
    def total_frames(self):
        """Sequence record duration (including gaps), otherwise raw durations."""
        if self.record_timeline:
            return max(1, max((m.session_offset + m.frame_count
                               for m in self.members), default=1))
        return sum(m.frame_count for m in self.members) or 1

    def color_for(self, index):
        if not self.colors:
            return DEFAULT_SEGMENT_COLORS[0]
        return self.colors[index % len(self.colors)]

    def segments(self):
        """Inclusive global spans for each member, with alternating colors."""
        out = []
        cursor = 0
        for i, member in enumerate(self.members):
            start = member.session_offset if self.record_timeline else cursor
            end = start + member.frame_count - 1
            out.append(Segment(i, start, end, member, self.color_for(i)))
            cursor = end + 1
        return out

    def locate(self, global_frame):
        """Map a global sequence frame to ``(member_index, local_frame)``."""
        if not self.members:
            return (0, 0)
        total = self.total_frames()
        g = max(0, min(int(global_frame), total - 1))
        if self.record_timeline:
            # Last occurrence takes precedence for overlapping edits. This is
            # deterministic even when CMX transitions were approximated as cuts.
            for i in range(len(self.members) - 1, -1, -1):
                member = self.members[i]
                if member.session_offset <= g < member.session_offset + member.frame_count:
                    return (i, g - member.session_offset)
            return (None, None)  # gap: no decoder should be requested
        cursor = 0
        for i, member in enumerate(self.members):
            if g < cursor + member.frame_count:
                return (i, g - cursor)
            cursor += member.frame_count
        last = len(self.members) - 1
        return (last, self.members[last].frame_count - 1)

    def record_frame_address(self, record_frame, session_fps=None,
                             source_fps_by_member=None):
        """Read-only exact frame address of an imported SEQUENCE record edit.

        An occurrence index is the *edit position*, not a media Source ID or an
        invented persistent EDL/OTIO occurrence_id. ``decode_index`` is a
        zero-based media index, NEVER a numbered EXR/PNG source frame label.
        The caller with an actual decoder can resolve that index to a signed
        label. Intentional record gaps and outside-trim samples have no source.

        In particular, ``hold=True`` only freezes a SOURCE TRIM while an edit
        is on record; it never fills an empty record gap or an out-of-range seek.
        No rounding of 24000/1001 or implicit audio/speed conform occurs.
        """
        if self.mode != SEQUENCE or not self.record_timeline:
            raise ValueError('A record-frame address requires a record SEQUENCE.')
        if type(record_frame) is not int:
            raise ValueError('Record frame must be an exact integer index.')
        if record_frame < 0 or record_frame >= self.total_frames():
            return {'kind': 'out_of_range', 'record_frame': record_frame,
                    'occurrence_index': None, 'source_id': None,
                    'decode_index': None}
        occurrence, local = self.locate(record_frame)
        if occurrence is None:
            return {'kind': 'record_gap', 'record_frame': record_frame,
                    'occurrence_index': None, 'source_id': None,
                    'decode_index': None}
        member = self.members[occurrence]
        fps = None
        if source_fps_by_member is not None:
            fps = source_fps_by_member[occurrence]
        rate = (member.rate if member.rate is not None else
                rational_fps(fps) / rational_fps(session_fps)
                if fps is not None and session_fps is not None else Fraction(1))
        raw = member.source_in + (local * rate).__floor__()
        outside = not member.trim_in <= raw <= member.trim_out
        mapped = member.map_frame(record_frame, session_fps, fps)
        kind = ('outside_trim' if mapped is None else
                'held_trim' if outside else 'source')
        return {'kind': kind, 'record_frame': record_frame,
                'occurrence_index': occurrence,
                'record_local_frame': local,
                'source_id': member.source_id,
                'decode_index': mapped,
                'unclamped_decode_index': raw,
                'source_rate': '{}/{}'.format(rate.numerator, rate.denominator)}

    def global_of(self, member_index, local_frame=0):
        """Global sequence frame for a member's local frame."""
        if not self.members:
            return 0
        member_index = max(0, min(int(member_index), len(self.members) - 1))
        cursor = (self.members[member_index].session_offset
                  if self.record_timeline else
                  sum(m.frame_count for m in self.members[:member_index]))
        local = max(0, min(int(local_frame),
                           self.members[member_index].frame_count - 1))
        return cursor + local

    def clip_range(self, member_index):
        """Inclusive ``(start, end)`` global range of a member."""
        segs = self.segments()
        member_index = max(0, min(int(member_index), len(segs) - 1))
        seg = segs[member_index]
        return (seg.start, seg.end)

    def clip_index_at(self, global_frame):
        return self.locate(global_frame)[0]

    def next_clip_range(self, global_frame):
        """Range of the clip after the one containing ``global_frame``."""
        idx = self.clip_index_at(global_frame)
        if idx is None:
            upcoming = [(seg.start, seg.index) for seg in self.segments()
                        if seg.start > int(global_frame)]
            idx = min(upcoming)[1] if upcoming else len(self.members) - 1
            return self.clip_range(idx)
        return self.clip_range(min(idx + 1, len(self.members) - 1))

    def prev_clip_range(self, global_frame):
        """Range of the clip before the one containing ``global_frame``."""
        idx = self.clip_index_at(global_frame)
        if idx is None:
            preceding = [(seg.end, seg.index) for seg in self.segments()
                         if seg.end < int(global_frame)]
            idx = max(preceding)[1] if preceding else 0
            return self.clip_range(idx)
        return self.clip_range(max(idx - 1, 0))

    # -- grid layout -------------------------------------------------------
    @staticmethod
    def grid_shape(n):
        """``(cols, rows)`` for a near-square grid of ``n`` tiles."""
        n = max(1, int(n))
        cols = int(math.ceil(math.sqrt(n)))
        rows = int(math.ceil(n / float(cols)))
        return (cols, rows)

    def grid_cells(self):
        """Normalized ``(x, y, w, h)`` tiles in ``[0, 1]``, last row centered."""
        n = len(self.members)
        if n == 0:
            return []
        cols, rows = self.grid_shape(n)
        w = 1.0 / cols
        h = 1.0 / rows
        cells = []
        for i in range(n):
            row = i // cols
            # Center the final (possibly short) row horizontally.
            in_row = min(cols, n - row * cols)
            col = i - row * cols
            row_width = in_row * w
            x = (1.0 - row_width) / 2.0 + col * w
            y = row * h
            cells.append((x, y, w, h))
        return cells

    # -- serialization -----------------------------------------------------
    def to_dict(self):
        return {'mode': self.mode,
                'members': [m.to_dict() for m in self.members],
                'colors': list(self.colors),
                'parameters': dict(self.parameters)}

    @classmethod
    def from_dict(cls, data):
        members = [CompositionMember.from_dict(m)
                   for m in (data.get('members') or [])]
        return cls(mode=data.get('mode', SEQUENCE), members=members,
                   colors=data.get('colors'),
                   parameters=data.get('parameters'))
