# -*- coding: utf-8 -*-
"""Bounded frame caches and asynchronous frame decoding for Maya.

FFmpeg pipes compressed JPEGs directly into memory. There are no temporary
frame directories, per-frame subprocesses, blocking thread joins, or polling
of the filesystem. Decoder requests are guarded by process identity so stale output
cannot enter the cache after a seek or media change.
"""
from __future__ import absolute_import

import collections
import hashlib
import os
import stat

from ..core.qtcompat import QtCore, QtGui, Signal
from ..core import utils
from ..media import exr
from ..media import sequence as sequence_policy
from ..media import missing_placeholder
from .cache_policy import CachePolicy, CACHE_OFF
from . import cache_policy as playback_policy


# The integrity opt-in is intentionally separate from the 256 MiB EXR decoded
# raster budget. At most 512 MiB is streamed, in 1 MiB chunks, never buffered.
MAX_INTEGRITY_FILE_BYTES = 512 * 1024 * 1024


def _file_identity(info):
    """Identity also includes ctime: restored mtime alone cannot mask writes."""
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            getattr(info, 'st_mtime_ns', int(info.st_mtime * 1e9)),
            getattr(info, 'st_ctime_ns', int(info.st_ctime * 1e9)))


def _safe_original_path(path):
    """Pin an original's canonical regular file; reject symlinked leaf files.

    macOS uses /var -> /private/var for ordinary temporary EXRs. Canonicalize
    parent paths before hashing/decoding and ensure the referring path still
    resolves to this same canonical original on every subsequent check.
    """
    if not isinstance(path, (str, bytes, os.PathLike)):
        return None
    try:
        requested = os.path.abspath(os.fspath(path))
        if os.path.islink(requested):
            return None
        normalized = os.path.realpath(requested)
        info = os.lstat(normalized)
        if not stat.S_ISREG(info.st_mode):
            return None
        return normalized
    except (OSError, ValueError, TypeError):
        return None


def _stable_content_hash(path):
    """SHA256 one bounded opened inode, with pathname/FD identity fences.

    This is NOT a second decode; it never invokes an image reader. A replaced
    path, mutated file, symlink, directory, FIFO, or unbounded file cannot be
    presented as a trusted cache proof. The caller must independently compare
    pre-decode and post-decode hashes to detect intervening source changes.
    """
    normalized = _safe_original_path(path)
    if normalized is None:
        return None
    flags = os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0)
    flags |= getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    try:
        before = os.lstat(normalized)
        if (not stat.S_ISREG(before.st_mode) or
                not 0 < before.st_size <= MAX_INTEGRITY_FILE_BYTES):
            return None
        descriptor = os.open(normalized, flags)
        try:
            opened = os.fstat(descriptor)
            if (_file_identity(opened) != _file_identity(before) or
                    not stat.S_ISREG(opened.st_mode)):
                return None
            digest = hashlib.sha256()
            remaining = opened.st_size
            while remaining:
                block = os.read(descriptor, min(1024 * 1024, remaining))
                if not block:
                    return None
                digest.update(block)
                remaining -= len(block)
            finished = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        after = os.lstat(normalized)
        identity = _file_identity(before)
        if (identity != _file_identity(finished) or
                identity != _file_identity(after) or
                _safe_original_path(normalized) != normalized):
            return None
        return {'path': normalized, 'size': int(before.st_size),
                'sha256': digest.hexdigest(), 'identity': identity}
    except (OSError, ValueError, OverflowError):
        return None


