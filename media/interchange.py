# -*- coding: utf-8 -*-
"""CMX3600 EDL and optional OpenTimelineIO interchange for review sessions.

This module never probes media or imports Qt. An import returns a stable
``Session`` plus a ``Composition`` of clip *occurrences*. Each occurrence has
its own Source identity even when several edits use the same reel. Absolute
source and record timecodes are kept in Source.metadata['interchange'], while
CompositionMember expresses the local trim and session position in frames.

Public API:
    import_edl(text, fps='24', media_paths=None) -> InterchangeResult
    export_edl(session, composition=None, fps=None, title=None) -> InterchangeResult
    import_otio(timeline_or_path, otio_module=None) -> InterchangeResult
    export_otio(session, composition=None, fps=None, otio_module=None) -> InterchangeResult

Results provide .session, .composition, .text, .timeline, .issues, .warnings,
.errors, and .ok. Issues are dictionaries with severity/code/message/location.
OTIO is imported only when those adapters are called; missing dependencies
produce a structured 'otio_unavailable' error rather than an ImportError.
"""
from __future__ import absolute_import

import os
import re
import tempfile
from fractions import Fraction
from urllib.parse import unquote, urlsplit

from ..app.session import (KIND_MOVIE, ROLE_ORIGINAL, Representation, Session,
                           Source)
from ..playback.composition import Composition, CompositionMember, SEQUENCE
from .sequence import is_drop_frame_rate, timecode as frame_to_timecode


_TC = re.compile(r'^\d{2,}:\d{2}:\d{2}[:;]\d{2}$')
_EVENT = re.compile(r'^\s*(\d+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(.+)$')
_FCM = re.compile(r'^FCM\s*:\s*(.*)$', re.I)
_TITLE = re.compile(r'^TITLE\s*:\s*(.*)$', re.I)
_REEL_COMMENT = re.compile(r'^\*\s*(?:FROM CLIP NAME|SOURCE FILE|FILE NAME)\s*:\s*(.+)$', re.I)
_MOTION = re.compile(r'^M2\s+(\S+)\s+([+-]?\d+(?:\.\d+)?)\s+(\S+)', re.I)
_NTSC_RATES = {'23.976': Fraction(24000, 1001),
               '29.97': Fraction(30000, 1001),
               '59.94': Fraction(60000, 1001)}


class InterchangeResult(object):
    """Conversion product and explicit loss/capability diagnostics."""

    def __init__(self, session=None, composition=None, text=None,
                 timeline=None, issues=None):
        self.session = session
        self.composition = composition
        self.text = text
        self.timeline = timeline
        self.issues = list(issues or [])

    def issue(self, code, message, location=None, severity='warning'):
        self.issues.append({'severity': severity, 'code': code,
                            'message': str(message), 'location': location})

    @property
    def warnings(self):
        return [i for i in self.issues if i['severity'] == 'warning']

    @property
    def errors(self):
        return [i for i in self.issues if i['severity'] == 'error']

    @property
    def ok(self):
        return not self.errors


_LOSSY_EXPORT_CODES = frozenset((
    'unsupported_transition', 'unsupported_tracks', 'unsupported_track',
    'unsupported_item', 'unsupported_effect', 'unsupported_composition',
    'overlapping_edits', 'retime_in_edl', 'motion_effect_omitted',
    'mixed_frame_rates', 'freeze_as_hold', 'unsupported_retime',
    'unverified_editorial_alignment',
    'record_audio_intent_omitted', 'unverified_editorial_audio_intent',
))


def lossy_export_issues(result):
    """Issues that require informed approval BEFORE an interchange write."""
    return [item for item in result.issues
            if item.get('severity') == 'error' or
            item.get('code') in _LOSSY_EXPORT_CODES]


def _check_record_audio_interchange(session, composition, result, target):
    """Prevent EDL/OTIO from quietly dropping verified per-cut sound choices.

    FrameReviewer's audio routes belong to its .frsession and verified Export
    spec, not to CMX3600's simple V cuts. Encoding absolute external WAV paths
    into a generic OTIO vendor field without importer parity/permission would
    also be misleading. Explicit informed lossy consent is required instead.
    """
    bed = session.view.get('record_audio_overlay_intent')
    if bed is not None:
        from ..app.record_audio_overlay_intent import restore_overlay_intent
        try:
            overlay = restore_overlay_intent(composition, bed)
        except (ValueError, TypeError, KeyError) as exc:
            result.issue('unverified_editorial_audio_intent',
                         'Saved audio-only record layer no longer matches '
                         'exact cuts ({}); {} cannot safely export it.'.format(
                             exc, target), severity='error')
        else:
            if overlay is not None:
                if type(overlay) is list:
                    result.issue('record_audio_intent_omitted',
                        '{} VIDEO-only interchange omits all {} independent '
                        'ordered audio-only overlays, their WAV origins, '
                        'record/source timing, explicit gain and overlaps. '
                        'Preserve the .frsession and independently verified '
                        'record WAV/movie. Explicit informed VIDEO-only lossy '
                        'consent is required before writing this cut list.'
                        .format(target, len(overlay)))
                else:
                    result.issue('record_audio_intent_omitted',
                        '{} is not a lossless export of the independent audio-only '
                        'PCM bed and explicit overlapping sound/gain. Preserve the '
                        'Session and verified record WAV/movie; informed consent '
                        'is required for a VIDEO-only cut list.'.format(target))
    saved = session.view.get('record_audio_intent')
    if saved is None:
        return
    from ..app.record_audio_intent import restore_intent
    try:
        routes = restore_intent(composition, saved)
    except (ValueError, TypeError, KeyError) as exc:
        result.issue('unverified_editorial_audio_intent',
                     'Saved per-edit audio choices do not match the current '
                     'record cuts ({}); {} export cannot claim exact audio '
                     'provenance.'.format(exc, target), severity='error')
        return
    if routes:
        result.issue('record_audio_intent_omitted',
                     '{} cannot losslessly carry the {} selected per-occurrence '
                     'embedded/external soundtrack routes. The .frsession '
                     'retains audio intent and independently verified record '
                     'WAV/MOV delivery remains supported; exporting this edit '
                     'list alone omits its chosen sound.'.format(
                         target, len(routes)))


