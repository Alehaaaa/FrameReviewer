# -*- coding: utf-8 -*-
"""Export dialog and the ffmpeg export job.

Untracked exports run a single ffmpeg filter chain. Tracked exports stream raw
decoded frames into memory, crop each around the tracked centre, and re-encode
-- never dumping the full-resolution source to disk.
"""
from __future__ import absolute_import

import os
import subprocess
import sys
import tempfile

from .qtcompat import QtCore, QtWidgets, Signal
from .ffmpegtools import _no_window_kwargs, media_environment
from . import utils, tracking, style

JPEG_QUALITY = 98


class ExportOptionsDialog(QtWidgets.QDialog):
    def __init__(self, parent, input_path, default_dir):
        QtWidgets.QDialog.__init__(self, parent)
        self.setWindowTitle('Export Media')
        self.setMinimumWidth(540)
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(8)

        root.addWidget(QtWidgets.QLabel('Output name'))
        self.name_le = QtWidgets.QLineEdit(utils.name_from_path(input_path))
        root.addWidget(self.name_le)

        modes = QtWidgets.QHBoxLayout()
        self.video_cb = QtWidgets.QCheckBox('Video .mp4')
        self.sequence_cb = QtWidgets.QCheckBox('Image sequence .jpg')
        self.video_cb.setChecked(True)
        modes.addWidget(self.video_cb)
        modes.addWidget(self.sequence_cb)
        modes.addStretch()
        root.addLayout(modes)

        root.addWidget(QtWidgets.QLabel('Export directory'))
        directory = QtWidgets.QHBoxLayout()
        self.dir_le = QtWidgets.QLineEdit(default_dir)
        self.dir_le.setReadOnly(True)
        self.dir_btn = QtWidgets.QPushButton('Choose…')
        directory.addWidget(self.dir_le, 1)
        directory.addWidget(self.dir_btn)
        root.addLayout(directory)

        self.warning = QtWidgets.QLabel()
        self.warning.setWordWrap(True)
        self.warning.setStyleSheet('color:%s;' % style.WARNING)
        self.warning.hide()
        root.addWidget(self.warning)
        self.overwrite_cb = QtWidgets.QCheckBox('Overwrite existing selected outputs')
        self.overwrite_cb.hide()
        root.addWidget(self.overwrite_cb)

        actions = QtWidgets.QHBoxLayout()
        actions.addStretch()
        cancel = QtWidgets.QPushButton('Cancel')
        self.export_btn = QtWidgets.QPushButton('Export')
        actions.addWidget(cancel)
        actions.addWidget(self.export_btn)
        root.addLayout(actions)

        self.name_le.textChanged.connect(self._refresh)
        self.video_cb.toggled.connect(self._refresh)
        self.sequence_cb.toggled.connect(self._refresh)
        self.overwrite_cb.toggled.connect(self._refresh)
        self.dir_btn.clicked.connect(self._choose_dir)
        cancel.clicked.connect(self.reject)
        self.export_btn.clicked.connect(self._accept_checked)
        self._refresh()

    def name(self):
        return self.name_le.text().strip()

    def directory(self):
        return self.dir_le.text()

    def paths(self):
        directory, name = self.directory(), self.name()
        result = []
        if self.video_cb.isChecked():
            result.append(os.path.join(directory, name + '.mp4'))
        if self.sequence_cb.isChecked():
            result.append(os.path.join(directory, name, name + '.####.jpg'))
        return result

    def conflicts(self):
        directory, name = self.directory(), self.name()
        found = []
        if not utils.valid_name(name) or not os.path.isdir(directory):
            return found
        if self.video_cb.isChecked():
            path = os.path.join(directory, name + '.mp4')
            if os.path.lexists(path):
                found.append(path)
        if self.sequence_cb.isChecked():
            path = os.path.join(directory, name)
            if os.path.lexists(path):
                found.append(path)
        return found

    def _choose_dir(self):
        path = QtWidgets.QFileDialog.getExistingDirectory(self, 'Export directory', self.directory())
        if path:
            self.dir_le.setText(path)
            self._refresh()

    def _refresh(self, *args):
        valid = (utils.valid_name(self.name()) and os.path.isdir(self.directory())
                 and (self.video_cb.isChecked() or self.sequence_cb.isChecked()))
        conflicts = self.conflicts() if valid else []
        if not valid:
            message = 'Enter a valid name, directory, and select at least one output.'
        elif conflicts:
            message = 'Existing output will be replaced:\n' + '\n'.join(conflicts)
        else:
            message = ''
        self.warning.setText(message)
        self.warning.setVisible(bool(message))
        self.overwrite_cb.setVisible(bool(conflicts))
        if not conflicts and self.overwrite_cb.isChecked():
            self.overwrite_cb.blockSignals(True)
            self.overwrite_cb.setChecked(False)
            self.overwrite_cb.blockSignals(False)
        self.export_btn.setEnabled(bool(valid and (not conflicts or self.overwrite_cb.isChecked())))

    def _accept_checked(self):
        self._refresh()
        if self.export_btn.isEnabled():
            self.accept()


