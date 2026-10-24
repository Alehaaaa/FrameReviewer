# -*- coding: utf-8 -*-
"""Software multi-source frame source for grid / difference composition.

A drop-in for :class:`~framereviewer.playback.frames.SequenceSource` (same
signals and ``get``/``ensure``/cache interface) that, for a given session frame,
reads the matching frame from every member clip and composites them into one
image via the NumPy :mod:`~framereviewer.viewer.compositor`. This is the
software path that makes grid and difference actually render today; the GPU
viewer will later do the same combine on the GPU.

Members may be image sequences or movies. Grid/difference maps the same session
frame into every member and holds shorter clips on their last frame; sequence
uses concatenated indices for legacy lists or absolute record positions for
imported EDL/OTIO compositions. Record gaps render black, and every edit uses
the one global session frame clock (there is no per-clip playhead).
Frames are decoded at the cache policy's display width.
"""
from __future__ import absolute_import

import collections
import math

from ..core.qtcompat import QtCore, QtGui, Signal
from .cache_policy import CachePolicy, CACHE_OFF
from .frames import SequenceSource
from .rolling_cache import VideoFrameSource
from .composition import (SEQUENCE, GRID, DIFFERENCE, INVERTED_DIFFERENCE,
                          A_B, BLEND, WIPE, SPLIT, CHECKER, OVER, FLICKER,
                          TWO_SOURCE_MODES, Composition, CompositionMember,
                          validate_mode)


def _qimage_to_array(image):
    import numpy as np
    img = image.convertToFormat(QtGui.QImage.Format_RGBA8888)
    w, h = img.width(), img.height()
    ptr = img.constBits()
    try:
        ptr.setsize(h * w * 4)
    except AttributeError:
        pass
    return np.frombuffer(bytes(ptr), dtype=np.uint8).reshape((h, w, 4)).copy()


def _array_to_qimage(arr):
    import numpy as np
    arr = np.ascontiguousarray(arr, dtype=np.uint8)
    h, w = arr.shape[:2]
    return QtGui.QImage(arr.data, w, h, w * 4,
                        QtGui.QImage.Format_RGBA8888).copy()


