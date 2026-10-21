# -*- coding: utf-8 -*-
"""Optical-flow crop tracking with stable translation/similarity/affine/projective models.

All tracking takes place in the same post-flip image coordinates as the UI and
export. Optical flow uses several image features, even in 1-point mode; the
mode controls the degrees of freedom in the fitted transform. The exported
quadrilateral carries distortion information that a centre alone would lose.
OpenCV and NumPy are optional until the user explicitly requests tracking.
"""
from __future__ import absolute_import

import os
import select
import subprocess
import time

from .qtcompat import QtCore, Signal
from .ffmpegtools import _no_window_kwargs, media_environment

try:
    import cv2
    import numpy as np
    HAVE_CV2 = True
except ImportError:
    cv2 = None
    np = None
    HAVE_CV2 = False


MODES = (
    (1, '1 Point · Position'),
    (2, '2 Points · Rotation / Scale'),
    (3, '3 Points · Affine'),
    (4, 'Perspective · 4+ Features'),
)


def _crop_corners(crop):
    x, y, w, h = crop
    return np.array([[x, y], [x + w, y], [x + w, y + h],
                     [x, y + h]], dtype=np.float64)


def _transformed_points(matrix, points):
    """Apply a homogeneous 3×3 matrix to a list of xy coordinates."""
    homogeneous = np.concatenate(
        (points, np.ones((len(points), 1), dtype=np.float64)), axis=1)
    output = np.dot(homogeneous, matrix.T)
    denominators = output[:, 2:3]
    if np.any(np.abs(denominators) < 1e-7):
        raise ValueError('Degenerate tracking perspective')
    return output[:, :2] / denominators


def _motion(previous_points, next_points, mode):
    """Find robust frame-to-frame motion; return None for insufficient matches."""
    a = np.asarray(previous_points, dtype=np.float32).reshape((-1, 2))
    b = np.asarray(next_points, dtype=np.float32).reshape((-1, 2))
    required = {1: 1, 2: 2, 3: 3, 4: 4}[mode]
    if len(a) < required:
        return None
    if mode == 1:
        # Median flow resists outlier features while keeping one-point motion.
        translation = np.median(b - a, axis=0)
        result = np.eye(3, dtype=np.float64)
        result[:2, 2] = translation
        return result
    if mode == 2:
        affine, mask = cv2.estimateAffinePartial2D(
            a, b, method=cv2.RANSAC, ransacReprojThreshold=2.25,
            maxIters=1000, confidence=0.98)
    elif mode == 3:
        affine, mask = cv2.estimateAffine2D(
            a, b, method=cv2.RANSAC, ransacReprojThreshold=2.25,
            maxIters=1000, confidence=0.98)
    else:
        matrix, mask = cv2.findHomography(
            a, b, cv2.RANSAC, 2.25, maxIters=1000, confidence=0.98)
        if matrix is None or mask is None or np.count_nonzero(mask) < 4:
            return None
        return np.asarray(matrix, dtype=np.float64)
    if affine is None or mask is None or np.count_nonzero(mask) < required:
        return None
    matrix = np.eye(3, dtype=np.float64)
    matrix[:2, :] = affine
    return matrix


def _plausible_motion(matrix, corners, width, height):
    """Reject LK outlier estimates that cause a one-frame crop explosion."""
    if not np.all(np.isfinite(matrix)):
        return False
    try:
        moved = _transformed_points(matrix, corners)
    except ValueError:
        return False
    size = np.linalg.norm(moved - np.roll(moved, 1, axis=0), axis=1)
    original = np.linalg.norm(corners - np.roll(corners, 1, axis=0), axis=1)
    ratios = size / np.maximum(original, 1.0)
    if np.any(ratios < 0.65) or np.any(ratios > 1.5):
        return False
    distance = np.linalg.norm(moved.mean(axis=0) - corners.mean(axis=0))
    return bool(distance <= max(40, .24 * max(width, height)))


def _smooth_quad(previous, current):
    """Light low-pass filtering to suppress subpixel jitter during playback."""
    if previous is None:
        return current
    movement = np.linalg.norm(current - previous, axis=1).max()
    # Larger intentional motion is followed promptly. Small noisy changes are
    # filtered more strongly; still frames settle without snap-back.
    alpha = .58 if movement < 1.5 else (.74 if movement < 6.0 else .91)
    filtered = previous + alpha * (current - previous)
    if movement < .12:
        return previous
    return filtered