def write_interchange_atomic(path, result):
    """Safely publish a fully serialized .edl or .otio without predeletion.

    OTIO export produces the canonical JSON using its adapter before this
    step. A write/serialization failure never truncates an existing artist
    timeline. Atomic replace is on the selected destination filesystem.
    """
    if (not result.ok or not isinstance(result.text, str) or
            not result.text.strip()):
        raise ValueError('Refusing to write invalid or unserialized interchange.')
    final = os.path.abspath(os.fspath(path))
    suffix = os.path.splitext(final)[1].lower()
    if suffix not in ('.edl', '.otio'):
        raise ValueError('Atomic timeline publication requires .edl or .otio.')
    if os.path.islink(final):
        raise ValueError('Refusing to replace a symlinked timeline destination.')
    raw = result.text.encode('utf-8')
    if len(raw) > 16 * 1024 * 1024:
        raise ValueError('Timeline exceeds bounded 16 MiB text export limit.')
    directory = os.path.dirname(final)
    if not os.path.isdir(directory):
        raise ValueError('Timeline export directory does not exist.')
    fd, staging = tempfile.mkstemp(prefix='.framereviewer_interchange_',
                                  suffix=suffix, dir=directory)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staging, final)
    finally:
        if os.path.lexists(staging):
            os.unlink(staging)
    return final


def _fps(value):
    """Exact FPS; NTSC decimal shorthand resolves to the broadcast ratio."""
    if isinstance(value, (tuple, list)) and len(value) == 2:
        rate = Fraction(int(value[0]), int(value[1]))
    elif isinstance(value, Fraction):
        rate = value
    else:
        rate = _NTSC_RATES.get(str(value).strip())
        if rate is None:
            rate = Fraction(str(value or 24))
    if rate <= 0:
        raise ValueError('Frame rate must be positive.')
    return rate


def _rate_of(source, fallback):
    timing = source.timing
    return _fps(timing.get('fps_rational') or timing.get('fps') or fallback)


