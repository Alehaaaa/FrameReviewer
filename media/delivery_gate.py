# -*- coding: utf-8 -*-
"""Fail-closed editorial provenance gate for precision record deliveries.

An imported EDL/OTIO can preview with approximations and informational
warnings. A managed render is a deliverable: silently treating a dissolve as
a cut or an absolute editorial timecode as decoder frame zero is not valid.
This validator is Qt-free, side-effect-free and does not mutate the imported
Session, Composition or decoded MediaInfo objects.
"""
from __future__ import absolute_import

from ..playback.composition import Composition, SEQUENCE
from .time_alignment import align_composition


_LOSSY_CODES = frozenset((
    'unsupported_transition', 'unsupported_tracks', 'unsupported_track',
    'unsupported_item', 'unsupported_effect', 'unsupported_retime',
    'overlapping_edits', 'invalid_edit_range', 'invalid_timecode',
    'missing_source_range', 'unsupported_drop_frame_rate',
    'motion_effect', 'freeze_frame', 'unrecognized_line', 'invalid_event',
))


def validate_record_delivery(session, composition, media_infos):
    """Return validated editorial lineage or raise a precise ValueError.

    ``media_infos`` are freshly probed and ordered by edit occurrence. For an
    imported composition, compare the artist's LIVE preview against a clean
    alignment of its SAVED unaligned editorial contract. This prevents stale
    source TC, relinks and imported approximations from being exported as
    verified frame-accurate record edits. Existing manual compositions with
    no imported editorial metadata remain subject to v4's independent strict
    rational/geometry/decoder validations.
    """
    if composition is None or composition.mode != SEQUENCE:
        raise ValueError('Record delivery requires the active Sequence timeline.')
    if len(composition.members) != len(media_infos):
        raise ValueError('Record delivery needs one probed media occurrence per edit.')
    metadata = session.metadata.get('interchange') or {}
    for issue in metadata.get('import_issues', ()):
        if (isinstance(issue, dict) and
                (issue.get('severity') == 'error' or
                 issue.get('code') in _LOSSY_CODES)):
            raise ValueError('Imported edit needs a lossless timeline before '
                             'managed delivery: {} ({})'.format(
                                 issue.get('code'), issue.get('message')))
    for index, member in enumerate(composition.members):
        source = session.source(member.source_id)
        if source is None:
            raise ValueError('Record edit {} has no matching logical Source ID.'
                             .format(index + 1))
        edit = source.metadata.get('interchange') or {}
        if edit.get('transition') not in (None, 'C'):
            raise ValueError('Record edit {} retains an unsupported editorial '
                             'transition; hard-cut delivery would be lossy.'
                             .format(index + 1))
        if edit.get('motion') or edit.get('freeze') or (
                edit.get('linear_time_warp') not in (None, '1', '1/1')):
            raise ValueError('Record edit {} carries an editorial motion or '
                             'freeze effect that cannot be rendered faithfully.'
                             .format(index + 1))

    saved = session.view.get('interchange_composition')
    if not isinstance(saved, dict):
        if metadata.get('format') in ('cmx3600', 'otio'):
            raise ValueError('Imported record timeline is missing its original '
                             'edit/trim contract; refusing unverifiable delivery.')
        return composition
    try:
        original = Composition.from_dict(saved)
    except (TypeError, ValueError, KeyError) as exc:
        raise ValueError('Saved editorial composition is invalid: {}'.format(exc))
    if (original.mode != SEQUENCE or
            [m.source_id for m in original.members] !=
            [m.source_id for m in composition.members]):
        raise ValueError('Saved edit identities/order no longer match the active '
                         'record timeline; reload and verify the intended edit.')

    aligned, issues = align_composition(session, original, media_infos)
    if issues:
        raise ValueError('Record source in/out cannot be proven against media: '
                         + '; '.join(issues))
    for index, (live, verified) in enumerate(zip(composition.members,
                                                 aligned.members)):
        # The backend v4 independently checks limits, holds, input SHA and
        # output-time maps. Here we require source TC to resolve to the exact
        # same preview occurrence indices, never inferred from a filename.
        fields = ('source_in', 'trim_in', 'trim_out', 'session_offset',
                  'frame_count', 'rate', 'hold')
        if any(getattr(live, name) != getattr(verified, name)
               for name in fields):
            raise ValueError('Record edit {} does not match the verified '
                             'editorial source timecode/trim; reload/relink '
                             'before managed delivery.'.format(index + 1))
    return composition