class TrackingJob(QtCore.QThread):
    progress = Signal(int)
    # Centres are kept for existing consumers; quads encode perspective.
    result = Signal(object, object, object)  # centres, crop_size, quadrilaterals
    error = Signal(str)

    def __init__(self, ffmpeg, path, seq_files, fps, start, end, crop,
                 flip_h, flip_v, parent=None, mode=1, frame_size=None):
        QtCore.QThread.__init__(self, parent)
        self.ffmpeg = ffmpeg
        self.path = path
        self.seq_files = list(seq_files or [])
        self.fps = max(0.001, float(fps))
        # Keep QThread.start() callable.
        self.start_frame = int(start)
        self.end_frame = int(end)
        self.crop = tuple(map(int, crop))
        self.flip_h = bool(flip_h)
        self.flip_v = bool(flip_v)
        self.mode = int(mode)
        self.frame_size = tuple(map(int, frame_size)) if frame_size else None
        self._proc = None
        if self.mode not in (1, 2, 3, 4):
            raise ValueError('Unsupported tracking mode')

    def stop_now(self):
        """Interrupt a stalled streaming decoder during UI shutdown."""
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass

    def _read_frame(self, index):
        if self.seq_files:
            return cv2.imread(self.seq_files[index])
        cmd = [self.ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin',
               '-ss', '{:.7f}'.format(index / self.fps), '-i', self.path,
               '-frames:v', '1', '-f', 'image2pipe', '-vcodec', 'mjpeg',
               '-q:v', '3', 'pipe:1']
        kwargs = dict(stderr=subprocess.DEVNULL)
        kwargs.update(_no_window_kwargs())
        kwargs['env'] = media_environment()
        blob = subprocess.check_output(cmd, timeout=12, **kwargs)
        return cv2.imdecode(np.frombuffer(blob, np.uint8), cv2.IMREAD_COLOR)

    def _frames(self):
        """Stream contiguous video frames through one FFmpeg process.

        Seeking on every frame loses motion detail at edit/keyframe boundaries,
        consumes CPU and leads to inconsistent optical-flow input. Sequences
        are already frame-addressed and retain their regular image reader.
        """
        if self.seq_files or not self.frame_size:
            for index in range(self.start_frame, self.end_frame + 1):
                if self.isInterruptionRequested():
                    return
                yield self._read_frame(index)
            return
        width, height = self.frame_size
        frame_bytes = width * height * 3
        if frame_bytes < 3:
            raise RuntimeError('Invalid tracking frame dimensions')
        span = self.end_frame - self.start_frame + 1
        cmd = [self.ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin',
               '-ss', '{:.8f}'.format(self.start_frame / self.fps),
               '-i', self.path, '-frames:v', str(span), '-an', '-sn',
               '-pix_fmt', 'bgr24', '-f', 'rawvideo', 'pipe:1']
        kwargs = dict(stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                      bufsize=0)
        kwargs.update(_no_window_kwargs())
        kwargs['env'] = media_environment()
        proc = subprocess.Popen(cmd, **kwargs)
        self._proc = proc
        try:
            for index in range(self.start_frame, self.end_frame + 1):
                pixels = bytearray()
                last_progress = time.monotonic()
                while len(pixels) < frame_bytes:
                    if self.isInterruptionRequested():
                        return
                    if os.name != 'nt':
                        readable, _, _ = select.select([proc.stdout], [], [], .20)
                        if not readable:
                            if time.monotonic() - last_progress > 12.0:
                                raise RuntimeError(
                                    'Tracking decoder stalled at frame {}'.format(index))
                            continue
                    part = os.read(proc.stdout.fileno(),
                                   min(1024 * 1024, frame_bytes - len(pixels)))
                    if not part:
                        raise RuntimeError(
                            'Tracking decoder ended early at frame {}'.format(index))
                    pixels.extend(part)
                    last_progress = time.monotonic()
                yield np.frombuffer(pixels, np.uint8).reshape(
                    (height, width, 3))
        finally:
            self.stop_now()
            proc.wait()
            try:
                proc.stdout.close()
            except (OSError, ValueError):
                pass
            self._proc = None

    @staticmethod
    def _features(gray, region, retained=None):
        """Find corners within the moving crop, avoiding existing LK points."""
        height, width = gray.shape[:2]
        mask = np.zeros((height, width), dtype=np.uint8)
        vertices = np.round(region).astype(np.int32)
        cv2.fillConvexPoly(mask, vertices, 255)
        if retained is not None:
            for p in retained.reshape((-1, 2)):
                cv2.circle(mask, tuple(np.round(p).astype(int)), 7, 0, -1)
        points = cv2.goodFeaturesToTrack(
            gray, maxCorners=120, qualityLevel=.012, minDistance=6,
            mask=mask, blockSize=5)
        return points

    def run(self):
        try:
            if not HAVE_CV2:
                raise RuntimeError('Tracking needs OpenCV (cv2) and NumPy in Maya Python.')
            x, y, w, h = self.crop
            if w < 2 or h < 2:
                raise ValueError('Draw a crop area before tracking.')
            base = _crop_corners(self.crop)
            cumulative = np.eye(3, dtype=np.float64)
            smoothed = None
            previous = None
            points = None
            centers, quads = {}, {}
            span = self.end_frame - self.start_frame + 1
            if span <= 0:
                raise ValueError('Invalid tracking range.')
            for index, image in enumerate(self._frames(), self.start_frame):
                if self.isInterruptionRequested():
                    return
                if image is None:
                    raise RuntimeError('Could not decode frame {}'.format(index))
                if self.flip_h:
                    image = cv2.flip(image, 1)
                if self.flip_v:
                    image = cv2.flip(image, 0)
                gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
                height, width = gray.shape[:2]
                if previous is not None and points is not None and len(points):
                    advanced, status, _ = cv2.calcOpticalFlowPyrLK(
                        previous, gray, points, None, winSize=(31, 31), maxLevel=4,
                        criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, .01))
                    if advanced is not None and status is not None:
                        reverse, backward_status, _ = cv2.calcOpticalFlowPyrLK(
                            gray, previous, advanced, None, winSize=(31, 31),
                            maxLevel=4,
                            criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT,
                                      30, .01))
                        valid = status.reshape(-1).astype(bool)
                        if reverse is not None and backward_status is not None:
                            valid &= backward_status.reshape(-1).astype(bool)
                            valid &= (np.linalg.norm(
                                reverse.reshape(-1, 2) - points.reshape(-1, 2),
                                axis=1) < 1.75)
                        else:
                            valid[:] = False
                        before = points.reshape(-1, 2)[valid]
                        after = advanced.reshape(-1, 2)[valid]
                        proposed = _motion(before, after, self.mode)
                        last_quad = _transformed_points(cumulative, base)
                        if proposed is not None and _plausible_motion(
                                proposed, last_quad, width, height):
                            cumulative = np.dot(proposed, cumulative)
                        # Only retain trustworthy tracks. At low counts a fresh
                        # feature search is seeded around the estimated crop.
                        points = after.reshape((-1, 1, 2)).astype(np.float32)
                    else:
                        points = None
                current_quad = _transformed_points(cumulative, base)
                smoothed = _smooth_quad(smoothed, current_quad)
                quads[index] = [tuple(map(float, point)) for point in smoothed]
                centers[index] = tuple(map(float, smoothed.mean(axis=0)))
                if points is None or len(points) < 30 or index % 14 == 0:
                    fresh = self._features(gray, current_quad, points)
                    if fresh is not None:
                        points = fresh if points is None or not len(points) else np.concatenate(
                            (points, fresh), axis=0)
                # Uniform media offers no features; track the crop's middle as
                # a last resort, and hold a still crop if LK has no confidence.
                if points is None or not len(points):
                    points = current_quad.mean(axis=0).astype(np.float32).reshape(1, 1, 2)
                previous = gray
                if index % 5 == 0 or index == self.end_frame:
                    self.progress.emit(int(100 * (index - self.start_frame + 1) / span))
            self.result.emit(centers, (w, h), quads)
        except Exception as exc:
            self.error.emit(str(exc))
