# -*- coding: utf-8 -*-
"""Map editorial timecode frames to actual decoded-media indices.

An EDL timecode (e.g. 01:00:10:00) is not the decoder index 864240.
MediaInfo's embedded first-frame timecode establishes that origin. Where the
origin is absent, leave the non-destructive editorial model unchanged and
report the ambiguity instead of seeking to a guessed source frame.
"""
from __future__ import absolute_import

from ..playback.composition import Composition
from .interchange import _fps, timecode_to_frames


def align_composition(session, composition, media_infos):
    """Return (new Composition, issues) using media-local trim coordinates.

    ``media_infos`` must align to composition members (a repeated Source may
    appear more than once). The source's original imported metadata is never
    mutated, so reopening the same edit cannot apply an offset twice.
    """
    copy = Composition.from_dict(composition.to_dict())
    issues = []
    if len(media_infos) != len(copy.members):
        issues.append('Media probe count does not match editorial edit occurrences; '
                      'alignment requires one source per record occurrence.')
        return copy, issues
    for index, (member, info) in enumerate(zip(copy.members, media_infos)):
        source = session.source(member.source_id)
        if source is None:
            continue
        editorial = source.metadata.get('interchange') or {}
        authored_in = editorial.get('source_in_frame')
        if authored_in is None:
            continue
        media_tc = getattr(info, 'timecode', None)
        frames = getattr(info, 'frame_numbers', None) if getattr(info, 'is_sequence', False) else None
        offset = None
        # Import uses an absolute editorial source clock; the media decoder
        # clock must have the SAME exact frame rate before translating TC to a
        # decode index. Compare rationals, never rounded 23.976 vs 24 floats.
        try:
            source_rate = _fps(source.timing.get('fps_rational') or
                               source.timing.get('fps'))
            media_rate = _fps(getattr(info, 'fps_rational', None) or
                              getattr(info, 'fps', None))
        except (ValueError, TypeError, OverflowError, ZeroDivisionError) as exc:
            issues.append('Clip {}: invalid frozen or probed frame rate ({}).'.format(
                index + 1, exc))
            continue
        if source_rate != media_rate:
            issues.append('Clip {}: source FPS {} differs from probed media FPS {}; '
                          'absolute source TC cannot be aligned without an '
                          'explicit conform.'.format(index + 1, source_rate, media_rate))
            continue
        if frames is not None:
            # Numbered image sequences establish their original frame identity
            # authoritatively. Never fall through to unrelated embedded TC
            # when that explicit frame number is not in the source index.
            if int(authored_in) not in frames:
                issues.append('Clip {}: editorial source frame {} is absent '
                              'from the verified sequence frame-number map.'
                              .format(index + 1, authored_in))
                continue
            if (len(frames) != len(set(frames)) or
                    len(frames) != int(getattr(info, 'frame_count', 0))):
                issues.append('Clip {}: sequence frame identities are ambiguous '
                              'or disagree with probed frame count.'.format(index + 1))
                continue
            offset = frames.index(int(authored_in))
        elif media_tc:
            try:
                offset = int(authored_in) - timecode_to_frames(media_tc, media_rate)
            except (TypeError, ValueError) as exc:
                issues.append('Clip {}: unparseable media timecode ({}).'.format(
                    index + 1, exc))
        elif int(authored_in) == 0:
            offset = 0
        else:
            issues.append('Clip {}: no embedded source timecode; editorial in-point '
                          'cannot be verified against media.'.format(index + 1))
        if offset is None:
            continue
        if offset < 0 or offset >= int(getattr(info, 'frame_count', 1)):
            issues.append('Clip {}: source in-point {} is outside decoded media.'.format(
                index + 1, offset))
            continue
        member.source_in += offset
        member.trim_in += offset
        member.trim_out += offset
        if member.trim_out >= int(info.frame_count):
            issues.append('Clip {}: edit extends beyond decoded media; final '
                          'frame will hold.'.format(index + 1))
    return copy, issues

