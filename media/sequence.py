# -*- coding: utf-8 -*-
"""OpenRV-faithful image-sequence discovery, frame expressions, and time model.

Pure Python: no Qt, no ffmpeg, no NumPy, so it is cheap to import and fully unit
testable off a Maya host. The semantics mirror OpenRV's ``TwkUtil`` frame
utilities (``FrameUtils.cpp`` / ``FileSequence.cpp``):

* Frame numbers are **real source frame numbers**, held in a sparse map keyed by
  the number on disk. Negative frames and holes are preserved, and ``first`` /
  ``last`` report the true range rather than a zero-based display index.
* Frame *expressions* understand the ``@``, ``#``, ``` ` ``` (Maya tick) and
  printf ``%0Nd`` cookies, each with optional ``+/-`` offset, ``xN`` multiplier
  and ``/N`` modulo, exactly as ``replaceFrameSymbols`` does.
* Shake-style time strings (``1-10``, ``1-10x2``, ``1,10,20-30x2``, ``-15--10``)
  round-trip through :func:`parse_frame_range` and :func:`frame_str`.

A :class:`FrameMapping` separates the time domains the review tool must not
conflate: the dense zero-based **display index** used by the playback cache, the
sparse **source frame** number on disk (or in the movie), rational FPS, and a
non-drop **timecode**. Keeping these distinct is the prerequisite for note
identity, comparison alignment and export that the rest of the backlog builds
on.
"""
from __future__ import absolute_import

import os
import re


# -- missing-frame display policy -----------------------------------------
#
# How the viewer should present a hole in a sequence. These are data only; the
# GPU/software viewer decides how to honor them.
MISSING_HOLD = 'hold'        # repeat the previous available frame
MISSING_BLACK = 'black'      # show black
MISSING_CHECKER = 'checker'  # show a checker placeholder
MISSING_ERROR = 'error'      # surface an explicit error frame
MISSING_POLICIES = (MISSING_HOLD, MISSING_BLACK, MISSING_CHECKER, MISSING_ERROR)
DEFAULT_MISSING_POLICY = MISSING_HOLD


def validate_missing_policy(policy):
    return policy if policy in MISSING_POLICIES else DEFAULT_MISSING_POLICY


# -- frame expressions ------------------------------------------------------
#
# Optional modifiers that may trail any cookie: a signed offset, an ``xN``
# multiplier, a ``/N`` modulo and a second signed offset. Applied in the order
# OpenRV applies them: ``f += off1; f *= mult; f %= mod; f += off2``.
_MODS = r'(?P<off1>[-+]?\d+)?(?:x(?P<mult>\d+))?(?:/(?P<mod>\d+))?(?P<off2>[-+]?\d+)?'

# ``(?<!\\)`` stands in for OpenRV's "not escaped" guard and also permits a
# cookie at the very start of the string (nothing precedes it).
_AT_RE = re.compile(r'(?<!\\)(?P<sym>@+)' + _MODS)
_HASH_RE = re.compile(r'(?<!\\)(?P<sym>#+)' + _MODS)
_TICK_RE = re.compile(r'(?<!\\)`(?P<fps>\d+)?' + _MODS)
_PCT_RE = re.compile(r'(?<!\\)(?P<sym>%[-+ 0-9]*[di])' + _MODS)

# A cookie, optionally preceded by a Shake time range, embedded in a name.
_COOKIE_RE = re.compile(r'([@#]+|%[-+ 0-9]*[di])')
_RANGE_COOKIE_RE = re.compile(
    r'(?P<range>-?\d+(?:-{1,2}\d+)?(?:x\d+)?'
    r'(?:,-?\d+(?:-{1,2}\d+)?(?:x\d+)?)*)?'
    r'(?P<cookie>[@#]+|%[-+ 0-9]*[di])')


def _apply_mods(frame, match):
    """Apply the optional offset/multiply/modulo modifiers to ``frame``."""
    value = int(frame)
    off1 = match.group('off1')
    mult = match.group('mult')
    mod = match.group('mod')
    off2 = match.group('off2')
    if off1:
        value += int(off1)
    if mult:
        value *= int(mult)
    if mod:
        value %= int(mod)
    if off2:
        value += int(off2)
    return value


