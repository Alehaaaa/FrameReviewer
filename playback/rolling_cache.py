# -*- coding: utf-8 -*-
"""Directional, memory-bounded rolling video cache with threaded FFmpeg decode.

JPEG parsing and QImage decoding happen in a QThread, never in Maya's GUI
thread. QPixmap creation happens in small, rate-limited batches in the GUI
thread because pixmaps are GUI resources. A bounded worker semaphore prevents
queued QImages from accumulating without limit when Maya's UI is busy.

A new playhead location starts decoding at the playhead, grows forward first,
and retains previously displayed frames behind. Forward decoders survive
rolling-window shifts.
There is no disk cache, poster extraction, or blocking QThread.wait on exit.
"""
from __future__ import absolute_import

import collections
import subprocess
import threading
import time

from ..core.qtcompat import QtCore, QtGui, Signal, Slot
from ..core import utils
from .cache_policy import (CachePolicy, CACHE_OFF, CACHE_REGION, ONCE, LOOP,
                           validate_loop_mode)
from ..media.ffmpeg import media_environment, _no_window_kwargs

# VC_SEAMLESS_THREADED_CACHE_V4
SHIFT_STEP = 12
TRANSFER_LIMIT = 8            # Max decoded QImages waiting for GUI upload.
UPLOAD_PER_TICK = 1           # One QPixmap upload per UI tick: avoid frame spikes.
UPLOAD_INTERVAL_MS = 8
SEEK_SETTLE_MS = 45           # Coalesce fast timeline scrubs before launching FFmpeg.
MAX_RUNNING_DECODERS = 3      # Includes workers still shutting down after a seek.
DECODE_STALL_SECONDS = 12.0   # Time waiting for FFmpeg to produce more stdout.
MAX_JPEG_BYTES = 16 * 1024 * 1024
LOOP_PREROLL_FRAMES = 12      # Matches the pump's maximum rebuffer runway.
# Optional movie precision is produced only for independently probed >8-bit
# SDR codec samples. Never derive it from a JPEG or claim scene-linear HDR.
MAX_PRECISION_MOVIE_PIXELS = 1024 * 1024
MAX_PRECISION_MOVIE_BYTES = MAX_PRECISION_MOVIE_PIXELS * 8


class _MoviePrecisionWorker(QtCore.QThread):
    """One bounded, cancellable raw 16-bit RGBA decode outside Maya UI."""
    decoded = Signal(int, int, object, str)

    def __init__(self, ffmpeg, path, frame, fps, generation, width, height,
                 source_id, par=1.0, parent=None):
        super(_MoviePrecisionWorker, self).__init__(parent)
        self.ffmpeg, self.path = ffmpeg, path
        self.frame, self.fps, self.generation = int(frame), fps, generation
        self.width, self.height = int(width), int(height)
        self.source_id, self.par = str(source_id), float(par)
        self._cancelled = threading.Event()
        self._proc = None
        self._lock = threading.Lock()

    def cancel(self):
        self._cancelled.set()
        with self._lock:
            proc = self._proc
        if proc is not None:
            try:
                if proc.poll() is None:
                    proc.kill()
            except OSError:
                pass

    def run(self):
        proc = None
        payload, error = None, ''
        try:
            from fractions import Fraction
            from ..core.frame_payload import FramePayload, FrameGeometry
            import numpy as np
            if (self.width * self.height > MAX_PRECISION_MOVIE_PIXELS or
                    self.width < 1 or self.height < 1):
                raise ValueError('Movie precision frame exceeds safe dimensions.')
            expected = self.width * self.height * 8
            seconds = Fraction(self.frame, 1) / self.fps
            args = [self.ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin',
                    '-ss', '{:.9f}'.format(float(seconds)), '-i', self.path,
                    '-an', '-sn', '-dn', '-frames:v', '1', '-threads', '2',
                    '-vf', 'scale={}:{}:flags=neighbor,format=rgba64le'.format(
                        self.width, self.height), '-f', 'rawvideo',
                    '-pix_fmt', 'rgba64le', 'pipe:1']
            kwargs = dict(stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, bufsize=0,
                          env=media_environment())
            kwargs.update(_no_window_kwargs())
            proc = subprocess.Popen(args, **kwargs)
            with self._lock:
                self._proc = proc
            if self._cancelled.is_set():
                proc.kill()
            # Exactly one fixed-size RGBA64 frame at bounded dimensions.
            # communicate() imposes a wall-time cap even on a stalled decoder;
            # stdout is capped by FFmpeg's scale/pix_fmt and -frames:v 1.
            data, _ = proc.communicate(timeout=8)
            if (not self._cancelled.is_set() and len(data) == expected and
                    proc.returncode == 0):
                rgba = np.frombuffer(data, dtype='<u2').reshape(
                    self.height, self.width, 4)
                payload = FramePayload(
                    rgba, source_id=self.source_id, source_frame=self.frame,
                    generation=self.generation, stage='source',
                    geometry=FrameGeometry(self.width, self.height,
                                           pixel_aspect=self.par),
                    alpha_mode='straight',
                    color_metadata={
                        'input_space': 'sRGB', 'movie_decode': 'rgba64le',
                        'movie_precision': '16-bit CPU SDR from >8-bit source',
                        'hdr_verified': False,
                        'input_assumption': 'SDR sRGB unless source color is configured'})
            else:
                error = 'FFmpeg 16-bit SDR movie decode unsupported or incomplete'
        except (OSError, RuntimeError, ValueError, ImportError,
                subprocess.TimeoutExpired, MemoryError) as exc:
            error = str(exc)
        finally:
            if proc is not None:
                try:
                    if proc.poll() is None:
                        proc.kill()
                    proc.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                if proc.stdout is not None:
                    proc.stdout.close()
            with self._lock:
                self._proc = None
            if not self._cancelled.is_set():
                self.decoded.emit(self.generation, self.frame, payload, error)