def timecode_to_frames(value, fps, drop_frame=None):
    """Parse SMPTE NDF or DF timecode to an integer frame index.

    Semicolon indicates drop-frame unless ``drop_frame`` is explicitly set.
    Invalid skipped DF numbers (at a non-tenth minute) are rejected.
    """
    text = str(value).strip()
    if not _TC.match(text):
        raise ValueError('Invalid SMPTE timecode: {}'.format(value))
    rate = _fps(fps)
    drop = (';' in text) if drop_frame is None else bool(drop_frame)
    if drop and not is_drop_frame_rate(float(rate)):
        raise ValueError('Drop-frame timecode requires 29.97 or 59.94 FPS.')
    h, m, s, f = [int(v) for v in re.split('[:;]', text)]
    nominal = int(round(float(rate)))
    if m > 59 or s > 59 or f >= nominal:
        raise ValueError('Timecode component outside frame-rate bounds: ' + text)
    frame = ((h * 3600 + m * 60 + s) * nominal + f)
    if drop:
        count = 2 if nominal == 30 else 4
        if m % 10 and s == 0 and f < count:
            raise ValueError('Skipped drop-frame number: ' + text)
        minutes = h * 60 + m
        frame -= count * (minutes - minutes // 10)
    return frame


def frames_to_timecode(frame, fps, drop_frame=False):
    if frame < 0:
        raise ValueError('Timecode cannot encode a negative frame number.')
    return frame_to_timecode(int(frame), float(_fps(fps)), drop_frame)


def _source_for_edit(name, path, reel, source_start, source_end, rate,
                     record_start, record_end, metadata=None):
    """Model one edit as a trimmed source; source absolute TC remains explicit."""
    source_length = max(1, source_end - source_start)
    info = dict(metadata or {})
    info.update({'reel': reel, 'source_in_frame': source_start,
                 'source_out_frame': source_end, 'record_in_frame': record_start,
                 'record_out_frame': record_end,
                 'source_in_tc': (frames_to_timecode(source_start, rate,
                                     info.get('drop_frame', False))
                                  if source_start >= 0 else None),
                 'source_out_tc': (frames_to_timecode(source_end, rate,
                                      info.get('drop_frame', False))
                                   if source_end >= 0 else None)})
    timing = {'fps': float(rate), 'fps_rational': [rate.numerator, rate.denominator],
              'start_frame': source_start, 'frame_count': source_length}
    source = Source(name=name, representations=[Representation(
        ROLE_ORIGINAL, path or 'reel://{}'.format(reel), KIND_MOVIE)],
        timing=timing, metadata={'interchange': info})
    return source


def _member(source, record_duration, offset, source_duration=None, rate=None):
    source_length = int(source.timing['frame_count'])
    record_duration = max(1, int(record_duration))
    source_duration = max(1, int(source_duration or source_length))
    speed = _fps(rate) if rate is not None else Fraction(source_duration, record_duration)
    # Freeze is represented by one addressable source frame held for its
    # entire record duration; CompositionMember does not accept zero rate.
    return CompositionMember(source.id, record_duration, source.name,
                             source_in=0, session_offset=int(offset),
                             rate=speed, trim_in=0,
                             trim_out=max(0, source_length - 1), hold=True)


def _wrap_session(name, rate, sources, members, kind, drop=False):
    comp = Composition(SEQUENCE, members)
    session = Session(name=name, sources=sources,
                      view={'comparison_mode': SEQUENCE,
                            'composition': comp.to_dict()},
                      metadata={'interchange': {
                          'format': kind, 'fps_rational': [rate.numerator,
                                                           rate.denominator],
                          'drop_frame': bool(drop)}})
    return session, comp


def import_edl(text, fps='24', media_paths=None):
    """Read CMX3600 video cuts and preserve their source/record time domains.

    ``media_paths`` may map reel IDs or source-file comment names to media URLs.
    Record gaps and unsupported audio/transitions are kept as diagnostics and
    raw edit metadata. An import does not claim to resolve missing footage.
    """
    rate = _fps(fps)
    result = InterchangeResult()
    title, drop = 'Imported EDL', False
    sources, members, events = [], [], []
    current, record_cursor, record_origin = None, None, None
    mapping = dict(media_paths or {})

    def finish():
        nonlocal current, record_cursor, record_origin
        if current is None:
            return
        event = current
        current = None
        reel = event['reel']
        location = 'event {}'.format(event['number'])
        if event['channel'] not in ('V', 'B'):
            result.issue('unsupported_channel',
                         'Audio-only edit {} was omitted.'.format(event['channel']),
                         location)
            # The preview can omit audio and remain loadable, but a managed
            # record delivery MUST NOT silently ship the video-only cut list.
            # Keep the longstanding informational code for editors, plus the
            # strict delivery_gate-recognized loss category.
            result.issue('unsupported_tracks',
                         'EDL audio edit {} is not represented in the record '
                         'timeline; delivery would drop its soundtrack.'.format(
                             event['channel']), location)
            events.append(event)
            return
        if event['transition'] != 'C':
            result.issue('unsupported_transition',
                         'CMX transition {} is not represented as a cut.'.format(
                             event['transition']), location)
        start, stop = event['record_start'], event['record_end']
        source_start, source_stop = event['source_start'], event['source_end']
        freeze = (source_stop == source_start and
                  (event['transition'] in ('F', 'FI') or
                   (event.get('motion') and
                    float(event['motion']['speed']) == 0.0)))
        if stop <= start or (source_stop <= source_start and not freeze):
            result.issue('invalid_edit_range',
                         'Edit has a zero or negative frame span.',
                         location, 'error')
            events.append(event)
            return
        if freeze:
            source_stop = source_start + 1
            result.issue('freeze_frame',
                         'Single source frame is held for the record duration.',
                         location)
        if record_origin is None:
            record_origin = start
            record_cursor = start
        if start < record_cursor:
            result.issue('overlapping_edits',
                         'Edit overlaps preceding video edit by {} frames.'.format(
                             record_cursor - start), location)
        elif start > record_cursor:
            result.issue('record_gap',
                         'EDL contains a {}-frame gap before this edit; '
                         'gap position is retained in member offset '
                         '(the current sequence player concatenates clips).'.format(
                             start - record_cursor), location)
        record_cursor = max(record_cursor, stop)
        raw_path = event.get('path')
        looks_like_path = (raw_path and (os.path.isabs(raw_path) or
                           '/' in raw_path or '\\' in raw_path or
                           os.path.splitext(raw_path)[1].lower() in
                           ('.mov', '.mp4', '.mxf', '.exr', '.avi', '.mkv', '.dpx')))
        path = (mapping.get(reel) or mapping.get(raw_path) or
                (raw_path if looks_like_path else None) or
                'reel://{}'.format(reel))
        if path.startswith('reel://'):
            result.issue('unresolved_media',
                         'Reel {} has no linked media path.'.format(reel), location)
        source_rate = rate
        source_name = event.get('clip_name') or reel
        metadata = {'event_number': event['number'], 'channel': event['channel'],
                    'transition': event['transition'],
                    'transition_duration': event.get('transition_duration'),
                    'drop_frame': drop, 'source_path': raw_path,
                    'raw_event': event['raw'], 'motion': event.get('motion'),
                    'freeze': freeze, 'comments': event.get('comments', [])}
        source = _source_for_edit(source_name, path, reel, source_start,
                                  source_stop, source_rate, start, stop, metadata)
        member = _member(source, stop - start, start - record_origin,
                         source_stop - source_start)
        sources.append(source)
        members.append(member)
        events.append(event)

    for lineno, raw in enumerate(str(text).splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        title_match = _TITLE.match(line)
        if title_match:
            title = title_match.group(1).strip() or title
            continue
        fcm_match = _FCM.match(line)
        if fcm_match:
            drop = 'DROP' in fcm_match.group(1).upper() and 'NON' not in fcm_match.group(1).upper()
            if drop and not is_drop_frame_rate(float(rate)):
                result.issue('unsupported_drop_frame_rate',
                             'FCM DROP FRAME requires 29.97 or 59.94 FPS.',
                             'line {}'.format(lineno), 'error')
            continue
        comment = _REEL_COMMENT.match(line)
        if comment and current is not None:
            item = comment.group(1).strip()
            if line.upper().startswith('* FROM CLIP NAME:'):
                current['clip_name'] = item
            current['path'] = item
            continue
        motion = _MOTION.match(line)
        if motion and current is not None:
            current['motion'] = {'reel': motion.group(1),
                                 'speed': motion.group(2),
                                 'start_tc': motion.group(3)}
            result.issue('motion_effect',
                         'M2 speed is preserved in metadata; source/record '
                         'timecodes define the imported linear mapping.',
                         'line {}'.format(lineno))
            continue
        if line.startswith('*'):
            if current is not None:
                current.setdefault('comments', []).append(line)
            continue
        if line.startswith(';'):
            continue
        match = _EVENT.match(line)
        if not match:
            result.issue('unrecognized_line', 'Unrecognized EDL line.',
                         'line {}'.format(lineno))
            continue
        tokens = line.split()
        timecodes = tokens[-4:]
        if len(tokens) < 8 or not all(_TC.match(t) for t in timecodes):
            result.issue('invalid_event', 'Malformed CMX event or timecodes.',
                         'line {}'.format(lineno), 'error')
            continue
        finish()
        transition = tokens[3].upper()
        event = {'number': tokens[0], 'reel': tokens[1],
                 'channel': tokens[2].upper(), 'transition': transition,
                 'transition_duration': (tokens[4] if len(tokens) > 8 else None),
                 'raw': line}
        try:
            # FCM is authoritative: unlike standalone SMPTE parsing, an EDL
            # cannot mix colon/semicolon clocks without losing the frame
            # numbering convention. In particular, 29.97 DF and NDF differ
            # by hundreds of frames after a few minutes.
            if any((';' in tc) != drop for tc in timecodes):
                raise ValueError('Event timecode delimiter contradicts EDL FCM '
                                 '{}: {}'.format('DROP' if drop else 'NON-DROP',
                                                 ' '.join(timecodes)))
            event['source_start'], event['source_end'], event['record_start'], event['record_end'] = [
                timecode_to_frames(tc, rate, drop_frame=drop) for tc in timecodes]
        except ValueError as exc:
            result.issue('invalid_timecode', str(exc),
                         'line {}'.format(lineno), 'error')
            continue
        current = event
    finish()
    result.session, result.composition = _wrap_session(
        title, rate, sources, members, 'cmx3600', drop)
    result.session.metadata['interchange']['events'] = events
    result.session.metadata['interchange']['record_origin_frame'] = (
        record_origin if record_origin is not None else 0)
    return result


def _composition_or_session(session, composition):
    if composition is not None:
        return composition
    data = session.view.get('composition')
    if isinstance(data, dict):
        return Composition.from_dict(data)
    return Composition(SEQUENCE, [CompositionMember(
        source.id, int(source.timing.get('frame_count') or 1), source.name)
        for source in session.sources])


def _session_rate(session, explicit=None):
    if explicit is not None:
        return _fps(explicit)
    saved = session.metadata.get('interchange', {}).get('fps_rational')
    if saved:
        return _fps(saved)
    if session.sources:
        return _rate_of(session.sources[0], Fraction(24))
    return Fraction(24)


def _editorial_export_member(session, composition, index, member, result):
    """Undo a *proven* decoder-index shift when writing absolute source TC.

    `align_composition` adds the media's embedded-TC offset to source_in AND
    both trim bounds, without changing the original session edit. Adding this
    offset again to Source.metadata['interchange'].source_in_frame writes the
    wrong editorial clock (e.g. 01:00:10:00 becomes 01:00:20:00).

    The imported, unaligned snapshot is retained by session.view. Compare by
    occurrence POSITION, not reel or Source ID (both may recur), and rebase
    only if the three source-coordinate shifts and entire record contract
    agree. Ambiguous edits keep their live values but get a lossy diagnostic;
    never fabricate an absolute TC or mutate playback source coordinates.
    """
    provenance = session.view.get('interchange_composition')
    if not isinstance(provenance, dict):
        provenance = session.view.get('composition')
    source = session.source(member.source_id)
    if not isinstance(provenance, dict) or source is None or \
            not source.metadata.get('interchange'):
        return member
    try:
        original = Composition.from_dict(provenance)
    except (KeyError, TypeError, ValueError):
        result.issue('unverified_editorial_alignment',
                     'Saved original editorial timeline is invalid; absolute '
                     'source timecode cannot be reliably rebased.',
                     'member {}'.format(index + 1))
        return member
    if (original.mode != SEQUENCE or
            len(original.members) != len(composition.members) or
            [m.source_id for m in original.members] !=
            [m.source_id for m in composition.members]):
        result.issue('unverified_editorial_alignment',
                     'Original edit occurrences/order no longer match; '
                     'source timecode may include decoder offset.',
                     'member {}'.format(index + 1))
        return member
    saved = original.members[index]
    differences = (member.source_in - saved.source_in,
                   member.trim_in - saved.trim_in,
                   member.trim_out - saved.trim_out)
    if not any(differences):
        return member
    if (differences[0] >= 0 and len(set(differences)) == 1 and
            member.frame_count == saved.frame_count and
            member.session_offset == saved.session_offset and
            member.rate == saved.rate and member.hold == saved.hold):
        return saved
    result.issue('unverified_editorial_alignment',
                 'Live source trim has changed beyond a uniform media-TC '
                 'alignment; original absolute source in/out is not proven.',
                 'member {}'.format(index + 1))
    return member


def export_edl(session, composition=None, fps=None, title=None):
    """Write a CMX3600 cut list. Always report representational losses."""
    result = InterchangeResult(session=session)
    composition = _composition_or_session(session, composition)
    result.composition = composition
    _check_record_audio_interchange(session, composition, result, 'CMX3600 EDL')
    rate = _session_rate(session, fps)
    drop = bool(session.metadata.get('interchange', {}).get('drop_frame', False))
    if drop and not is_drop_frame_rate(float(rate)):
        result.issue('unsupported_drop_frame_rate', 'Invalid drop-frame timeline FPS.',
                     severity='error')
        return result
    if composition.mode != SEQUENCE:
        result.issue('unsupported_composition',
                     'CMX3600 cannot encode {} composition; exported ordered cuts.'.format(
                         composition.mode))
    for original in session.metadata.get('interchange', {}).get('events', ()):
        if (isinstance(original, dict) and
                original.get('channel') not in ('V', 'B', None)):
            result.issue('unsupported_tracks',
                         'EDL audio event {} was imported only as metadata; '
                         're-export would silently lose the audio edit.'.format(
                             original.get('number', '?')))
    text = ['TITLE: {}'.format(title or session.name or 'FrameReviewer'),
            'FCM: {}'.format('DROP FRAME' if drop else 'NON-DROP FRAME'), '']
    record_origin = int(session.metadata.get('interchange', {}).get(
        'record_origin_frame', 0))
    cursor = record_origin
    for index, member in enumerate(composition.members, 1):
        source = session.source(member.source_id)
        if source is None:
            result.issue('missing_source', 'Composition source ID is missing.',
                         'member {}'.format(index), 'error')
            continue
        info = source.metadata.get('interchange', {})
        editorial_member = _editorial_export_member(
            session, composition, index - 1, member, result)
        rec_in = int(member.session_offset) + record_origin
        if index > 1 and rec_in == record_origin and not info.get('record_in_frame') == record_origin:
            rec_in = cursor
        if index > 1 and rec_in == record_origin and 'record_in_frame' not in info:
            rec_in = cursor
        if rec_in < cursor:
            result.issue('overlapping_edits', 'CMX3600 event overlaps preceding cut.',
                         'member {}'.format(index))
        elif rec_in > cursor:
            result.issue('record_gap', 'Timeline gap retained as record timecode.',
                         'member {}'.format(index))
        rec_out = rec_in + member.frame_count
        cursor = max(cursor, rec_out)
        source_rate = _rate_of(source, rate)
        if source_rate != rate:
            result.issue('mixed_frame_rates',
                         'CMX3600 assumes one event timebase; source FPS is {}.'.format(
                             source_rate), 'member {}'.format(index))
        absolute_start = int(info.get('source_in_frame', source.start_frame))
        absolute_start += int(editorial_member.source_in)
        if editorial_member.trim_in > editorial_member.source_in:
            absolute_start += (editorial_member.trim_in -
                               editorial_member.source_in)
        scalar = (editorial_member.rate if editorial_member.rate is not None
                  else Fraction(1))
        source_length = max(1, (member.frame_count * scalar).__ceil__())
        if info.get('freeze'):
            source_length = 1
            result.issue('freeze_as_hold',
                         'Freeze is represented by a one-frame source range '
                         'with a longer record duration.',
                         'member {}'.format(index))
        absolute_end = absolute_start + source_length
        if scalar != 1:
            result.issue('retime_in_edl',
                         'Linear speed represented only by source/record lengths; '
                         'CMX cut event has no independent rate parameter.',
                         'member {}'.format(index))
        if info.get('transition') not in (None, 'C'):
            result.issue('unsupported_transition',
                         'Original transition {} exported as cut.'.format(
                             info['transition']), 'member {}'.format(index))
        reel = str(info.get('reel') or source.name or 'AX')[:8].replace(' ', '_')
        try:
            timecodes = [frames_to_timecode(f, source_rate, drop) for f in
                         (absolute_start, absolute_end)]
            timecodes += [frames_to_timecode(f, rate, drop) for f in
                          (rec_in, rec_out)]
        except ValueError as exc:
            result.issue('invalid_export_timecode', str(exc),
                         'member {}'.format(index), 'error')
            continue
        text.append('{:03d}  {:<8} V     C        {}'.format(
            index, reel, ' '.join(timecodes)))
        if info.get('motion'):
            result.issue('motion_effect_omitted',
                         'Original M2 motion command is retained in source '
                         'metadata but omitted from exported cuts.',
                         'member {}'.format(index))
        path = source.active_path()
        if path and not path.startswith('reel://'):
            text.append('* FROM CLIP NAME: {}'.format(path))
        for comment in info.get('comments') or []:
            if isinstance(comment, str) and comment.startswith('*'):
                text.append(comment)
        text.append('')
    result.text = '\n'.join(text).rstrip() + '\n'
    return result


def _otio_module(module):
    if module is not None:
        return module
    try:
        import opentimelineio as otio
    except ImportError:
        return None
    return otio


def _otio_rate(value):
    rate = _fps(str(value))
    for broadcast in _NTSC_RATES.values():
        if abs(float(rate - broadcast)) < 0.00001:
            return broadcast
    return rate


def _uri_path(url):
    """Convert a file URL into a local media path; leave remote URLs intact."""
    url = str(url or '')
    if not url.startswith('file://'):
        return url
    parts = urlsplit(url)
    path = unquote(parts.path)
    if parts.netloc not in ('', 'localhost'):
        return '//{}{}'.format(parts.netloc, path)
    return path


def _otio_frames(value, source_rate, destination_rate):
    """Convert RationalTime to frames without cumulative floating drift."""
    if value is None:
        return 0
    rate = _otio_rate(value.rate or source_rate)
    return int(round(Fraction(str(value.value)) * destination_rate / rate))


def import_otio(timeline_or_path, otio_module=None):
    """Import OTIO video clips, including media references and retime metadata.

    Multi-track effects, transitions and non-video tracks are reported as
    explicit lossy omissions; the original OTIO clip metadata is retained.
    """
    result = InterchangeResult()
    otio = _otio_module(otio_module)
    if otio is None:
        result.issue('otio_unavailable',
                     'OpenTimelineIO is not installed; install opentimelineio '
                     'to enable OTIO import/export.', severity='error')
        return result
    try:
        timeline = (otio.adapters.read_from_file(timeline_or_path)
                    if isinstance(timeline_or_path, str) and
                    os.path.exists(timeline_or_path) else
                    otio.adapters.read_from_string(timeline_or_path, 'otio_json')
                    if isinstance(timeline_or_path, str) else timeline_or_path)
        if timeline is None or not hasattr(timeline, 'tracks'):
            raise ValueError('Input is not an OTIO Timeline.')
    except Exception as exc:
        result.issue('invalid_otio', str(exc), severity='error')
        return result
    result.timeline = timeline
    title = getattr(timeline, 'name', '') or 'Imported OTIO'
    tracks = list(timeline.tracks)
    video = [t for t in tracks if str(getattr(t, 'kind', '')).lower().endswith('video')]
    if not video and tracks:
        video = tracks[:1]
    if len(video) > 1:
        result.issue('unsupported_tracks',
                     '{} video tracks found; only the first is imported.'.format(len(video)))
    for track in tracks:
        if track not in video:
            result.issue('unsupported_track',
                         'Track {} was not flattened into the review playlist.'.format(
                             getattr(track, 'name', 'unnamed')))
    track = video[0] if video else []
    timeline_metadata = dict(getattr(timeline, 'metadata', {}) or {})
    vendor = timeline_metadata.get('FrameReviewer', {})
    origin = int(vendor.get('record_origin_frame', 0))
    try:
        rate_data = vendor.get('fps_rational')
        if rate_data is None:
            for item in video[0] if video else ():
                if hasattr(item, 'source_range') and item.source_range is not None:
                    rate_data = item.source_range.start_time.rate
                    break
        rate = _otio_rate(rate_data or 24)
    except (ValueError, ZeroDivisionError):
        rate = Fraction(24)
    sources, members = [], []
    used_source_ids = set()
    cursor = 0
    for index, item in enumerate(track):
        type_name = type(item).__name__.lower()
        position = 'OTIO item {}'.format(index + 1)
        if 'transition' in type_name:
            result.issue('unsupported_transition',
                         'OTIO transition {} omitted.'.format(
                             getattr(item, 'name', type_name)), position)
            continue
        if 'gap' in type_name:
            duration = getattr(item, 'duration', lambda: None)()
            skipped = max(0, _otio_frames(duration, rate, rate))
            cursor += skipped
            result.issue('record_gap', '{}-frame OTIO Gap retained as offset '
                         '(the current sequence player concatenates clips).'.format(
                             skipped), position)
            continue
        if 'clip' not in type_name:
            result.issue('unsupported_item', 'OTIO {} omitted.'.format(type_name),
                         position)
            continue
        media_reference = getattr(item, 'media_reference', None)
        url = (getattr(media_reference, 'target_url', None) or
                'reel://{}'.format(getattr(item, 'name', 'UNKNOWN')))
        path = _uri_path(url)
        if path.startswith('reel://'):
            result.issue('unresolved_media', 'OTIO clip has no target media URL.',
                         position)
        source_range = getattr(item, 'source_range', None)
        if source_range is None:
            source_range = getattr(media_reference, 'available_range', None)
        if source_range is None:
            result.issue('missing_source_range',
                         'OTIO clip has no source/available range.', position, 'error')
            continue
        start = source_range.start_time
        duration = source_range.duration
        source_rate = _otio_rate(start.rate)
        source_in = _otio_frames(start, source_rate, source_rate)
        source_count = max(1, _otio_frames(duration, source_rate, source_rate))
        otio_meta = dict(getattr(item, 'metadata', {}) or {})
        vendor_clip = dict(otio_meta.get('FrameReviewer', {}) or {})
        scalar = Fraction(1)
        freeze = False
        for effect in list(getattr(item, 'effects', []) or []):
            effect_name = type(effect).__name__.lower()
            if 'lineartimewarp' in effect_name:
                scalar *= Fraction(str(getattr(effect, 'time_scalar', 1)))
            elif 'freezeframe' in effect_name:
                freeze = True
            else:
                result.issue('unsupported_effect',
                             'OTIO effect {} preserved only in metadata.'.format(
                                 effect_name), position)
        if scalar <= 0:
            result.issue('unsupported_retime',
                         'Reverse/zero time warp is not supported by CompositionMember.',
                         position, 'error')
            continue
        # OTIO clip duration is source time adjusted for speed. Vendor metadata
        # is authoritative when the timeline previously came from FrameReviewer.
        native_length = max(1, round(Fraction(source_count, 1) * rate /
                                     (source_rate * scalar)))
        record_length = int(vendor_clip.get('record_duration') or native_length)
        if (not freeze and vendor_clip.get('record_duration') is not None and
                record_length != native_length):
            result.issue('unsupported_retime',
                         'Vendor record_duration {} disagrees with native OTIO '
                         'source_range/retime length {}; retaining the native '
                         'duration and refusing unverified delivery.'.format(
                             record_length, native_length), position)
            record_length = native_length
        record_offset = int(vendor_clip.get('session_offset', cursor))
        if (vendor_clip.get('session_offset') is not None and
                record_offset != cursor):
            result.issue('unsupported_retime',
                         'Vendor session_offset {} contradicts actual OTIO '
                         'sequential track position {}; retaining native '
                         'track order.'.format(record_offset, cursor), position)
            record_offset = cursor
        exact_proofs = {
            'source_fps_rational': [source_rate.numerator, source_rate.denominator],
            'source_start_frame': source_in,
            'source_duration_frames': source_count,
            'record_in_frame': record_offset + origin,
            'record_out_frame': record_offset + origin + record_length}
        for key, true_value in exact_proofs.items():
            if key in vendor_clip and vendor_clip[key] != true_value:
                result.issue('unsupported_retime',
                             'Vendor {} {} contradicts verified OTIO time '
                             'domain {}; delivery requires reconciliation.'.format(
                                 key, vendor_clip[key], true_value), position)
        if record_offset < cursor:
            result.issue('overlapping_edits',
                         'OTIO clip starts before preceding edit.', position)
        cursor = max(cursor, record_offset + record_length)
        reel = str(vendor_clip.get('reel') or getattr(item, 'name', 'AX'))
        old_interchange = dict(vendor_clip.get('source_interchange') or {})
        old_interchange.update({'otio_metadata': {
                                    k: v for k, v in otio_meta.items()
                                    if k != 'FrameReviewer'},
                                'media_url': url, 'freeze': freeze,
                                'linear_time_warp': str(scalar),
                                'drop_frame': bool(vendor_clip.get('drop_frame',
                                                    old_interchange.get('drop_frame', False)))})
        source = _source_for_edit(
            getattr(item, 'name', '') or reel, path, reel, source_in,
            source_in + source_count, source_rate, record_offset + origin,
            record_offset + origin + record_length, old_interchange)
        requested_id = vendor_clip.get('source_id') or source.id
        if requested_id in used_source_ids:
            # A Source is one edit occurrence; identical reels and media URLs
            # may repeat but sharing its mutable Session Source ID would make
            # both CompositionMembers address the *first* edit's trim/TC.
            old_interchange['logical_source_id'] = (
                vendor_clip.get('logical_source_id') or requested_id)
            old_interchange['occurrence_id'] = (
                vendor_clip.get('occurrence_id') or
                '{}-edit-{}'.format(requested_id, index + 1))
            source.metadata['interchange'].update(old_interchange)
            suffix = 2
            unique = '{}-occurrence-{}'.format(requested_id, suffix)
            while unique in used_source_ids:
                suffix += 1
                unique = '{}-occurrence-{}'.format(requested_id, suffix)
            result.issue('duplicate_source_id',
                         'Repeated vendor Source ID {} split into unique edit '
                         'occurrence {} (logical identity retained).'.format(
                             requested_id, unique), position)
            requested_id = unique
        source.id = requested_id
        used_source_ids.add(source.id)
        playback_rate = Fraction(source_rate, rate) * scalar
        member = _member(source, record_length, record_offset,
                         source_count, playback_rate)
        if freeze:
            member.trim_out = 0
            member.rate = Fraction(1)
        sources.append(source)
        members.append(member)
    result.session, result.composition = _wrap_session(
        title, rate, sources, members, 'otio', bool(vendor.get('drop_frame', False)))
    if vendor.get('session_id'):
        result.session.id = vendor['session_id']
    result.session.metadata['interchange']['record_origin_frame'] = origin
    result.session.metadata['interchange']['otio_metadata'] = timeline_metadata
    return result


def export_otio(session, composition=None, fps=None, otio_module=None):
    """Export a Timeline via optional OpenTimelineIO, without importing Qt."""
    result = InterchangeResult(session=session)
    otio = _otio_module(otio_module)
    if otio is None:
        result.issue('otio_unavailable',
                     'OpenTimelineIO is not installed; install opentimelineio '
                     'to enable OTIO import/export.', severity='error')
        return result
    composition = _composition_or_session(session, composition)
    result.composition = composition
    _check_record_audio_interchange(session, composition, result, 'OTIO')
    rate = _session_rate(session, fps)
    if composition.mode != SEQUENCE:
        result.issue('unsupported_composition',
                     'Only the ordered clip sequence can be exported as one OTIO track.')
    timeline = otio.schema.Timeline(name=session.name or 'FrameReviewer')
    track = otio.schema.Track(name='Video', kind=otio.schema.TrackKind.Video)
    timeline.tracks.append(track)
    timeline.metadata['FrameReviewer'] = {
        'fps_rational': [rate.numerator, rate.denominator],
        'session_id': session.id,
        'record_origin_frame': session.metadata.get('interchange', {}).get(
            'record_origin_frame', 0),
        'drop_frame': session.metadata.get('interchange', {}).get('drop_frame', False)}
    for key, value in session.metadata.get('interchange', {}).get('otio_metadata', {}).items():
        if key != 'FrameReviewer':
            timeline.metadata[key] = value
    cursor = 0
    for index, member in enumerate(composition.members, 1):
        source = session.source(member.source_id)
        if source is None:
            result.issue('missing_source', 'Source ID {} is missing.'.format(
                member.source_id), 'member {}'.format(index), 'error')
            continue
        record_offset = (int(member.session_offset) if index == 1 or
                         member.session_offset else cursor)
        if record_offset > cursor:
            duration = otio.opentime.RationalTime(record_offset - cursor, float(rate))
            track.append(otio.schema.Gap(source_range=otio.opentime.TimeRange(
                otio.opentime.RationalTime(0, float(rate)), duration)))
        elif record_offset < cursor:
            result.issue('overlapping_edits',
                         'OTIO sequential track cannot preserve overlapping edit.',
                         'member {}'.format(index))
        cursor = max(cursor, record_offset + member.frame_count)
        info = source.metadata.get('interchange', {})
        editorial_member = _editorial_export_member(
            session, composition, index - 1, member, result)
        source_rate = _rate_of(source, rate)
        start = (int(info.get('source_in_frame', source.start_frame)) +
                 editorial_member.source_in)
        native_rate = editorial_member.rate or Fraction(source_rate, rate)
        source_frames = max(1, (Fraction(member.frame_count) * native_rate).__ceil__())
        scalar = ((editorial_member.rate or Fraction(source_rate, rate)) /
                  Fraction(source_rate, rate))
        if info.get('freeze'):
            scalar = Fraction(1)
            source_frames = 1
        clip = otio.schema.Clip(name=source.name)
        path = source.active_path()
        if path and not path.startswith('reel://'):
            clip.media_reference = otio.schema.ExternalReference(
                target_url=info.get('media_url') or path)
        else:
            result.issue('unresolved_media',
                         'OTIO clip has no resolved path.', 'member {}'.format(index))
        clip.source_range = otio.opentime.TimeRange(
            otio.opentime.RationalTime(start, float(source_rate)),
            otio.opentime.RationalTime(source_frames, float(source_rate)))
        clip.metadata['FrameReviewer'] = {
            'source_id': source.id, 'reel': info.get('reel', source.name),
            'record_duration': member.frame_count,
            'session_offset': record_offset,
            'occurrence_id': info.get('occurrence_id', source.id),
            'logical_source_id': info.get('logical_source_id', source.id),
            'source_fps_rational': [source_rate.numerator, source_rate.denominator],
            'source_start_frame': start,
            'source_duration_frames': source_frames,
            'record_in_frame': record_offset + int(timeline.metadata['FrameReviewer']['record_origin_frame']),
            'record_out_frame': record_offset + int(timeline.metadata['FrameReviewer']['record_origin_frame']) + member.frame_count,
            'drop_frame': info.get('drop_frame', False),
            'source_interchange': dict(info)}
        if info.get('otio_metadata'):
            for key, value in info['otio_metadata'].items():
                if key != 'FrameReviewer':
                    clip.metadata[key] = value
        if info.get('freeze'):
            clip.effects.append(otio.schema.FreezeFrame())
        elif scalar != 1:
            clip.effects.append(otio.schema.LinearTimeWarp(time_scalar=float(scalar)))
        if info.get('transition') not in (None, 'C'):
            result.issue('unsupported_transition',
                         'Original transition retained as source metadata only.',
                         'member {}'.format(index))
        track.append(clip)
    result.timeline = timeline
    try:
        result.text = otio.adapters.write_to_string(timeline, 'otio_json')
    except (AttributeError, ValueError, RuntimeError) as exc:
        result.issue('otio_serialize_failed', str(exc), severity='error')
    return result