def replace_frame_symbols(pattern, frame, fps=24):
    """Substitute every frame cookie in ``pattern`` with ``frame``.

    Mirrors ``TwkUtil::replaceFrameSymbols``. Examples (frame = 12)::

        foo.%d.xxx     -> foo.12.xxx
        foo.%04d.xxx   -> foo.0012.xxx
        foo.@.xxx      -> foo.12.xxx
        foo.@@@.xxx    -> foo.012.xxx
        foo.#.xxx      -> foo.0012.xxx
        foo.@+10.xxx   -> foo.22.xxx
        foo.#-10.xxx   -> foo.0002.xxx
        foo.#/7.xxx    -> foo.0005.xxx
        foo.`.xxx      -> foo.3000.xxx   (Maya ticks: frame * 6000 / fps)
        foo\\@.#.xxx   -> foo@.0012.xxx  (escaped cookie kept literal)
    """
    text = pattern

    def at(match):
        pad = len(match.group('sym'))
        return ('%0' + str(pad) + 'd') % _apply_mods(frame, match)

    def hash_(match):
        # OpenRV pads '#' to four digits regardless of how many '#' appear.
        return '%04d' % _apply_mods(frame, match)

    def tick(match):
        rate = int(match.group('fps')) if match.group('fps') else fps
        rate = rate or 1
        return '%d' % (_apply_mods(frame, match) * 6000 // rate)

    def pct(match):
        spec = match.group('sym')
        if spec.endswith('i'):  # Python % has no 'i'; it means the same as 'd'.
            spec = spec[:-1] + 'd'
        return spec % _apply_mods(frame, match)

    text = _AT_RE.sub(at, text)
    text = _HASH_RE.sub(hash_, text)
    text = _TICK_RE.sub(tick, text)
    text = _PCT_RE.sub(pct, text)

    for escaped in ('\\@', '\\#', '\\`', '\\%'):
        text = text.replace(escaped, escaped[1])
    return text


def has_frame_cookie(name):
    """True if ``name`` contains a ``@``/``#``/``%0Nd`` frame cookie."""
    return _COOKIE_RE.search(name) is not None


# -- Shake-style time strings ----------------------------------------------

_SEGMENT_RE = re.compile(r'^(-?\d+)(?:-(-?\d+))?(?:x(-?\d+))?$')
_ONLY_RANGE_RE = re.compile(r'^-?\d+(?:-{1,2}\d+)?(?:x\d+)?'
                            r'(?:,-?\d+(?:-{1,2}\d+)?(?:x\d+)?)*$')


def parse_frame_range(text):
    """Expand a Shake-style time string to a sorted, de-duplicated frame list.

    ``1-10`` -> 1..10, ``1-10x2`` -> 1,3,5,7,9, ``1,10,20-30x2`` and
    ``-15--10`` (negative frames) are all understood. Mirrors
    ``TwkUtil::frameRange``.
    """
    frames = set()
    # Imported authored ranges are untrusted user text. A multi-billion-frame
    # request must not allocate a multi-billion-element set before the Player
    # and cache have the opportunity to enforce their own bounded span.
    max_frames = 1000000
    for segment in str(text).split(','):
        segment = segment.strip()
        if not segment:
            continue
        match = _SEGMENT_RE.match(segment)
        if not match:
            continue
        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) is not None else start
        step = int(match.group(3)) if match.group(3) else 1
        if step == 0:
            step = 1
        if step < 0:
            step = -step
        if end < start:
            start, end = end, start
        values = range(start, end + 1, step)
        if len(values) > max_frames or len(frames) + len(values) > max_frames * 2:
            raise ValueError('Declared sequence exceeds 1,000,000 frames.')
        frames.update(values)
        if len(frames) > max_frames:
            raise ValueError('Declared sequence exceeds 1,000,000 frames.')
    return sorted(frames)


def is_frame_range(text):
    """True if ``text`` is a pure Shake time string (e.g. ``1-10`` or ``-1-10``)."""
    return bool(_ONLY_RANGE_RE.match(str(text).strip()))


def guess_increment(frames, start=0, lookahead=3):
    """Constant step of the first ``lookahead`` frames, else 1. Mirrors OpenRV."""
    if len(frames) <= 1 or start + 1 >= len(frames):
        return 1
    step = frames[start + 1] - frames[start]
    for i in range(start + 1, min(lookahead, len(frames) - 1)):
        if frames[i + 1] - frames[i] != step:
            return 1
    return step if step > 0 else 1


def frame_str(frames):
    """Compact a frame list into a Shake time string. Inverse of :func:`parse_frame_range`."""
    frames = sorted(set(int(f) for f in frames))
    if not frames:
        return ''
    if len(frames) == 1:
        return str(frames[0])

    pieces = []
    run = [frames[0]]
    step = guess_increment(frames)
    for i, frame in enumerate(frames):
        nxt = frames[i + 1] if i < len(frames) - 1 else None
        if nxt == frame + step:
            run.append(frame)
            continue
        if len(run) == 1:
            pieces.append(str(frame))
        elif step == 1:
            pieces.append('{}-{}'.format(run[0], frame))
        else:
            pieces.append('{}-{}x{}'.format(run[0], frame, step))
        if nxt is not None:
            run = [nxt]
            step = guess_increment(frames, i + 1)
    return ','.join(pieces)