def split_jpegs(buffer):
    """Split complete JPEG images from FFmpeg's image2pipe byte stream."""
    frames = []
    offset = 0
    while True:
        start = buffer.find(b'\xff\xd8', offset)
        if start < 0:
            break
        end = buffer.find(b'\xff\xd9', start + 2)
        if end < 0:
            return frames, buffer[start:]
        frames.append(bytes(buffer[start:end + 2]))
        offset = end + 2
    return frames, bytearray()


def window_for_playhead(frame, capacity, first, last, direction=1,
                        lookback_percent=25):
    """Directional window with OpenRV's default 25% look-back allocation."""
    first = int(first)
    last = max(first, int(last))
    frame = int(utils.clamp(frame, first, last))
    count = min(last - first + 1, max(1, int(capacity)))
    lookback_percent = max(0, min(90, int(lookback_percent)))
    behind = int((count - 1) * lookback_percent / 100.0)
    ahead = count - 1 - behind
    if direction < 0:
        behind, ahead = ahead, behind
    low, high = frame - behind, frame + ahead
    if low < first:
        high += first - low
        low = first
    if high > last:
        low -= high - last
        high = last
    return max(first, low), min(last, high)


def contiguous_ranges(frames):
    """Compress actual resident frame numbers into intervals for painting."""
    ranges = []
    for frame in sorted(frames):
        if ranges and frame <= ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], max(frame, ranges[-1][1]))
        else:
            ranges.append((frame, frame))
    return ranges


