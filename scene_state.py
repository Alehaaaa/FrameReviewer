# -*- coding: utf-8 -*-
"""Per-scene FrameReviewer state, stored inside the Maya file.

A reviewer's media, marked range, playhead, markers, flips, crop and Maya-sync
offset are written into the open scene so that reopening the scene reloads the
exact review setup. The state lives *only* in the scene, never in machine-level
preferences:

* ``fileInfo`` under :data:`STATE_KEY` travels *with the .ma/.mb file*. It is
  written eagerly (debounced) as the review state changes and again just before
  Maya serializes the scene (OpenMaya ``kBeforeSave``), so the data is always
  present in the file and reopens in any Maya -- not just the one that saved it.
  It is read back right after a scene opens (``kAfterOpen``).

No optionVar / userPrefs are used, so nothing persists at the system level.

The JSON payload is base64-encoded before it reaches ``fileInfo`` because Maya
escapes quotes and backslashes in fileInfo strings; base64 sidesteps all of
that and round-trips byte-for-byte. Nothing here raises into Maya: a bad or
absent payload simply means "no state to restore".
"""
from __future__ import absolute_import

import base64
import json
import os

from .qtcompat import QtCore

try:
    from maya import cmds
except Exception:                       # not inside Maya
    cmds = None

try:
    import maya.api.OpenMaya as om      # api 2.0, for before-save/after-open
except Exception:
    om = None

STATE_KEY = 'FrameReviewer_state'
SCHEMA = 1


# --------------------------------------------------------------- serialization
def _encode(state):
    return base64.b64encode(
        json.dumps(state).encode('utf-8')).decode('ascii')


def _decode(blob):
    if isinstance(blob, (list, tuple)):
        blob = blob[0] if blob else ''
    if not blob:
        return None
    text = str(blob)
    try:
        # Current format: base64 of UTF-8 JSON.
        return json.loads(base64.b64decode(text).decode('utf-8'))
    except Exception:
        pass
    try:
        # Tolerate a raw JSON payload (older/hand-written values).
        return json.loads(text)
    except Exception:
        return None


# --------------------------------------------------------------- capture/apply
def capture(window):
    """Build a JSON-serializable snapshot of the window's review state.

    Returns ``None`` when there is nothing worth restoring (no media loaded).
    """
    info = getattr(window, 'info', None)
    if info is None:
        return None
    player = window.player
    try:
        crop = window.surface.crop()
    except Exception:
        crop = None
    state = {
        'schema': SCHEMA,
        'source_path': info.path,
        'is_sequence': bool(getattr(info, 'is_sequence', False)),
        'fps': float(getattr(info, 'fps', 24.0)),
        'in_frame': int(player.in_frame),
        'out_frame': int(player.out_frame),
        'frame': int(player.current_frame),
        'markers': sorted(int(f) for f in getattr(window, '_markers', set())),
        'crop': list(crop) if crop else None,
        'flip_h': bool(window.act_flip_h.isChecked()),
        'flip_v': bool(window.act_flip_v.isChecked()),
        'speed': float(window.speed_combo.currentData() or 1.0),
        'muted': bool(player.muted),
        'volume': int(window.volume_slider.value()),
        'sync_enabled': bool(window.sync_btn.isChecked()),
        'sync_offset': int(window.sync_offset_spin.value()),
        'sync_player_to_maya': bool(window.sync_player_to_maya.isChecked()),
        'sync_maya_to_player': bool(window.sync_maya_to_player.isChecked()),
    }
    return state