class CompositeSource(QtCore.QObject):
    """Composites member frames per session frame (grid / difference modes)."""

    frameAvailable = Signal(int)

    @staticmethod
    def _declared_alpha(member, source_index, profiles):
        """Only a Source-authored/explicit alpha contract can override EXR.

        MediaInfo.alpha_mode on a probed OpenEXR is *inferred* from RGBA
        channels, not independently proven premultiplication. Do not pass
        that ambiguous value into a native sample decoder automatically.
        A per-source session color profile, an explicit member.color, or
        metadata with an explicit confirmation flag can own the association.
        """
        available = ('opaque', 'straight', 'premultiplied')
        declared = None
        candidates = (source_index, str(source_index),
                      getattr(member, 'source_id', None),
                      getattr(member, 'path', None))
        for key in candidates:
            if key in profiles:
                entry = profiles[key]
                if isinstance(entry, dict) and 'alpha_mode' in entry:
                    declared = entry['alpha_mode']
                    break
        if declared is None:
            local = getattr(member, 'color', None)
            if isinstance(local, dict):
                declared = local.get('alpha_mode')
        if declared is None:
            metadata = getattr(member, 'metadata', None)
            if isinstance(metadata, dict) and (
                    metadata.get('alpha_mode_explicit') is True or
                    metadata.get('alpha_association_confirmed') is True):
                declared = metadata.get('alpha_mode')
        if declared is not None and declared not in available:
            raise ValueError('Explicit Source alpha association must be opaque, straight or premultiplied.')
        return declared
    cacheChanged = Signal(object, object)

    @staticmethod
    def _fps_fraction(value):
        """Keep native rational frame rates exact, even in headless contexts."""
        from fractions import Fraction
        if isinstance(value, (list, tuple)) and len(value) == 2:
            return Fraction(int(value[0]), int(value[1]))
        return Fraction(str(value))

    def __init__(self, members, mode, parent=None, cache_policy=None,
                 ffmpeg=None, composition=None, parameters=None,
                 session_fps=None, color_states_by_source=None,
                 integrity_proof=False, record_preview_v4=False,
                 record_occurrence_ids=None):
        QtCore.QObject.__init__(self, parent)
        self._mode = validate_mode(mode)
        self._policy = cache_policy or CachePolicy()
        self._cache = collections.OrderedDict()
        self._precision_cache = {}    # same bounded lifetime as display QPixmaps
        self._precision_status = {}
        self._color_state = None      # optional shared working/display OCIO state
        self._source_color_states = dict(color_states_by_source or {})
        # Optional strongest possible preview/export provenance. Normal
        # playback and legacy adapters keep their no-hash performance path.
        self._integrity_proof = bool(integrity_proof)
        self._integrity_blocked = False
        self._integrity_verifying = False
        self._media_infos = []  # original Source metadata, not occurrence clones
        self._sources = []
        self._counts = []
        self._member_fps = []
        self._member_color_states = []
        self._member_geometries = []  # original native (w, h, PAR), not preview
        self._requested = set()
        self._last_frame = None
        self._inspection_presented = None  # exact SEQUENCE get(frame) pixmap only
        self._closed = False
        self._record_preview_v4 = bool(record_preview_v4)
        self._record_render_revision = 0
        self._session_fps = self._fps_fraction(session_fps or
                                  (getattr(members[0], 'fps', None)
                                   if members else None) or 24)
        self._blank_size = (max(1, int(getattr(members[0], 'width', 1))),
                            max(1, int(getattr(members[0], 'height', 1)))) if members else (1, 1)
        for member in members:
            if hasattr(member, 'is_sequence'):
                count = max(1, int(member.frame_count))
                if member.is_sequence:
                    identity = ({'source_id': member.source_id}
                                if getattr(member, 'source_id', '') else {})
                    if self._integrity_proof:
                        identity['integrity_proof'] = True
                    association = self._declared_alpha(
                        member, len(self._sources), self._source_color_states)
                    if association is not None:
                        identity['alpha_mode'] = association
                    if (isinstance(getattr(member, 'metadata', None), dict) and
                            member.metadata.get('exr_selection_explicit')):
                        identity.update({key: member.metadata[key]
                                         for key in ('exr_layer', 'exr_part')
                                         if member.metadata.get(key) is not None})
                    if (isinstance(getattr(member, 'metadata', None), dict) and
                            member.metadata.get('exr_display_canvas_rectified')):
                        identity['exr_display_canvas'] = True
                        identity['exr_canvas_contract'] = {
                            'display_window': member.metadata['display_window'],
                            'pixel_aspect': member.pixel_aspect_ratio}
                    if getattr(member, 'missing_policy', 'skip') == 'skip':
                        # A dense cache index is NOT the signed source label.
                        # Preserve it for PNG/TIFF as well as EXR (the EXR
                        # filename heuristic cannot identify those formats).
                        labels = getattr(member, 'sequence_source_frames', None)
                        if labels is not None and len(labels) != len(member.sequence_files):
                            raise ValueError('Sequence source labels do not match files.')
                        if labels is not None:
                            identity['source_frames'] = labels
                        source = SequenceSource(
                            member.sequence_files, self, self._policy, **identity)
                    else:
                        source = SequenceSource(
                            member.sequence_files, self, self._policy,
                            source_frames=member.sequence_source_frames,
                            include_holes=True, missing_policy=member.missing_policy,
                            declared_frames=member.frame_numbers,
                            source_size=(member.width, member.height), **identity)
                elif ffmpeg:
                    source = VideoFrameSource(
                        ffmpeg, member.path, member.fps, count, self,
                        self._policy, precision={
                            'source_id': getattr(member, 'source_id', '') or member.path,
                            'fps_rational': getattr(member, 'fps_rational', None),
                            'width': member.width, 'height': member.height,
                            'pixel_aspect_ratio': getattr(
                                member, 'pixel_aspect_ratio', 1.0),
                            'bit_depth': getattr(member, 'bit_depth', None),
                            'color_tags': getattr(member, 'color_tags', {}),
                        })
                else:
                    continue
            else:
                files = list(member or [])
                if not files:
                    continue
                count = len(files)
                source = SequenceSource(
                    files, self, self._policy,
                    **({'integrity_proof': True} if self._integrity_proof else {}))
            member_index = len(self._sources)
            source.frameAvailable.connect(
                lambda frame, index=member_index:
                    self._member_frame_available(index, frame))
            source.cacheChanged.connect(self._member_cache_changed)
            self._sources.append(source)
            self._media_infos.append(member)
            self._counts.append(count)
            if hasattr(member, 'width') and hasattr(member, 'height'):
                self._member_geometries.append((
                    int(member.width), int(member.height),
                    float(getattr(member, 'pixel_aspect_ratio', 1.0))))
            else:
                self._member_geometries.append(None)
            fps_rational = getattr(member, 'fps_rational', None)
            self._member_fps.append(self._fps_fraction(
                fps_rational or getattr(member, 'fps', None) or self._session_fps))
            member_color = getattr(member, 'color', None)
            self._member_color_states.append(
                dict(member_color) if isinstance(member_color, dict) else None)
        self.composition = composition or Composition(
            self._mode, [CompositionMember(str(i), count)
                         for i, count in enumerate(self._counts)],
            parameters=parameters)
        if parameters and composition is not None:
            self.composition.parameters.update(parameters)
        self._mode = self.composition.mode
        # Decoder objects are unique media; composition members are timeline
        # occurrences. A single decoder may back several edits of the same
        # source_id, avoiding duplicate FFmpeg workers and competing playheads.
        if len(self.composition.members) == len(self._sources):
            self._occurrence_sources = list(range(len(self._sources)))
        elif self.composition.record_timeline and self._sources:
            first_occurrences = list(dict.fromkeys(
                m.source_id for m in self.composition.members))
            if len(first_occurrences) != len(self._sources):
                raise ValueError('Expected one media info per edit or per unique '
                                 'composition source_id in first-use order.')
            indexes = {identity: i for i, identity in enumerate(first_occurrences)}
            self._occurrence_sources = [indexes[m.source_id]
                                        for m in self.composition.members]
        else:
            self._occurrence_sources = list(range(len(self._sources)))
        self._count = self._timeline_length()
        self._record_occurrence_ids = None
        if self._record_preview_v4:
            occurrences = self.composition.members
            ids = record_occurrence_ids
            if (self._mode != SEQUENCE or not self.composition.record_timeline or
                    not 2 <= len(occurrences) <= 8 or self._count > 120 or
                    not isinstance(ids, (tuple, list)) or
                    len(ids) != len(occurrences) or
                    any(not isinstance(value, str) or not value or
                        len(value) > 128 for value in ids) or
                    len(set(ids)) != len(ids)):
                raise ValueError('v4 record preview requires a bounded SEQUENCE '
                                 'record timeline and distinct frozen occurrence IDs.')
            last_end = 0
            reference = self._member_geometries[0]
            for index, cut in enumerate(occurrences):
                decoder_index = self._source_index(index)
                media = self._media_infos[decoder_index]
                geometry = self._member_geometries[decoder_index]
                if (cut.session_offset < last_end or cut.source_in < cut.trim_in or
                        cut.source_in > cut.trim_out or
                        cut.rate is None or
                        cut.rate != self._member_fps[decoder_index] / self._session_fps or
                        not getattr(media, 'source_id', '') == cut.source_id or
                        geometry is None or reference is None or
                        geometry[:2] != reference[:2] or
                        abs(geometry[2] - 1.0) > 1e-6):
                    raise ValueError('v4 record preview requires nonoverlapping '
                                     'square-pixel cuts, matching original Source IDs, '
                                     'output size and exact rational source rate.')
                last_end = cut.session_offset + cut.frame_count
            self._record_occurrence_ids = tuple(ids)

    def _timeline_length(self):
        if self._mode == SEQUENCE:
            if self.composition.record_timeline:
                return self.composition.total_frames()
            return sum(self._counts) or 1
        durations = []
        for index, count in enumerate(self._counts):
            member = self.composition.members[index]
            rate = (float(member.rate) if member.rate is not None else
                    self._member_fps[index] / self._session_fps)
            durations.append(member.session_offset + max(1, int(math.ceil(
                (member.trim_out - member.source_in + 1) / rate))))
        return max(1, max(durations or [1]))

    def set_comparison(self, mode=None, **parameters):
        """Change mode/parameters in place while preserving warm member decoders.

        Parameters: active (0 or 1), blend and wipe (0..1), orientation
        ('vertical'|'horizontal'), checker_size (pixels), flicker_frames.
        """
        if mode is not None:
            self._mode = self.composition.mode = validate_mode(mode)
        self.composition.parameters.update(parameters)
        self._record_render_revision += 1
        self._count = self._timeline_length()
        self._cache.clear()
        self._precision_cache.clear()
        self._precision_status.clear()
        self._inspection_presented = None
        self.cacheChanged.emit([], None)
        if self._last_frame is not None:
            self.frameAvailable.emit(self._last_frame)

    def _member_frame(self, member_index, session_frame):
        if self._mode == SEQUENCE and not self.composition.record_timeline:
            _, session_frame = self._sequence_location(session_frame)
        decoder_index = self._source_index(member_index)
        return self.composition.map_frame(member_index, session_frame,
                                          self._session_fps,
                                          self._member_fps[decoder_index])

    def _source_index(self, member_index):
        return (self._occurrence_sources[member_index]
                if self._mode == SEQUENCE else member_index)

    def _required_members(self, frame):
        if self._mode == SEQUENCE:
            if self.composition.record_timeline and not 0 <= int(frame) < self._count:
                return []
            index, _ = self._sequence_location(frame)
            return [] if index is None else [index]
        if self._mode in TWO_SOURCE_MODES:
            # Keep A and B warm even when only one is currently visible.
            return list(range(min(2, len(self._sources))))
        return list(range(len(self._sources)))

    def _shown_member(self, frame):
        if self._mode == A_B:
            return min(1, max(0, int(self.composition.parameters.get('active', 0))))
        period = max(1, int(self.composition.parameters.get('flicker_frames', 1)))
        return (int(frame) // period) % 2

    def selected_reference_field_supported(self):
        """Whether differing native geometry needs A/B/Flicker physical preview.

        This checks *geometry capability*, not sample readiness or an export
        promise. The actual selected frame must be decoded and rectified before
        ``selected_reference_field_ready`` can grant a pixel proof.
        Equal native (width, height, PAR) A/B and SEQUENCE intentionally keep
        their historic native source payload/pixmap path. Equal PAR alone is
        insufficient: different dimensions occupy different physical fields.
        """
        if self._closed or self._mode not in (A_B, FLICKER) or len(self._sources) < 2:
            return False
        if len(self._member_geometries) < 2:
            return False
        ref, other = self._member_geometries[:2]
        if ref is None or other is None:
            return False
        if (not all(math.isfinite(float(n)) and float(n) > 0
                    for n in tuple(ref) + tuple(other)) or
                not all(1.0 / 64 <= float(n[2]) <= 64.0 for n in (ref, other)) or
                (abs(float(ref[0]) - float(other[0])) <= 1e-8 and
                 abs(float(ref[1]) - float(other[1])) <= 1e-8 and
                 abs(float(ref[2]) - float(other[2])) <= 1e-8)):
            return False
        physical_width = int(math.floor(ref[0] * ref[2] + .5))
        physical_height = int(ref[1])
        return (0 < physical_width <= 32768 and
                0 < physical_height <= 32768 and
                physical_width * physical_height <= 16 * 1024 * 1024)

    def selected_reference_field_ready(self, frame=None):
        """Truthful gate: the CURRENT selected preview is cached Working float.

        A scaled preview remains useful, but does NOT certify exact native
        output dimensions for managed Working export. Only an actual cached
        full-reference physical raster returns True. Never calls a decoder.
        """
        if not self.selected_reference_field_supported():
            return False
        if frame is None:
            frame = self._last_frame
        if frame is None:
            return False
        payload = self._precision_cache.get(int(frame))
        display = self._cache.get(int(frame))
        if payload is None or display is None or display.isNull():
            return False
        meta = payload.color_metadata
        ref = self._member_geometries[0]
        shape = (payload.pixels.shape[1], payload.pixels.shape[0])
        if not (meta.get('selected_reference_field') and
                meta.get('selected_member_rectified') is True and
                meta.get('selected_member_index') == self._shown_member(int(frame)) and
                payload.stage == 'working' and payload.alpha_mode == 'straight' and
                str(payload.dtype) == 'float32' and
                shape == (display.width(), display.height()) ==
                (int(math.floor(ref[0] * ref[2] + .5)), int(ref[1]))):
            return False
        # A stale/torn selected cache entry must never unlock an export just
        # because its old QPixmap and metadata still exist after a source
        # eviction. Check the frozen native origin WITHOUT calling get/decode.
        selected = self._shown_member(int(frame))
        mapped = self._member_frame(selected, int(frame))
        if mapped is None:
            return False
        decoder = self._sources[self._source_index(selected)]
        original = self._cached_member_precision(decoder, mapped)
        if original is None and callable(getattr(decoder, 'frame_status', None)):
            original, _ = self._cached_held_precision(
                decoder, mapped, decoder.frame_status(mapped))
        lineage = meta.get('selected_original')
        # FramePayload recursively freezes dictionaries to MappingProxyType.
        return bool(original is not None and hasattr(lineage, 'get') and
                    lineage.get('source_id') == original.source_id and
                    lineage.get('source_frame') == original.source_frame and
                    lineage.get('generation') == original.generation and
                    lineage.get('source_alpha_mode') == original.alpha_mode)

    @property
    def selected_physical_parity(self):
        """Full-native Working reference field is *present*, never guessed."""
        return self.selected_reference_field_ready()

    def verified_selected_file_proof(self, frame):
        """Read-only SHA proof for the ACTUALLY selected original EXR frame.

        A QPixmap/framePresented receipt alone cannot establish that its
        decoded Source bytes still equal the disk file which a queued export
        would freeze. This bounded opt-in check never redecodes or seeks. It
        verifies the cached original signed frame, including an explicit Hold
        origin, rather than inventing file provenance for a missing label.
        Returns a copy of the original file proof plus the distinct requested
        label and selected logical source ID, or None (fail closed).
        """
        if (self._closed or not self._integrity_proof or
                self._integrity_blocked or self._integrity_verifying or
                self._mode not in (A_B, FLICKER) or
                not self.selected_reference_field_ready(frame)):
            return None
        frame = int(frame)
        selected = self._shown_member(frame)
        mapped = self._member_frame(selected, frame)
        if mapped is None:
            return None
        reader = self._sources[self._source_index(selected)]
        if not isinstance(reader, SequenceSource):
            return None
        requested = reader.frame_status(mapped)
        if requested.get('kind') == 'available':
            original_index = mapped
            original = self._cached_member_precision(reader, mapped)
        elif requested.get('kind') == 'hole':
            original, original_index = self._cached_held_precision(
                reader, mapped, requested)
        else:
            return None
        if original is None or original_index is None:
            return None
        cached_proof = reader.cached_file_proof(original_index)
        if (cached_proof is None or
                cached_proof.get('source_frame') != original.source_frame):
            # A fast metadata/ctime guard may invalidate and asynchronously
            # repopulate the reader *before* expensive SHA verification is
            # reached. Losing proof for an otherwise full-native currently
            # presented original is itself a content-integrity violation, not
            # an invitation to bless the new cached bytes automatically.
            self._integrity_blocked = True
            return None
        # A changed original may synchronously trigger an already connected
        # Qt cacheChanged/frameAvailable callback which REDECODES the new
        # bytes. That is not an artist approval to replace the frozen review
        # plate. Keep the entire present composition export-blocked until an
        # explicit source/recomposition reload creates a new decoder identity;
        # do not silently certify those new pixels under the old Source review.
        self._integrity_verifying = True
        try:
            if not reader.verify_cached_file_proof(original_index):
                self._integrity_blocked = True
                return None
        finally:
            self._integrity_verifying = False
        # Verification itself can synchronously invalidate composite pixels
        # via Qt cacheChanged. Never return a proof of such a torn presentation.
        if not self.selected_reference_field_ready(frame):
            return None
        return dict(cached_proof,
                    selected_member_index=selected,
                    member_source_id=self.composition.members[selected].source_id,
                    requested_source_frame=reader.source_frame_for_index(mapped),
                    held_from_source_frame=(original.source_frame
                                            if mapped != original_index else None))

    def verified_record_file_proof(self, frame):
        """Read-only SHA integrity of the currently cached v4 picture source.

        A managed v4 record export freezes bytes on disk independently. If an
        original EXR changes after its Working preview was presented, silently
        endorsing a new render as the reviewed plate would violate parity.
        This only certifies the one genuine source-backed cached record frame,
        not every future frame in the delivery range. No decoder/reseek occurs.
        Missing record gaps, black/checker slates, display-only fallbacks and
        unverified source geometry cannot acquire original HDR provenance.
        """
        if (self._closed or not self._record_preview_v4 or
                not self._integrity_proof or self._integrity_blocked or
                self._integrity_verifying or self._mode != SEQUENCE):
            return None
        frame = int(frame)
        rendered = self._precision_cache.get(frame)
        pixmap = self._cache.get(frame)
        if (rendered is None or rendered.stage != 'working' or
                pixmap is None or pixmap.isNull() or
                rendered.color_metadata.get('preview_downsampled')):
            return None
        address = self.record_frame_identity(frame)
        occurrence = address.get('occurrence_index')
        mapped = address.get('decode_index')
        if (type(occurrence) is not int or type(mapped) is not int or
                not 0 <= occurrence < len(self.composition.members)):
            return None
        reader = self._sources[self._source_index(occurrence)]
        if not isinstance(reader, SequenceSource):
            return None
        state = reader.frame_status(mapped)
        if state.get('kind') == 'available':
            original_index = mapped
            original = self._cached_member_precision(reader, mapped)
        elif state.get('kind') == 'hole':
            original, original_index = self._cached_held_precision(
                reader, mapped, state)
        else:
            return None
        if (original is None or original_index is None or
                rendered.source_id != original.source_id or
                rendered.source_frame != original.source_frame or
                rendered.generation != original.generation or
                rendered.color_metadata.get('record_occurrence_id') !=
                self._record_occurrence_ids[occurrence] or
                rendered.color_metadata.get('source_alpha_mode') !=
                original.alpha_mode):
            return None
        cached = reader.cached_file_proof(original_index)
        if (cached is None or cached.get('source_frame') !=
                original.source_frame):
            self._integrity_blocked = True
            return None
        self._integrity_verifying = True
        try:
            if not reader.verify_cached_file_proof(original_index):
                self._integrity_blocked = True
                return None
        finally:
            self._integrity_verifying = False
        if (self._closed or self._precision_cache.get(frame) is not rendered or
                self._cache.get(frame) is not pixmap):
            return None
        return dict(cached,
                    record_frame=frame, occurrence_index=occurrence,
                    occurrence_id=self._record_occurrence_ids[occurrence],
                    member_source_id=self.composition.members[occurrence].source_id,
                    requested_source_frame=reader.source_frame_for_index(mapped),
                    held_from_source_frame=(original.source_frame
                                            if original_index != mapped else None))

    def selected_reference_field_geometry(self, frame=None):
        """Read-only proven presentation contract, else None (never seek).

        ``reference_native`` is the unrotated Source A geometry (w,h,PAR);
        ``output_size`` is the actual square-pixel QPixmap/Working float size.
        ``reference_output_size`` is the physical native managed export size.
        ``full_native`` distinguishes an honestly downsampled preview.
        """
        if not self.selected_reference_field_supported():
            return None
        if frame is None:
            frame = self._last_frame
        if frame is None:
            return None
        result = self._precision_cache.get(int(frame))
        display = self._cache.get(int(frame))
        if result is None or display is None or not result.color_metadata.get(
                'selected_reference_field'):
            return None
        ref = self._member_geometries[0]
        return {'reference_native': tuple(ref),
                'reference_output_size': (int(math.floor(ref[0] * ref[2] + .5)),
                                          int(ref[1])),
                'output_size': (display.width(), display.height()),
                'output_pixel_aspect': 1.0,
                'selected_member_index': result.color_metadata['selected_member_index'],
                'stage': 'working',
                'full_native': self.selected_reference_field_ready(frame)}

    def _ensure_member(self, index, frame, direction=1):
        mapped = self._member_frame(index, frame)
        if mapped is not None:
            try:
                self._sources[self._source_index(index)].ensure(mapped, direction)
            except TypeError:
                self._sources[self._source_index(index)].ensure(mapped)

    @staticmethod
    def _blank(width, height, transparent=False):
        image = QtGui.QImage(max(1, width), max(1, height),
                             QtGui.QImage.Format_RGBA8888)
        image.fill(QtGui.QColor(0, 0, 0, 0 if transparent else 255))
        return QtGui.QPixmap.fromImage(image)

    def _record_v4_blank(self):
        """A bounded transparent record raster, matching preview resolution."""
        from ..viewer.color import MAX_PREVIEW_PIXELS
        width, height = self._blank_size
        scale = min(1.0, float(self._policy.decode_width) / width,
                    (float(MAX_PREVIEW_PIXELS) / (width * height)) ** .5)
        return self._blank(max(1, int(width * scale)),
                           max(1, int(height * scale)), transparent=True)

    @property
    def frame_count(self):
        return self._count

    def _qt_composite(self, images):
        """Dependency-free fallback for Maya installs without NumPy."""
        width, height = images[0].width(), images[0].height()
        canvas = QtGui.QImage(width, height, QtGui.QImage.Format_RGBA8888)
        canvas.fill(QtGui.QColor(0, 0, 0, 0 if self._mode == GRID else 255))
        painter = QtGui.QPainter(canvas)
        try:
            if self._mode == GRID:
                from .composition import Composition
                painter.setCompositionMode(QtGui.QPainter.CompositionMode_Source)
                for image, cell in zip(images, Composition('grid', [object()] * len(images)).grid_cells()):
                    x, y, w, h = cell
                    x0, y0 = int(round(x * width)), int(round(y * height))
                    x1 = int(round((x + w) * width))
                    y1 = int(round((y + h) * height))
                    if x1 > x0 and y1 > y0:
                        painter.drawImage(QtCore.QRect(x0, y0, x1 - x0, y1 - y0), image)
            elif len(images) > 1 and self._mode in (BLEND, WIPE, SPLIT,
                                                   CHECKER, OVER):
                a, b = images[:2]
                whole = QtCore.QRect(0, 0, width, height)
                params = self.composition.parameters
                if self._mode == OVER:
                    painter.drawImage(whole, b)
                    painter.drawImage(whole, a)
                else:
                    painter.drawImage(whole, a)
                    if self._mode == BLEND:
                        painter.setOpacity(max(0.0, min(1.0, float(
                            params.get('blend', 0.5)))))
                        painter.drawImage(whole, b)
                    elif self._mode in (WIPE, SPLIT):
                        fraction = max(0.0, min(1.0, float(
                            params.get('wipe', 0.5))))
                        if params.get('orientation') == 'horizontal':
                            rect = QtCore.QRect(0, int(height * fraction), width,
                                                height - int(height * fraction))
                        else:
                            rect = QtCore.QRect(int(width * fraction), 0,
                                                width - int(width * fraction), height)
                        painter.setClipRect(rect)
                        painter.drawImage(whole, b)
                    elif self._mode == CHECKER:
                        size = max(1, int(params.get('checker_size', 64)))
                        for y in range(0, height, size):
                            for x in range(0, width, size):
                                if ((x // size) + (y // size)) % 2:
                                    painter.setClipRect(QtCore.QRect(
                                        x, y, min(size, width - x),
                                        min(size, height - y)))
                                    painter.drawImage(whole, b)
            else:
                painter.drawImage(0, 0, images[0])
                if len(images) > 1:
                    mode = getattr(QtGui.QPainter, 'CompositionMode_Difference',
                                   QtGui.QPainter.CompositionMode_SourceOver)
                    painter.setCompositionMode(mode)
                    painter.drawImage(QtCore.QRect(0, 0, width, height), images[1])
        finally:
            painter.end()
        if self._mode == INVERTED_DIFFERENCE:
            inverted = canvas.copy()
            inverted.invertPixels()
            return QtGui.QPixmap.fromImage(inverted)
        return QtGui.QPixmap.fromImage(canvas)

    def _composite(self, pixmaps):
        images = [pixmap.toImage() for pixmap in pixmaps]
        try:
            from ..viewer import compositor
            import numpy  # noqa: F401
        except Exception:
            return self._qt_composite(images)
        arrays = [_qimage_to_array(img) for img in images]
        width = images[0].width()
        height = images[0].height()
        # The software compositor is the single pixel reference for both the
        # GPU fallback and live CompositeSource. QImage stores straight alpha,
        # while the compositing reference expects premultiplied Over operands.
        if self._mode == OVER and len(arrays) >= 2:
            import numpy as np
            arrays = [compositor.to_float_rgba(a) for a in arrays]
            for a in arrays:
                a[:, :, :3] *= a[:, :, 3:4]
        result = compositor.composite(
            self._mode, arrays, width=width, height=height,
            **self.composition.parameters)
        if self._mode == OVER and len(arrays) >= 2:
            alpha = result[:, :, 3:4]
            result[:, :, :3] /= np.maximum(alpha, 1e-8)
        return QtGui.QPixmap.fromImage(_array_to_qimage(compositor.to_uint8(result)))

    def set_color_state(self, state):
        """Optional OCIO/working/display configuration for precision comparisons.

        The returned payload remains working-stage; the primary surface must
        independently opt into the same color state to display configured OCIO
        output rather than its labelled unmanaged sRGB monitor fallback.
        """
        from ..viewer.color import ColorPipelineState
        self._color_state = ColorPipelineState.from_dict(state) if state else None
        self._record_render_revision += 1
        self._inspection_presented = None
        self._cache.clear()
        self._precision_cache.clear()
        self._precision_status.clear()
        self.cacheChanged.emit([], None)
        if self._last_frame is not None:
            self.frameAvailable.emit(self._last_frame)

    def set_source_color_states(self, color_states_by_source):
        """Configure independently authored per-Source input/alpha profiles.

        Keys may be a member index, CompositionMember.source_id, or the owned
        FramePayload.source_id (in that order of lookup). Per-source settings
        override source metadata for input/config/context and an EXPLICIT
        alpha association. A changed mode invalidates decoded SEQUENCE caches
        before re-presentation, so neither old premultiplied nor straight
        FramePayload pixels survive a new declared interpretation. It never
        guesses association from EXR channel names. The shared working space
        and one-time global grade remain unchanged.
        """
        requested = dict(color_states_by_source or {})
        associations = {}
        for index, source in enumerate(self._sources):
            if not hasattr(source, '_alpha_mode'):
                continue
            matching = []
            for occurrence, decoder_index in enumerate(self._occurrence_sources):
                if decoder_index != index:
                    continue
                member = self.composition.members[occurrence]
                for key in (occurrence, str(occurrence),
                            member.source_id, str(member.source_id)):
                    if key in requested and isinstance(requested[key], dict):
                        mode = requested[key].get('alpha_mode')
                        if mode is not None:
                            matching.append(mode)
            if len(set(matching)) > 1:
                raise ValueError('Repeated occurrences of one Source have conflicting alpha associations.')
            info = self._media_infos[index]
            associations[index] = (
                matching[0] if matching else
                self._declared_alpha(info, index, requested))
        # Validate ALL declarations before invalidating any live decoder.
        for alpha in associations.values():
            if alpha is not None and alpha not in ('straight', 'premultiplied', 'opaque'):
                raise ValueError('Invalid explicitly declared source alpha association.')
        changed_readers = []
        for index, alpha in associations.items():
            reader = self._sources[index]
            if reader._alpha_mode == alpha:
                continue
            reader._alpha_mode = alpha
            if hasattr(reader, '_cache') and hasattr(reader, '_forget'):
                for frame in tuple(reader._cache):
                    reader._forget(frame)
                changed_readers.append(reader)
        self._source_color_states = requested
        self._record_render_revision += 1
        self._cache.clear()
        self._precision_cache.clear()
        self._precision_status.clear()
        self._inspection_presented = None
        # Invalidate public comparison state FIRST, then emit reader changes:
        # synchronous Qt subscribers must not see old composited provenance
        # during a newly confirmed alpha interpretation.
        for reader in changed_readers:
            reader._notify_cache()
        self.cacheChanged.emit([], None)
        if self._last_frame is not None:
            self.frameAvailable.emit(self._last_frame)

    def enable_integrity_proof(self):
        """Switch a warm multi-source decoder to SHA-proven selected delivery.

        A/B or Flicker can be entered from an already decoded Blend/Grid
        without reopening media. Those old pixmaps were created without file
        SHA provenance. They MUST be discarded, never retroactively certified
        as identical to current disk bytes. Normal Blend/Grid keeps its
        original zero-hash throughput until the artist selects this stricter
        output capability. New frame presentation is required afterwards.
        """
        if self._closed or self._integrity_proof:
            return False
        self._integrity_proof = True
        self._record_render_revision += 1
        self._cache.clear()
        self._precision_cache.clear()
        self._precision_status.clear()
        self._inspection_presented = None
        for reader in self._sources:
            enable = getattr(reader, 'enable_integrity_proof', None)
            if callable(enable):
                enable()
        self.cacheChanged.emit([], None)
        return True

    def _source_color_state(self, index, original):
        member_id = getattr(self.composition.members[index], 'source_id', index)
        source_id = getattr(original, 'source_id', None)
        for key in (index, str(index), member_id, str(member_id),
                    source_id, str(source_id)):
            if key in self._source_color_states:
                return self._source_color_states[key]
        source_index = self._source_index(index)
        return (self._member_color_states[source_index]
                if source_index < len(self._member_color_states) else None)

    def get_display_status(self, frame):
        """Report actual comparison color fidelity and any explicit fallback."""
        frame = int(frame)
        if self._closed:
            return ''
        if self._mode == SEQUENCE:
            if not 0 <= frame < self._count:
                return 'Record frame outside the available timeline'
            member, _ = self._sequence_location(frame)
            if member is None:
                return ('Record gap: transparent black RGBA(0,0,0,0) '
                        '(no native source frame)' if self._record_preview_v4
                        else 'Record gap: opaque black (no source frame)')
            mapped = self._member_frame(member, frame)
            if mapped is None:
                return ('Record frame outside source trim: transparent black '
                        '(no native source sample)' if self._record_preview_v4
                        else 'Record frame outside source trim (black, no source sample)')
            if self._record_preview_v4 and frame in self._precision_status:
                return self._precision_status[frame]
            decoder = self._sources[self._source_index(member)]
            getter = getattr(decoder, 'get_display_status', None)
            quality = getter(mapped) if callable(getter) else ''
            if self.composition.record_timeline:
                identity = self.record_frame_identity(frame)
                if identity['kind'] == 'held_trim':
                    label = identity['source_frame']
                    return ('Record source trim held at {}'.format(label) +
                            ('; ' + quality if quality else ''))
            return quality or ''
        return self._precision_status.get(frame, '')

    def record_frame_identity(self, record_frame):
        """Read-only tooltip/transport proof for an EXACT record playhead frame.

        Provides occurrence *index*, Source ID, decoder index, actual signed
        source label, and existing decoder quality flags without fetching any
        media. Never use the signed label itself as a movie decode index.
        A track/record gap, trimmed-away sample, held trim and intentional
        sequence hole are all explicitly distinguishable.
        """
        if self._mode != SEQUENCE or not self.composition.record_timeline:
            raise ValueError('Record frame identity requires SEQUENCE record mode.')
        if self._closed:
            return {'kind': 'closed', 'record_frame': record_frame,
                    'occurrence_index': None, 'source_id': None,
                    'decode_index': None, 'source_frame': None}
        occurrence_rates = [self._member_fps[self._source_index(i)]
                            for i in range(len(self.composition.members))]
        result = self.composition.record_frame_address(
            record_frame, self._session_fps, occurrence_rates)
        row = dict(result, source_frame=None, displayed_source_frame=None,
                   source_kind=None, decoder_status=None)
        occurrence = row['occurrence_index']
        if self._record_occurrence_ids is not None:
            row['occurrence_id'] = (
                self._record_occurrence_ids[occurrence]
                if occurrence is not None else None)
        mapped = row['decode_index']
        if occurrence is None or mapped is None:
            return row
        index = self._source_index(occurrence)
        if not 0 <= index < len(self._sources) or \
                not 0 <= mapped < self._counts[index]:
            row.update(kind='outside_decoder', decode_index=None)
            return row
        source = self._sources[index]
        row['decoder_index'] = index
        label = getattr(source, 'source_frame_for_index', None)
        if callable(label):
            row['source_frame'] = label(mapped)
        else:
            # Movie and legacy dense frames: the exact FFmpeg decode index is
            # also the payload identity; no guessed file numbering.
            row['source_frame'] = mapped
        row['displayed_source_frame'] = row['source_frame']
        status = getattr(source, 'frame_status', None)
        if callable(status):
            state = status(mapped)
            if isinstance(state, dict):
                row['decoder_status'] = dict(state)
                row['source_kind'] = state.get('kind')
                if state.get('kind') == 'hole':
                    row['kind'] = 'source_hole'
                    row['displayed_source_frame'] = (
                        state.get('held_source_frame')
                        if state.get('policy') == 'hold' else None)
                elif state.get('kind') == 'decode_error':
                    row['kind'] = 'decode_error'
                    row['displayed_source_frame'] = None
        return row

    def member_payload(self, frame, member_index):
        """Return cached original native payload for an active member only.

        This read-only scopes/inspection contract does not request a decoder
        frame or contaminate the composition's working-stage payload. It also
        does not fabricate a source payload for Qt-only/missing/decode errors.
        """
        if self._closed or (self._mode == SEQUENCE and
                self.composition.record_timeline and
                not 0 <= int(frame) < self._count):
            return None
        index = int(member_index)
        if index not in self._required_members(int(frame)):
            return None
        mapped = self._member_frame(index, int(frame))
        if mapped is None:
            return None
        from ..core.frame_payload import FramePayload
        source = self._sources[self._source_index(index)]
        payload = self._cached_member_precision(source, mapped)
        if (payload is None and self._record_preview_v4 and
                callable(getattr(source, 'frame_status', None))):
            payload, _ = self._cached_held_precision(
                source, mapped, source.frame_status(mapped))
        return payload if isinstance(payload, FramePayload) else None

    @staticmethod
    def _cached_member_precision(source, index):
        """Read only native pixels already owned by decoder cache.

        SequenceSource.get_payload() can synchronously call get()/decode when
        a frame was evicted. Never call it for uncached frames, declared holes,
        or decoder failures; do not make a stale async precision request.
        VideoFrameSource.get_payload() is itself cache-only. Test doubles
        exposing get_payload() without a cache contract remain supported.
        """
        if (isinstance(source, SequenceSource) and
                hasattr(source, '_payload_cache') and
                hasattr(source, '_cache') and
                callable(getattr(source, 'frame_status', None))):
            status = source.frame_status(index)
            if status.get('kind') in ('hole', 'decode_error', 'out_of_range'):
                return None
            if index not in source._cache:
                return None
            return source._payload_cache.get(index)
        getter = getattr(source, 'get_payload', None)
        return getter(index) if callable(getter) else None

    @staticmethod
    def _cached_held_precision(source, index, status):
        """Resolve genuine cached hold-origin pixels, never a missing label.

        A SequenceSource hold returns the existing preceding QPixmap. It does
        not cache native pixels under the hole's signed source-frame number.
        We expose the original payload only when its PREVIOUS real index is
        still cached, the current frame status explicitly names that origin,
        and its immutable FramePayload identity agrees. No get/decode/seek.
        """
        if (not isinstance(status, dict) or status.get('kind') != 'hole' or
                status.get('policy') != 'hold' or
                type(status.get('held_source_frame')) is not int or
                not isinstance(source, SequenceSource) or
                not all(hasattr(source, attr) for attr in
                        ('_cache', '_payload_cache', '_previous_existing',
                         'source_frame_for_index', 'frame_status'))):
            return None, None
        held_index = source._previous_existing(index)
        if (held_index is None or held_index not in source._cache or
                source.frame_status(held_index).get('kind') != 'available' or
                source.source_frame_for_index(held_index) !=
                status['held_source_frame']):
            return None, None
        from ..core.frame_payload import FramePayload
        payload = source._payload_cache.get(held_index)
        if (not isinstance(payload, FramePayload) or
                payload.source_frame != status['held_source_frame']):
            return None, None
        return payload, held_index

    def member_color_state(self, member_index, payload=None):
        """Frozen-ish per-source input profile for independent member scopes."""
        return self._source_color_state(int(member_index), payload)

    def inspect_member_pixels(self, frame, uv, mode='all'):
        """Inspect original, cached member samples without decoding/re-seeking.

        Source pixels remain the original float precision and signed frame ID;
        they are NOT the graded composition or the 8-bit preview. For Grid the
        caller's normalized *composite canvas* position selects its actual
        stretched cell. Split also remaps the selected half to its member's
        native normalized position. For overlapping modes each participant is
        sampled at the same position, allowing artists to inspect A and B
        independently even though the display has one working-stage payload.

        mode='all' retains historical multi-member inspection; mode='visible'
        uses the EXACT composed pixel owner mask at the cached output raster
        (Wipe/Checker/Split/Grid/A-B/Flicker). Blend/Difference/Over preserve
        both mathematical operands. All returned rows
        include status (native_float, display_only, not_ready, outside_trim)
        rather than inventing original precision for a Qt-only decoder.
        """
        if self._closed or (self._mode == SEQUENCE and
                self.composition.record_timeline and
                not 0 <= int(frame) < self._count):
            return []
        if mode not in ('all', 'visible'):
            raise ValueError('Member inspection mode must be all or visible.')
        if not isinstance(uv, (tuple, list)) or len(uv) != 2:
            raise ValueError('Member inspection requires normalized (u, v).')
        u, v = float(uv[0]), float(uv[1])
        if not all(math.isfinite(x) and 0 <= x < 1 for x in (u, v)):
            return []
        frame = int(frame)
        indexes = self._required_members(frame)
        if not indexes:
            return []
        coordinates = {}
        visible_pixel = None
        if mode == 'visible':
            from ..viewer.geometry import comparison_pixel_owners
            # Never seek, decode or guess a mask dimension from source sizes.
            # Only a composed / presented cached frame establishes the actual
            # compositor raster. Missing/stale frames cannot own visible px.
            actual = (self._inspection_presented[1]
                      if self._mode == SEQUENCE and
                      self._inspection_presented is not None and
                      self._inspection_presented[0] == frame else
                      self._cache.get(frame))
            if actual is None or actual.isNull():
                return []
            args = dict(self.composition.parameters)
            if self._mode == SPLIT:
                # Native working compositor samples pixel centers; its Qt-only
                # fallback preserves the legacy direct split-resize contract.
                args['pixel_centers'] = frame in self._precision_cache
            if self._mode in (A_B, FLICKER):
                args['active'] = self._shown_member(frame)
            visible_pixel = comparison_pixel_owners(
                self._mode, (u, v), actual.width(), actual.height(),
                count=len(indexes), parameters=args,
                cells=self.composition.grid_cells() if self._mode == GRID else None)
            if visible_pixel is None:
                return []
            possible = tuple(indexes)
            indexes = [possible[i] for i, _ in visible_pixel[2]
                       if i < len(possible)]
            coordinates = {possible[i]: position for i, position in
                           visible_pixel[2] if i < len(possible)}
        elif self._mode == GRID:
            cells = self.composition.grid_cells()
            indexes = [i for i in indexes if i < len(cells) and
                       cells[i][0] <= u < cells[i][0] + cells[i][2] and
                       cells[i][1] <= v < cells[i][1] + cells[i][3]]
            for i in indexes:
                x, y, w, h = cells[i]
                coordinates[i] = ((u - x) / w, (v - y) / h)
        elif self._mode == SPLIT:
            horizontal = self.composition.parameters.get('orientation') == 'horizontal'
            selected = (0 if v < 0.5 else 1) if horizontal else (0 if u < 0.5 else 1)
            indexes = [selected] if selected in indexes else []
            coordinates[selected] = ((u, 2.0 * v % 1.0) if horizontal
                                     else (2.0 * u % 1.0, v))
        elif self._mode in (A_B, FLICKER) and mode == 'visible':
            chosen = self._shown_member(frame)
            indexes = [chosen] if chosen in indexes else []
        from ..core.frame_payload import FramePayload
        from ..viewer.sampling import SampleService
        result = []
        for index in indexes:
            mapped = self._member_frame(index, frame)
            identity = self.composition.members[index].source_id
            row = {'member_index': index, 'source_id': str(identity),
                   'member_source_id': str(identity),
                   'occurrence_index': index,
                   'member_frame_index': mapped, 'source_frame': None,
                   'status': 'outside_trim' if mapped is None else 'not_ready',
                   'source_color': self._source_color_state(index, None),
                   'source_rgba': None, 'source_stage': None,
                   'source_dtype': None, 'source_alpha_mode': None}
            if visible_pixel is not None:
                row.update(display_pixel_x=visible_pixel[0],
                           display_pixel_y=visible_pixel[1],
                           pixel_owner_mode=self._mode,
                           pixel_owner_uv=coordinates[index])
            if mapped is not None:
                source = self._sources[self._source_index(index)]
                state = getattr(source, 'frame_status', None)
                decoder_status = state(mapped) if callable(state) else None
                kind = (decoder_status.get('kind') if
                        isinstance(decoder_status, dict) else None)
                if decoder_status is not None:
                    row['decoder_status'] = decoder_status
                # HOLD owns the PREVIOUS signed source sample when that real
                # source payload is still cached and independently identified.
                # Missing/Black/Checker/Error have no measured native pixel.
                payload = (None if kind in ('hole', 'decode_error',
                                           'out_of_range') else
                           self._cached_member_precision(source, mapped))
                if kind == 'hole':
                    row['requested_source_frame'] = decoder_status.get('source_frame')
                    payload, held_index = self._cached_held_precision(
                        source, mapped, decoder_status)
                    if payload is not None:
                        row['held_from_index'] = held_index
                        row['held_from_source_frame'] = payload.source_frame
                if isinstance(payload, FramePayload):
                    # The payload's actual Source ID/signed frame/stage are
                    # independently owned even when this *pixel position*
                    # falls outside physical coverage or on EXR padding.
                    # Do not synthesize a sample there.
                    row.update(source_id=payload.source_id,
                               source_frame=payload.source_frame,
                               source_stage=payload.stage,
                               source_dtype=str(payload.dtype),
                               source_alpha_mode=payload.alpha_mode,
                               source_color=self._source_color_state(
                                   index, payload))
                    su, sv = coordinates.get(index, (u, v))
                    if (self._mode != GRID and self._member_geometries and
                            self._member_geometries[0] is not None):
                        from ..viewer.geometry import (ComparisonGeometry,
                                                       ViewGeometry)
                        reference = (self._member_geometries[0] or (
                            self._blank_size[0], self._blank_size[1], 1.0))
                        native = (self._member_geometries[
                            self._source_index(index)] or
                            (payload.geometry.width, payload.geometry.height,
                             payload.geometry.pixel_aspect))
                        # A/B/Flicker historically displayed native originals
                        # unrectified. Only invert physical field for an ACTUAL
                        # cached rectified selected preview, never for equal-PAR
                        # or a Qt fallback. In multi-owner modes the established
                        # physical reference mapping always applies.
                        rectified_selected = (
                            mode == 'visible' and self._mode in (A_B, FLICKER) and
                            frame in self._precision_cache and
                            self._precision_cache[frame].color_metadata.get(
                                'selected_reference_field') is True and
                            index == self._shown_member(frame))
                        if (self._mode not in (A_B, FLICKER, SEQUENCE) or
                                rectified_selected):
                            position = ComparisonGeometry(
                                ViewGeometry(*reference),
                                ViewGeometry(*native)).canvas_to_member_uv(su, sv)
                            if position is None:
                                row['status'] = 'outside_image'
                                row['source_uv'] = None
                                row['stage_provenance'] = {
                                    'native_stage': row['source_stage'],
                                    'native_dtype': row['source_dtype'],
                                    'source_color': row['source_color'],
                                    'composite_stage': 'working',
                                    'native_verified': False}
                                result.append(row)
                                continue
                            su, sv = position
                            if rectified_selected:
                                row['source_uv'] = (su, sv)
                    if payload.color_metadata.get('exr_canvas_rectified'):
                        from ..media.window_geometry import DisplayCanvasPlacement
                        placement = DisplayCanvasPlacement.from_geometry(
                            payload.geometry)
                        ix = min(payload.geometry.width - 1,
                                 int(su * payload.geometry.width))
                        iy = min(payload.geometry.height - 1,
                                 int(sv * payload.geometry.height))
                        row['exr_absolute'] = placement.canvas_to_exr(ix, iy)
                        if placement.canvas_sample_to_data(ix, iy) is None:
                            row['status'] = 'exr_canvas_padding'
                            row['stage_provenance'] = {
                                'native_stage': row['source_stage'],
                                'native_dtype': row['source_dtype'],
                                'source_color': row['source_color'],
                                'composite_stage': ('working' if
                                    (self._mode not in (A_B, FLICKER, SEQUENCE) or
                                     rectified_selected)
                                    else 'selected_source'),
                                'native_verified': False}
                            result.append(row)
                            continue
                    sampled = SampleService().sample(payload, su, sv,
                                                     coordinate='normalized',
                                                     tile=index)
                    # SampleService supplies normalized floating UINT values
                    # for readout. Also expose the immutable original stored
                    # integer/HALF/FLOAT channels with no 8-bit conversion.
                    raw = payload.pixels[sampled.y, sampled.x]
                    original_channels = (raw.tolist() if getattr(raw, 'ndim', 0)
                                         else [raw.item()])
                    stored = tuple(int(value) if payload.dtype.kind in 'ui'
                                   else float(value) for value in original_channels)
                    row.update(status='held_native' if kind == 'hole' else
                               'native_float' if payload.pixels.dtype.kind == 'f'
                               else 'native_integer',
                               source_id=sampled.source_id,
                               source_frame=sampled.source_frame,
                               source_rgba=sampled.rgba,
                               source_stage=sampled.stage,
                               source_dtype=sampled.dtype,
                               source_alpha_mode=sampled.alpha_mode,
                               native_storage_rgba=stored,
                               source_storage_rgba=stored,
                               source_generation=payload.generation,
                               source_tier=payload.tier,
                               movie_precision=payload.color_metadata.get('movie_precision'),
                               hdr_verified=payload.color_metadata.get('hdr_verified'),
                               x=sampled.x, y=sampled.y,
                               source_color=self._source_color_state(index, payload))
                else:
                    if kind == 'hole' and decoder_status.get('policy') == 'hold' and (
                            type(decoder_status.get('held_source_frame')) is int):
                        row['status'] = 'held_display_only'
                    elif kind in ('decode_error', 'missing', 'missing_frame',
                                  'intentional_hole', 'hole'):
                        row['status'] = ('decode_error' if kind == 'decode_error'
                                         else 'intentional_hole')
                    elif kind in ('held', 'hold'):
                        row['status'] = 'held_display_only'
                    else:
                        row['status'] = ('display_only' if self._cache.get(frame)
                                         is not None else 'not_ready')
                    # Signed source labels are factual only when returned by
                    # the active decoder, never guessed from decode_index.
                    label = getattr(source, 'source_frame_for_index', None)
                    if callable(label):
                        row['source_frame'] = label(mapped)
            row['stage_provenance'] = {
                'native_stage': row['source_stage'],
                'native_dtype': row['source_dtype'],
                'source_color': row['source_color'],
                'composite_stage': 'working' if (self._mode not in
                (A_B, FLICKER, SEQUENCE) or
                (mode == 'visible' and frame in self._precision_cache and
                 self._precision_cache[frame].color_metadata.get(
                     'selected_reference_field'))) else 'selected_source',
                'native_verified': row['source_rgba'] is not None}
            result.append(row)
        return result

    def get_payload(self, frame):
        """Return original member or working-space composite for Player's hook.

        Identical-native-geometry A/B, Flicker and SEQUENCE retain originals.
        For differing native size/PAR A/B/Flicker, the selected preview is a
        reference-field straight Working float (the native selected member is
        always available through member_payload and inspect_member_pixels).
        It must already be displayed/ready or be built by ``get``; a Qt-only
        fallback cannot masquerade as a rectified precision payload.
        QPixmap-only sources intentionally return None rather than pretending
        their 8-bit preview contains recoverable source precision.
        """
        if self._closed or (self._mode == SEQUENCE and
                self.composition.record_timeline and
                not 0 <= int(frame) < self._count):
            return None
        frame = int(frame)
        if self._record_preview_v4 and self._mode == SEQUENCE:
            if frame not in self._cache:
                self.get(frame)
            # A missing placeholder or transparent gap has NO native HDR
            # payload, even if its RGBA display pixmap looks like a real frame.
            return self._precision_cache.get(frame)
        if frame in self._precision_cache:
            return self._precision_cache[frame]
        if self.selected_reference_field_supported():
            if frame not in self._cache:
                self.get(frame)
            return self._precision_cache.get(frame)
        if self._mode == SEQUENCE:
            member, _ = self._sequence_location(frame)
            if member is None:
                return None
            index = self._source_index(member)
            mapped = self._member_frame(member, frame)
        elif self._mode in (A_B, FLICKER):
            member = min(self._shown_member(frame), len(self._sources) - 1)
            index = self._source_index(member)
            mapped = self._member_frame(member, frame)
        else:
            if frame not in self._cache:
                self.get(frame)
            return self._precision_cache.get(frame)
        if mapped is None:
            return None
        getter = getattr(self._sources[index], 'get_payload', None)
        return getter(mapped) if callable(getter) else None

    def _precision_selected_reference(self, frame, pixmaps, indexes):
        """One cached original -> proven first-source physical Working plate.

        No second source color transform, decoder seek or 8-bit re-linearizing:
        the unselected member is only a geometry reference and remains native.
        This method is called after ``get`` has populated the member caches.
        """
        try:
            from ..core.frame_payload import FramePayload
            from ..viewer.compositor import rectify_selected_working
            from ..viewer.color import MAX_PREVIEW_PIXELS, precision_display_image
            selected = self._shown_member(frame)
            index = indexes.index(selected)
            mapped = self._member_frame(selected, frame)
            if mapped is None:
                raise ValueError('Selected member is outside its source trim.')
            source = self._sources[self._source_index(selected)]
            original = self._cached_member_precision(source, mapped)
            if original is None and callable(getattr(source, 'frame_status', None)):
                # Sparse Hold displays a REAL previous source, not synthetic
                # pixels under the missing signed frame's identity.
                original, _held = self._cached_held_precision(
                    source, mapped, source.frame_status(mapped))
            if not isinstance(original, FramePayload):
                raise ValueError('Selected source has no cached native precision; '
                                 'physical A/B preview not proven.')
            reference = self._member_geometries[0]
            member = self._member_geometries[self._source_index(selected)]
            if reference is None or member is None:
                raise ValueError('Selected physical comparison geometry is unavailable.')
            physical_w = int(math.floor(reference[0] * reference[2] + .5))
            physical_h = int(reference[1])
            # Unlike a global UI transform this creates actual square-pixel
            # working samples. A downscaled preview is never exported/claimed
            # to be equal to native managed v2 output (see readiness predicate).
            preview_ref = pixmaps[indexes.index(0)]
            scale = min(1., preview_ref.width() / float(reference[0]),
                        preview_ref.height() / float(reference[1]),
                        math.sqrt(float(MAX_PREVIEW_PIXELS) /
                                  float(physical_w * physical_h)))
            width = max(1, int(math.floor(physical_w * scale + .5)))
            height = max(1, int(math.floor(physical_h * scale + .5)))
            pixels = rectify_selected_working(
                original, reference, member, width, height,
                color_state=self._color_state,
                source_color_state=self._source_color_state(selected, original),
                mode=self._mode)
            lineage = {'source_id': original.source_id,
                       'source_frame': original.source_frame,
                       'source_stage': original.stage,
                       'source_dtype': str(original.dtype),
                       'source_alpha_mode': original.alpha_mode,
                       'generation': original.generation}
            rendered = FramePayload(
                pixels, source_id='comparison:{}:{}'.format(
                    self._mode, original.source_id), source_frame=frame,
                alpha_mode='straight', stage='working',
                color_metadata={
                    'input_space': 'linear',
                    'working_space': getattr(self._color_state,
                                             'working_space', 'linear'),
                    'comparison_mode': self._mode,
                    'selected_reference_field': True,
                    'selected_member_rectified': True,
                    'selected_member_index': selected,
                    'selected_original': lineage,
                    'reference_native': list(reference),
                    'reference_output_size': [physical_w, physical_h],
                    'output_pixel_aspect': 1.0,
                    'preview_downsampled': (width, height) !=
                                           (physical_w, physical_h)})
            image, status = precision_display_image(rendered, self._color_state)
            pixmap = QtGui.QPixmap.fromImage(image)
            if pixmap.isNull():
                raise RuntimeError('Selected precision physical preview is empty.')
            self._precision_status[frame] = (
                'A/B/Flicker: selected native {} frame {}; reference PAR '
                '{} -> square-pixel Working float {}x{}; {}'.format(
                    original.source_id, original.source_frame, reference[2],
                    width, height, status) +
                (' (bounded preview, not full native export resolution)'
                 if (width, height) != (physical_w, physical_h) else ''))
            return pixmap, rendered
        except (ValueError, RuntimeError, TypeError, IndexError, KeyError,
                ImportError, OSError, MemoryError) as exc:
            self._precision_status[frame] = (
                'Unrectified selected Qt fallback: no verified physical '
                'Working precision ({})'.format(exc))
            return (None, None)

    def _precision_composite(self, frame, pixmaps, member_indexes):
        """Return (QPixmap, FramePayload) or (None, None) for Qt fallback.

        Decode is never duplicated: every member's `get()` populated its own
        precision cache before `get_payload()` is requested here. The result
        is bounded to the same display resolution used by the Qt fallback.
        """
        try:
            import numpy as np
            from ..viewer import compositor
            from ..viewer.color import precision_display_image
            from ..core.frame_payload import FramePayload
        except ImportError:
            self._precision_status[frame] = '8-bit Qt fallback: NumPy/precision unavailable'
            return (None, None)
        payloads = []
        originals = []
        source_states = []
        has_precision = False
        for index, pixmap in zip(member_indexes, pixmaps):
            mapped = self._member_frame(index, frame)
            source = self._sources[self._source_index(index)]
            getter = getattr(source, 'get_payload', None)
            original = getter(mapped) if mapped is not None and callable(getter) else None
            source_states.append(self._source_color_state(index, original))
            if isinstance(original, FramePayload):
                has_precision = True
                payloads.append(original)
                originals.append(dict(source_id=original.source_id,
                                      source_frame=original.source_frame,
                                      generation=original.generation,
                                      alpha_mode=original.alpha_mode,
                                      stage=original.stage,
                                      movie_precision=original.color_metadata.get(
                                          'movie_precision')))
            else:
                payloads.append(_qimage_to_array(pixmap.toImage()))
                originals.append(dict(source_id=str(getattr(
                    self.composition.members[index], 'source_id', index)),
                    source_frame=mapped, stage='8-bit display fallback',
                    alpha_mode='straight'))
        if not has_precision:
            return (None, None)
        width, height = pixmaps[0].width(), pixmaps[0].height()
        if width <= 0 or height <= 0:
            return (None, None)
        # One display-sized float canvas bounds the primary compositor's
        # temporary working buffers independently of full-resolution EXR input.
        try:
            from ..viewer.color import MAX_PREVIEW_PIXELS
            if width * height > MAX_PREVIEW_PIXELS:
                scale = (float(MAX_PREVIEW_PIXELS) / (width * height)) ** .5
                width, height = max(1, int(width * scale)), max(1, int(height * scale))
            params = dict(self.composition.parameters)
            if self._mode == GRID:
                params['cells'] = self.composition.grid_cells()
            pixels = compositor.composite_working(
                self._mode, payloads, self._color_state, width=width,
                height=height, source_color_states=source_states,
                source_geometries=[
                    self._member_geometries[self._source_index(index)]
                    for index in member_indexes], **params)
            source_id = 'comparison:{}:{}'.format(
                self._mode, ','.join(str(item['source_id']) for item in originals))
            result = FramePayload(
                pixels, source_id=source_id, source_frame=frame,
                color_metadata={'input_space': 'linear',
                                'working_space': (getattr(self._color_state,
                                                          'working_space', 'linear')),
                                'members': originals, 'comparison_mode': self._mode,
                                'source_color_states': [
                                    item.to_dict() if hasattr(item, 'to_dict')
                                    else dict(item or {}) for item in source_states],
                                'mixed_8bit': any(item['stage'] == '8-bit display fallback'
                                                   for item in originals)},
                alpha_mode='straight', stage='working')
            image, status = precision_display_image(result, self._color_state)
            pixmap = QtGui.QPixmap.fromImage(image)
            if pixmap.isNull():
                raise RuntimeError('Precision composite returned an empty image.')
            if result.color_metadata['mixed_8bit']:
                status += '; mixed 8-bit members linearized from sRGB'
            elif any(item['stage'] == 'source' and
                     item.get('movie_precision') for item in originals):
                status += '; movie source decoded as 16-bit SDR on CPU'
            self._precision_status[frame] = status
            return pixmap, result
        except (ValueError, RuntimeError, ImportError, TypeError, MemoryError, OSError) as exc:
            self._precision_status[frame] = '8-bit Qt fallback: precision composition failed ({})'.format(exc)
            return (None, None)

    def _trim_cache(self):
        limit = 1 if self._policy.cache_mode == CACHE_OFF else self._policy.max_frames
        budget = getattr(self._policy, 'cache_bytes', 0)
        def used_bytes():
            return sum(p.width() * p.height() * 4 for p in self._cache.values()) + sum(
                p.pixels.nbytes for p in self._precision_cache.values())
        while len(self._cache) > max(1, limit) or (
                budget and len(self._cache) > 1 and used_bytes() > budget):
            old, _ = self._cache.popitem(last=False)
            self._precision_cache.pop(old, None)
            self._precision_status.pop(old, None)

    def _record_v4_native_preview(self, frame, occurrence, mapped, source,
                                  pixmap, revision):
        """Transform actual cached native Source to v4 Working and monitor pixels.

        Use the same one-member CPU compositor as managed v4, which evaluates
        explicit per-source input/alpha and then the common grade exactly once.
        Missing black/checker/error cards and Qt-only previews never enter this
        path; no conversion from an 8-bit QPixmap can invent Source precision.
        """
        from ..core.frame_payload import FramePayload
        from ..viewer.compositor import composite_working
        from ..viewer.color import (MAX_PREVIEW_PIXELS,
                                    precision_display_image)
        original = self._cached_member_precision(source, mapped)
        if original is None and callable(getattr(source, 'frame_status', None)):
            original, _ = self._cached_held_precision(
                source, mapped, source.frame_status(mapped))
        if not isinstance(original, FramePayload):
            self._precision_status[frame] = (
                'v4 record: display-only source/hole; native Working '
                'precision not available')
            return pixmap
        width, height = int(original.geometry.width), int(original.geometry.height)
        if width * height > MAX_PREVIEW_PIXELS:
            factor = (float(MAX_PREVIEW_PIXELS) / (width * height)) ** .5
            width, height = max(1, int(width * factor)), max(1, int(height * factor))
        try:
            working = composite_working(
                A_B, [original], color_state=self._color_state,
                width=width, height=height,
                source_color_states=[self._source_color_state(
                    occurrence, original)])
            result = FramePayload(
                working, source_id=original.source_id,
                source_frame=original.source_frame, generation=original.generation,
                alpha_mode='straight', stage='working',
                color_metadata={
                    'input_space': 'linear',
                    'working_space': getattr(
                        self._color_state, 'working_space', 'linear'),
                    'record_frame': frame,
                    'record_occurrence_id': self._record_occurrence_ids[occurrence],
                    'record_occurrence_index': occurrence,
                    'logical_source_id': self.composition.members[occurrence].source_id,
                    'source_frame': original.source_frame,
                    'source_alpha_mode': original.alpha_mode,
                    'source_generation': original.generation,
                    'preview_downsampled': (width, height) !=
                                           (original.geometry.width,
                                            original.geometry.height)})
            image, status = precision_display_image(result, self._color_state)
            candidate = QtGui.QPixmap.fromImage(image)
            if candidate.isNull() or 'fallback:' in status.lower():
                raise ValueError('Managed float record display is unavailable.')
        except (ValueError, RuntimeError, TypeError, ImportError, OSError,
                MemoryError, OverflowError) as exc:
            self._precision_status[frame] = (
                'v4 record: unverified source preview; float parity unavailable '
                '({})'.format(exc))
            return pixmap
        if (revision != self._record_render_revision or self._closed or
                self._mode != SEQUENCE):
            return None
        self._precision_cache[frame] = result
        held_detail = ''
        identity = self.record_frame_identity(frame)
        if identity.get('kind') == 'held_trim':
            held_detail = '; source trim held at {}'.format(original.source_frame)
        elif identity.get('kind') == 'source_hole':
            held_detail = '; intentional hole held from {}'.format(original.source_frame)
        self._precision_status[frame] = (
            'v4 record: Source {} frame {} → Working float → {}'
            .format(original.source_id, original.source_frame, status) +
            held_detail +
            (' (bounded preview)' if result.color_metadata['preview_downsampled']
             else ''))
        return candidate

    def get(self, frame):
        frame = int(frame)
        if self._mode == SEQUENCE:
            self._inspection_presented = None
        if self._closed or (self._mode == SEQUENCE and
                self.composition.record_timeline and
                not 0 <= frame < self._count):
            return None
        self._last_frame = frame
        if frame in self._cache:
            self._cache.move_to_end(frame)
            return self._cache[frame]
        self._requested.add(frame)
        if not self._sources:
            return None
        if self._mode == SEQUENCE:
            source_index, local_frame = self._sequence_location(frame)
            if source_index is None:
                # Frozen v4 record gaps are transparent RGBA zero; the legacy
                # record preview intentionally retains its opaque-black UI.
                self._requested.discard(frame)
                pixmap = (self._record_v4_blank() if self._record_preview_v4
                          else self._blank(*self._blank_size))
                if self._record_preview_v4:
                    self._cache[frame] = pixmap
                    self._trim_cache()
                return pixmap
            source = self._sources[self._source_index(source_index)]
            mapped = self._member_frame(source_index, frame)
            revision = self._record_render_revision
            pixmap = (source.get(mapped) if mapped is not None else
                      (self._record_v4_blank() if self._record_preview_v4
                       else self._blank(*self._blank_size)))
            if self._record_preview_v4 and (
                    revision != self._record_render_revision or self._closed):
                return None
            if pixmap is None:
                self._ensure_member(source_index, frame)
                return None
            if self._record_preview_v4 and mapped is not None:
                pixmap = self._record_v4_native_preview(
                    frame, source_index, mapped, source, pixmap, revision)
                if pixmap is None:
                    return None
            if self._record_preview_v4:
                self._cache[frame] = pixmap
                self._trim_cache()
            self._requested.discard(frame)
            self._inspection_presented = (frame, pixmap)
            return pixmap
        gathered = []
        required = self._required_members(frame)
        for index in required:
            mapped = self._member_frame(index, frame)
            pixmap = (self._sources[index].get(mapped)
                      if mapped is not None else None)
            if mapped is None:
                gathered.append(None)
                continue
            if pixmap is None:
                self._ensure_member(index, frame)
                return None
            gathered.append(pixmap)
        if not gathered:
            return None
        size = next((item for item in gathered if item is not None), None)
        width, height = (size.width(), size.height()) if size else (1, 1)
        pixmaps = [p if p is not None else self._blank(
            width, height, transparent=self._mode == GRID)
                   for p in gathered]
        if self._mode in (A_B, FLICKER):
            pixmap = pixmaps[min(self._shown_member(frame), len(pixmaps) - 1)]
            if self.selected_reference_field_supported():
                rectified, precision = self._precision_selected_reference(
                    frame, pixmaps, required)
                if rectified is not None and precision is not None:
                    pixmap = rectified
                    self._precision_cache[frame] = precision
        elif len(pixmaps) == 1:
            pixmap = pixmaps[0]
        else:
            pixmap, precision = self._precision_composite(frame, pixmaps, required)
            if pixmap is None:
                pixmap = self._composite(pixmaps)
            elif precision is not None:
                self._precision_cache[frame] = precision
        self._requested.discard(frame)
        self._cache[frame] = pixmap
        self._trim_cache()
        self.cacheChanged.emit(self._cache_ranges(), None)
        return pixmap

    def _cache_ranges(self):
        frames = sorted(self._cache)
        ranges = []
        for f in frames:
            if ranges and f <= ranges[-1][1] + 1:
                ranges[-1] = (ranges[-1][0], f)
            else:
                ranges.append((f, f))
        return ranges

    # -- SequenceSource-compatible no-ops / helpers ------------------------
    def ensure(self, frame, direction=1):
        if self._mode == SEQUENCE and self.composition.record_timeline and \
                not 0 <= int(frame) < self._count:
            return
        if self._closed:
            return
        self._requested.add(int(frame))
        for index in self._required_members(frame):
            self._ensure_member(index, frame, direction)

    def _sequence_location(self, frame):
        if self.composition.record_timeline and \
                not 0 <= int(frame) < self._count:
            return (None, None)
        frame = min(max(0, int(frame)), self._count - 1)
        if self.composition.record_timeline:
            return self.composition.locate(frame)
        if not self._counts:
            return (None, None)
        offset = 0
        for index, count in enumerate(self._counts):
            if frame < offset + count:
                return index, frame - offset
            offset += count
        return len(self._counts) - 1, self._counts[-1] - 1

    def _member_frame_available(self, member_index, member_frame):
        # An asynchronous >8-bit movie sidecar may finish after the JPEG
        # preview was composed. Invalidate only frames actually referencing
        # that decoder/frame so source precision becomes visible on refresh.
        if self._closed or not 0 <= member_index < len(self._sources):
            return  # old QThread callback after reload/shutdown
        changed = []
        for frame in tuple(self._cache):
            if any(self._source_index(i) == member_index and
                   self._member_frame(i, frame) == member_frame
                   for i in self._required_members(frame)):
                self._cache.pop(frame, None)
                self._precision_cache.pop(frame, None)
                self._precision_status.pop(frame, None)
                changed.append(frame)
        for frame in changed:
            self.frameAvailable.emit(frame)
        for frame in tuple(self._requested):
            ready = all((self._member_frame(i, frame) is None or
                         self._sources[self._source_index(i)].get(
                             self._member_frame(i, frame))
                         is not None)
                        for i in self._required_members(frame))
            if ready:
                self._cache.pop(frame, None)
                self._precision_cache.pop(frame, None)
                self._precision_status.pop(frame, None)
                self.frameAvailable.emit(frame)

    def _member_cache_changed(self, *_args):
        if self._closed:
            return
        self.cacheChanged.emit(self._cache_ranges(), None)

    def set_loop_start(self, frame):
        if self._mode == SEQUENCE:
            source_index, local_frame = self._sequence_location(frame)
            decoder_index = (self._source_index(source_index)
                             if source_index is not None else None)
            for index, source in enumerate(self._sources):
                local = (self._member_frame(source_index, frame)
                         if self.composition.record_timeline and
                         decoder_index == index else local_frame)
                source.set_loop_start(local if decoder_index == index else 0)
            return
        for index, source in enumerate(self._sources):
            mapped = self._member_frame(index, frame)
            source.set_loop_start(0 if mapped is None else mapped)

    def set_playback_range(self, first, last):
        if self._closed:
            return
        if self._mode == SEQUENCE and self.composition.record_timeline:
            ranges = {}
            for index, member in enumerate(self.composition.members):
                start = max(int(first), member.session_offset)
                end = min(int(last), member.session_offset + member.frame_count - 1)
                if start > end:
                    continue
                start_frame = self._member_frame(index, start)
                end_frame = self._member_frame(index, end)
                if start_frame is None or end_frame is None:
                    continue
                decoder = self._source_index(index)
                previous = ranges.get(decoder)
                low, high = min(start_frame, end_frame), max(start_frame, end_frame)
                ranges[decoder] = (min(previous[0], low), max(previous[1], high)) \
                    if previous else (low, high)
            for index, (start, end) in ranges.items():
                source = self._sources[index]
                if hasattr(source, 'set_playback_range'):
                    source.set_playback_range(start, end)
            return
        for index, source in enumerate(self._sources):
            if not hasattr(source, 'set_playback_range'):
                continue
            if self._mode == SEQUENCE:
                offset = sum(self._counts[:index])
                local_start = max(0, int(first) - offset)
                local_end = min(self._counts[index] - 1, int(last) - offset)
            else:
                local_start = self._member_frame(index, first)
                local_end = self._member_frame(index, last)
            if local_start is not None and local_end is not None:
                source.set_playback_range(local_start, max(local_start, local_end))

    def set_loop(self, enabled):
        for source in self._sources:
            if hasattr(source, 'set_loop'):
                source.set_loop(enabled)

    def set_loop_mode(self, mode):
        for source in self._sources:
            if hasattr(source, 'set_loop_mode'):
                source.set_loop_mode(mode)
            elif hasattr(source, 'set_loop'):
                source.set_loop(mode != 'once')

    def set_cache_mode(self, mode):
        self._policy.cache_mode = mode
        for source in self._sources:
            source.set_cache_mode(mode)

    def cache_snapshot(self):
        return dict(mode='composite', frames=sorted(self._cache),
                    cached=len(self._cache), capacity=self._policy.max_frames,
                    decoded=len(self._cache), total=self._count,
                    members=len(self._sources),
                    payload_frames=sorted(self._precision_cache),
                    payload_bytes=sum(p.pixels.nbytes for p in self._precision_cache.values()))

    def shutdown(self, wait=True):
        self._closed = True
        self._record_render_revision += 1
        self._inspection_presented = None
        self._requested.clear()
        self._last_frame = None
        for source in self._sources:
            try:
                source.shutdown(wait=wait)
            except TypeError:
                source.shutdown()
        self._sources = []
        self._cache.clear()
        self._precision_cache.clear()
        self._precision_status.clear()