# -- sequence discovery -----------------------------------------------------

_FIELD_RE = re.compile(r'-?\d+')


class Sequence(object):
    """A sparse image sequence: real frame numbers mapped to file paths.

    Holes and negative frames survive because the mapping is keyed by the frame
    number on disk, never by a dense list position. ``ordered_paths`` yields the
    existing files in frame order for consumers (such as the playback cache)
    that still address frames by a zero-based display index.
    """

    def __init__(self, directory, prefix, suffix, paths, pad=1, single=False,
                 declared_frames=None):
        self.directory = directory
        self.prefix = prefix
        self.suffix = suffix
        self.pad = max(1, int(pad))
        self.single = bool(single)
        # frame number -> absolute path (existing files only)
        self.paths = dict(paths)
        self._frames = sorted(self.paths)
        # Frames the pattern declares (from an embedded range), which may list
        # holes that do not exist on disk. Falls back to the existing frames.
        self.declared_frames = (sorted(set(int(f) for f in declared_frames))
                                 if declared_frames else list(self._frames))

    # -- identity ----------------------------------------------------------
    @classmethod
    def single_file(cls, path):
        path = os.path.abspath(path)
        directory, name = os.path.split(path)
        return cls(directory, name, '', {0: path}, pad=1, single=True,
                   declared_frames=[0])

    @property
    def frames(self):
        """Sorted real frame numbers that exist on disk."""
        return list(self._frames)

    @property
    def count(self):
        """Number of existing frames (the playable display length)."""
        return len(self._frames)

    @property
    def first(self):
        return self._frames[0] if self._frames else None

    @property
    def last(self):
        return self._frames[-1] if self._frames else None

    @property
    def span(self):
        """Inclusive frame span ``last - first + 1`` (counts holes)."""
        if not self._frames:
            return 0
        return self._frames[-1] - self._frames[0] + 1

    @property
    def missing(self):
        """Frame numbers inside ``[first, last]`` with no file on disk."""
        if self.single or not self._frames:
            return []
        present = set(self._frames)
        return [f for f in range(self._frames[0], self._frames[-1] + 1)
                if f not in present]

    @property
    def has_holes(self):
        return bool(self.missing)

    # -- patterns ----------------------------------------------------------
    def symbol(self):
        """Padding cookie: ``#`` for 4-digit padding, else ``@`` repeated."""
        if self.single:
            return ''
        return '#' if self.pad == 4 else '@' * self.pad

    def pattern(self):
        """Pattern basename, e.g. ``shot.#.exr`` or ``shot.@@@.exr``."""
        if self.single:
            return self.prefix
        return '{}{}{}'.format(self.prefix, self.symbol(), self.suffix)

    def pattern_path(self):
        return os.path.join(self.directory, self.pattern())

    def range_str(self):
        return frame_str(self._frames)

    def display_name(self):
        """Human/OpenRV name with embedded range, e.g. ``shot.1-48#.exr``."""
        if self.single:
            return self.prefix
        return '{}{}{}{}'.format(self.prefix, self.range_str(),
                                 self.symbol(), self.suffix)

    # -- lookups -----------------------------------------------------------
    def path_for_frame(self, frame):
        return self.paths.get(int(frame))

    def contains(self, frame):
        return int(frame) in self.paths

    def ordered_paths(self):
        """Existing files in frame order (what the dense cache consumes)."""
        return [self.paths[f] for f in self._frames]

    def frame_at_index(self, index):
        return self._frames[index]

    def index_of_frame(self, frame):
        return self._frames.index(int(frame))

    def mapping(self, fps=24.0):
        """A :class:`FrameMapping` over this sequence's existing frames."""
        return FrameMapping(self._frames or [0], fps=fps)

    def __len__(self):
        return self.count

    def __repr__(self):
        return '<Sequence {} [{}]>'.format(self.display_name(),
                                           self.range_str())


def _scan_matches(directory, prefix, suffix):
    """Return ``{frame: path}`` and the minimum zero-padding seen on disk."""
    pattern = re.compile(r'^' + re.escape(prefix) + r'(-?\d+)' +
                         re.escape(suffix) + r'$')
    matches = {}
    pad = None
    try:
        names = os.listdir(directory or '.')
    except OSError:
        return matches, 1
    for name in names:
        hit = pattern.match(name)
        if not hit:
            continue
        digits = hit.group(1)
        frame = int(digits)
        matches[frame] = os.path.join(directory, name)
        width = len(digits.lstrip('-'))
        pad = width if pad is None else min(pad, width)
    return matches, (pad or 1)