class ExportJob(QtCore.QThread):
    progress = Signal(int)
    result = Signal()
    error = Signal(str)

    def __init__(self, ffmpeg, source, seq_files, width, height, context, parent=None):
        QtCore.QThread.__init__(self, parent)
        self.ffmpeg = ffmpeg
        self.source = source
        self.seq_files = list(seq_files or [])
        self.width, self.height = int(width), int(height)
        self.ctx = dict(context)
        self._proc = None
        self._input_temp = None

    def stop_now(self):
        if self._proc is not None:
            try:
                self._proc.kill()
            except OSError:
                pass

    # -- source / filters -------------------------------------------------
    def _source_args(self):
        c = self.ctx
        start_ss = ['-ss', '{:.8f}'.format(c['start'] / max(0.001, c['source_fps']))]
        if self.seq_files:
            if self._input_temp is None:
                self._input_temp = tempfile.mkdtemp(prefix='framereviewer_export_')
                playlist = os.path.join(self._input_temp, 'images.ffconcat')
                with open(playlist, 'w') as handle:
                    handle.write('ffconcat version 1.0\n')
                    for frame in self.seq_files:
                        escaped = os.path.abspath(frame).replace("'", "'\\''")
                        handle.write("file '{}'\n".format(escaped))
                        handle.write('duration {:.12f}\n'.format(1.0 / max(0.001, c['source_fps'])))
            return start_ss + ['-safe', '0', '-f', 'concat', '-i',
                               os.path.join(self._input_temp, 'images.ffconcat')]
        return start_ss + ['-i', self.source]

    def _filters(self, moving=False):
        c = self.ctx
        filters = []
        # Moving crops are flipped in OpenCV before perspective rectification.
        # Reapplying ffmpeg flips would cancel the requested orientation.
        if c['flip_h'] and not moving:
            filters.append('hflip')
        if c['flip_v'] and not moving:
            filters.append('vflip')
        if not moving and c['crop']:
            x, y, w, h = c['crop']
            filters.append('crop={}:{}:{}:{}'.format(w, h, x, y))
        if not moving:
            filters.append('scale={}:{}'.format(c['out_w'], c['out_h']))
        if c['speed'] != 1.0:
            filters.append('setpts=PTS/{:.8f}'.format(c['speed']))
        return filters

    def _output_path(self):
        path = self.ctx['path']
        return path.replace('####', '%04d') if self.ctx['sequence'] else path

    def _final_args(self, src_args, moving=False):
        c = self.ctx
        args = [self.ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-y'] + src_args
        if not moving:
            args += ['-frames:v', str(c['count'])]
        filters = self._filters(moving=moving)
        if filters:
            args += ['-vf', ','.join(filters)]
        fps = c['fps']
        if c['sequence']:
            args += ['-vsync', '0', '-r', '{:.8f}'.format(fps), '-start_number', '1001',
                     '-q:v', '2', self._output_path()]
        else:
            args += ['-r', '{:.8f}'.format(fps), '-c:v', 'libx264', '-crf', '18',
                     '-preset', 'medium', '-pix_fmt', 'yuv420p', '-an', self._output_path()]
        return args

    def _execute(self, args):
        kwargs = dict(stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        kwargs.update(_no_window_kwargs())
        kwargs['env'] = media_environment()
        self._proc = subprocess.Popen(args, **kwargs)
        _, err = self._proc.communicate()
        code = self._proc.returncode
        self._proc = None
        if code:
            raise RuntimeError(err.decode('utf-8', 'replace')[-1500:])

    # -- tracked export ---------------------------------------------------
    def _nearest_crop(self, index):
        c = self.ctx
        original = c['crop'] or (0, 0, self.width, self.height)
        if not c['tracking_enabled'] or not c['centers']:
            return original
        centers = c['centers']
        nearest = min(centers, key=lambda f: abs(f - index))
        cx, cy = centers[nearest]
        dx, dy = c['tracking_offset']
        w, h = c['tracking_size'] or original[2:]
        w = int(utils.clamp(w, 2, self.width))
        h = int(utils.clamp(h, 2, self.height))
        x = int(utils.clamp(round(cx + dx - w / 2), 0, self.width - w))
        y = int(utils.clamp(round(cy + dy - h / 2), 0, self.height - h))
        return x, y, w, h

    def _nearest_quad(self, index):
        """Apply crop edits to the tracked corners in the displayed coordinate space."""
        c = self.ctx
        quads = c.get('tracking_quads') or {}
        if not quads:
            return None
        nearest = min(quads, key=lambda frame: abs(frame - index))
        corners = tracking.np.asarray(quads[nearest], dtype=tracking.np.float32)
        if corners.shape != (4, 2):
            return None
        centers = c['centers']
        nearest_center = min(centers, key=lambda frame: abs(frame - index))
        cx, cy = centers[nearest_center]
        base_w, base_h = c.get('tracking_base_size') or c.get('tracking_size') or (1, 1)
        current_w, current_h = c.get('tracking_size') or (base_w, base_h)
        offset_x, offset_y = c['tracking_offset']
        corners[:, 0] = cx + (corners[:, 0] - cx) * current_w / max(1.0, base_w) + offset_x
        corners[:, 1] = cy + (corners[:, 1] - cy) * current_h / max(1.0, base_h) + offset_y
        if not tracking.np.all(tracking.np.isfinite(corners)):
            return None
        return corners

    def _tracked(self):
        c = self.ctx
        cv2, np = tracking.cv2, tracking.np
        frame_bytes = self.width * self.height * 3
        cmd = ([self.ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin'] + self._source_args()
               + ['-frames:v', str(c['count']), '-an', '-sn', '-pix_fmt', 'bgr24', '-f', 'rawvideo', 'pipe:1'])
        tempdir = tempfile.mkdtemp(prefix='framereviewer_export_')
        proc = None
        try:
            kwargs = dict(stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=1024 * 1024)
            kwargs.update(_no_window_kwargs())
            kwargs['env'] = media_environment()
            proc = subprocess.Popen(cmd, **kwargs)
            self._proc = proc
            for i in range(c['count']):
                if self.isInterruptionRequested():
                    raise RuntimeError('Export cancelled')
                data = bytearray()
                while len(data) < frame_bytes:
                    chunk = proc.stdout.read(min(1024 * 1024, frame_bytes - len(data)))
                    if not chunk:
                        raise RuntimeError('FFmpeg decode ended at frame {}'.format(i))
                    data.extend(chunk)
                image = np.frombuffer(data, dtype=np.uint8).reshape((self.height, self.width, 3))
                if c['flip_h']:
                    image = cv2.flip(image, 1)
                if c['flip_v']:
                    image = cv2.flip(image, 0)
                quad = (self._nearest_quad(c['start'] + i)
                        if c.get('tracking_mode', 1) != 1 else None)
                if quad is not None:
                    # Perspective rectification also works for similarity and
                    # affine tracks, preserving rotation, scale, and shear.
                    dest = np.float32([[0, 0], [c['out_w'] - 1, 0],
                                       [c['out_w'] - 1, c['out_h'] - 1],
                                       [0, c['out_h'] - 1]])
                    warp = cv2.getPerspectiveTransform(quad.astype(np.float32), dest)
                    cropped = cv2.warpPerspective(
                        image, warp, (c['out_w'], c['out_h']),
                        flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)
                else:
                    x, y, w, h = self._nearest_crop(c['start'] + i)
                    cropped = cv2.resize(
                        image[y:y + h, x:x + w], (c['out_w'], c['out_h']),
                        interpolation=cv2.INTER_AREA)
                path = os.path.join(tempdir, 'crop_{:06d}.jpg'.format(i))
                if not cv2.imwrite(path, cropped, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY]):
                    raise RuntimeError('Cannot write processing frame {}'.format(path))
                if i % 5 == 0:
                    self.progress.emit(int(75 * (i + 1) / c['count']))
            proc.stdout.close()
            if proc.wait() != 0:
                raise RuntimeError('FFmpeg raw frame decode failed.')
            self._proc = None
            self.progress.emit(80)
            self._execute(self._final_args(
                ['-framerate', '{:.8f}'.format(c['source_fps']), '-start_number', '0',
                 '-i', os.path.join(tempdir, 'crop_%06d.jpg')], moving=True))
        finally:
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait()
            self._proc = None
            import shutil
            shutil.rmtree(tempdir, ignore_errors=True)

    def run(self):
        try:
            if self.ctx['tracking_enabled'] and self.ctx['centers']:
                if not tracking.HAVE_CV2:
                    raise RuntimeError('OpenCV required for tracked export')
                self._tracked()
            else:
                self._execute(self._final_args(self._source_args()))
            self.progress.emit(100)
            self.result.emit()
        except Exception as exc:
            self.error.emit(str(exc))
        finally:
            if self._input_temp:
                import shutil
                shutil.rmtree(self._input_temp, ignore_errors=True)
                self._input_temp = None