def apply_state(window, state):
    """Restore a snapshot onto the window. Tolerant of partial/old payloads."""
    if not isinstance(state, dict):
        return
    path = state.get('source_path')
    if not path:
        return
    # Suppress eager fileInfo writes while we load media and move the playhead,
    # so a half-restored state never overwrites the scene's stored state.
    window._restoring_scene_state = True
    if not os.path.exists(path):
        try:
            window.status_lbl.setText(
                'FrameReviewer: saved media was not found: ' + path)
        except Exception:
            pass
        window._restoring_scene_state = False
        return

    # Load the media only when it is not already the current source, so
    # reopening the scene that is already loaded does not flash a reload.
    current = getattr(getattr(window, 'info', None), 'path', None)
    if not current or os.path.normpath(current) != os.path.normpath(path):
        try:
            window._load_path(path)
        except Exception:
            window._restoring_scene_state = False
            return
    if getattr(window, 'info', None) is None:
        window._restoring_scene_state = False
        return

    last = window.player.frame_count - 1

    def _try(fn):
        try:
            fn()
        except Exception:
            pass

    if 'in_frame' in state and 'out_frame' in state:
        lo = int(max(0, min(last, state['in_frame'])))
        hi = int(max(lo, min(last, state['out_frame'])))
        _try(lambda: window._set_marked_range(lo, hi))

    if state.get('markers'):
        frames = sorted({int(f) for f in state['markers'] if 0 <= int(f) <= last})
        def _markers():
            window._markers = set(frames)
            window.timeline.set_markers(tuple(frames))
        _try(_markers)

    if state.get('crop'):
        _try(lambda: window.surface.set_crop(tuple(state['crop'])))

    for act, key in ((window.act_flip_h, 'flip_h'), (window.act_flip_v, 'flip_v')):
        if key in state:
            _try(lambda a=act, k=key: a.setChecked(bool(state[k])))

    if 'speed' in state:
        choices = [window.speed_combo.itemData(i)
                   for i in range(window.speed_combo.count())]
        choices = [float(c) for c in choices if c is not None]
        if choices:
            _try(lambda: window._set_speed(
                min(choices, key=lambda s: abs(s - float(state['speed'])))))

    if 'muted' in state:
        _try(lambda: window._set_muted(bool(state['muted'])))
    if 'volume' in state:
        _try(lambda: window.volume_slider.setValue(
            int(max(0, min(100, state['volume'])))))

    if 'sync_offset' in state:
        _try(lambda: window.sync_offset_spin.setValue(int(state['sync_offset'])))
    for act, key in ((window.sync_player_to_maya, 'sync_player_to_maya'),
                     (window.sync_maya_to_player, 'sync_maya_to_player')):
        if key in state:
            _try(lambda a=act, k=key: a.setChecked(bool(state[k])))

    # Restore the playhead and re-arm Maya sync only after the freshly loaded
    # media has settled. A synchronous seek here races the decoder, which
    # presents its first frame and resets the playhead to 0, so the saved time
    # would be lost. Seek now and again on the next couple of event-loop turns
    # so the saved frame reliably wins.
    frame = int(max(0, min(last, state.get('frame', 0))))

    def _restore_playhead():
        if getattr(window, '_closed', False) or window.info is None:
            return
        try:
            window.player.seek(frame)
            window.timeline.set_frame(frame)
        except Exception:
            pass
        if state.get('sync_enabled'):
            try:
                window.sync_btn.setChecked(True)
            except Exception:
                pass

    _try(_restore_playhead)
    QtCore.QTimer.singleShot(0, _restore_playhead)
    QtCore.QTimer.singleShot(200, _restore_playhead)
    # Clear the guard after the last deferred restore, so eager scene-state
    # writes resume once everything is back in place.
    QtCore.QTimer.singleShot(
        250, lambda: setattr(window, '_restoring_scene_state', False))


# --------------------------------------------------------------- fileInfo store
def save_to_scene(window):
    """Write the current review state into the open scene's fileInfo."""
    if cmds is None:
        return
    state = capture(window)
    try:
        if state is None:
            if cmds.fileInfo(STATE_KEY, query=True):
                cmds.fileInfo(remove=STATE_KEY)
        else:
            cmds.fileInfo(STATE_KEY, _encode(state))
    except Exception:
        pass


def load_scene_state():
    """Return the state stored in the open scene, or ``None``."""
    if cmds is None:
        return None
    try:
        return _decode(cmds.fileInfo(STATE_KEY, query=True))
    except Exception:
        return None


# --------------------------------------------------------------- scene callbacks
class ScenePersistence(object):
    """Owns the Maya callbacks that save on write and restore on open.

    Uses OpenMaya ``kBeforeSave`` so the fileInfo captured belongs to the save
    in progress, and ``kAfterOpen`` to restore. Falls back to ``scriptJob``
    (post-save) when the API 2.0 message module is unavailable.
    """

    def __init__(self, window):
        self.window = window
        self._callback_ids = []
        self._script_jobs = []

    def install(self):
        if cmds is None:
            return
        self.remove()
        if om is not None:
            try:
                self._callback_ids.append(om.MSceneMessage.addCallback(
                    om.MSceneMessage.kBeforeSave, self._on_before_save))
                self._callback_ids.append(om.MSceneMessage.addCallback(
                    om.MSceneMessage.kAfterOpen, self._on_after_open))
                return
            except Exception:
                self._callback_ids = []
        # Fallback: scriptJob fires after the save, so the state lands in the
        # next save rather than this one, but restore on open is unaffected.
        try:
            self._script_jobs.append(cmds.scriptJob(
                event=['SceneSaved', self._safe_save], protected=False))
            self._script_jobs.append(cmds.scriptJob(
                event=['SceneOpened', self._safe_restore], protected=False))
        except Exception:
            self._script_jobs = []

    def remove(self):
        for cid in self._callback_ids:
            try:
                om.MMessage.removeCallback(cid)
            except Exception:
                pass
        self._callback_ids = []
        for job in self._script_jobs:
            try:
                if cmds.scriptJob(exists=job):
                    cmds.scriptJob(kill=job, force=True)
            except Exception:
                pass
        self._script_jobs = []

    # -- callback bodies (never raise into Maya) --------------------------
    def _alive(self):
        window = self.window
        return window is not None and not getattr(window, '_closed', False)

    def _on_before_save(self, *args):
        self._safe_save()

    def _on_after_open(self, *args):
        self._safe_restore()

    def _safe_save(self):
        if not self._alive():
            return
        try:
            save_to_scene(self.window)
        except Exception:
            pass

    def _safe_restore(self):
        if not self._alive():
            return
        state = load_scene_state()
        if state is None:
            return
        # Defer to the next event-loop turn so the scene is fully settled and
        # Maya is not still inside its open notification.
        QtCore.QTimer.singleShot(
            0, lambda: apply_state(self.window, state) if self._alive() else None)