def discover_sequence(path, min_sequence_size=1):
    """Discover the sequence a single member file belongs to.

    Mirrors ``TwkUtil::sequenceOfFile``: every numeric field in the name is a
    candidate frame field; the field matching the most sibling files wins, with
    a tie broken toward a dot-delimited field (the conventional frame slot).
    Returns a :class:`Sequence`; a lone file yields a single-file sequence.
    """
    path = os.path.abspath(path)
    directory, name = os.path.split(path)

    fields = list(_FIELD_RE.finditer(name))
    if not fields:
        return Sequence.single_file(path)

    best = None
    for field in fields:
        prefix = name[:field.start()]
        suffix = name[field.end():]
        matches, pad = _scan_matches(directory, prefix, suffix)
        if not matches:
            continue
        dotted = prefix.endswith('.') or prefix.endswith('_')
        score = (len(matches), dotted, field.start())
        if best is None or score > best[0]:
            best = (score, prefix, suffix, matches, pad)

    if best is None or best[0][0] < max(1, min_sequence_size):
        return Sequence.single_file(path)

    _, prefix, suffix, matches, pad = best
    return Sequence(directory, prefix, suffix, matches, pad=pad)


def split_sequence_name(name):
    """Split ``shot.1-10#.exr`` into (``'1-10'``, ``'shot.#.exr'``).

    Returns ``(time_str, pattern)``. ``time_str`` is empty when the name has a
    bare cookie and no embedded range. Mirrors ``TwkUtil::splitSequenceName``;
    returns ``(None, name)`` when there is no cookie at all.
    """
    match = _RANGE_COOKIE_RE.search(name)
    if not match:
        return None, name
    time_str = match.group('range') or ''
    cookie = match.group('cookie')
    if time_str:
        pattern = name[:match.start('range')] + cookie + name[match.end('cookie'):]
    else:
        pattern = name
    return time_str, pattern


def expand_pattern(pattern, min_sequence_size=1):
    """Build a :class:`Sequence` from a pattern such as ``shot.%04d.exr``.

    Recognizes ``%04d``/printf, ``#`` and ``@@@@`` cookies, with an optional
    embedded Shake range (``shot.1-48#.exr``). Existing files on disk populate
    the sparse map; a declared range additionally records holes that are absent
    on disk so the viewer can honor a missing-frame policy.
    """
    pattern = os.path.abspath(pattern)
    directory, name = os.path.split(pattern)
    time_str, bare = split_sequence_name(name)
    if time_str is None:
        # No cookie: treat as a concrete member file.
        return discover_sequence(pattern, min_sequence_size=min_sequence_size)

    cookie = _COOKIE_RE.search(bare)
    prefix = bare[:cookie.start()]
    suffix = bare[cookie.end():]
    matches, pad = _scan_matches(directory, prefix, suffix)

    # Infer padding from the cookie itself when nothing is on disk yet.
    cookie_text = cookie.group(0)
    if not matches:
        if cookie_text.startswith('#'):
            pad = 4
        elif cookie_text.startswith('@'):
            pad = len(cookie_text)
        else:  # %0Nd
            digits = re.search(r'%[-+ ]*0?(\d+)', cookie_text)
            pad = int(digits.group(1)) if digits else 1

    declared = parse_frame_range(time_str) if time_str else None
    return Sequence(directory, prefix, suffix, matches, pad=pad,
                    declared_frames=declared)


# -- time domains -----------------------------------------------------------

def parse_rational_fps(text, default=(24, 1)):
    """Parse ``'24000/1001'`` or ``'23.976'`` into an exact ``(num, den)``."""
    if isinstance(text, (tuple, list)) and len(text) == 2:
        try:
            return int(text[0]), int(text[1])
        except (TypeError, ValueError):
            return default
    text = str(text or '').strip()
    try:
        if '/' in text:
            num, den = text.split('/', 1)
            num, den = int(num), int(den)
            return (num, den) if den else default
        if '.' in text:
            whole, frac = text.split('.', 1)
            frac = frac.rstrip('0') or '0'
            den = 10 ** len(frac)
            num = int(whole) * den + (int(frac) if frac != '0' else 0)
            return _reduce(num, den)
        return (int(text), 1)
    except (TypeError, ValueError):
        return default