class _DecodeWorker(QtCore.QThread):
    """Only background thread interacts with subprocess pipes or JPEG decode.

    Worker QObject lives in GUI thread as expected by Qt; only `run()` does
    blocking work. Signals carry QImages, never QPixmaps, across threads.
    """
    imageReady = Signal(int, int, object)        # generation, index, QImage
    decodeDone = Signal(int, bool, str, int)    # generation, success, error, frames emitted

    def __init__(self, ffmpeg, source, fps, start, end, generation,
                 decode_width, allowed_end=None, parent=None):
        super(_DecodeWorker, self).__init__(parent)
        self._ffmpeg = ffmpeg
        self._source = source
        self._fps = fps
        self.start_frame = int(start)
        self.end_frame = int(end)
        self._allowed_end = int(end if allowed_end is None else allowed_end)
        self.generation = generation
        self._decode_width = int(decode_width)
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._window_ready = threading.Condition(self._lock)
        self._proc = None
        self._permits = threading.Semaphore(TRANSFER_LIMIT)
        self._read_started = None
        self._timed_out = False

    def release_image(self):
        self._permits.release()

    def cancel(self):
        """Request prompt shutdown without waiting or blocking the UI."""
        self._cancel.set()
        with self._window_ready:
            proc = self._proc
            self._window_ready.notify_all()
        if proc is not None:
            try:
                if proc.poll() is None:
                    proc.kill()
            except (OSError, ValueError):
                pass

    def extend_to(self, frame):
        """Let a persistent forward decoder advance into a shifted window."""
        with self._window_ready:
            frame = min(self.end_frame, int(frame))
            if frame > self._allowed_end:
                self._allowed_end = frame
                self._window_ready.notify_all()

    def _watch_output(self, proc, stopped):
        """Unblock a stalled pipe read without involving Maya's event loop.

        Waiting for GUI upload permits is intentionally excluded: a busy GUI
        must not cause otherwise healthy FFmpeg decoding to time out.
        """
        while not stopped.wait(0.25):
            with self._lock:
                started = self._read_started
                if (started is None or self._cancel.is_set() or
                        time.monotonic() - started <= DECODE_STALL_SECONDS):
                    continue
                self._timed_out = True
                self._read_started = None
            try:
                if proc.poll() is None:
                    proc.kill()
            except (OSError, ValueError):
                pass
            return

    def run(self):
        proc = None
        completed = 0
        received = 0
        success = False
        message = ''
        stderr_parts = collections.deque(maxlen=8)
        monitor_stop = threading.Event()
        stderr_thread = None
        try:
            args = [
                self._ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin',
                '-ss', '{:.7f}'.format(self.start_frame / self._fps),
                '-i', self._source,
                '-an', '-vsync', '0',
                '-frames:v', str(self.end_frame - self.start_frame + 1),
                '-threads', '2',
                '-vf', "scale='min({},iw)':-2".format(self._decode_width),
                '-f', 'image2pipe', '-vcodec', 'mjpeg', '-q:v', '5', 'pipe:1',
            ]
            kwargs = dict(stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          bufsize=0, env=media_environment())
            kwargs.update(_no_window_kwargs())
            proc = subprocess.Popen(args, **kwargs)
            with self._lock:
                self._proc = proc
            def capture_stderr():
                while True:
                    data = proc.stderr.read(2048)
                    if not data:
                        break
                    stderr_parts.append(data)
            stderr_thread = threading.Thread(target=capture_stderr, daemon=True)
            stderr_thread.start()
            threading.Thread(target=self._watch_output,
                             args=(proc, monitor_stop), daemon=True).start()
            if self._cancel.is_set():
                proc.kill()
            remaining = bytearray()
            frame = self.start_frame
            while frame <= self.end_frame and not self._cancel.is_set():
                # Keep one FFmpeg process alive across rolling-window shifts,
                # but stop reading before it can outrun the bounded cache.
                with self._window_ready:
                    while (frame > self._allowed_end and
                           not self._cancel.is_set()):
                        self._window_ready.wait(0.25)
                if self._cancel.is_set():
                    break
                with self._lock:
                    self._read_started = time.monotonic()
                try:
                    chunk = proc.stdout.read(65536)
                finally:
                    with self._lock:
                        self._read_started = None
                if not chunk:
                    break
                remaining.extend(chunk)
                blobs, remaining = split_jpegs(remaining)
                if len(remaining) > MAX_JPEG_BYTES:
                    raise RuntimeError('FFmpeg emitted an oversized or incomplete JPEG frame.')
                for blob in blobs:
                    if self._cancel.is_set() or frame > self.end_frame:
                        break
                    with self._window_ready:
                        while (frame > self._allowed_end and
                               not self._cancel.is_set()):
                            self._window_ready.wait(0.25)
                    if self._cancel.is_set():
                        break
                    image = QtGui.QImage.fromData(blob, 'JPG')
                    index = frame
                    frame += 1
                    received += 1
                    if image.isNull():
                        continue
                    while not self._cancel.is_set():
                        if self._permits.acquire(timeout=0.05):
                            break
                    else:
                        break
                    if self._cancel.is_set():
                        self._permits.release()
                        break
                    self.imageReady.emit(self.generation, index, image)
                    completed += 1
            if not self._cancel.is_set():
                try:
                    # -frames:v caps FFmpeg's output to this exact batch.
                    # Keep the read end open until FFmpeg exits: closing it as
                    # soon as the last JPEG arrives can give a healthy FFmpeg
                    # process EPIPE/SIGPIPE while it finishes flushing.
                    code = proc.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    code = proc.wait(timeout=2)
                    message = 'FFmpeg did not exit after delivering frames.'
                if proc.stdout is not None:
                    proc.stdout.close()
                if stderr_thread is not None:
                    stderr_thread.join(timeout=0.5)
                stderr_tail = b''.join(stderr_parts)[-1800:]
                # Some FFmpeg versions return an encoder-init error at an
                # out-of-range seek even though no input frames exist. This
                # specific "before EOF" + empty-output combination is EOF,
                # provided the request started after frame zero.
                empty_eof = (not received and self.start_frame > 0 and
                             b'Could not open encoder before EOF' in stderr_tail and
                             b'Nothing was written into output file' in stderr_tail)
                if self._timed_out:
                    message = 'FFmpeg stalled for over {:.0f} seconds without output.'.format(
                        DECODE_STALL_SECONDS)
                elif message:
                    pass
                elif code != 0 and not empty_eof:
                    message = 'FFmpeg exited with code {} ({} frames decoded).'.format(
                        code, completed)
                elif received and completed != received:
                    message = 'FFmpeg produced {} unreadable JPEG frame(s).'.format(
                        received - completed)
                elif not received and self.start_frame == 0:
                    message = 'FFmpeg produced no decodable video frames.'
                elif remaining:
                    message = 'FFmpeg finished with an incomplete JPEG frame.'
                success = not message
                if not success:
                    # stderr is consumed on its own thread so FFmpeg cannot
                    # deadlock if it emits a large amount of diagnostic output.
                    tail = stderr_tail.decode('utf-8', 'replace').strip()
                    if tail:
                        message += '\n' + tail
        except Exception as exc:
            if not self._cancel.is_set():
                message = str(exc)
        finally:
            monitor_stop.set()
            if proc is not None:
                try:
                    if proc.poll() is None:
                        proc.kill()
                    proc.wait(timeout=2)
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    pass
                if proc.stdout is not None:
                    proc.stdout.close()
                if proc.stderr is not None:
                    proc.stderr.close()
            with self._lock:
                self._proc = None
            self.decodeDone.emit(self.generation, success or self._cancel.is_set(),
                                 message, completed)