class SequenceSource(QtCore.QObject):
    """Sequence reader with a joint byte-bounded pixmap/precision EXR cache.

    ``get`` remains the historical QPixmap API. ``get_payload`` exposes an
    immutable EXR source frame without another decode, while cache eviction and
    CACHE_OFF free both the display and high-precision representation together.
    """
    frameAvailable = Signal(int)
    cacheChanged = Signal(object, object)

    # The sequence resolver supplies only *existing* files by default. Opt in
    # to source-number span playback explicitly; never turn legacy dense index
    # playback into a different timeline just because filenames contain holes.
    MISSING_HOLD = sequence_policy.MISSING_HOLD
    MISSING_BLACK = sequence_policy.MISSING_BLACK
    MISSING_CHECKER = sequence_policy.MISSING_CHECKER
    MISSING_ERROR = sequence_policy.MISSING_ERROR

    def __init__(self, files, parent=None, cache_policy=None, source_id='',
                 source_frames=None, color_state=None, alpha_mode=None,
                 missing_policy=sequence_policy.DEFAULT_MISSING_POLICY,
                 include_holes=False, source_size=None, declared_frames=None,
                 exr_layer=None, exr_part=None, exr_display_canvas=False,
                 exr_canvas_contract=None, integrity_proof=False):
        QtCore.QObject.__init__(self, parent)
        self._policy = cache_policy or CachePolicy()
        self._files = list(files)
        if source_frames is not None and len(source_frames) != len(self._files):
            raise ValueError('source_frames must match the sequence file count.')
        if missing_policy not in sequence_policy.MISSING_POLICIES:
            raise ValueError('Unknown missing-frame policy: {!r}'.format(missing_policy))
        self._missing_policy = missing_policy
        self._include_holes = bool(include_holes)
        self._holes = set()
        if self._include_holes:
            if source_frames is None or len(source_frames) == 0:
                raise ValueError('include_holes requires nonempty source_frames.')
            real = [int(number) for number in source_frames]
            if len(set(real)) != len(real) or real != sorted(real):
                raise ValueError('source_frames must be unique and increasing in span mode.')
            if declared_frames is None:
                timeline = range(real[0], real[-1] + 1)
            else:
                timeline = [int(number) for number in declared_frames]
                if (not timeline or len(set(timeline)) != len(timeline) or
                        timeline != sorted(timeline) or
                        not set(real).issubset(timeline)):
                    raise ValueError('declared_frames must be sorted, unique and include source_frames.')
            if len(timeline) > 1000000:
                raise ValueError('Sequence timeline exceeds 1,000,000 frames.')
            by_frame = dict(zip(real, self._files))
            self._files = [by_frame.get(number) for number in timeline]
            source_frames = timeline
        elif declared_frames is not None:
            raise ValueError('declared_frames requires include_holes=True.')
        self._holes = set(i for i, path in enumerate(self._files) if path is None)
        # Sparse spans can contain up to one million declared positions. A
        # linear backwards scan for Hold on every long scrub was quadratic in
        # the number of missing frames. Binary search only existing members.
        self._existing_indices = tuple(i for i, path in enumerate(self._files)
                                       if path is not None)
        self._closed = False
        # Synchronous Qt cache listeners and precision/placeholder converters
        # can reenter source mutations while get() is still running. Never
        # commit the output of a superseded decode or return pixels after close.
        self._revision = 0
        if source_size is not None:
            if (len(source_size) != 2 or
                    min(int(source_size[0]), int(source_size[1])) <= 0):
                raise ValueError('source_size must contain positive width and height.')
            self._source_size = tuple(int(v) for v in source_size)
        else:
            self._source_size = None
        self._source_id = str(source_id or '')
        self._source_frames = tuple(source_frames) if source_frames is not None else None
        self._color_state = color_state
        self._alpha_mode = alpha_mode
        self._exr_layer = exr_layer
        self._exr_part = exr_part
        self._exr_display_canvas = bool(exr_display_canvas)
        self._exr_canvas_contract = (dict(exr_canvas_contract)
                                     if exr_canvas_contract is not None else None)
        self._cache = collections.OrderedDict()
        self._cache_bytes = 0
        self._payload_cache = {}
        self._payload_bytes = 0
        self._display_status = {}
        self._decode_errors = {}
        self._integrity_proof = bool(integrity_proof)
        # Internal immutable-by-convention dictionaries. Never trust metadata
        # alone as a SHA content comparison; verify_cached_file_proof hashes.
        self._file_proofs = {}

    def enable_integrity_proof(self):
        """Opt in at runtime, invalidating every previously unproven decode.

        A warm CompositeSource may have loaded plates in another compare mode
        without SHA fencing. Hashing those OLD cached frames later cannot
        prove what the decoder actually read, so do not backfill provenance.
        Force a normal subsequent get() to decode under the double hash guard.
        Returns True only on an active False->True transition; idempotent.
        """
        if self._closed or self._integrity_proof:
            return False
        self._revision += 1
        revision = self._revision
        self._integrity_proof = True
        old_frames = tuple(self._cache)
        for frame in old_frames:
            self._forget(frame)
        self._file_proofs.clear()
        self._decode_errors.clear()
        self._notify_cache()
        for frame in old_frames:
            if self._closed or revision != self._revision:
                break
            self.frameAvailable.emit(frame)
        return True

    def _invalidate_original_proof(self, frame):
        """Invalidate both old display/native content and notify dependents."""
        if self._closed:
            return
        self._revision += 1
        revision = self._revision
        self._file_proofs.pop(frame, None)
        if frame in self._cache:
            self._forget(frame)
        self._decode_errors[frame] = 'Source file changed since cached decode'
        self._notify_cache()
        if not self._closed and revision == self._revision:
            self.frameAvailable.emit(frame)

    def _cached_file_proof_record(self, frame):
        """Metadata-only freshness guard for a cached ORIGINAL frame."""
        if (self._closed or not self._integrity_proof or
                type(frame) is not int or frame in self._holes or
                not 0 <= frame < len(self._files) or
                frame not in self._cache):
            return None
        proof = self._file_proofs.get(frame)
        if proof is None:
            return None
        if exr.is_exr(self._files[frame]) and frame not in self._payload_cache:
            # EXR precision cannot be proved from the 8-bit QPixmap alone;
            # a manually torn Source cache is as untrusted as an eviction.
            self._invalidate_original_proof(frame)
            return None
        path = proof['path']
        try:
            current = os.lstat(path)
            good = (_safe_original_path(self._files[frame]) == path and
                    stat.S_ISREG(current.st_mode) and
                    _file_identity(current) == proof['identity'])
        except (OSError, ValueError, TypeError):
            good = False
        if not good:
            self._invalidate_original_proof(frame)
            return None
        return proof

    def cached_file_proof(self, frame):
        """Original decoded file provenance, cache-only; never reads pixels.

        Returns a fresh {path, source_frame, sha256, size} dict, or None.
        For sparse Hold the *hole index is never proof*: request the actual
        previous existing index and retain its signed label separately.
        Symlink/metadata replacement invalidates stale pixmap AND payload.
        """
        proof = self._cached_file_proof_record(frame)
        if proof is None:
            return None
        return {key: proof[key] for key in
                ('path', 'source_frame', 'sha256', 'size')}

    def verify_cached_file_proof(self, frame):
        """Hash current original file without seeking/decoding; fail closed.

        Replacing bytes and restoring the size/mtime cannot cause a match.
        On any change the stale display and precision cache are invalidated
        with frameAvailable emitted for dependent compositors. No recaching or
        decode occurs here: the caller must explicitly request a new frame.
        """
        proof = self._cached_file_proof_record(frame)
        if proof is None:
            return False
        revision = self._revision
        current = _stable_content_hash(proof['path'])
        if self._closed or revision != self._revision:
            return False
        if (current is None or any(current[key] != proof[key]
                                   for key in ('identity', 'size', 'sha256'))):
            self._invalidate_original_proof(frame)
            return False
        return True

    @property
    def frame_count(self):
        """Playback length, including explicitly requested holes in span mode."""
        return len(self._files)

    @property
    def missing_policy(self):
        return self._missing_policy

    def source_frame_for_index(self, index):
        """Resolve a display index to its signed source frame, even for holes."""
        if not 0 <= index < len(self._files):
            raise IndexError('Display frame index is outside the sequence.')
        return self._source_frame(index)

    def _previous_existing(self, frame):
        # Local binary search keeps this class usable by legacy test/plugin
        # shims that compile SequenceSource independently of module imports.
        low, high = 0, len(self._existing_indices)
        while low < high:
            middle = (low + high) // 2
            if self._existing_indices[middle] < frame:
                low = middle + 1
            else:
                high = middle
        return self._existing_indices[low - 1] if low else None

    def frame_status(self, frame):
        """Structured per-frame diagnostics without hiding corrupt media.

        ``hole`` means the resolver explicitly declared a missing source
        frame. ``decode_error`` means an actual path was present but could not
        be decoded. Only ``hole`` responds to Hold/Black/Checker/Error policy.
        """
        if not 0 <= frame < len(self._files):
            return {'kind': 'out_of_range', 'index': frame}
        status = {'index': frame, 'source_frame': self._source_frame(frame),
                  'policy': self._missing_policy}
        if frame in self._holes:
            status['kind'] = 'hole'
            if self._missing_policy == self.MISSING_HOLD:
                previous = self._previous_existing(frame)
                status['held_source_frame'] = (self._source_frame(previous)
                                               if previous is not None else None)
        elif frame in self._decode_errors:
            status['kind'] = 'decode_error'
            status['reason'] = self._decode_errors[frame]
        else:
            status['kind'] = 'available'
        return status

    def set_missing_policy(self, policy):
        """Switch hole presentation without affecting actual decoded frames."""
        if policy not in sequence_policy.MISSING_POLICIES:
            raise ValueError('Unknown missing-frame policy: {!r}'.format(policy))
        if self._closed or policy == self._missing_policy:
            return
        self._revision += 1
        self._missing_policy = policy
        # A million-frame sparse sequence may have only one cached pixmap.
        # Touch cached hole entries rather than materializing every hole ID.
        for frame in tuple(self._cache):
            if frame in self._holes:
                self._forget(frame)
        self._notify_cache()

    def _source_frame(self, index):
        if self._source_frames is not None:
            return int(self._source_frames[index])
        path = self._files[index]
        inferred = exr.filename_source_frame(path) if path is not None else None
        return inferred if inferred is not None else int(index)

    def _hole_size(self):
        """Match the display decode size without decoding a missing file."""
        if self._source_size is not None:
            width, height = self._source_size
        else:
            width = height = 0
            for pixmap in self._cache.values():
                if not pixmap.isNull():
                    width, height = pixmap.width(), pixmap.height()
                    break
            if not width or not height:
                for index in self._existing_indices:
                    path = self._files[index]
                    size = QtGui.QImageReader(path).size()
                    if size.isValid():
                        width, height = size.width(), size.height()
                        break
        if width <= 0 or height <= 0:
            width, height = 320, 180
        # Very large/tall source geometries can be legal metadata even when
        # the actual EXR cannot enter the 256 MiB native decoder. A missing
        # frame must still be representable without asking the placeholder
        # raster for >64 MiB or more than the playback cache's byte budget.
        # The older Maya/host test adapters compile SequenceSource in an
        # isolated namespace without the optional raster helper. They render
        # through QPainter; use the same 64 MiB cap in that supported fallback.
        placeholder_module = globals().get('missing_placeholder')
        raster_budget = getattr(placeholder_module, 'MAX_RASTER_BYTES',
                                64 * 1024 * 1024)
        max_pixels = max(1, min(raster_budget,
                                self._policy.cache_bytes) // 4)
        scale = min(1.0, self._policy.decode_width / float(width),
                    16384.0 / float(height),
                    (max_pixels / float(width * height)) ** .5)
        if scale < 1.0:
            # Retain the historical rounded aspect-ratio resize when only the
            # configured decode width applies; the final clamp enforces bytes.
            width = max(1, min(self._policy.decode_width,
                               int(round(width * scale))))
            height = max(1, min(16384, int(round(height * scale))))
        if width * height > max_pixels:
            height = max(1, max_pixels // width)
        return width, height

    def _hole_pixmap(self, frame, policy):
        """Canonical display-only Qt raster, with no native EXR payload."""
        width, height = self._hole_size()
        if hasattr(QtGui, 'QImage') and hasattr(QtGui.QImage, 'Format_RGBA8888'):
            raw = missing_placeholder.raster_placeholder(
                policy, width, height, channels=4)
            # Explicit copy owns the buffer before Python releases ``raw``;
            # QPixmap conversion is exact native-size image presentation.
            image = QtGui.QImage(raw, width, height, width * 4,
                                 QtGui.QImage.Format_RGBA8888).copy()
            return QtGui.QPixmap.fromImage(image)

        # Existing Qt-free test doubles implement QPixmap/QPainter but not
        # QImage. Retain their old draw-call contract in this mock-only branch;
        # real Maya always uses the shared packed raster above.
        pixmap = QtGui.QPixmap(width, height)
        pixmap.fill(QtGui.QColor('#000000' if policy == self.MISSING_BLACK
                                  else '#292e34'))
        if policy in (self.MISSING_CHECKER, self.MISSING_ERROR):
            painter = QtGui.QPainter(pixmap)
            try:
                if policy == self.MISSING_CHECKER:
                    cell = max(8, min(32, width // 12 or 8))
                    painter.setPen(QtCore.Qt.NoPen)
                    painter.setBrush(QtGui.QColor('#4b5159'))
                    for y in range(0, height, cell):
                        for x in range(0, width, cell):
                            if (x // cell + y // cell) % 2:
                                painter.drawRect(x, y, min(cell, width - x),
                                                 min(cell, height - y))
                else:
                    painter.fillRect(0, 0, width, max(3, height // 16),
                                     QtGui.QColor('#bb554e'))
                    painter.setPen(QtGui.QColor('#e3d7d5'))
                    painter.drawText(
                        0, 0, width, height,
                        QtCore.Qt.AlignCenter | QtCore.Qt.TextWordWrap,
                        'MISSING FRAME\n{}'.format(self._source_frame(frame)))
            finally:
                painter.end()
        return pixmap

    def _cache_pixmap(self, frame, pixmap, payload=None, status=None,
                      file_proof=None, revision=None):
        if self._closed or (revision is not None and
                            revision != self._revision):
            return None
        if frame in self._cache:
            # A Qt callback may have completed this frame while our decoder
            # was in flight; replace atomically without duplicating counters.
            self._forget(frame)
        self._cache[frame] = pixmap
        self._cache_bytes += pixmap.width() * pixmap.height() * 4
        if payload is not None:
            self._payload_cache[frame] = payload
            self._payload_bytes += payload.pixels.nbytes
        if status is not None:
            self._display_status[frame] = status
        if file_proof is not None:
            self._file_proofs[frame] = file_proof
        self._trim_cache()
        self._notify_cache()
        if (self._closed or (revision is not None and
                             revision != self._revision) or
                frame not in self._cache):
            return None
        return self._cache[frame]

    def _get_hole(self, frame):
        revision = self._revision
        policy = self._missing_policy
        if policy == self.MISSING_HOLD:
            previous = self._previous_existing(frame)
            if previous is not None:
                # Do not duplicate the held cache/payload: the frame retains
                # its real origin and all evictions continue to operate once.
                result = self.get(previous)
                return result if not self._closed and revision == self._revision else None
            # Leading declared holes have no previous pixel to hold. Explicit
            # black makes that boundary visible without inventing source data.
            policy = self.MISSING_BLACK
        return self._cache_pixmap(
            frame, self._hole_pixmap(frame, policy),
            status='Missing source frame {} ({})'.format(
                self._source_frame(frame),
                'black: no previous frame' if self._missing_policy == self.MISSING_HOLD
                else policy), revision=revision)

    def _forget(self, frame):
        self._file_proofs.pop(frame, None)
        pixmap = self._cache.pop(frame)
        self._cache_bytes -= pixmap.width() * pixmap.height() * 4
        payload = self._payload_cache.pop(frame, None)
        if payload is not None:
            self._payload_bytes -= payload.pixels.nbytes
        self._display_status.pop(frame, None)

    def _trim_cache(self):
        frame_limit = (1 if self._policy.cache_mode == CACHE_OFF
                       else self._policy.max_frames)
        while (len(self._cache) > frame_limit or
               (len(self._cache) > 1 and
                self._cache_bytes + self._payload_bytes > self._policy.cache_bytes)):
            self._forget(next(iter(self._cache)))

    def _notify_cache(self):
        if self._closed:
            return
        frames = sorted(self._cache)
        ranges = []
        for f in frames:
            if ranges and f <= ranges[-1][1] + 1:
                ranges[-1] = (ranges[-1][0], f)
            else:
                ranges.append((f, f))
        self.cacheChanged.emit(ranges, None)

    def get_payload(self, frame):
        """Return an owned, immutable EXR FramePayload or None for other media.

        The returned array stays valid after eviction or ``shutdown`` because
        each FramePayload owns its bytes, independent of OpenEXR and the cache.
        """
        if self._closed or not (0 <= frame < len(self._files)):
            return None
        revision = self._revision
        if frame in self._holes:
            if self._missing_policy != self.MISSING_HOLD:
                return None
            previous = self._previous_existing(frame)
            result = self.get_payload(previous) if previous is not None else None
            return result if not self._closed and revision == self._revision else None
        if not exr.is_exr(self._files[frame]):
            return None
        if self._integrity_proof and frame in self._cache:
            if self._cached_file_proof_record(frame) is None:
                return None  # never silently redecode from a read-only hook
        if frame not in self._cache:
            if self.get(frame) is None:
                return None
        if (self._closed or revision != self._revision or
                frame not in self._cache):
            return None
        self._cache.move_to_end(frame)
        return self._payload_cache.get(frame)

    def get_display_status(self, frame):
        """Human-readable OCIO/unmanaged label for the frame's display pixmap."""
        if frame in self._holes:
            status = self.frame_status(frame)
            held = status.get('held_source_frame')
            suffix = ('held from {}'.format(held) if held is not None else
                      'black: no earlier frame') if self._missing_policy == self.MISSING_HOLD else self._missing_policy
            return 'Missing source frame {} ({})'.format(status['source_frame'], suffix)
        if frame in self._decode_errors:
            return 'Decode failure for source frame {}: {}'.format(
                self._source_frame(frame), self._decode_errors[frame])
        return self._display_status.get(frame, '')

    def set_color_state(self, state):
        """Re-render cached EXR display pixmaps from native payloads, no re-decode."""
        from ..viewer.color import ColorPipelineState
        state = ColorPipelineState.from_dict(state)
        if self._closed:
            return
        self._revision += 1
        revision = self._revision
        self._color_state = state
        for frame, payload in tuple(self._payload_cache.items()):
            if self._closed or revision != self._revision:
                return
            if (self._integrity_proof and
                    self._cached_file_proof_record(frame) is None):
                continue  # stale Source bytes cannot be rerendered as current
            pixmap, status = exr.payload_to_pixmap(
                payload, self._policy.decode_width, state, with_status=True)
            if self._closed or revision != self._revision:
                return
            if pixmap is None or pixmap.isNull():
                continue
            old = self._cache.get(frame)
            if old is None:
                continue
            self._cache_bytes += (pixmap.width() * pixmap.height() -
                                  old.width() * old.height()) * 4
            self._cache[frame] = pixmap
            self._display_status[frame] = status
        self._trim_cache()
        self._notify_cache()

    def get(self, frame):
        if self._closed:
            return None
        if frame in self._cache:
            trusted = (not self._integrity_proof or frame in self._holes or
                       self._cached_file_proof_record(frame) is not None)
            if self._closed:
                return None
            if trusted and frame in self._cache:
                self._cache.move_to_end(frame)
                return self._cache[frame]
            # The former file is stale or unverified. A fresh get may decode
            # the new bytes, but no old pixmap/payload can survive its cache.
            if frame in self._cache:
                # An invalidation's Qt listener may have *already* decoded a
                # new SHA-verified original. Preserve that latest cache entry.
                if self._cached_file_proof_record(frame) is not None:
                    self._cache.move_to_end(frame)
                    return self._cache[frame]
                if self._closed:
                    return None
                if frame in self._cache:
                    self._invalidate_original_proof(frame)
        if self._closed:
            return None
        revision = self._revision
        if not (0 <= frame < len(self._files)):
            return None
        if frame in self._holes:
            return self._get_hole(frame)
        path = self._files[frame]
        payload = None
        proof_before = None
        if self._integrity_proof:
            proof_before = _stable_content_hash(path)
            if self._closed or revision != self._revision:
                return None
            if proof_before is None:
                self._decode_errors[frame] = (
                    'Integrity proof unavailable: file absent, symlinked, '
                    'unstable, unreadable, or over SHA256 budget')
                return None
            # Decode the exact opened-file path that was hashed: never follow
            # a mutable ancestor directory symlink to different source bytes.
            path = proof_before['path']
        if exr.is_exr(path):
            # Legacy EXR decoders and in-process test adapters accept the
            # original reader signature. Explicit part/layer selection is
            # optional and must not leak null kwargs into that contract.
            exr_selection = {}
            if self._exr_layer is not None:
                exr_selection['layer'] = self._exr_layer
            if self._exr_part is not None:
                exr_selection['part'] = self._exr_part
            if self._exr_display_canvas:
                exr_selection['display_canvas'] = True
            payload = exr.read_frame_payload(
                path, source_id=self._source_id or path,
                source_frame=self._source_frame(frame), tier='source',
                alpha_mode=self._alpha_mode, **exr_selection)
            if self._closed or revision != self._revision:
                return None
            if payload is None:
                self._decode_errors[frame] = (
                    'Source file unavailable' if not os.path.isfile(path)
                    else 'EXR data could not be decoded')
                return None
            if self._exr_canvas_contract is not None:
                # Data windows may legitimately vary per EXR frame, but the
                # reference displayWindow and PAR are the original source's
                # frozen physical coordinate frame. A different displayWindow
                # would silently move annotations and pixels if accepted.
                contract = self._exr_canvas_contract
                expected = tuple(contract['display_window'])
                actual = payload.geometry.display_window
                if (not payload.color_metadata.get('exr_canvas_rectified') or
                        tuple(actual) != expected or
                        abs(float(payload.geometry.pixel_aspect) -
                            float(contract['pixel_aspect'])) > 1e-6):
                    self._decode_errors[frame] = (
                        'EXR displayWindow/PAR differs from the frozen '
                        'review source geometry; refusing unaligned pixels')
                    return None
            pixmap, status = exr.payload_to_pixmap(
                payload, self._policy.decode_width, self._color_state,
                with_status=True)
            if self._closed or revision != self._revision:
                return None
            if pixmap is None or pixmap.isNull():
                self._decode_errors[frame] = 'EXR display conversion failed'
                return None
        else:
            reader = QtGui.QImageReader(path)
            original = reader.size()
            if original.isValid() and original.width() > self._policy.decode_width:
                target_height = max(
                    1, int(original.height() * self._policy.decode_width /
                           original.width()))
                reader.setScaledSize(QtCore.QSize(
                    self._policy.decode_width, target_height))
            image = reader.read()
            if self._closed or revision != self._revision:
                return None
            if image.isNull():
                self._decode_errors[frame] = (reader.errorString() or
                                              'Image could not be decoded')
                return None
            if image.width() > self._policy.decode_width:
                image = image.scaledToWidth(
                    self._policy.decode_width, QtCore.Qt.SmoothTransformation)
            pixmap = QtGui.QPixmap.fromImage(image)
        if self._integrity_proof:
            proof_after = _stable_content_hash(path)
            if self._closed or revision != self._revision:
                return None
            if (proof_after is None or
                    _safe_original_path(self._files[frame]) != path or
                    any(proof_before[key] != proof_after[key]
                        for key in ('path', 'identity', 'size', 'sha256'))):
                self._decode_errors[frame] = (
                    'Integrity proof failed: original changed during decode')
                return None
            proof_after['source_frame'] = self._source_frame(frame)
        self._decode_errors.pop(frame, None)
        return self._cache_pixmap(frame, pixmap, payload=payload,
                                  status=status if payload is not None else None,
                                  file_proof=proof_after if self._integrity_proof
                                  else None, revision=revision)

    def ensure(self, frame):
        return

    def set_loop_start(self, frame):
        return

    def set_cache_mode(self, mode):
        if self._closed:
            return
        if mode != self._policy.cache_mode:
            self._revision += 1
        self._policy.cache_mode = mode
        if mode == CACHE_OFF:
            while len(self._cache) > 1:
                self._forget(next(iter(self._cache)))

    def cache_snapshot(self):
        return dict(mode='sequence', frames=sorted(self._cache),
                    cached=len(self._cache), capacity=self._policy.max_frames,
                    bytes=self._cache_bytes + self._payload_bytes,
                    pixmap_bytes=self._cache_bytes,
                    payload_bytes=self._payload_bytes,
                    payload_frames=sorted(self._payload_cache),
                    byte_budget=self._policy.cache_bytes,
                    decode_width=self._policy.decode_width,
                    missing_policy=self._missing_policy,
                    hole_count=len(self._holes),
                    decode_errors={self._source_frame(frame): reason
                                   for frame, reason in self._decode_errors.items()},
                    decoded=len(self._cache),
                    total=min(self._policy.max_frames, len(self._files)))

    def shutdown(self):
        if self._closed:
            return
        self._closed = True
        self._revision += 1
        self._cache.clear()
        self._cache_bytes = 0
        self._payload_cache.clear()
        self._payload_bytes = 0
        self._display_status.clear()
        self._decode_errors.clear()
        self._file_proofs.clear()


# VC_ROLLING_CACHE_RANGE_V1

class FramePump(QtCore.QObject):
    """Elapsed-clock playback that holds the last good frame during buffering."""
    frameChanged = Signal(int)
    stopped = Signal()
    bufferingChanged = Signal(bool)

    def __init__(self, source, fps, frame_count, parent=None):
        QtCore.QObject.__init__(self, parent)
        self._source = source
        self._fps = max(0.001, float(fps))
        self._last_frame = max(0, int(frame_count) - 1)
        self._timer = QtCore.QTimer(self)
        self._timer.setTimerType(QtCore.Qt.PreciseTimer)
        self._timer.setInterval(8)
        self._timer.timeout.connect(self._tick)
        self._clock = QtCore.QElapsedTimer()
        self._anchor = 0
        self.frame = 0
        self.in_frame = 0
        self.out_frame = 0
        self.loop = False
        self.loop_mode = playback_policy.ONCE
        self.speed = 1.0
        self._direction = 1
        self.playback_policy = playback_policy.REALTIME
        self.presented_frames = 0
        self.dropped_frames = 0
        self.repeated_ticks = 0
        self._buffering = False
        self._external_clock = None
        self._external_wrap = None
        # Decode a short contiguous runway before starting or resuming. This
        # absorbs FFmpeg/GUI scheduling jitter without adding perceptible wait.
        self._runway_frames = max(3, min(12, int(round(self._fps * 0.25))))

    def set_external_clock(self, get_ms, on_wrap=None):
        """Use soundtrack time as playback master when audio is available."""
        self._external_clock = get_ms
        self._external_wrap = on_wrap

    def is_buffering(self):
        return self._buffering

    def _set_buffering(self, buffering):
        buffering = bool(buffering)
        if self._buffering != buffering:
            self._buffering = buffering
            self.bufferingChanged.emit(buffering)

    def is_playing(self):
        return self._timer.isActive()

    def _has_runway(self):
        checker = getattr(self._source, 'contiguous_available', None)
        if checker is not None:
            return checker(self.frame, self._runway_frames, self._direction)
        return self._source.get(self.frame) is not None

    def _ensure(self, frame, direction=None):
        if direction is None:
            direction = self._direction
        try:
            self._source.ensure(frame, direction)
        except TypeError:
            # Image sequences and older plugin sources only take a frame.
            self._source.ensure(frame)

    def set_loop_mode(self, mode):
        self.loop_mode = playback_policy.validate_loop_mode(mode)
        self.loop = mode != playback_policy.ONCE
        if self.is_playing():
            self._anchor = self.frame
            self._clock.restart()

    def _transport_mode(self):
        # Preserve callers assigning ``pump.loop`` directly.
        return (self.loop_mode if self.loop and
                self.loop_mode == playback_policy.PING_PONG else
                playback_policy.LOOP if self.loop else playback_policy.ONCE)

    def _effective_out(self):
        """Honor a clean FFmpeg EOF earlier than ffprobe's frame estimate."""
        return min(self.out_frame,
                   getattr(self._source, 'last_available_frame', self._last_frame))

    def play(self):
        if self._timer.isActive():
            return
        end = self._effective_out()
        if self.in_frame >= end:
            self.seek(end, restrict_to_range=False)
            self.stopped.emit()
            return
        at_forward_end = self._direction > 0 and self.frame >= end
        at_reverse_end = self._direction < 0 and self.frame <= self.in_frame
        if (self._transport_mode() == playback_policy.PING_PONG and
                self.in_frame <= self.frame <= end and
                (at_forward_end or at_reverse_end)):
            self._direction *= -1
        elif (self.frame < self.in_frame or self.frame > end or
              at_forward_end or at_reverse_end):
            self.seek(self.in_frame if self._direction > 0 else end)
        self._anchor = self.frame
        self._clock.start()
        self._ensure(self.frame)
        self._set_buffering(not self._has_runway())
        self._timer.start()

    def pause(self):
        if self._timer.isActive():
            self._timer.stop()
            self.stopped.emit()
        self._set_buffering(False)

    def set_speed(self, speed):
        speed = float(speed)
        self.speed = (0.0 if speed == 0.0 else
                      (-1 if speed < 0 else 1) * max(0.01, abs(speed)))
        self._direction = -1 if self.speed < 0 else 1
        if self.is_playing():
            self._anchor = self.frame
            self._clock.restart()
        self._ensure(self.frame)

    def set_playback_policy(self, policy):
        self.playback_policy = playback_policy.validate_playback_policy(policy)
        if self.is_playing():
            self._anchor = self.frame
            self._clock.restart()

    def stats_snapshot(self):
        return dict(policy=self.playback_policy,
                    presented=self.presented_frames,
                    dropped=self.dropped_frames,
                    repeated_ticks=self.repeated_ticks,
                    buffering=self._buffering,
                    frame=self.frame, speed=self.speed,
                    direction=self._direction,
                    loop_mode=self._transport_mode())

    def seek(self, frame, restrict_to_range=True):
        low = self.in_frame if restrict_to_range else 0
        high = self.out_frame if restrict_to_range else self._last_frame
        high = min(high, getattr(self._source, 'last_available_frame', self._last_frame))
        low = min(low, high)
        self.frame = int(utils.clamp(frame, low, high))
        self._ensure(self.frame)
        self._set_buffering(not self._has_runway())
        if self.is_playing():
            self._anchor = self.frame
            self._clock.restart()
        self.frameChanged.emit(self.frame)

    def _tick(self):
        end = self._effective_out()
        if self.in_frame >= end:
            self.pause()
            return
        if self._buffering:
            self._ensure(self.frame)
            if not self._has_runway():
                return
            self._set_buffering(False)
            self._anchor = self.frame
            self._clock.restart()
        if (self._external_clock is not None and self.speed > 0 and
                self._transport_mode() != playback_policy.PING_PONG and
                self.playback_policy == playback_policy.REALTIME):
            position_ms = self._external_clock()
            if position_ms is not None:
                candidate = utils.ms_to_frame(position_ms, self._fps)
                if candidate > end:
                    if self._transport_mode() == playback_policy.LOOP:
                        if self._external_wrap is not None:
                            self._external_wrap(self.in_frame)
                        self.seek(self.in_frame)
                    else:
                        self.seek(end, restrict_to_range=False)
                        self.pause()
                    return
                candidate = int(utils.clamp(candidate, self.in_frame, end))
                self._ensure(candidate)
                if self._source.get(candidate) is None:
                    # Pause both clocks until a contiguous runway is ready;
                    # resuming one frame at a time creates visible judder.
                    self._set_buffering(True)
                    return
                self._set_buffering(False)
                if candidate != self.frame:
                    self.frame = candidate
                    self.frameChanged.emit(self.frame)
                return
        if self.frame > end or self.frame < self.in_frame:
            self.seek(max(self.in_frame, min(end, self.frame)),
                      restrict_to_range=False)
        mode = self._transport_mode()
        boundary = end if self._direction > 0 else self.in_frame
        if mode == playback_policy.ONCE and self.frame == boundary:
            self.pause()
            return
        if self._source.get(self.frame) is None:
            self._set_buffering(True)
            self._ensure(self.frame)
            self._anchor = self.frame
            self._clock.restart()
            return
        if self.speed == 0.0:
            self.repeated_ticks += 1
            return
        candidate = playback_policy.candidate_frame(
            self.playback_policy, self.frame, self._anchor,
            self._clock.elapsed(), self._fps,
            self._direction * abs(self.speed))
        if ((candidate - self.frame) * self._direction <= 0):
            self.repeated_ticks += 1
            self._set_buffering(False)
            self._ensure(self.frame)
            return
        original = candidate
        candidate, reflected = playback_policy.transport_frame(
            candidate, self.in_frame, end, mode)
        new_direction = (-self._direction if reflected else self._direction)
        wrapped = original < self.in_frame or original > end
        self._ensure(candidate, new_direction)
        if self._source.get(candidate) is None:
            self._set_buffering(True)
            # Seek ahead of the existing decode window when a high playback
            # rate or GUI hiccup skips over the prefetched frames. Otherwise
            # the clock can repeatedly wait for a frame nobody requested.
            self._ensure(candidate)
            self._anchor = self.frame
            self._clock.restart()
            return
        self._set_buffering(False)
        if self.playback_policy == playback_policy.REALTIME:
            self.dropped_frames += max(0, abs(original - self.frame) - 1)
        self.frame = candidate
        if wrapped:
            self._direction = new_direction
            self._anchor = self.frame
            self._clock.restart()
        self.presented_frames += 1
        self.frameChanged.emit(self.frame)
        if self.playback_policy == playback_policy.PLAY_ALL:
            self._anchor = self.frame
            self._clock.restart()
        if mode == playback_policy.ONCE and self.frame == boundary:
            self.pause()
        else:
            self._ensure(self.frame)

    def pixmap(self):
        return self._source.get(self.frame)