def _reduce(num, den):
    try:
        from math import gcd
    except ImportError:  # pragma: no cover - very old Python
        def gcd(a, b):
            while b:
                a, b = b, a % b
            return a
    g = gcd(num, den) or 1
    return num // g, den // g


def is_drop_frame_rate(fps):
    """True for the NTSC rates SMPTE drop-frame timecode is defined for."""
    return abs(float(fps) - 29.97) < 0.02 or abs(float(fps) - 59.94) < 0.02


def timecode(display_frame, fps, drop_frame=False):
    """``HH:MM:SS:FF`` timecode for a zero-based ``display_frame``.

    With ``drop_frame`` the SMPTE drop-frame reckoning is used (the standard
    Jim-Robinson algorithm) and the frame separator is ``;``. Drop-frame is only
    defined for 29.97 and 59.94 fps; requesting it at another rate raises.
    """
    frame = max(0, int(display_frame))
    if drop_frame:
        if not is_drop_frame_rate(fps):
            raise ValueError(
                'Drop-frame timecode is only defined for 29.97 and 59.94 fps.')
        drop = int(round(float(fps) * 0.066666))      # 2 at 29.97, 4 at 59.94
        nominal = int(round(float(fps)))              # 30 or 60
        per_minute = nominal * 60 - drop
        per_10_minutes = int(round(float(fps) * 600))
        tens = frame // per_10_minutes
        remainder = frame % per_10_minutes
        if remainder > drop:
            frame += drop * 9 * tens + drop * ((remainder - drop) // per_minute)
        else:
            frame += drop * 9 * tens
        ff = frame % nominal
        ss = (frame // nominal) % 60
        mm = (frame // (nominal * 60)) % 60
        hh = frame // (nominal * 3600)
        return '{:02d}:{:02d}:{:02d};{:02d}'.format(hh, mm, ss, ff)
    rate = int(round(fps)) or 1
    ff = frame % rate
    total_seconds = frame // rate
    ss = total_seconds % 60
    mm = (total_seconds // 60) % 60
    hh = total_seconds // 3600
    return '{:02d}:{:02d}:{:02d}:{:02d}'.format(hh, mm, ss, ff)


class FrameMapping(object):
    """Keeps the display / source / time domains distinct.

    * **display index** - dense, zero-based, what the playback cache addresses.
    * **source frame** - the real frame number on disk or in the movie, which
      may be non-zero, negative, or sparse (holes).
    * **rational FPS** - exact ``(num, den)`` so 23.976/29.97/59.94 identities
      do not round.
    * **timecode** - non-drop ``HH:MM:SS:FF`` derived from the display index.
    """

    def __init__(self, source_frames, fps=24.0, fps_rational=None):
        frames = sorted(int(f) for f in source_frames)
        self._frames = frames or [0]
        if fps_rational is not None:
            self._num, self._den = parse_rational_fps(fps_rational)
        else:
            self._num, self._den = parse_rational_fps(fps)

    @classmethod
    def for_movie(cls, frame_count, fps=24.0, start_frame=0, fps_rational=None):
        """A contiguous mapping for a movie (or any dense source)."""
        count = max(1, int(frame_count))
        start = int(start_frame)
        return cls(range(start, start + count), fps=fps, fps_rational=fps_rational)

    # -- fps ---------------------------------------------------------------
    @property
    def fps_rational(self):
        return (self._num, self._den)

    @property
    def fps(self):
        return self._num / float(self._den or 1)

    # -- range -------------------------------------------------------------
    @property
    def count(self):
        return len(self._frames)

    @property
    def first_source(self):
        return self._frames[0]

    @property
    def last_source(self):
        return self._frames[-1]

    @property
    def source_frames(self):
        return list(self._frames)

    # -- conversions -------------------------------------------------------
    def display_to_source(self, index):
        index = max(0, min(int(index), len(self._frames) - 1))
        return self._frames[index]

    def source_to_display(self, frame):
        """Display index of ``frame``; nearest existing frame when it is a hole."""
        frame = int(frame)
        try:
            return self._frames.index(frame)
        except ValueError:
            import bisect
            pos = bisect.bisect_left(self._frames, frame)
            if pos <= 0:
                return 0
            if pos >= len(self._frames):
                return len(self._frames) - 1
            before = self._frames[pos - 1]
            after = self._frames[pos]
            return pos if (after - frame) < (frame - before) else pos - 1

    def display_to_seconds(self, index):
        return int(index) * self._den / float(self._num or 1)

    def source_to_seconds(self, frame):
        return self.display_to_seconds(self.source_to_display(frame))

    def display_to_timecode(self, index, drop_frame=False):
        return timecode(index, self.fps, drop_frame=drop_frame)

    def __len__(self):
        return self.count