class VideoFrameSource(QtCore.QObject):
    """Rolling frame store and non-blocking threaded FFmpeg scheduler."""
    frameAvailable = Signal(int)
    errorText = Signal(str)
    cacheChanged = Signal(object, object)       # cached ranges, planned window
    endReached = Signal(int)                   # Actual last frame on clean early EOF.

    def __init__(self, ffmpeg, source, fps, frame_count, parent=None,
                 cache_policy=None, precision=None):
        super(VideoFrameSource, self).__init__(parent)
        self._policy = cache_policy or CachePolicy()
        self._ffmpeg = ffmpeg
        self._source = source
        self._fps = max(0.001, float(fps))
        self._count = max(1, int(frame_count))
        self.last_available_frame = self._count - 1
        self._first, self._last = 0, self._count - 1
        self._loop_start = 0
        self._loop_enabled = False
        self._loop_mode = ONCE
        self._direction = 1
        self._current = 0
        self._capacity = self._policy.max_frames
        self._window_capacity = self._policy.max_frames
        self._window = window_for_playhead(
            0, self._policy.max_frames, 0, self._last, 1,
            self._policy.lookback_percent)
        self._cache = collections.OrderedDict()
        self._cache_bytes = 0
        self._pending = collections.deque()     # (worker, index, QImage)
        self._worker = None
        self._completion = None
        self._workers = set()                   # includes cancellation in progress
        self._generation = 0
        self._failed = False
        self._closed = False
        options = dict(precision or {})
        depth = options.get('bit_depth')
        try:
            depth = int(depth)
        except (ValueError, TypeError):
            depth = 0
        transfer = str((options.get('color_tags') or {}).get('color_transfer', '')).lower()
        supported_sdr_transfers = ('', 'unknown', 'unspecified', 'bt709',
                                   'iec61966-2-1', 'smpte170m', 'bt470bg')
        # This worker performs SDR RGB normalization, not PQ/HLG tone mapping.
        self._precision_enabled = bool(
            8 < depth <= 16 and transfer in supported_sdr_transfers and
            0 < int(options.get('width') or 0) * int(options.get('height') or 0) <=
            32 * MAX_PRECISION_MOVIE_PIXELS)
        self._precision_source_id = str(options.get('source_id') or source)
        self._precision_par = float(options.get('pixel_aspect_ratio') or 1.0)
        self._precision_fps = options.get('fps_rational') or fps
        from fractions import Fraction
        self._precision_fps = (Fraction(*self._precision_fps)
                               if isinstance(self._precision_fps, (tuple, list))
                               else Fraction(str(self._precision_fps)))
        w, h = int(options.get('width') or 1), int(options.get('height') or 1)
        scale = min(1., float(self._policy.decode_width) / max(w, 1),
                    (float(MAX_PRECISION_MOVIE_PIXELS) / max(1, w * h)) ** .5)
        self._precision_width = max(1, int(w * scale))
        self._precision_height = max(1, int(h * scale))
        self._precision_cache = collections.OrderedDict()
        self._precision_bytes = 0
        self._precision_generation = 0
        self._precision_worker = None
        self._precision_workers = set()
        self._precision_requested = None
        self._precision_error = ''
        self._state_timer = QtCore.QTimer(self)
        self._state_timer.setSingleShot(True)
        self._state_timer.setInterval(40)
        self._state_timer.timeout.connect(self._emit_state)
        self._upload_timer = QtCore.QTimer(self)
        self._upload_timer.setInterval(UPLOAD_INTERVAL_MS)
        self._upload_timer.timeout.connect(self._upload)
        self._seek_timer = QtCore.QTimer(self)
        self._seek_timer.setSingleShot(True)
        self._seek_timer.setInterval(SEEK_SETTLE_MS)
        self._seek_timer.timeout.connect(self._schedule)

    @property
    def cache_bytes(self):
        return self._cache_bytes

    def cached_ranges(self):
        return contiguous_ranges(self._cache)

    def cache_window(self):
        return self._window

    def cache_snapshot(self):
        """Return inexpensive diagnostics without exposing cache objects."""
        return dict(mode='video', frames=sorted(self._cache),
                    cached=len(self._cache), capacity=self._capacity,
                    bytes=self._cache_bytes,
                    byte_budget=self._policy.cache_bytes,
                    window=self._window, direction=self._direction,
                    worker_active=self._worker is not None,
                    pending_uploads=len(self._pending),
                    decode_width=self._policy.decode_width,
                    failed=self._failed,
                    precision_frames=sorted(self._precision_cache),
                    precision_bytes=self._precision_bytes,
                    precision_enabled=self._precision_enabled,
                    precision_error=self._precision_error)

    def get_payload(self, frame):
        """Actual FFmpeg >8-bit SDR movie samples, never reconstructed JPEGs.

        None means the movie's native precision is unavailable or decoding is
        still pending. The Qt preview remains valid while the CPU decode runs.
        Returned FramePayload owns immutable samples past cache eviction.
        """
        frame = int(frame)
        if self._closed or not self._precision_enabled or frame not in self._cache:
            return None
        payload = self._precision_cache.get(frame)
        if payload is not None:
            self._precision_cache.move_to_end(frame)
        # Inspection/scopes must be read-only; get()/upload start the optional
        # sidecar after actual video frames enter the visible cache.
        return payload

    def _request_precision(self, frame):
        if (not self._precision_enabled or self._closed or
                frame not in self._cache or frame in self._precision_cache or
                self._precision_requested == frame):
            return
        # Limit detached cancelled QThreads and FFmpeg processes under scrubs.
        live = [w for w in tuple(self._precision_workers) if w.isRunning()]
        if len(live) >= 2:
            self._cancel_precision()
            return
        self._cancel_precision()
        self._precision_generation += 1
        worker = _MoviePrecisionWorker(
            self._ffmpeg, self._source, frame, self._precision_fps,
            self._precision_generation, self._precision_width,
            self._precision_height, self._precision_source_id,
            self._precision_par, QtCore.QCoreApplication.instance())
        worker.decoded.connect(self._on_precision_decoded)
        worker.finished.connect(self._collect_precision_workers)
        self._precision_workers.add(worker)
        self._precision_worker = worker
        self._precision_requested = frame
        worker.start()

    def _cancel_precision(self):
        if self._precision_worker is not None:
            self._precision_worker.cancel()
        self._precision_worker = None
        self._precision_requested = None

    @Slot()
    def _collect_precision_workers(self):
        for worker in tuple(self._precision_workers):
            if worker is self._precision_worker:
                continue
            if not worker.isRunning():
                self._precision_workers.discard(worker)
                worker.deleteLater()

    @Slot(int, int, object, str)
    def _on_precision_decoded(self, generation, frame, payload, error):
        if (self._closed or self._precision_worker is None or
                generation != self._precision_generation or
                frame != self._precision_requested):
            return
        self._precision_worker = None
        self._precision_requested = None
        if payload is None:
            self._precision_error = error or 'No native movie sample available'
            self._precision_enabled = False  # Avoid repeated decoder storms.
            return
        if frame not in self._cache:
            return
        self._precision_cache[frame] = payload
        self._precision_bytes += payload.pixels.nbytes
        self._prune_precision()
        self.frameAvailable.emit(frame)

    def _prune_precision(self):
        budget = min(MAX_PRECISION_MOVIE_BYTES * 2,
                     max(MAX_PRECISION_MOVIE_BYTES, self._policy.cache_bytes // 4))
        while self._precision_cache and (
                len(self._precision_cache) > 2 or self._precision_bytes > budget):
            index, item = self._precision_cache.popitem(last=False)
            self._precision_bytes -= item.pixels.nbytes

    def set_playback_range(self, start, end):
        self._first = int(utils.clamp(start, 0, self._count - 1))
        self._last = int(utils.clamp(end, self._first, self._count - 1))
        self._loop_start = self._first
        self._move_window(self._current, force=True)
        self._schedule()

    def set_loop_start(self, frame):
        self._loop_start = int(utils.clamp(frame, self._first, self._last))
        self._mark_state()

    def set_loop(self, enabled):
        self.set_loop_mode(LOOP if enabled else ONCE)

    def set_loop_mode(self, mode):
        self._loop_mode = validate_loop_mode(mode)
        # Ping-pong turns around at the same edge; only wrapping needs a
        # separate, pinned cache runway at the opposite endpoint.
        self._loop_enabled = mode == LOOP
        self._move_window(self._current, force=True)
        self._schedule()

    def set_cache_mode(self, mode):
        self._policy.cache_mode = mode
        self._cancel_worker()
        self._move_window(self._current, force=True)
        self._schedule()

    def get(self, frame):
        frame = int(frame)
        image = self._cache.get(frame)
        if image is not None:
            self._cache.move_to_end(frame)
            if self._precision_enabled:
                self._request_precision(frame)
        return image

    def contiguous_available(self, frame, count, direction=1):
        """Whether a short playback runway is already resident in memory."""
        frame = int(frame)
        if self._policy.cache_mode == CACHE_OFF:
            count = 1
        count = max(1, int(count))
        boundary = (min(self._last, self.last_available_frame) if direction >= 0
                    else self._first)
        stop = (min(boundary, frame + count - 1) if direction >= 0
                else max(boundary, frame - count + 1))
        step = 1 if direction >= 0 else -1
        return all(index in self._cache
                   for index in range(frame, stop + step, step))

    def ensure(self, frame, direction=1):
        if self._closed or self._failed:
            return
        frame = int(utils.clamp(frame, 0, self.last_available_frame))
        large_seek = abs(frame - self._current) > max(4, self._capacity // 8)
        new_direction = 1 if direction >= 0 else -1
        direction_changed = self._direction != new_direction
        self._direction = new_direction
        if (large_seek and self._worker is not None and
                (frame < self._current or frame not in self._cache)):
            self._cancel_worker()
        self._move_window(frame, force=direction_changed)
        if large_seek:
            # Frequent slider events must not create one subprocess and QThread
            # per intermediate position. Cancel obsolete work immediately and
            # decode only the latest requested frame after the slider settles.
            self._seek_timer.start()
        else:
            self._schedule()

    def _move_window(self, frame, force=False):
        self._current = int(utils.clamp(frame, 0, self.last_available_frame))
        # The marked range constrains playback, not inspection. When scrubbing
        # outside it, build a temporary cache window around the requested frame.
        if self._first <= self._current <= self._last:
            window_first, window_last = self._first, min(self._last, self.last_available_frame)
        else:
            window_first, window_last = 0, self.last_available_frame
        reserve = (min(LOOP_PREROLL_FRAMES, self._capacity - 1)
                   if self._loop_enabled and
                   self._policy.cache_mode != CACHE_OFF else 0)
        capacity = max(1, self._capacity - reserve)
        if self._policy.cache_mode == CACHE_OFF:
            desired = (self._current, self._current)
        elif self._policy.cache_mode == CACHE_REGION:
            region = (window_first,
                      min(window_last, window_first + capacity - 1))
            # If the marked region is larger than RAM, retain its first chunk
            # like OpenRV and service explicit positions through a one-frame
            # temporary window instead of continuously evicting the region.
            desired = (region if region[0] <= self._current <= region[1]
                       else (self._current, self._current))
        else:
            desired = window_for_playhead(
                self._current, capacity, window_first, window_last,
                self._direction, self._policy.lookback_percent)
        if not force and self._window_capacity == capacity:
            low, high = self._window
            if low <= self._current <= high:
                if self._direction >= 0:
                    runway = high - self._current
                    lookback = int(
                        (capacity - 1) * self._policy.lookback_percent / 100.0)
                    ideal = capacity - 1 - lookback
                else:
                    runway = self._current - low
                    lookback = int(
                        (capacity - 1) * self._policy.lookback_percent / 100.0)
                    ideal = capacity - 1 - lookback
                if runway >= max(1, ideal - SHIFT_STEP):
                    return
        if force or desired != self._window or capacity != self._window_capacity:
            self._window_capacity = capacity
            self._window = desired
            if self._worker is not None and self._direction >= 0:
                self._worker.extend_to(desired[1])
            self._prune()
            self._mark_state()

    def _preserve(self, frame):
        if not self._loop_enabled:
            return False
        if self._direction < 0:
            last = min(self._last, self.last_available_frame)
            return max(self._first, last - LOOP_PREROLL_FRAMES + 1) <= frame <= last
        return self._loop_start <= frame <= min(
            self._loop_start + LOOP_PREROLL_FRAMES - 1,
            self._last, self.last_available_frame)

    def _remove(self, frame):
        image = self._cache.pop(frame, None)
        if image is not None:
            self._cache_bytes -= image.width() * image.height() * 4
        payload = self._precision_cache.pop(frame, None)
        if payload is not None:
            self._precision_bytes -= payload.pixels.nbytes

    def _prune(self):
        low, high = self._window
        for frame in list(self._cache):
            if not low <= frame <= high and not self._preserve(frame):
                self._remove(frame)
        while self._cache and (len(self._cache) > self._capacity or
                               (len(self._cache) > 1 and
                                self._cache_bytes > self._policy.cache_bytes)):
            candidates = [f for f in self._cache if not self._preserve(f)]
            if not candidates:
                candidates = list(self._cache)
            victim = max(candidates, key=lambda f: (f < low or f > high,
                                                    abs(f - self._current)))
            self._remove(victim)

    def _first_missing(self):
        low, high = self._window
        current = self._current
        if current not in self._cache:
            return current, high
        if self._direction >= 0:
            # Fill from current -> forward only. The 1/3 behind is retained
            # from frames already played, not redecoded by spawning one FFmpeg
            # process per historical frame (that was a major CPU spike).
            for index in range(current + 1, high + 1):
                if index not in self._cache:
                    return index, high
        else:
            # FFmpeg is a forward decoder. For explicit reverse prefetch,
            # decode a small segment ending at the nearest missing frame.
            for index in range(current - 1, low - 1, -1):
                if index not in self._cache:
                    return max(
                        low, index - self._policy.batch_frames + 1), index
        if self._loop_enabled and self._first <= current <= self._last:
            # Fill the opposite edge before wrapping; its short pre-roll stays
            # inside the same fixed frame/byte capacity.
            threshold = min(15, max(4, self._capacity // 5))
            playback_last = min(self._last, self.last_available_frame)
            if self._direction >= 0 and playback_last - current <= threshold:
                start = self._loop_start
                stop = min(start + LOOP_PREROLL_FRAMES - 1, playback_last)
                for index in range(start, stop + 1):
                    if index not in self._cache:
                        return index, stop
            elif self._direction < 0 and current - self._first <= threshold:
                stop = playback_last
                start = max(self._first, stop - LOOP_PREROLL_FRAMES + 1)
                for index in range(stop, start - 1, -1):
                    if index not in self._cache:
                        return start, stop
        return None

    def _schedule(self):
        if self._closed or self._failed or self._seek_timer.isActive():
            return
        target = self._first_missing()
        if self._worker is not None:
            if target is None:
                return
            requested = target[0]
            worker = self._worker
            # Window shifts do not interrupt a decoder supplying the next frames.
            if worker.start_frame <= requested <= worker.end_frame:
                return
            if (self._current in self._cache and self._direction >= 0 and
                    worker.start_frame <= self._current + 1 <= worker.end_frame):
                return
            # A true seek to an uncached region takes precedence over old work.
            obsolete = (self._current not in self._cache or
                        requested < worker.start_frame or
                        requested > worker.end_frame)
            if obsolete:
                self._cancel_worker()
        if self._worker is not None or self._pending:
            return
        if target is None:
            return
        live_workers = 0
        for worker in tuple(self._workers):
            try:
                live_workers += int(worker.isRunning())
            except RuntimeError:
                self._workers.discard(worker)
        if live_workers >= MAX_RUNNING_DECODERS:
            # Cancelled workers can still be flushing and releasing their
            # subprocess pipes. Avoid spawning unlimited decoders under scrub.
            self._seek_timer.start()
            return
        start, maximum = target
        # `maximum` already belongs to the active cache window. That window can
        # intentionally be outside the marked playback range while inspecting
        # source frames, so clamping it to `_last` can create stop < start.
        stop = min(maximum, self.last_available_frame)
        if self._direction < 0:
            stop = min(stop, start + self._policy.batch_frames - 1)
        if stop < start:
            return
        self._generation += 1
        # Forward playback keeps a single process alive. The worker blocks at
        # `stop` until the rolling window advances, avoiding periodic FFmpeg
        # startup/keyframe-seek stalls while retaining the same memory bound.
        decode_end = self.last_available_frame if self._direction >= 0 else stop
        worker = _DecodeWorker(self._ffmpeg, self._source, self._fps,
                               start, decode_end, self._generation,
                               self._policy.decode_width, stop,
                               QtCore.QCoreApplication.instance())
        worker.imageReady.connect(self._on_image)
        worker.decodeDone.connect(self._on_done)
        worker.finished.connect(self._collect_workers)
        self._workers.add(worker)
        self._worker = worker
        worker.start()

    @Slot()
    def _collect_workers(self):
        """Delete a thread only after every pending image has been uploaded.

        Deleting a QThread at finished() while queued QImages still reference
        its Python wrapper risks touching an invalid Shiboken object on upload.
        """
        for worker in tuple(self._workers):
            if worker is self._worker or any(
                    pending_worker is worker for pending_worker, _, _ in self._pending):
                continue
            try:
                if not worker.isRunning():
                    self._workers.discard(worker)
                    worker.deleteLater()
            except RuntimeError:
                self._workers.discard(worker)

    def _cancel_worker(self):
        worker = self._worker
        self._worker = None
        self._completion = None
        if worker is not None:
            worker.cancel()
        while self._pending:
            old_worker, _, _ = self._pending.popleft()
            old_worker.release_image()
        self._upload_timer.stop()

    @Slot(int, int, object)
    def _on_image(self, generation, frame, image):
        worker = self._worker
        if (self._closed or worker is None or
                worker.generation != generation):
            # The cancelled worker releases outstanding tokens as it notices
            # shutdown; prevent leaked worker-blocking transfers on a seek.
            for w in tuple(self._workers):
                if w.generation == generation:
                    w.release_image()
                    break
            return
        self._pending.append((worker, int(frame), image))
        if not self._upload_timer.isActive():
            self._upload_timer.start()

    @Slot()
    def _upload(self):
        count = 0
        while self._pending and count < UPLOAD_PER_TICK:
            worker, frame, image = self._pending.popleft()
            try:
                if (not self._closed and self._worker is worker and
                        not image.isNull()):
                    pixmap = QtGui.QPixmap.fromImage(image)
                    size = pixmap.width() * pixmap.height() * 4
                    if size > 0:
                        # Recalculate capacity whenever frame dimensions change.
                        capacity = min(
                            self._policy.max_frames,
                            max(1, self._policy.cache_bytes // size))
                        if capacity != self._capacity:
                            self._capacity = capacity
                            self._move_window(self._current, force=True)
                        self._remove(frame)
                        self._cache[frame] = pixmap
                        self._cache_bytes += size
                        self._prune()
                        self.frameAvailable.emit(frame)
                        if self._precision_enabled and frame == self._current:
                            self._request_precision(frame)
                        self._mark_state()
            finally:
                worker.release_image()
            count += 1
        if not self._pending:
            self._upload_timer.stop()
            if self._completion is not None:
                self._complete_worker(*self._completion)
            else:
                self._schedule()

    @Slot(int, bool, str, int)
    def _on_done(self, generation, success, message, decoded):
        worker = self._worker
        if worker is None or worker.generation != generation or self._closed:
            return
        if self._pending:
            # Uploads must finish before releasing the active worker. Otherwise
            # the last queued images would be silently discarded at each batch.
            self._completion = (success, message, decoded)
            return
        self._complete_worker(success, message, decoded)

    def _complete_worker(self, success, message, decoded):
        worker = self._worker
        self._completion = None
        self._worker = None
        self._collect_workers()
        if not success:
            self._failed = True
            self.errorText.emit('FFmpeg background decoding failed: ' + message)
        else:
            # FFmpeg may correctly hit end-of-stream before reaching the
            # requested frame cap. Treat a clean short batch as EOF, record
            # its upper bound, and never schedule those absent frames again.
            if worker is not None:
                requested = worker.end_frame - worker.start_frame + 1
                if decoded < requested:
                    actual_last = max(0, worker.start_frame + decoded - 1)
                    if actual_last < self.last_available_frame:
                        self.last_available_frame = actual_last
                        self._move_window(self._current, force=True)
                        self.endReached.emit(actual_last)
            self._schedule()
        self._mark_state()

    def _mark_state(self):
        if not self._closed and not self._state_timer.isActive():
            self._state_timer.start()

    def _emit_state(self):
        if not self._closed:
            self.cacheChanged.emit(self.cached_ranges(), self._window)

    def shutdown(self, wait=True):
        self._closed = True
        self._cancel_precision()
        for worker in tuple(self._precision_workers):
            worker.cancel()
        if wait:
            for worker in tuple(self._precision_workers):
                try:
                    if worker.isRunning():
                        worker.wait(2000)
                except RuntimeError:
                    pass
            self._collect_precision_workers()
        self._seek_timer.stop()
        self._upload_timer.stop()
        self._state_timer.stop()
        self._cancel_worker()
        workers = tuple(self._workers)
        for worker in workers:
            worker.cancel()
        stopped = True
        if wait:
            for worker in workers:
                try:
                    if worker.isRunning() and not worker.wait(2000):
                        stopped = False
                except RuntimeError:
                    pass
            self._collect_workers()
        self._cache.clear()
        self._cache_bytes = 0
        self._precision_cache.clear()
        self._precision_bytes = 0
        return stopped
