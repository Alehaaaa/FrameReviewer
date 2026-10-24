# -*- coding: utf-8 -*-
"""FrameReviewer: a dock-free Maya media review and finishing player.

The layout follows RV's shape -- a menu bar on top where every visual item and
playback setting has a home, a maximal central viewer, a slim side panel for
crop/output data, and a thin transport at the bottom. The window owns no
playback logic; it binds widgets and menu actions to the Player's state and to
the ffmpeg jobs (waveform, thumbnails, tracking, export).
"""
from __future__ import absolute_import

import bisect
import copy
import hashlib
import html
import json
import os
import shutil
import tempfile
import glob
import sys
import uuid

from ..core.qtcompat import (QtCore, QtGui, QtWidgets, QAction, QActionGroup, QShortcut,
                       HAVE_MULTIMEDIA, HAVE_MAYA_DOCKING,
                       MayaQWidgetDockableMixin, maya_main_window,
                       add_to_current_maya_layout, cmds)
from .. import style
from ..core import utils
from ..core.support_report import report_json
from ..media import ffmpeg as ffmpegtools
from ..viewer import tracking
from ..viewer.surface import PlaybackSurface
from ..viewer.timeline import MarkedTimelineSlider
from ..playback.player import Player
from ..playback.audio_routing import AudioRoute
from ..playback.cache_policy import CachePolicy
from ..playback.waveform import WaveformJob
from ..playback.record_waveform import RecordWaveformPreview
from ..playback.thumbnails import ThumbnailStripJob
from ..viewer.tracking import TrackingJob
from ..exporting import ExportOptionsDialog, ExportJob
from ..exporting.spec import ExportSpec
from ..exporting.queue_store import ExportQueueStore
from ..exporting.queue_runner import (ExportQueueRunner, QueueBusyError,
                                      existing_export_job_factory)
from ..integrations.shotgrid.widgets import ShotGridPanel
from .settings import Settings
from ..core.undo import UndoStack
from . import scene_state
from .scene_state import ScenePersistence
from .session_controller import SessionController
from ..media.resolver import SourceResolver
from ..viewer.color import ColorCorrectionDialog, ColorPipelineState
from ..media.playlist import PlaylistPanel
from ..playback import composition as composition_mod
from ..notes import serialization as note_serialization
from ..notes.review_records import (ReviewDocument, ReviewSurfaceAdapter,
                                    ReviewProjectionError, ReviewProjectionStale)
from ..notes.tool_registry import TOOL_SPECS as NOTE_TOOL_SPECS, TOOL_BY_ID, tool_defaults
from ..tools import WORKSPACE_TOOLS
from ..widgets import CompactStack, OptionBoxAction, VolumeButton
from ..widgets.sidebar_style import sidebar_stylesheet
from ..widgets.review_panel import ReviewRecordsPanel

THUMB_LANE_H = 36
SPEEDS = (0.25, 0.5, 1.0, 1.5, 2.0)
ICON_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'icons')


class FrameReviewerWindow(MayaQWidgetDockableMixin, QtWidgets.QDialog):
    _instance = None
    _object_name = 'FramestoreFrameReviewer'
    _workspace_control = _object_name + 'WorkspaceControl'

    def __init__(self, parent=None):
        super(FrameReviewerWindow, self).__init__(parent=parent)
        self.setObjectName(self._object_name)
        self.setWindowTitle('FrameReviewer')
        self.resize(1180, 700)
        self.setAttribute(QtCore.Qt.WA_DeleteOnClose, True)

        self.ffmpeg = ffmpegtools.find_binary('ffmpeg')
        self.ffprobe = ffmpegtools.find_binary('ffprobe')

        self.info = None
        # A/B review: a user-set reference clip and the latest playblast, which
        # the Toggle Reference action swaps the viewer between.
        self._reference_path = None
        self._playblast_path = None
        # True while restoring saved scene state, so loading media then does not
        # overwrite the scene's stored state with the half-restored values.
        self._restoring_scene_state = False
        self._scene_restore_generation = 0
        self._scene_restore_loading = False
        self._navigation_generation = 0
        self._crop_edit_generation = 0
        self._closed = False
        self._shutting_down = False
        self._shutdown_clean = None
        self._shutdown_pending_jobs = []
        self._markers = set()

        # The one persisted-state store, created before any widget so their
        # build-time signals can read and write it without ordering guards.
        self.settings = Settings()
        self.session_controller = SessionController()
        self.source_resolver = SourceResolver(self.session_controller.mapper)
        self._restoring_review = False

        # tracking state
        self.tracking_enabled = False
        self.tracked_centers = {}
        self.tracked_quads = {}
        self.tracked_quality = {}
        self._stabilization_plan = None
        self.tracking_size = None
        self.tracking_base_size = None
        self.tracking_offset = QtCore.QPointF(0.0, 0.0)
        self._applying_tracked = False
        self._tracking_job = None

        # lane generation state
        self._waveform_ready = False
        self._thumbs_ready = False

        # export state
        self._export_job = None
        self._export_queue = []
        self._export_done = []
        self._export_failed = False
        self._export_queue_store = None
        self._export_runner = None
        # Export-only intent. These occurrence-specific audio choices are NOT
        # edits to the review Session and are never automatically saved.
        self._record_audio_override_key = None
        self._record_audio_overrides = {}

        # status-bar task progress (export, tracking, thumbnails, waveform, ...)
        self._tasks = {}
        self._task_order = []
        self._thumb_total = 1
        self._thumb_done = 0

        # undo/redo of drawings, erasings and range edits
        self._undo = UndoStack()
        self._drawing_snapshot = {}
        self._review_adapter = None
        self._review_documents = {}
        self._range_snapshot = (0, 0)
        self._suspend_undo = False
        self._history_frame = 0
        self._recent_drawing_edit = False
        self._navigation_step = None
        self._current_draw_mode = 'brush'
        self._draw_tool_sizes = tool_defaults()
        # Shared by the main Tools menu, tool rail and its context menu.
        self._tool_specs = WORKSPACE_TOOLS

        self.surface = PlaybackSurface()
        # Keyboard shortcuts are window-subtree scoped (WidgetWithChildrenShortcut)
        # so they work while docked. That needs a focus target inside the panel:
        # let both the window and the viewer accept focus.
        self.setFocusPolicy(QtCore.Qt.StrongFocus)
        self.surface.setFocusPolicy(QtCore.Qt.StrongFocus)
        # Must exist before _build_ui(): _build_side() assigns playlist_panel
        # and later load reconciliation reads _composition.
        self.playlist_panel = None          # embedded sidebar playlist
        self._composition = None            # active multi-clip composition, if any
        self._applying_composition = False  # guards load<->playlist recursion
        # A transport seek is not evidence that new comparison pixels were
        # presented. Inspection reads cached original native values only after
        # a matching framePresented receipt from the SAME CompositeSource and
        # QPixmap. This is read-only: never an autosave or second decoder.
        self._comparison_presented_receipt = None
        self._build_ui()
        # Scope the inspector theme; Maya's menus, viewer, transport and color
        # management retain their own palette and rendering behavior.
        self.setStyleSheet(sidebar_stylesheet())

        self.cache_policy = CachePolicy.from_settings(self.settings)
        self.player = Player(
            self.ffmpeg, self.surface, self, self.cache_policy)
        self._init_persistent_exports()
        self.waveform_job = WaveformJob(self.ffmpeg, self)
        self.record_waveform = RecordWaveformPreview(self)
        self._record_waveform_bins = {}
        self._record_waveform_edit_key = None
        self._record_audition_spec = None
        self._record_overlay_key = None
        self._record_overlay_choice = None
        self.thumb_job = ThumbnailStripJob(self.ffmpeg, self)
        self._sync_offset = None
        self._sync_updating = False
        self._last_maya_frame = None
        self._queued_maya_frame = None
        self._queued_player_frame = None
        self._maya_sync_timer = QtCore.QTimer(self)
        self._maya_sync_timer.setInterval(20)
        self._maya_sync_timer.timeout.connect(self._sync_from_maya)
        # Writing Maya's currentTime synchronously inside a video frameChanged
        # callback re-enters Maya's evaluation and can crash during scrub/play.
        # Defer writes and cap their rate; likewise coalesce Maya scrub reads.
        self._maya_write_timer = QtCore.QTimer(self)
        self._maya_write_timer.setSingleShot(True)
        self._maya_write_timer.timeout.connect(self._flush_maya_write)
        self._maya_read_timer = QtCore.QTimer(self)
        self._maya_read_timer.setSingleShot(True)
        self._maya_read_timer.timeout.connect(self._flush_maya_scrub)

        # Save review state into the Maya scene on write; restore it on open.
        self._scene_persistence = ScenePersistence(self)
        # Recovery is deliberately infrequent. ScenePersistence already writes
        # fileInfo on Maya's actual scene save and we flush on window shutdown.
        # Normal navigation does not qualify as an edit or start this timer.
        self._scene_state_timer = QtCore.QTimer(self)
        self._scene_state_timer.setSingleShot(True)
        self._scene_state_timer.setInterval(120000)
        self._scene_state_timer.timeout.connect(self._write_scene_state)
        self.color_dialog = None            # lazily created color-correction tool
        self.scopes_dialog = None           # lazily created, generation-safe scopes

        # Project the session onto the playlist immediately. SessionController
        # predates all UI construction and survives decoder/representation swaps.
        self.playlist_panel.set_session(self.session_controller)

        self._connect()
        self._restore_preferences()
        self._current_flip_h = self.act_flip_h.isChecked()
        self._current_flip_v = self.act_flip_v.isChecked()
        self._connect_preference_saving()
        # Restore persisted onion settings after UI wiring; these settings are
        # display-only and never modify the exported annotation data.
        for control, setting in (
                (self.onion_enable, self.settings.get_bool('onion/enabled', False)),
                (self.onion_before, self.settings.get_int('onion/before', 2)),
                (self.onion_after, self.settings.get_int('onion/after', 2)),
                (self.onion_opacity, self.settings.get_int('onion/opacity', 35)),
                (self.onion_falloff, self.settings.get_bool('onion/falloff', True)),
                (self.onion_tint, self.settings.get_bool('onion/tint', True))):
            control.blockSignals(True)
            if isinstance(control, QtWidgets.QCheckBox):
                control.setChecked(setting)
            else:
                control.setValue(setting)
            control.blockSignals(False)
        self._update_onion_skin()
        self._set_enabled(False)
        self.surface.set_placeholder_visible(True)
        # Restore any media the current scene was reviewing (and arm the
        # save/open callbacks for subsequent scene writes and opens).
        self._scene_persistence.install()
        self._restore_scene_state_on_launch()

    # ------------------------------------------------------------------ UI
    def _restore_preferences(self):
        settings = self.settings
        # First launch defaults to Loop ON. Once changed, respect the user.
        loop_default = 'loop' if settings.get_bool('playback/loop', True) else 'once'
        loop_mode = settings.get_str('playback/loop_mode', loop_default)
        if loop_mode not in ('once', 'loop', 'ping_pong'):
            loop_mode = loop_default
        self.act_loop.setChecked(loop_mode == 'loop')
        self.act_ping_pong.setChecked(loop_mode == 'ping_pong')
        self.player.set_loop_mode(loop_mode)
        playback_policy = settings.get_str('playback/policy', 'realtime')
        if playback_policy not in ('realtime', 'play_all'):
            playback_policy = 'realtime'
        self.act_realtime.setChecked(playback_policy == 'realtime')
        self.act_play_all.setChecked(playback_policy == 'play_all')
        self.player.set_playback_policy(playback_policy)
        cache_mode = self.cache_policy.cache_mode
        self.cache_mode_actions[cache_mode].setChecked(True)
        self.player.set_cache_mode(cache_mode)
        selected_mode = settings.get_int('tracking/mode', 1)
        selected_index = self.tracking_mode.findData(selected_mode)
        if selected_index >= 0:
            self.tracking_mode.setCurrentIndex(selected_index)
        self.act_thumbs.setChecked(settings.get_bool('view/thumbnails', True))
        self.act_wave.setChecked(settings.get_bool('view/waveform', True))
        first_organized_launch = not settings.contains('ui/organized_layout_v2')
        self.act_side.setChecked(
            True if first_organized_launch else
            settings.get_bool('view/side_panel', True))
        for act, key in ((self.act_toolbar, 'view/toolbar'),
                         (self.act_timeline, 'view/timeline'),
                         (self.act_transport, 'view/transport'),
                         (self.act_statusbar, 'view/statusbar')):
            act.setChecked(settings.get_bool(key, True))
        settings.set('ui/organized_layout_v2', True)
        self.act_crop.setChecked(settings.get_bool('view/crop_overlay', True))
        composition_mode = settings.get_str(
            'view/composition',
            'safe' if settings.get_bool('view/safe_areas', False) else 'off')
        composition_index = self.composition_combo.findData(composition_mode)
        self.composition_combo.setCurrentIndex(max(0, composition_index))
        self.surface.set_composition_mode(self.composition_combo.currentData())
        self._set_muted(settings.get_bool('playback/muted', False))
        self.volume_slider.setValue(
            int(utils.clamp(settings.get_int('playback/volume', 50), 0, 100)))
        for mode, default in tuple(self._draw_tool_sizes.items()):
            key = 'draw/width' if mode == 'brush' else 'draw/width/' + mode
            maximum = TOOL_BY_ID[mode].max_size
            self._draw_tool_sizes[mode] = int(utils.clamp(
                settings.get_int(key, default), 1, maximum))
        for control, key, default in (
            (self.draw_opacity, 'draw/opacity', 100),
            (self.draw_smoothing, 'draw/smoothing', 20),
        ):
            control.setValue(settings.get_int(key, default))
        self.draw_width.setValue(self._draw_tool_sizes['brush'])
        self.pressure_size_check.setChecked(settings.get_bool('draw/pressure_size', True))
        self.pressure_opacity_check.setChecked(settings.get_bool('draw/pressure_opacity', False))
        self._set_drawing_color(settings.get_str('draw/color', style.DRAW_DEFAULT))
        self._set_draw_mode('brush')
        self.sync_player_to_maya.setChecked(settings.get_bool('sync/player_to_maya', True))
        self.sync_maya_to_player.setChecked(settings.get_bool('sync/maya_to_player', True))
        self.sync_offset_spin.blockSignals(True)
        self.sync_offset_spin.setValue(settings.get_int('sync/offset', 0))
        self.sync_offset_spin.blockSignals(False)
        # The widget may start in its default state and never emit toggled.
        self.timeline.set_thumbnails_visible(self.act_thumbs.isChecked())
        self.timeline.set_waveform_visible(self.act_wave.isChecked())
        self.review_thumbs_check.setChecked(self.act_thumbs.isChecked())
        self.review_wave_check.setChecked(self.act_wave.isChecked())
        self.crop_overlay_check.setChecked(self.act_crop.isChecked())
        self.side.setVisible(self.act_side.isChecked())
        self.surface.set_crop_visible(self.act_crop.isChecked())
        self._apply_tool_visibility()
        desired_speed = settings.get_float('playback/speed', 1.0)
        self._set_speed(min(SPEEDS, key=lambda speed: abs(speed - desired_speed)))
        geometry = settings.get_bytes('window/geometry')
        if geometry is not None:
            self.restoreGeometry(geometry)
        splitter_state = settings.get_bytes('window/splitter_state')
        if splitter_state is not None:
            self._splitter.restoreState(splitter_state)

    def _connect_preference_saving(self):
        for action, key in (
            (self.act_loop, 'playback/loop'),
            (self.act_thumbs, 'view/thumbnails'),
            (self.act_wave, 'view/waveform'),
            (self.act_side, 'view/side_panel'),
            (self.act_toolbar, 'view/toolbar'),
            (self.act_timeline, 'view/timeline'),
            (self.act_transport, 'view/transport'),
            (self.act_statusbar, 'view/statusbar'),
            (self.act_crop, 'view/crop_overlay'),
            (self.sync_player_to_maya, 'sync/player_to_maya'),
            (self.sync_maya_to_player, 'sync/maya_to_player'),
        ):
            action.toggled.connect(
                lambda enabled, name=key: self.settings.set(name, bool(enabled)))
        self.act_realtime.toggled.connect(
            lambda enabled: enabled and
            self.settings.set('playback/policy', 'realtime'))
        self.act_play_all.toggled.connect(
            lambda enabled: enabled and
            self.settings.set('playback/policy', 'play_all'))
        for mode, cache_action in self.cache_mode_actions.items():
            cache_action.toggled.connect(
                lambda enabled, value=mode: enabled and
                self.settings.set('cache/mode', value))
        self.speed_combo.currentIndexChanged.connect(
            lambda *_: self.settings.set('playback/speed',
                 float(self.speed_combo.currentData() or 1.0)))
        self.tracking_mode.currentIndexChanged.connect(
            lambda *_: self.settings.set('tracking/mode',
                                        int(self.tracking_mode.currentData() or 1)))
        self.volume_slider.valueChanged.connect(
            lambda value: self.settings.set('playback/volume', int(value)))
        for control, key in (
            (self.draw_opacity, 'draw/opacity'),
            (self.draw_smoothing, 'draw/smoothing'),
        ):
            control.valueChanged.connect(
                lambda value, name=key: self.settings.set(name, int(value)))
        self.pressure_size_check.toggled.connect(
            lambda value: self.settings.set('draw/pressure_size', bool(value)))
        self.pressure_opacity_check.toggled.connect(
            lambda value: self.settings.set('draw/pressure_opacity', bool(value)))
        # Speed menu selection calls _set_speed() with combo signals blocked.
        for speed, action in self.speed_actions.items():
            action.triggered.connect(
                lambda _checked=False, value=speed:
                    self.settings.set('playback/speed', float(value)))

    def _save_preferences(self):
        self.settings.set('window/geometry', self.saveGeometry())
        self.settings.set('window/splitter_state', self._splitter.saveState())
        self.settings.sync()

    def _set_media_window_title(self, path=None):
        """Sync Qt's caption with Maya's separately managed workspace title.

        Maya retains workspaceControl labels independently of QWidget title and
        QSettings geometry, so always explicitly reset it for a fresh window.
        """
        title = ('FrameReviewer: ' + os.path.basename(path)) if path else 'FrameReviewer'
        self.setWindowTitle(title)
        if cmds is not None:
            try:
                if cmds.workspaceControl(self._workspace_control, query=True, exists=True):
                    cmds.workspaceControl(self._workspace_control, edit=True, label=title)
            except (RuntimeError, ValueError):
                pass

    def _reset_preferences(self):
        result = QtWidgets.QMessageBox.question(
            self, 'Reset FrameReviewer Preferences',
            'Clear saved FrameReviewer preferences? Defaults will be restored '
            'the next time you open FrameReviewer.',
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No)
        if result == QtWidgets.QMessageBox.Yes:
            # Wipe the store, then stop this live session from writing anything
            # back (its widgets keep their current state until the next launch).
            self.settings.clear()
            self.settings.suspend(True)
            self._populate_recent_menu()

    def _build_ui(self):
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(3)
        root.setMenuBar(self._build_menu())

        self._splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self._splitter.addWidget(self._build_workspace())
        self.side = self._build_side()
        self._splitter.addWidget(self.side)
        self._splitter.setSizes([820, 360])
        self.side.hide()
        root.addWidget(self._splitter, 1)

        self.timeline = MarkedTimelineSlider()
        root.addWidget(self.timeline)
        self.transport_bar = QtWidgets.QWidget()
        self.transport_bar.setLayout(self._build_transport())
        root.addWidget(self.transport_bar)

        self.progress = QtWidgets.QProgressBar()
        self.progress.setFixedWidth(210)
        self.progress.setTextVisible(True)
        self.progress.setAlignment(QtCore.Qt.AlignCenter)
        self.progress.hide()

        self.status_lbl = QtWidgets.QLabel('Import media to begin.')
        self.status_lbl.setOpenExternalLinks(True)
        self.status_lbl.setTextFormat(QtCore.Qt.RichText)
        self.progress.setFixedHeight(self.status_lbl.sizeHint().height())
        self.pixel_lbl = QtWidgets.QLabel('')
        self.pixel_lbl.setObjectName('frameReviewerPixelInspector')
        self.pixel_lbl.setToolTip('Mouse hover: displayed RGBA or native HDR source values. Pin A/B in View → Image Inspection.')
        self.pixel_lbl.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self.pixel_lbl.setMinimumWidth(245)
        self.pixel_lbl.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        self.status_bar = QtWidgets.QWidget()
        status_row = QtWidgets.QHBoxLayout(self.status_bar)
        status_row.setContentsMargins(4, 1, 4, 1)
        status_row.setSpacing(8)
        status_row.addWidget(self.progress, 0, QtCore.Qt.AlignVCenter)
        status_row.addWidget(self.status_lbl, 1, QtCore.Qt.AlignVCenter)
        status_row.addWidget(self.pixel_lbl, 0, QtCore.Qt.AlignVCenter)
        root.addWidget(self.status_bar)

        # Read-only nearest-neighbor inspection overlay is parented to the
        # actual image viewport, not the scene. It never changes annotation
        # hit-testing, crop coordinates, Source pixels or the review Session.
        self.pixel_magnifier_panel = QtWidgets.QFrame(self.surface.viewport())
        self.pixel_magnifier_panel.setObjectName('frameReviewerPixelMagnifier')
        self.pixel_magnifier_panel.setAttribute(
            QtCore.Qt.WA_TransparentForMouseEvents, True)
        self.pixel_magnifier_panel.setStyleSheet(
            '#frameReviewerPixelMagnifier {{ background:{}; border:1px solid {}; }}'
            .format(style.PANEL, style.BORDER))
        magnifier_layout = QtWidgets.QVBoxLayout(self.pixel_magnifier_panel)
        magnifier_layout.setContentsMargins(6, 6, 6, 6)
        magnifier_layout.setSpacing(3)
        self.pixel_magnifier_image = QtWidgets.QLabel(self.pixel_magnifier_panel)
        self.pixel_magnifier_image.setObjectName('frameReviewerMagnifierImage')
        self.pixel_magnifier_image.setAlignment(QtCore.Qt.AlignCenter)
        self.pixel_magnifier_image.setFixedSize(132, 132)
        magnifier_layout.addWidget(self.pixel_magnifier_image)
        self.pixel_magnifier_details = QtWidgets.QLabel(self.pixel_magnifier_panel)
        self.pixel_magnifier_details.setObjectName('frameReviewerMagnifierDetails')
        self.pixel_magnifier_details.setAlignment(QtCore.Qt.AlignCenter)
        self.pixel_magnifier_details.setWordWrap(True)
        magnifier_layout.addWidget(self.pixel_magnifier_details)
        self.pixel_magnifier_panel.setFixedWidth(148)
        self.pixel_magnifier_panel.hide()

    def _build_workspace(self):
        workspace = QtWidgets.QWidget()
        row = QtWidgets.QHBoxLayout(workspace)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)

        rail = QtWidgets.QFrame()
        rail.setObjectName('vcToolRail')
        self.toolbar = rail
        rail.setFixedWidth(50)
        tools = QtWidgets.QVBoxLayout(rail)
        tools.setContentsMargins(4, 9, 4, 7)
        tools.setSpacing(4)
        self._tool_buttons = {}
        self._tool_group = QtWidgets.QButtonGroup(self)
        self._tool_group.setExclusive(True)
        for key, _name, tooltip in self._tool_specs:
            if key in ('draw', 'crop', 'export'):
                divider = QtWidgets.QFrame()
                divider.setObjectName('vcRailDivider')
                divider.setFixedHeight(1)
                tools.addSpacing(6)
                tools.addWidget(divider)
                tools.addSpacing(6)
            button = QtWidgets.QToolButton()
            button.setObjectName('vcToolButton')
            button.setAutoRaise(True)          # flat; the stylesheet draws state
            button.setIcon(self._icon(key))
            button.setIconSize(QtCore.QSize(21, 21))
            button.setToolTip(tooltip)
            button.setAccessibleName(_name)
            button.setCheckable(True)
            button.setFixedSize(40, 40)
            font = button.font()
            font.setBold(True)
            button.setFont(font)
            button.clicked.connect(lambda _checked=False, name=key: self._show_tool(name))
            self._tool_group.addButton(button)
            self._tool_buttons[key] = button
            tools.addWidget(button)
        tools.addStretch(1)

        # Right-click the rail to choose which tools it shows.
        rail.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        rail.customContextMenuRequested.connect(
            lambda pos: self._show_toolbar_menu(rail.mapToGlobal(pos)))

        row.addWidget(rail)
        row.addWidget(self.surface, 1)
        self._tool_buttons['media'].setChecked(True)
        return workspace

    def _tool_enabled(self, key):
        """Whether a rail tool is turned on (settings are the source of truth)."""
        return self.settings.get_bool('toolbar/%s' % key, True)

    def _enabled_tools(self):
        return [key for key, _n, _t in self._tool_specs if self._tool_enabled(key)]

    def _apply_tool_visibility(self):
        """Show/hide each rail button to match its persisted enabled state."""
        for key, button in self._tool_buttons.items():
            button.setVisible(self._tool_enabled(key))

    def _show_toolbar_menu(self, global_pos):
        menu = QtWidgets.QMenu(self)
        header = menu.addAction('Tools')
        header.setEnabled(False)
        menu.addSeparator()
        for key, name, _tooltip in self._tool_specs:
            act = menu.addAction(name)
            act.setCheckable(True)
            act.setChecked(self._tool_enabled(key))
            act.toggled.connect(
                lambda enabled, k=key: self._set_tool_enabled(k, enabled))
        menu.exec_(global_pos) if hasattr(menu, 'exec_') else menu.exec(global_pos)

    def _set_tool_enabled(self, key, enabled):
        # Keep at least one tool in the rail.
        if not enabled and self._enabled_tools() == [key]:
            return
        self.settings.set('toolbar/%s' % key, bool(enabled))
        self._tool_buttons[key].setVisible(enabled)
        # If the active tool was just hidden, fall back to the first enabled one.
        if not enabled and self._tool_buttons[key].isChecked():
            for other in self._enabled_tools():
                self._show_tool(other)
                break

    def _icon(self, name):
        png = os.path.join(ICON_DIR, '{}.png'.format(name))
        return QtGui.QIcon(png if os.path.isfile(png) else
                           os.path.join(ICON_DIR, '{}.svg'.format(name)))

    def _add_option_box_item(self, menu, text, primary_slot, option_slot):
        """Add a menu row with a native-style option box (the square at right).

        Qt's QMenu has no native ``optionBox`` like Maya's MEL menus, so an
        OptionBoxAction renders one. The slots run deferred so the menu fully
        unwinds (and any dialog opens cleanly) after the click.
        """
        action = OptionBoxAction(text, menu)
        action.primaryTriggered.connect(
            lambda: QtCore.QTimer.singleShot(0, primary_slot))
        action.optionsTriggered.connect(
            lambda: QtCore.QTimer.singleShot(0, option_slot))
        menu.addAction(action)
        return action

    def _build_menu(self):
        bar = QtWidgets.QMenuBar(self)
        # Keep the menu inside the panel. On macOS a window QMenuBar otherwise
        # becomes the native global menu, where submenu aboutToShow does not
        # fire reliably -- which left Recent Media perpetually empty.
        bar.setNativeMenuBar(False)

        def action(menu, text, slot=None, checkable=False, checked=False, shortcut=None):
            act = QAction(text, self)
            act.setCheckable(checkable)
            act.setChecked(checked)
            if shortcut:
                shortcuts = shortcut if isinstance(shortcut, (tuple, list)) else (shortcut,)
                act.setShortcuts([QtGui.QKeySequence(value) for value in shortcuts])
                act.setShortcutContext(QtCore.Qt.WidgetWithChildrenShortcut)
                # A QAction added only to a menu scopes its shortcut to that
                # (never-focused) menu. Docked in Maya, FrameReviewer is not the
                # active top-level window, so the shortcut never resolves. Also
                # registering the action on the window makes the shortcut fire
                # whenever focus is anywhere inside the panel's widget tree.
                self.addAction(act)
            if slot is not None:
                if checkable:
                    act.toggled.connect(slot)
                else:
                    # triggered(checked) passes a bool; swallow it for no-arg slots.
                    act.triggered.connect(lambda *_: slot())
            menu.addAction(act)
            return act

        # Task-oriented Maya menu hierarchy. Common actions stay one click
        # away; submenus group less frequent options but never nest further.
        # Preserve shortcut ownership and QAction references while moving items.
        file_menu = bar.addMenu('File')
        self.act_open = action(file_menu, 'Open Media or Review…', self._choose_open,
                               shortcut='Ctrl+O')
        self.act_import = action(file_menu, 'Add Media…', self._choose_media, shortcut='I')
        self.recent_menu = file_menu.addMenu('Open Recent')
        self.recent_menu.aboutToShow.connect(self._populate_recent_menu)
        self._populate_recent_menu()
        file_menu.addSeparator()
        action(file_menu, 'Open Session…', self._choose_session)
        action(file_menu, 'Save Session', self._save_session, shortcut='Ctrl+S')
        action(file_menu, 'Save Session As…', self._save_session_as,
               shortcut='Ctrl+Shift+S')
        action(file_menu, 'Recover Session…', self._choose_recovery)
        file_menu.addSeparator()
        interchange_menu = file_menu.addMenu('Timeline Interchange')
        action(interchange_menu, 'Import EDL / OTIO…', self._import_timeline)
        action(interchange_menu, 'Export EDL / OTIO…', self._export_timeline)
        notes_menu = file_menu.addMenu('Notes Files')
        action(notes_menu, 'Import Notes…', self._import_notes)
        action(notes_menu, 'Export Notes…', self._export_notes)
        file_menu.addSeparator()
        self.act_export = action(file_menu, 'Export Media…', self._export, shortcut='Ctrl+E')
        file_menu.addSeparator()
        action(file_menu, 'Close FrameReviewer', self.close, shortcut='Ctrl+W')

        edit_menu = bar.addMenu('Edit')
        self.act_undo = action(edit_menu, 'Undo', self._undo_action,
                               shortcut=('Ctrl+Z', 'Z'))
        self.act_redo = action(edit_menu, 'Redo', self._redo_action,
                               shortcut=('Ctrl+Shift+Z', 'Ctrl+Y'))
        edit_menu.addSeparator()
        action(edit_menu, 'Delete Selected', self.surface.delete_selected_strokes)
        action(edit_menu, 'Clear Current Frame', self.surface.clear_current_drawing)
        action(edit_menu, 'Insert Blank Frame', self.surface.create_blank_drawing)
        edit_menu.addSeparator()
        action(edit_menu, 'Delete All Notes…', self._delete_all_notes)
        edit_menu.addSeparator()
        action(edit_menu, 'Reset Preferences…', self._reset_preferences)

        play_menu = bar.addMenu('Playback')
        action(play_menu, 'Play / Pause', lambda: self.player.toggle(), shortcut='Space')
        action(play_menu, 'Previous Frame', lambda: self._scrub(self.player.current_frame - 1),
               shortcut='Left')
        action(play_menu, 'Next Frame', lambda: self._scrub(self.player.current_frame + 1),
               shortcut='Right')
        play_menu.addSeparator()
        action(play_menu, 'Reverse', self._play_reverse, shortcut='J')
        action(play_menu, 'Pause', lambda: self.player.pause(), shortcut='K')
        action(play_menu, 'Forward', self._play_forward, shortcut='L')
        speed_menu = play_menu.addMenu('Playback Speed')
        self._speed_group = QActionGroup(self)
        self.speed_actions = {}
        for value in SPEEDS:
            act = QAction('{:g}×'.format(value), self)
            act.setCheckable(True)
            act.setChecked(value == 1.0)
            act.triggered.connect(lambda _=False, v=value: self._set_speed(v))
            self._speed_group.addAction(act)
            speed_menu.addAction(act)
            self.speed_actions[value] = act
        play_menu.addSeparator()
        self.act_loop = action(play_menu, 'Loop', self.player_set_loop, checkable=True,
                               shortcut='Shift+L')
        self.act_ping_pong = action(
            play_menu, 'Ping-Pong', self._set_ping_pong, checkable=True)
        range_menu = play_menu.addMenu('In / Out Range')
        action(range_menu, 'Set In Point', self._mark_in, shortcut='[')
        action(range_menu, 'Set Out Point', self._mark_out, shortcut=']')
        action(range_menu, 'Reset In / Out', self._reset_marked_range, shortcut='Shift+R')
        markers_menu = play_menu.addMenu('Frame Markers')
        self.act_toggle_marker = action(
            markers_menu, 'Toggle Marker', self._toggle_frame_marker, shortcut='M')
        self.act_previous_marker = action(
            markers_menu, 'Previous Marker', self._previous_frame_marker,
            shortcut='Ctrl+Left')
        self.act_next_marker = action(
            markers_menu, 'Next Marker', self._next_frame_marker,
            shortcut='Ctrl+Right')
        markers_menu.addSeparator()
        self.act_clear_markers = action(
            markers_menu, 'Clear Markers', self._clear_frame_markers)
        play_menu.addSeparator()
        policy_menu = play_menu.addMenu('Playback Timing')
        self._playback_policy_group = QActionGroup(self)
        self._playback_policy_group.setExclusive(True)
        self.act_realtime = action(
            policy_menu, 'Realtime (audio clock; video may skip)',
            lambda enabled: enabled and
            self.player.set_playback_policy('realtime'),
            checkable=True, checked=True)
        self.act_play_all = action(
            policy_menu, 'Play All Frames',
            lambda enabled: enabled and
            self.player.set_playback_policy('play_all'),
            checkable=True, checked=False)
        self._playback_policy_group.addAction(self.act_realtime)
        self._playback_policy_group.addAction(self.act_play_all)
        audio_menu = play_menu.addMenu('Audio')
        action(audio_menu, 'Select Embedded Audio Track…',
               self._select_embedded_audio_stream)
        action(audio_menu, 'Attach External Audio…', self._attach_external_audio)
        action(audio_menu, 'Remove External Audio', self._remove_external_audio)
        action(audio_menu, 'Audio Delay…', self._set_audio_delay)
        audio_menu.addSeparator()
        action(audio_menu, 'Routing & Sync Diagnostics…',
               self._show_audio_routing_diagnostics)
        cache_menu = play_menu.addMenu('Cache')
        self._cache_mode_group = QActionGroup(self)
        self._cache_mode_group.setExclusive(True)
        self.cache_mode_actions = {}
        for key, label in (('off', 'Off'), ('region', 'Region Cache'),
                           ('lookahead', 'Look-Ahead Cache')):
            cache_action = action(
                cache_menu, label,
                lambda enabled, value=key: enabled and
                self.player.set_cache_mode(value),
                checkable=True, checked=key == 'lookahead')
            self._cache_mode_group.addAction(cache_action)
            self.cache_mode_actions[key] = cache_action
        # A source-owned interpretation choice, not a global preference. The
        # default explicitly keeps legacy skip-holes playback; other choices
        # play an actual signed source-frame timeline with visible holes.
        missing_menu = play_menu.addMenu('Missing Sequence Frames')
        self._missing_group = QActionGroup(self)
        self._missing_group.setExclusive(True)
        self.missing_actions = {}
        for mode, label in (('skip', 'Skip Missing (Existing Frames)'),
                            ('hold', 'Hold Previous Frame'),
                            ('black', 'Black'), ('checker', 'Checker'),
                            ('error', 'Error Placeholder')):
            act = QAction(label, self)
            act.setCheckable(True)
            act.setChecked(mode == 'skip')
            act.setEnabled(False)
            act.triggered.connect(lambda _=False, value=mode:
                                  self._set_missing_frame_policy(value))
            self._missing_group.addAction(act)
            missing_menu.addAction(act)
            self.missing_actions[mode] = act

        view_menu = bar.addMenu('View')
        action(view_menu, 'Fit Media to View', self.surface.fit_view, shortcut='F')
        action(view_menu, 'Zoom In', lambda: self.surface.zoom_by(1.25), shortcut='Ctrl++')
        action(view_menu, 'Zoom Out', lambda: self.surface.zoom_by(0.8), shortcut='Ctrl+-')
        self.act_fullscreen = action(view_menu, 'Full Screen', self._toggle_fullscreen,
                                     checkable=True, checked=False, shortcut='F11')
        view_menu.addSeparator()
        action(view_menu, 'Open Playlist', self._show_playlist)
        panels_menu = view_menu.addMenu('Show Panels')
        self.act_toolbar = action(panels_menu, 'Tool Rail', self._toggle_toolbar,
                                  checkable=True, checked=True)
        self.act_side = action(panels_menu, 'Properties', self._toggle_side,
                               checkable=True, checked=False)
        self.act_timeline = action(panels_menu, 'Timeline', self._toggle_timeline,
                                   checkable=True, checked=True)
        self.act_transport = action(panels_menu, 'Playback Controls', self._toggle_transport,
                                    checkable=True, checked=True)
        self.act_statusbar = action(panels_menu, 'Status Bar', self._toggle_statusbar,
                                    checkable=True, checked=True)
        view_menu.addSeparator()
        overlays_menu = view_menu.addMenu('Overlays')
        self.act_thumbs = action(overlays_menu, 'Timeline Thumbnails', self._toggle_thumbnails,
                                 checkable=True, checked=True)
        self.act_wave = action(overlays_menu, 'Audio Waveform', self._toggle_waveform,
                               checkable=True, checked=True)
        self.act_onion = action(overlays_menu, 'Neighboring Notes (Onion Skin)',
                                self._toggle_onion, checkable=True,
                                shortcut='Ctrl+Shift+O')
        self.act_crop = action(overlays_menu, 'Crop Overlay', self.surface.set_crop_visible,
                               checkable=True, checked=True)
        view_menu.addSeparator()
        self.act_flip_h = action(view_menu, 'Flip Horizontally', self._on_flip_changed,
                                 checkable=True, shortcut='Shift+X')
        self.act_flip_v = action(view_menu, 'Flip Vertically', self._on_flip_changed,
                                 checkable=True, shortcut='Shift+Y')
        action(view_menu, 'Rotate View 90° Clockwise',
               lambda: self._rotate_view(90))
        action(view_menu, 'Rotate View 90° Counterclockwise',
               lambda: self._rotate_view(-90))
        action(view_menu, 'Reset View Rotation',
               lambda: self._set_view_rotation(0))
        analysis_menu = view_menu.addMenu('Image Inspection')
        action(analysis_menu, 'Select EXR Part / Layer…',
               self._choose_exr_view)
        self.act_pixel_inspection = action(
            analysis_menu, 'Display Pixel Inspector', self.surface.set_pixel_inspection,
            checkable=True, checked=True)
        self.act_pixel_magnifier = action(
            analysis_menu, 'Native-aware Pixel Magnifier',
            self._set_pixel_magnifier_visible, checkable=True, checked=False)
        self.act_pixel_magnifier.setToolTip(
            'Magnify the actual displayed image with a nearest-neighbor '
            'pixel grid, not annotation overlays or an HDR monitor. Original '
            'source HALF/FLOAT/UINT values are shown separately only when '
            'verified; no invented source pixels for missing frames or EXR padding.')
        action(analysis_menu, 'Pin Pixel Sample A',
               lambda: self.surface.set_pixel_probe('A'), shortcut='Ctrl+Alt+1')
        action(analysis_menu, 'Pin Pixel Sample B',
               lambda: self.surface.set_pixel_probe('B'), shortcut='Ctrl+Alt+2')
        action(analysis_menu, 'Clear Pinned Samples', self.surface.clear_pixel_probes)
        action(analysis_menu, 'Image Scopes…', self._show_scopes)
        action(analysis_menu, 'Color Pipeline…', self._show_color_pipeline)

        tools_menu = bar.addMenu('Tools')
        for key, name, tooltip in self._tool_specs:
            shortcut = {'media': 'V', 'playlist': 'P', 'crop': 'C', 'draw': 'D',
                        'track': 'T', 'review': 'R', 'export': 'X',
                        'shotgrid': 'G'}.get(key)
            act = action(tools_menu, name, lambda selected=key: self._show_tool(selected),
                         shortcut=shortcut)
            act.setToolTip(tooltip)
        tools_menu.addSeparator()
        annotation_tools = tools_menu.addMenu('Annotation Tools')
        action(annotation_tools, 'Brush', lambda: self._activate_draw_mode('brush'),
               shortcut='B')
        action(annotation_tools, 'Eraser', lambda: self._activate_draw_mode('eraser'),
               shortcut='E')
        reference_menu = tools_menu.addMenu('Reference / Compare')
        self.act_set_reference = action(
            reference_menu, 'Set Current as Reference', self._set_reference)
        self.act_toggle_reference = action(
            reference_menu, 'Toggle Reference / Playblast',
            self._toggle_reference_playblast)
        self.act_toggle_reference.setEnabled(False)
        maya_menu = tools_menu.addMenu('Maya Integration')
        self._add_option_box_item(
            maya_menu, 'Playblast Current Shot',
            self._playblast_current_shot, self._show_playblast_options)
        action(maya_menu, 'Build Camera Sequencer Playlist…',
               self._build_sequencer_playlist)

        help_menu = bar.addMenu('Help')
        action(help_menu, 'Playback Diagnostics…', self._show_playback_diagnostics)
        action(help_menu, 'Capabilities / Support Report…', self._support_report)
        action(help_menu, 'About FrameReviewer', self._about)
        # Qt/PySide2 can garbage-collect Python-owned QMenu wrappers created
        # through addMenu(str), leaving QAction.menu() with a deleted QObject.
        # Keep all menus alive for the entire window, especially nested menus.
        self._menu_bar = bar
        self._menus_keepalive = (
            file_menu, self.recent_menu, interchange_menu, notes_menu,
            edit_menu, play_menu, speed_menu,
            range_menu, markers_menu, policy_menu, audio_menu, cache_menu,
            missing_menu,
            view_menu, panels_menu, overlays_menu, analysis_menu, tools_menu,
            annotation_tools, reference_menu, maya_menu, help_menu)
        return bar

    def _build_side(self):
        side = QtWidgets.QWidget()
        side.setObjectName('vcSide')
        side.setMinimumWidth(306)
        side.setMaximumWidth(460)
        column = QtWidgets.QVBoxLayout(side)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)

        header_frame = QtWidgets.QFrame()
        header_frame.setObjectName('vcSideHeader')
        header = QtWidgets.QVBoxLayout(header_frame)
        header.setContentsMargins(14, 13, 12, 12)
        header.setSpacing(9)
        eyebrow = QtWidgets.QLabel('PROPERTIES')
        eyebrow.setObjectName('vcSideEyebrow')
        header.addWidget(eyebrow)
        heading_row = QtWidgets.QHBoxLayout()
        heading_row.setSpacing(10)
        self.tool_header_icon = QtWidgets.QLabel()
        self.tool_header_icon.setObjectName('vcSideIcon')
        self.tool_header_icon.setFixedSize(38, 38)
        self.tool_header_icon.setAlignment(QtCore.Qt.AlignCenter)
        self.tool_header_icon.setPixmap(self._icon('media').pixmap(21, 21))
        heading_row.addWidget(self.tool_header_icon)
        heading_text = QtWidgets.QVBoxLayout()
        heading_text.setContentsMargins(0, 0, 0, 0)
        heading_text.setSpacing(1)
        self.tool_title = QtWidgets.QLabel('Media')
        self.tool_title.setObjectName('vcSideTitle')
        self.tool_title.setTextFormat(QtCore.Qt.PlainText)
        heading_text.addWidget(self.tool_title)
        self.tool_subtitle = QtWidgets.QLabel('Source and clip information')
        self.tool_subtitle.setObjectName('vcToolSubtitle')
        self.tool_subtitle.setTextFormat(QtCore.Qt.PlainText)
        self.tool_subtitle.setWordWrap(True)
        heading_text.addWidget(self.tool_subtitle)
        heading_row.addLayout(heading_text, 1)
        close = QtWidgets.QToolButton()
        close.setObjectName('vcSideClose')
        close.setText('×')
        close.setFixedSize(29, 29)
        close.setAccessibleName('Close Properties')
        close.setToolTip('Hide Properties panel')
        close.clicked.connect(lambda: self.act_side.setChecked(False))
        heading_row.addWidget(close, 0, QtCore.Qt.AlignTop)
        header.addLayout(heading_row)
        column.addWidget(header_frame)

        self.tool_stack = QtWidgets.QStackedWidget()
        column.addWidget(self.tool_stack, 1)
        self._tool_pages = {}

        def page(name, scrollable=True):
            widget = QtWidgets.QWidget()
            widget.setObjectName('vcPageBody')
            layout = QtWidgets.QVBoxLayout(widget)
            layout.setContentsMargins(12, 12, 12, 12)
            layout.setSpacing(10)
            if scrollable:
                scroll = QtWidgets.QScrollArea()
                scroll.setObjectName('vcPageScroll')
                scroll.setWidgetResizable(True)
                scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
                scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
                scroll.viewport().setAutoFillBackground(False)
                scroll.setWidget(widget)
                panel = scroll
            else:
                # Playlist owns an independently scrollable media list. A
                # second QScrollArea would compress it and steal wheel events.
                panel = widget
            self.tool_stack.addWidget(panel)
            self._tool_pages[name] = panel
            return layout

        def section(layout, title, collapsible=False, collapsed=False):
            """Build a distinct, breathable group with optional disclosure."""
            card = QtWidgets.QFrame()
            card.setObjectName('vcCard')
            card_layout = QtWidgets.QVBoxLayout(card)
            card_layout.setContentsMargins(12, 10, 12, 12)
            card_layout.setSpacing(9)
            if collapsible:
                key = 'sidebar/sections/' + title.lower().replace(' ', '_')
                opened = self.settings.get_bool(key, not collapsed)
                heading = QtWidgets.QToolButton()
                heading.setObjectName('vcDisclosure')
                heading.setText(title)
                heading.setCheckable(True)
                heading.setChecked(opened)
                heading.setMinimumHeight(22)
                heading.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
                heading.setArrowType(
                    QtCore.Qt.DownArrow if opened else QtCore.Qt.RightArrow)
            else:
                heading = QtWidgets.QLabel(title)
                heading.setObjectName('vcCardTitle')
            card_layout.addWidget(heading)

            content = QtWidgets.QWidget()
            inner = QtWidgets.QVBoxLayout(content)
            inner.setContentsMargins(0, 0, 0, 0)
            inner.setSpacing(8)
            card_layout.addWidget(content)
            inner.heading = heading
            inner.content = content
            inner.card = card
            if collapsible:
                content.setVisible(opened)

                def toggle(opened, body=content, button=heading, key=key):
                    body.setVisible(opened)
                    button.setArrowType(QtCore.Qt.DownArrow if opened
                                        else QtCore.Qt.RightArrow)
                    self.settings.set(key, bool(opened))

                heading.toggled.connect(toggle)
            layout.addWidget(card)
            return inner

        def hint(text):
            # Dim, word-wrapped helper text that never clips at narrow widths.
            label = QtWidgets.QLabel(text)
            label.setWordWrap(True)
            label.setProperty('dim', True)
            return label

        media_page = page('media')
        source = section(media_page, 'Source')
        self.media_lbl = QtWidgets.QLabel('No media')
        self.media_lbl.setWordWrap(True)
        source.addWidget(self.media_lbl)
        self.media_import_btn = QtWidgets.QPushButton('Import Media…')
        self.media_import_btn.setIcon(self._icon('media'))
        self.media_import_btn.setProperty('role', 'primary')
        source.addWidget(self.media_import_btn)
        # Loading media is the first action, not the last line of a metadata
        # card. Keep the source readout below it as reference information.
        media_page.insertWidget(0, self.media_import_btn)
        navigation = section(media_page, 'Navigation')
        self.media_prev_marker_btn = QtWidgets.QPushButton('Previous Marker')
        self.media_next_marker_btn = QtWidgets.QPushButton('Next Marker')
        self.media_prev_marker_btn.setIcon(self._icon('previous'))
        self.media_next_marker_btn.setIcon(self._icon('next'))
        marker_row = QtWidgets.QHBoxLayout()
        marker_row.addWidget(self.media_prev_marker_btn)
        marker_row.addWidget(self.media_next_marker_btn)
        navigation.addLayout(marker_row)
        media_page.addStretch(1)

        playlist_page = page('playlist', scrollable=False)
        self.playlist_panel = PlaylistPanel()
        self.playlist_panel.compositionChanged.connect(
            self._on_composition_changed)
        self.playlist_panel.comparisonParametersChanged.connect(
            self._on_comparison_parameters_changed)
        self.playlist_panel.sourcesChanged.connect(
            lambda _ids: self._schedule_scene_state_save())
        self.playlist_panel.relinkRequested.connect(self._relink_source)
        self.playlist_panel.activateRequested.connect(self._activate_source)
        self.playlist_panel.roleRequested.connect(self._switch_source_role)
        playlist_page.addWidget(self.playlist_panel, 1)

        crop_page = page('crop')
        crop_box = section(crop_page, 'Crop Region')
        self.crop_lbl = QtWidgets.QLabel('None (full frame)')
        self.crop_lbl.setWordWrap(True)
        crop_box.addWidget(self.crop_lbl)
        self.crop_overlay_check = QtWidgets.QCheckBox('Show crop overlay')
        self.crop_overlay_check.setChecked(True)
        self.clear_crop_btn = QtWidgets.QPushButton('Reset')
        self.clear_crop_btn.setIcon(self._icon('clear-note'))
        self.clear_crop_btn.setToolTip('Reset to the full frame')
        crop_row = QtWidgets.QHBoxLayout()
        crop_row.addWidget(self.crop_overlay_check)
        crop_row.addStretch(1)
        crop_row.addWidget(self.clear_crop_btn)
        crop_box.addLayout(crop_row)
        self.crop_lbl.setToolTip(
            'Drag in the viewer to create a crop; drag inside it to reposition')
        crop_page.addStretch(1)

        draw_page = page('draw')
        tools_box = section(draw_page, 'Tool')
        tool_row = QtWidgets.QHBoxLayout()
        tool_row.setSpacing(3)
        self._draw_mode_buttons = {}
        for spec in NOTE_TOOL_SPECS:
            button = QtWidgets.QToolButton()
            if spec.icon:
                button.setIcon(self._icon(spec.icon))
            else:
                button.setText(spec.label[:1])
                font = button.font()
                font.setBold(True)
                font.setPointSize(font.pointSize() + 2)
                button.setFont(font)
            button.setCheckable(True)
            button.setAutoRaise(False)
            button.setProperty('toolMode', True)
            button.setFixedSize(32, 34)
            button.setIconSize(QtCore.QSize(19, 19))
            button.setToolTip(spec.tooltip)
            button.setAccessibleName(spec.label)
            tool_row.addWidget(button)
            self._draw_mode_buttons[spec.id] = button
        # Compatibility names keep signal wiring and third-party scripts stable
        # while new tools only need one registry entry above.
        self.select_mode_btn = self._draw_mode_buttons['select']
        self.brush_mode_btn = self._draw_mode_buttons['brush']
        self.eraser_mode_btn = self._draw_mode_buttons['eraser']
        self.rect_mode_btn = self._draw_mode_buttons['rectangle']
        self.ellipse_mode_btn = self._draw_mode_buttons['ellipse']
        self.arrow_mode_btn = self._draw_mode_buttons['arrow']
        self.text_mode_btn = self._draw_mode_buttons['text']
        mode_buttons = tuple(self._draw_mode_buttons[spec.id]
                             for spec in NOTE_TOOL_SPECS)
        tool_row.addStretch(1)
        self.brush_mode_btn.setChecked(True)
        self.draw_mode_group = QtWidgets.QButtonGroup(self)
        self.draw_mode_group.setExclusive(True)
        for button in mode_buttons:
            self.draw_mode_group.addButton(button)
        tools_box.addLayout(tool_row)

        brush = section(draw_page, 'Tool Settings')
        self.draw_tip_title = brush.heading
        self.draw_settings_stack = CompactStack()
        brush.addWidget(self.draw_settings_stack)

        select_settings = QtWidgets.QWidget()
        select_layout = QtWidgets.QVBoxLayout(select_settings)
        select_layout.setContentsMargins(0, 0, 0, 0)
        select_layout.setSpacing(4)
        select_hint = QtWidgets.QLabel(
            'Click a note or drag around several. Shift-click changes the selection.')
        select_hint.setWordWrap(True)
        select_hint.setProperty('dim', True)
        self.delete_selected_btn = QtWidgets.QPushButton('Delete Selected')
        self.delete_selected_btn.setIcon(self._icon('clear-note'))
        select_layout.addWidget(select_hint)
        select_layout.addWidget(self.delete_selected_btn)
        self.draw_settings_stack.addWidget(select_settings)

        draw_settings = QtWidgets.QWidget()
        draw_settings_layout = QtWidgets.QVBoxLayout(draw_settings)
        draw_settings_layout.setContentsMargins(0, 0, 0, 0)
        draw_settings_layout.setSpacing(4)
        self.draw_settings_stack.addWidget(draw_settings)

        self.draw_color_panel = QtWidgets.QWidget()
        color_row = QtWidgets.QHBoxLayout(self.draw_color_panel)
        color_row.setContentsMargins(0, 0, 0, 0)
        self.draw_color_btn = QtWidgets.QToolButton()
        self.draw_color_btn.setFixedSize(34, 24)
        self.draw_color_btn.setToolTip('Brush color')
        popup_mode = getattr(QtWidgets.QToolButton, 'InstantPopup', None)
        if popup_mode is None:
            popup_mode = QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup
        self.draw_color_btn.setPopupMode(popup_mode)
        self.draw_color_menu = QtWidgets.QMenu(self.draw_color_btn)
        self.draw_color_picker = QtWidgets.QColorDialog(
            QtGui.QColor(style.DRAW_DEFAULT), self.draw_color_menu)
        no_buttons = getattr(QtWidgets.QColorDialog, 'NoButtons', None)
        non_native = getattr(QtWidgets.QColorDialog, 'DontUseNativeDialog', None)
        if hasattr(QtWidgets.QColorDialog, 'ColorDialogOption'):
            options = QtWidgets.QColorDialog.ColorDialogOption
            no_buttons = no_buttons or options.NoButtons
            non_native = non_native or options.DontUseNativeDialog
        self.draw_color_picker.setOptions(no_buttons | non_native)
        color_widget_action = QtWidgets.QWidgetAction(self.draw_color_menu)
        color_widget_action.setDefaultWidget(self.draw_color_picker)
        self.draw_color_menu.addAction(color_widget_action)
        self.draw_color_btn.setMenu(self.draw_color_menu)
        color_row.addWidget(self.draw_color_btn)
        self.draw_swatches = []
        for color in style.DRAW_SWATCHES:
            swatch = QtWidgets.QToolButton()
            swatch.setFixedSize(22, 22)
            swatch.setToolTip(color)
            swatch.setStyleSheet('background:%s; border:1px solid %s;'
                                 % (color, style.SWATCH_BORDER))
            swatch.setProperty('brushColor', color)
            self.draw_swatches.append(swatch)
            color_row.addWidget(swatch)
        draw_settings_layout.addWidget(self.draw_color_panel)

        def numeric_control(layout, label, minimum, maximum, value, suffix):
            container = QtWidgets.QWidget()
            row = QtWidgets.QHBoxLayout(container)
            row.setContentsMargins(0, 0, 0, 0)
            caption = QtWidgets.QLabel(label)
            caption.setFixedWidth(64)
            slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
            slider.setRange(minimum, maximum)
            slider.setValue(value)
            spin = QtWidgets.QSpinBox()
            spin.setRange(minimum, maximum)
            spin.setValue(value)
            spin.setSuffix(suffix)
            spin.setFixedWidth(72)
            slider.valueChanged.connect(spin.setValue)
            spin.valueChanged.connect(slider.setValue)
            row.addWidget(caption)
            row.addWidget(slider, 1)
            row.addWidget(spin)
            layout.addWidget(container)
            return slider, spin, caption, container

        (self.draw_width_slider, self.draw_width,
         self.draw_width_caption, self.draw_width_panel) = numeric_control(
            draw_settings_layout, 'Size', 1, 100, 6, ' px')
        (self.draw_opacity_slider, self.draw_opacity,
         self.draw_opacity_caption, self.draw_opacity_panel) = numeric_control(
            draw_settings_layout, 'Opacity', 1, 100, 100, '%')
        (self.draw_smoothing_slider, self.draw_smoothing,
         self.draw_smoothing_caption, self.draw_smoothing_panel) = numeric_control(
            draw_settings_layout, 'Smoothing', 0, 100, 20, '%')

        dynamics = section(draw_page, 'Pen Pressure', collapsible=True,
                           collapsed=True)
        self.pressure_card = dynamics.card
        dynamics_row = QtWidgets.QHBoxLayout()
        self.pressure_size_check = QtWidgets.QCheckBox('Pressure controls size')
        self.pressure_size_check.setChecked(True)
        self.pressure_opacity_check = QtWidgets.QCheckBox('Pressure controls opacity')
        self.pressure_size_check.setText('Pressure size')
        self.pressure_opacity_check.setText('Pressure opacity')
        dynamics_row.addWidget(self.pressure_size_check)
        dynamics_row.addWidget(self.pressure_opacity_check)
        dynamics.addLayout(dynamics_row)
        pressure_row = QtWidgets.QHBoxLayout()
        pressure_row.addWidget(QtWidgets.QLabel('Pressure'))
        self.pressure_meter = QtWidgets.QProgressBar()
        self.pressure_meter.setRange(0, 100)
        self.pressure_meter.setValue(0)
        self.pressure_meter.setTextVisible(False)
        pressure_row.addWidget(self.pressure_meter, 1)
        dynamics.addLayout(pressure_row)
        self.tablet_status_lbl = QtWidgets.QLabel('Mouse input · tablet pressure ready')
        self.tablet_status_lbl.setProperty('dim', True)
        dynamics.addWidget(self.tablet_status_lbl)
        keys = section(draw_page, 'Annotation Keys')
        draw_nav = QtWidgets.QHBoxLayout()
        self.prev_drawing_btn = QtWidgets.QToolButton()
        self.prev_drawing_btn.setIcon(self._icon('previous'))
        self.prev_drawing_btn.setToolTip('Previous annotated frame')
        self.next_drawing_btn = QtWidgets.QToolButton()
        self.next_drawing_btn.setIcon(self._icon('next'))
        self.next_drawing_btn.setToolTip('Next annotated frame')
        self.drawing_lbl = QtWidgets.QLabel('No drawing on this frame')
        self.drawing_lbl.setAlignment(QtCore.Qt.AlignCenter)
        self.drawing_lbl.setWordWrap(True)
        draw_nav.addWidget(self.prev_drawing_btn)
        draw_nav.addWidget(self.drawing_lbl, 1)
        draw_nav.addWidget(self.next_drawing_btn)
        keys.addLayout(draw_nav)

        edit_row = QtWidgets.QHBoxLayout()
        self.undo_drawing_btn = QtWidgets.QToolButton()
        self.undo_drawing_btn.setIcon(self._icon('undo'))
        self.undo_drawing_btn.setToolTip('Undo stroke')
        self.clear_drawing_btn = QtWidgets.QToolButton()
        self.clear_drawing_btn.setIcon(self._icon('clear-note'))
        self.clear_drawing_btn.setToolTip('Remove the current annotation key')
        self.blank_drawing_btn = QtWidgets.QPushButton('+  Blank Here')
        self.blank_drawing_btn.setProperty('role', 'primary')
        self.blank_drawing_btn.setToolTip(
            'Stop the previous annotation from holding past this frame')
        edit_row.addWidget(self.undo_drawing_btn)
        edit_row.addWidget(self.clear_drawing_btn)
        edit_row.addWidget(self.blank_drawing_btn, 1)
        keys.addLayout(edit_row)

        manage = section(draw_page, 'Manage Notes', collapsible=True,
                         collapsed=True)
        file_row = QtWidgets.QHBoxLayout()
        self.import_notes_btn = QtWidgets.QToolButton()
        self.import_notes_btn.setIcon(self._icon('import-notes'))
        self.import_notes_btn.setText('Import')
        self.export_notes_btn = QtWidgets.QToolButton()
        self.export_notes_btn.setIcon(self._icon('save-notes'))
        self.export_notes_btn.setText('Export')
        for button in (self.import_notes_btn, self.export_notes_btn):
            button.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
            button.setToolTip(button.text() + ' annotation notes')
            file_row.addWidget(button, 1)
        manage.addLayout(file_row)
        self.delete_all_notes_btn = QtWidgets.QPushButton('Delete All Notes')
        self.delete_all_notes_btn.setIcon(self._icon('clear-note'))
        self.delete_all_notes_btn.setProperty('role', 'danger')
        self.delete_all_notes_btn.setToolTip('Permanently remove every annotation key')
        manage.addWidget(self.delete_all_notes_btn)
        for button in (self.prev_drawing_btn, self.next_drawing_btn,
                       self.undo_drawing_btn, self.clear_drawing_btn):
            button.setFixedSize(29, 27)
            button.setIconSize(QtCore.QSize(16, 16))
        onion = section(draw_page, 'Onion Skin', collapsible=True,
                        collapsed=True)
        self.onion_enable = QtWidgets.QCheckBox('Show neighboring notes')
        onion.addWidget(self.onion_enable)
        depth_row = QtWidgets.QHBoxLayout()
        self.onion_before = QtWidgets.QSpinBox()
        self.onion_after = QtWidgets.QSpinBox()
        for control in (self.onion_before, self.onion_after):
            control.setRange(0, 20)
            control.setValue(2)
        depth_row.addWidget(QtWidgets.QLabel('Previous keys'))
        depth_row.addWidget(self.onion_before)
        depth_row.addWidget(QtWidgets.QLabel('Next keys'))
        depth_row.addWidget(self.onion_after)
        onion.addLayout(depth_row)
        opacity_row = QtWidgets.QHBoxLayout()
        self.onion_opacity = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.onion_opacity.setRange(0, 100)
        self.onion_opacity.setValue(35)
        self.onion_opacity_label = QtWidgets.QLabel('35%')
        opacity_row.addWidget(QtWidgets.QLabel('Opacity'))
        opacity_row.addWidget(self.onion_opacity, 1)
        opacity_row.addWidget(self.onion_opacity_label)
        onion.addLayout(opacity_row)
        option_row = QtWidgets.QHBoxLayout()
        self.onion_falloff = QtWidgets.QCheckBox('Fade distance')
        self.onion_falloff.setChecked(True)
        self.onion_tint = QtWidgets.QCheckBox('Color sides')
        self.onion_tint.setChecked(True)
        option_row.addWidget(self.onion_falloff)
        option_row.addWidget(self.onion_tint)
        onion.addLayout(option_row)
        # Review cadence: pick an annotation tool, jump between annotation
        # keys, adjust the current tool, then optionally tweak onion/pressure.
        # This puts everyday controls above specialist setup on short screens.
        draw_page.insertWidget(1, keys.card)
        draw_page.insertWidget(3, onion.card)
        draw_page.addStretch(1)

        track_page = page('track')
        tracker = section(track_page, 'Crop Tracking')
        self.tracking_mode = QtWidgets.QComboBox()
        for value, description in tracking.MODES:
            self.tracking_mode.addItem(description, value)
        self.tracking_mode.setToolTip(
            'The number of transform constraints. Perspective fits 4 or more '
            'optical-flow image features, with outlier rejection.')
        tracker.addWidget(self.tracking_mode)
        track_row = QtWidgets.QHBoxLayout()
        self.track_btn = QtWidgets.QPushButton('Track Forward')
        self.track_btn.setIcon(self._icon('track'))
        self.track_btn.setProperty('role', 'primary')
        self.clear_track_btn = QtWidgets.QPushButton('Clear')
        track_row.addWidget(self.track_btn, 1)
        track_row.addWidget(self.clear_track_btn)
        tracker.addLayout(track_row)
        self.correct_track_btn = QtWidgets.QPushButton('Correct Current Frame')
        self.correct_track_btn.setToolTip(
            'Position the tracked crop, then bake its offset into THIS '
            'source-frame key only (translation mode). No motion is invented '
            'for other frames; the correction can be undone.')
        self.correct_track_btn.setProperty('role', 'utility')
        tracker.addWidget(self.correct_track_btn)
        self.stabilize_view_cb = QtWidgets.QCheckBox('Stabilize View (preview only)')
        self.stabilize_view_cb.setToolTip(
            'Opt-in preview from verified translation tracks, including the '
            'image, crop, notes and pixel probes in the same native scene. '
            'No pixels are baked or exported; unsupported tracks refuse. '
            'Disable before media delivery.')
        tracker.addWidget(self.stabilize_view_cb)
        self.tracking_lbl = QtWidgets.QLabel('No tracking')
        tracker.addWidget(self.tracking_lbl)
        self.tracking_unavailable_hint = hint(
            'Crop tracking requires OpenCV (cv2) and NumPy in Maya Python. '
            'Install those modules to enable optical-flow tracking.')
        tracker.addWidget(self.tracking_unavailable_hint)
        self.tracking_unavailable_hint.setVisible(not tracking.HAVE_CV2)
        if not tracking.HAVE_CV2:
            self.track_btn.setEnabled(False)
            self.clear_track_btn.setEnabled(False)
            self.tracking_mode.setEnabled(False)
            self.tracking_lbl.setText('Tracking unavailable')
        self.tracking_mode.setToolTip(
            self.tracking_mode.toolTip() +
            ' Create a crop, move to its first frame, then track forward.')
        track_page.addStretch(1)

        review_page = page('review')
        overlays = section(review_page, 'Overlays', collapsible=True,
                           collapsed=True)
        composition_row = QtWidgets.QHBoxLayout()
        composition_row.addWidget(QtWidgets.QLabel('Composition'))
        self.composition_combo = QtWidgets.QComboBox()
        for value, label in (('off', 'Off'), ('thirds', 'Rule of thirds'),
                             ('golden', 'Golden ratio'), ('diagonal', 'Diagonals'),
                             ('center', 'Center cross'), ('safe', 'Safe areas')):
            self.composition_combo.addItem(label, value)
        self.composition_combo.setToolTip('Framing guides drawn over the image')
        composition_row.addWidget(self.composition_combo, 1)
        overlays.addLayout(composition_row)
        self.color_btn = QtWidgets.QPushButton('Color Correction…')
        self.color_btn.setToolTip(
            'Non-destructive display grade (exposure, contrast, saturation, '
            'temperature); never alters exported media or notes')
        overlays.addWidget(self.color_btn)
        self.review_thumbs_check = QtWidgets.QCheckBox('Thumbnail timeline')
        self.review_wave_check = QtWidgets.QCheckBox('Audio waveform')
        overlays.addWidget(self.review_thumbs_check)
        overlays.addWidget(self.review_wave_check)
        view_box = section(review_page, 'Viewer')
        zoom_row = QtWidgets.QHBoxLayout()
        self.zoom_out_btn = QtWidgets.QPushButton('−')
        self.fit_btn = QtWidgets.QPushButton('Fit')
        self.zoom_in_btn = QtWidgets.QPushButton('+')
        zoom_row.addWidget(self.zoom_out_btn)
        zoom_row.addWidget(self.fit_btn)
        zoom_row.addWidget(self.zoom_in_btn)
        view_box.addLayout(zoom_row)
        self.review_flip_h = QtWidgets.QCheckBox('Flip horizontally')
        self.review_flip_v = QtWidgets.QCheckBox('Flip vertically')
        view_box.addWidget(self.review_flip_h)
        view_box.addWidget(self.review_flip_v)
        display = section(review_page, 'Image Processing')
        display.addWidget(self.color_btn)
        self.color_pipeline_btn = QtWidgets.QPushButton('Color Pipeline…')
        self.color_pipeline_btn.setToolTip(
            'Set the source/working/display color interpretation for precision EXR '
            'review. Optional OCIO config needs PyOpenColorIO.')
        self.color_pipeline_btn.clicked.connect(self._show_color_pipeline)
        display.addWidget(self.color_pipeline_btn)
        self.scopes_btn = QtWidgets.QPushButton('Image Scopes…')
        self.scopes_btn.setToolTip('Inspect RGB/Y histograms, waveform and vectorscope')
        self.scopes_btn.clicked.connect(self._show_scopes)
        display.addWidget(self.scopes_btn)
        review_page.insertWidget(0, view_box.card)
        review_page.insertWidget(1, display.card)
        records = section(review_page, 'Review Records', collapsible=True,
                          collapsed=True)
        self.review_records_disclosure = records.heading
        self.review_records_panel = ReviewRecordsPanel()
        records.addWidget(self.review_records_panel)
        review_page.insertWidget(2, records.card)
        review_page.addStretch(1)

        export_page = page('export')
        output = section(export_page, 'Output')
        resolution = QtWidgets.QHBoxLayout()
        self.out_w_le = QtWidgets.QLineEdit()
        self.out_h_le = QtWidgets.QLineEdit()
        size_validator = QtGui.QIntValidator(2, 32768, self)
        self.out_w_le.setValidator(size_validator)
        self.out_h_le.setValidator(size_validator)
        self.out_w_le.setPlaceholderText('Width')
        self.out_h_le.setPlaceholderText('Height')
        resolution.addWidget(self.out_w_le)
        resolution.addWidget(QtWidgets.QLabel('×'))
        resolution.addWidget(self.out_h_le)
        output.addLayout(resolution)
        self.fps_combo = QtWidgets.QComboBox()
        self.fps_combo.addItem('24 fps', '24')
        self.fps_combo.addItem('Source frame rate', 'source')
        output.addWidget(self.fps_combo)
        output.addWidget(QtWidgets.QLabel('Color output stage'))
        self.export_color_stage = QtWidgets.QComboBox()
        self.export_color_stage.addItem('Source (original)', 'source')
        self.export_color_stage.addItem('Working (scene-linear EXR)', 'working')
        self.export_color_stage.addItem('Display (view-transformed)', 'display')
        self.export_color_stage.setToolTip(
            'Working/Display require a supported precision source and color '
            'renderer; unsupported jobs are rejected rather than mislabeled.')
        output.addWidget(self.export_color_stage)
        self.export_preserve_audio_cb = QtWidgets.QCheckBox(
            'Preserve selected embedded audio (Display MP4)')
        self.export_preserve_audio_cb.setToolTip(
            'Opt-in: only one CFR movie, Display MP4, unchanged frame rate, '
            'zero PTS/offset and selected embedded soundtrack. The final '
            'AAC audio is re-encoded, sample-aligned to the frozen frame '
            'range, and verified. Source routes, external audio and complex '
            'mixing remain unsupported. Audio is dropped by default.')
        self.export_preserve_audio_cb.setEnabled(False)
        output.addWidget(self.export_preserve_audio_cb)
        self.export_record_audio_cb = QtWidgets.QCheckBox(
            'Include verified record PCM soundtrack (WAV in sequence folder)')
        self.export_record_audio_cb.setToolTip(
            'Opt-in for supported hard-cut SEQUENCE/EDL/OTIO record outputs only. '
            'Each edit occurrence must have independently frozen, verified '
            '48 kHz stereo PCM audio (embedded or separately proven external '
            'zero-origin WAV), or explicit image-edit silence. The WAV travels '
            'inside the same durable output folder. No unverified delays, '
            'dissolves, retimes or crossfades.')
        self.export_record_audio_cb.setEnabled(False)
        output.addWidget(self.export_record_audio_cb)
        self.export_record_av_cb = QtWidgets.QCheckBox(
            'Also deliver verified SDR RGB/PCM record MOV (inside PNG/WAV folder)')
        self.export_record_av_cb.setToolTip(
            'Opt-in bounded v4 Display-only derivative: a lossless H.264 RGB '
            '8-bit SDR MOV with original 48 kHz stereo PCM inside the SAME '
            'atomically published sequence/WAV folder. Native Display PNG16 '
            'frames remain reference assets; transparent pixels are explicitly '
            'composited onto black. MOV pixels and audio must independently '
            'decode to the reference; not an HDR or alpha-preserving movie. '
            'No standalone mux, AAC, retime, unverified effects or audio delay.')
        self.export_record_av_cb.setEnabled(False)
        output.addWidget(self.export_record_av_cb)
        self.export_record_rgba16_cb = QtWidgets.QCheckBox(
            'Also deliver exact RGBA16/PCM lossless record NUT (Display SDR)')
        self.export_record_rgba16_cb.setToolTip(
            'Opt-in v4 Display-only FFV1 NUT derivative inside the SAME durable '
            'PNG16/WAV folder. Independently decodes and verifies EVERY 16-bit '
            'straight RGBA component, selected 48 kHz stereo PCM sample and '
            'exact 24000/1001 frame timestamps (1/48000 stream clock). '
            'Preserves Display SDR alpha and precision, NOT original scene-linear '
            'HDR EXR; NUT has limited third-party player interoperability.')
        self.export_record_rgba16_cb.setEnabled(False)
        output.addWidget(self.export_record_rgba16_cb)
        self.record_audio_routes_btn = QtWidgets.QPushButton('Audio per edit…')
        self.record_audio_routes_btn.setToolTip(
            'Optional export-only audio selection for each record occurrence; '
            'repeated Sources can select different embedded tracks or external '
            'WAVs. Does not edit Session or source playback audio.')
        self.record_audio_routes_btn.setEnabled(False)
        self.record_audio_routes_btn.clicked.connect(self._choose_record_audio_routes)
        output.addWidget(self.record_audio_routes_btn)
        self.record_audio_overlay_btn = QtWidgets.QPushButton(
            'Audio-only record layer…')
        self.record_audio_overlay_btn.setToolTip(
            'Optional ONE independently verified external zero-origin PCM WAV '
            'audio-only bed overlapping the selected per-edit sound, with '
            'absolute rational record-frame placement, independent source trim '
            'and exactly declared gain 0, 1/2 or 1. The same verified mixed '
            'PCM drives waveform, Qt audition and durable WAV/MOV exports. '
            'This is not crossfade, transition or arbitrary multitrack support.')
        self.record_audio_overlay_btn.setEnabled(False)
        self.record_audio_overlay_btn.clicked.connect(
            self._choose_record_audio_overlay)
        output.addWidget(self.record_audio_overlay_btn)
        self.record_audio_layers_btn = QtWidgets.QPushButton(
            'Manage ordered audio-only layers…')
        self.record_audio_layers_btn.setToolTip(
            'Explicitly author up to THREE separately placed canonical 48 kHz '
            'stereo PCM WAV events over independently selected cut sound. '
            'The displayed row order is the exact signed PCM16 saturated '
            'mix order, including clipping; each event has its own source in, '
            'record start, duration, immutable source proof and constant exact '
            'mute/half/unity gain. Not arbitrary DSP or physical sync.')
        self.record_audio_layers_btn.setEnabled(False)
        self.record_audio_layers_btn.clicked.connect(
            self._choose_record_audio_layers)
        output.addWidget(self.record_audio_layers_btn)
        self.record_audio_preview_btn = QtWidgets.QPushButton(
            'Verify record waveform…')
        self.record_audio_preview_btn.setToolTip(
            'Independently decode the selected record sources and plot exact '
            'per-frame PCM peaks and silence on the current timeline. This '
            'does NOT certify speaker output or synchronize physical devices.')
        self.record_audio_preview_btn.setEnabled(False)
        self.record_audio_preview_btn.clicked.connect(self._preview_record_waveform)
        output.addWidget(self.record_audio_preview_btn)
        self.record_audio_audition_btn = QtWidgets.QPushButton(
            'Audition verified record audio…')
        self.record_audio_audition_btn.setToolTip(
            'Opt-in bounded Qt PCM speaker audition for the selected verified '
            'record soundtrack, with the primary video playhead in control. '
            'A real audio output device is required; latency/drift and '
            'sample-accurate hardware synchronization are NOT certified.')
        self.record_audio_audition_btn.setEnabled(False)
        self.record_audio_audition_btn.clicked.connect(self._audition_record_sound)
        output.addWidget(self.record_audio_audition_btn)
        self.export_record_audio_cb.toggled.connect(
            lambda *_: self._sync_export_audio_controls())
        self.export_color_stage.currentIndexChanged.connect(
            lambda *_: self._sync_export_audio_controls())
        range_box = section(export_page, 'Export Range')
        self.export_range_lbl = QtWidgets.QLabel('No media loaded')
        range_box.addWidget(self.export_range_lbl)
        self.export_btn = QtWidgets.QPushButton('Export Media…')
        self.export_btn.setIcon(self._icon('export'))
        self.export_btn.setProperty('role', 'primary')
        self.export_btn.setMinimumHeight(30)
        export_page.addWidget(self.export_btn)
        # Starting an export and verifying the current range are the highest
        # value actions. Resolution/framerate are advanced delivery parameters.
        export_page.insertWidget(0, self.export_btn)
        export_page.insertWidget(1, range_box.card)
        jobs = section(export_page, 'Export Queue')
        self.export_queue_list = QtWidgets.QListWidget()
        self.export_queue_list.setObjectName('frameReviewerExportQueue')
        self.export_queue_list.setMinimumHeight(105)
        self.export_queue_list.setMaximumHeight(200)
        self.export_queue_list.currentItemChanged.connect(
            lambda *_: self._update_export_queue_actions())
        jobs.addWidget(self.export_queue_list)
        export_queue_controls = QtWidgets.QHBoxLayout()
        self.export_queue_start_btn = QtWidgets.QPushButton('Start Queue')
        self.export_queue_start_btn.clicked.connect(self._start_export_queue)
        self.export_queue_cancel_btn = QtWidgets.QPushButton('Cancel')
        self.export_queue_cancel_btn.clicked.connect(self._cancel_queued_export)
        self.export_queue_retry_btn = QtWidgets.QPushButton('Retry')
        self.export_queue_retry_btn.clicked.connect(self._retry_queued_export)
        self.export_queue_open_btn = QtWidgets.QPushButton('Reveal')
        self.export_queue_open_btn.clicked.connect(self._reveal_queued_export)
        for control in (self.export_queue_start_btn,
                        self.export_queue_cancel_btn,
                        self.export_queue_retry_btn,
                        self.export_queue_open_btn):
            export_queue_controls.addWidget(control)
        jobs.addLayout(export_queue_controls)
        export_page.addStretch(1)

        # ShotGrid lives in its own scrollable panel widget rather than the
        # shared page() scaffold, so the browser/notes manage their own layout.
        sg_scroll = QtWidgets.QScrollArea()
        sg_scroll.setWidgetResizable(True)
        sg_scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.shotgrid_panel = ShotGridPanel(
            load_path=self._load_path, settings=self.settings, parent=self,
            queue_playlist=self._queue_playlist_paths,
            link_version=self._link_shotgrid_version,
            review_context=self._shotgrid_review_context,
            on_published=self._on_shotgrid_published)
        sg_scroll.setWidget(self.shotgrid_panel)
        self.tool_stack.addWidget(sg_scroll)
        self._tool_pages['shotgrid'] = sg_scroll

        # Importance hierarchy: primary actions (Import, Blank Here, Track
        # Forward, Export) stay prominent; these secondary/utility actions are
        # demoted to the quiet, compact "utility" tier so they stop competing
        # with the main action in each pane.
        for button in (self.media_prev_marker_btn, self.media_next_marker_btn,
                       self.clear_crop_btn, self.delete_selected_btn,
                       self.clear_track_btn, self.zoom_out_btn, self.fit_btn,
                       self.zoom_in_btn):
            button.setProperty('role', 'utility')

        footer = QtWidgets.QFrame()
        footer.setObjectName('vcSideFooter')
        footer_layout = QtWidgets.QHBoxLayout(footer)
        footer_layout.setContentsMargins(14, 7, 13, 7)
        footer_layout.setSpacing(6)
        instruction = QtWidgets.QLabel('TOOL SHORTCUT')
        instruction.setObjectName('vcSideFooterLabel')
        footer_layout.addWidget(instruction)
        footer_layout.addStretch(1)
        self.tool_shortcut_hint = QtWidgets.QLabel('V')
        self.tool_shortcut_hint.setObjectName('vcSideShortcut')
        self.tool_shortcut_hint.setAlignment(QtCore.Qt.AlignCenter)
        footer_layout.addWidget(self.tool_shortcut_hint)
        column.addWidget(footer)
        return side

    def _build_transport(self):
        """Balanced transport: playback left, exact frame center, range right."""
        outer = QtWidgets.QGridLayout()
        outer.setContentsMargins(1, 0, 1, 0)
        outer.setHorizontalSpacing(6)
        left_widget = QtWidgets.QWidget()
        left = QtWidgets.QHBoxLayout(left_widget)
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(3)
        right_widget = QtWidgets.QWidget()
        right = QtWidgets.QHBoxLayout(right_widget)
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(3)

        def separator():
            line = QtWidgets.QFrame()
            line.setFrameShape(QtWidgets.QFrame.VLine)
            line.setFrameShadow(QtWidgets.QFrame.Sunken)
            line.setFixedHeight(18)
            return line

        buttons = (
            ('first_btn', 'SP_MediaSkipBackward', 'Go to In'),
            ('prev_btn', 'SP_MediaSeekBackward', 'Previous frame'),
            ('play_btn', 'SP_MediaPlay', 'Play / pause (Space)'),
            ('next_btn', 'SP_MediaSeekForward', 'Next frame'),
            ('last_btn', 'SP_MediaSkipForward', 'Go to Out'),
        )
        for name, icon_name, hint in buttons:
            btn = QtWidgets.QToolButton()
            btn.setFixedSize(38 if name == 'play_btn' else 34, 34)
            btn.setIconSize(QtCore.QSize(26 if name == 'play_btn' else 23,
                                         26 if name == 'play_btn' else 23))
            btn.setAutoRaise(True)
            btn.setToolTip(hint)
            btn.setIcon(self._transport_icon(icon_name))
            setattr(self, name, btn)
            left.addWidget(btn)

        left.addSpacing(3)
        left.addWidget(separator())
        left.addSpacing(3)
        self.speed_combo = QtWidgets.QComboBox()
        for value in SPEEDS:
            self.speed_combo.addItem('{:g}×'.format(value), value)
        self.speed_combo.setCurrentIndex(SPEEDS.index(1.0))
        self.speed_combo.setFixedWidth(64)
        self.speed_combo.setToolTip('Playback speed')
        left.addWidget(self.speed_combo)

        self.loop_btn = QtWidgets.QToolButton()
        self.loop_btn.setText('Loop')
        self.loop_btn.setCheckable(True)
        self.loop_btn.setChecked(self.act_loop.isChecked())
        self.loop_btn.setFixedSize(48, 32)
        self.loop_btn.setToolTip('Loop inside the marked range (L)')

        self.sync_btn = QtWidgets.QToolButton()
        self.sync_btn.setText('Sync')
        self.sync_btn.setCheckable(True)
        self.sync_btn.setFixedHeight(32)
        self.sync_btn.setToolTip('Keep FrameReviewer and Maya on a fixed frame offset')
        popup_mode = getattr(QtWidgets.QToolButton, 'MenuButtonPopup', None)
        if popup_mode is None:
            popup_mode = QtWidgets.QToolButton.ToolButtonPopupMode.MenuButtonPopup
        self.sync_btn.setPopupMode(popup_mode)
        sync_menu = QtWidgets.QMenu(self.sync_btn)
        self.sync_player_to_maya = QAction('Sync player to Maya', self)
        self.sync_player_to_maya.setCheckable(True)
        self.sync_player_to_maya.setChecked(True)
        sync_menu.addAction(self.sync_player_to_maya)
        self.sync_maya_to_player = QAction('Sync Maya to player', self)
        self.sync_maya_to_player.setCheckable(True)
        self.sync_maya_to_player.setChecked(True)
        sync_menu.addAction(self.sync_maya_to_player)
        # The frame offset belongs to the Sync workflow, so it lives inside the
        # Sync button's menu rather than as a standalone widget that does
        # nothing while Sync is off. Maya frame = video frame + offset.
        sync_menu.addSeparator()
        offset_row = QtWidgets.QWidget()
        offset_layout = QtWidgets.QHBoxLayout(offset_row)
        offset_layout.setContentsMargins(10, 4, 10, 6)
        offset_layout.setSpacing(6)
        offset_label = QtWidgets.QLabel('Frame offset')
        self.sync_offset_spin = QtWidgets.QSpinBox()
        self.sync_offset_spin.setRange(-1000000, 1000000)
        self.sync_offset_spin.setValue(0)
        self.sync_offset_spin.setMaximumWidth(90)
        self.sync_offset_spin.setToolTip(
            'Frames to shift the video against Maya (Maya frame = video frame '
            '+ offset)')
        offset_layout.addWidget(offset_label)
        offset_layout.addStretch(1)
        offset_layout.addWidget(self.sync_offset_spin)
        offset_action = QtWidgets.QWidgetAction(sync_menu)
        offset_action.setDefaultWidget(offset_row)
        sync_menu.addAction(offset_action)
        self.sync_btn.setMenu(sync_menu)

        self.mute_btn = VolumeButton()
        self.mute_btn.setFixedSize(34, 34)
        self.mute_btn.setIconSize(QtCore.QSize(23, 23))
        self.mute_btn.setAutoRaise(True)
        self.mute_btn.setIcon(self._icon('volume'))
        self.mute_btn.setToolTip(
            'Click for volume · Double-click to mute or unmute')

        self.volume_menu = QtWidgets.QMenu(self.mute_btn)
        volume_widget = QtWidgets.QWidget()
        volume_layout = QtWidgets.QVBoxLayout(volume_widget)
        volume_layout.setContentsMargins(8, 8, 8, 8)
        self.volume_value_lbl = QtWidgets.QLabel('50')
        self.volume_value_lbl.setAlignment(QtCore.Qt.AlignCenter)
        self.volume_slider = QtWidgets.QSlider(QtCore.Qt.Vertical)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(50)
        self.volume_slider.setFixedHeight(120)
        self.volume_slider.setToolTip('Playback volume')
        volume_layout.addWidget(self.volume_value_lbl)
        volume_layout.addWidget(self.volume_slider, 1, QtCore.Qt.AlignHCenter)
        volume_action = QtWidgets.QWidgetAction(self.volume_menu)
        volume_action.setDefaultWidget(volume_widget)
        self.volume_menu.addAction(volume_action)
        # Playback settings: speed, volume and loop belong together.
        left.addWidget(self.mute_btn)
        left.addWidget(self.loop_btn)
        # Maya synchronization is a separate workflow from local playback.
        left.addSpacing(3)
        left.addWidget(separator())
        left.addSpacing(3)
        left.addWidget(self.sync_btn)
        left.addStretch(1)

        self.frame_lbl = QtWidgets.QLabel('0 / 0')
        self.frame_lbl.setAlignment(QtCore.Qt.AlignCenter)
        self.frame_lbl.setMinimumWidth(88)
        self.frame_lbl.setToolTip('Current frame / final frame')

        def marker_label(name):
            label = QtWidgets.QLabel(name)
            font = label.font()
            font.setBold(True)
            label.setFont(font)
            return label

        self.set_in_btn = QtWidgets.QToolButton()
        self.set_in_btn.setText('[')
        self.set_in_btn.setFixedSize(23, 24)
        self.set_in_btn.setToolTip('Mark In at playhead ([)')
        right.addWidget(self.set_in_btn)
        right.addWidget(marker_label('IN'))

        self.in_spin = QtWidgets.QSpinBox()
        self.in_spin.setButtonSymbols(QtWidgets.QAbstractSpinBox.NoButtons)
        self.in_spin.setAlignment(QtCore.Qt.AlignRight)
        self.in_spin.setFixedWidth(66)
        self.in_spin.setToolTip('Editable In frame')
        right.addWidget(self.in_spin)
        right.addSpacing(3)
        right.addWidget(separator())
        right.addSpacing(3)

        right.addWidget(marker_label('OUT'))
        self.out_spin = QtWidgets.QSpinBox()
        self.out_spin.setButtonSymbols(QtWidgets.QAbstractSpinBox.NoButtons)
        self.out_spin.setAlignment(QtCore.Qt.AlignRight)
        self.out_spin.setFixedWidth(66)
        self.out_spin.setToolTip('Editable Out frame')
        right.addWidget(self.out_spin)

        self.set_out_btn = QtWidgets.QToolButton()
        self.set_out_btn.setText(']')
        self.set_out_btn.setFixedSize(23, 24)
        self.set_out_btn.setToolTip('Mark Out at playhead (])')
        right.addWidget(self.set_out_btn)

        self.reset_range_btn = QtWidgets.QToolButton()
        self.reset_range_btn.setText('Reset')
        self.reset_range_btn.setFixedSize(42, 24)
        self.reset_range_btn.setToolTip('Restore full frame range (Shift+R)')
        right.addSpacing(3)
        right.addWidget(separator())
        right.addSpacing(3)
        right.addWidget(self.reset_range_btn)

        outer.addWidget(left_widget, 0, 0)
        outer.addWidget(self.frame_lbl, 0, 1, QtCore.Qt.AlignCenter)
        outer.addWidget(right_widget, 0, 2, QtCore.Qt.AlignRight)
        flank_width = max(left_widget.sizeHint().width(), right_widget.sizeHint().width())
        outer.setColumnMinimumWidth(0, flank_width)
        outer.setColumnMinimumWidth(2, flank_width)
        outer.setColumnStretch(0, 1)
        outer.setColumnStretch(2, 1)
        return outer

    def _transport_icon(self, name):
        return self._icon({
            'SP_MediaSkipBackward': 'first',
            'SP_MediaSeekBackward': 'previous',
            'SP_MediaPlay': 'play',
            'SP_MediaPause': 'pause',
            'SP_MediaSeekForward': 'next',
            'SP_MediaSkipForward': 'last',
        }.get(name, name))

    # -------------------------------------------------------------- wiring
    def _connect(self):
        self.player.frameChanged.connect(self._on_frame_changed)
        # A transport-frame event may precede asynchronous decode/presentation.
        # Scopes must inspect the pixels actually put on screen, not the last
        # QPixmap that happened to be resident when the playhead moved.
        self.player.framePresented.connect(self._update_scopes)
        self.player.framePresented.connect(self._on_comparison_frame_presented)
        self.player.cacheChanged.connect(self.timeline.set_cache_ranges)
        self.player.bufferProgressChanged.connect(self.timeline.set_buffer_progress)
        self.player.playingChanged.connect(self._on_playing_changed)
        self.player.errorText.connect(
            lambda m: self.status_lbl.setText('Playback error: ' + html.escape(m)))

        self.surface.cropChanged.connect(self._on_crop_changed)
        self.surface.scrubRequested.connect(lambda d: self._scrub(self.player.current_frame + d))
        # A real artist drag/drop is a user open. Programmatic media loads
        # (scene restoration, composition placeholders and automated tests)
        # are NOT evidence that a file belongs in Open Recent.
        self.surface.mediaDropped.connect(
            lambda path: self._load_path(path, remember_recent=True))
        self.surface.importRequested.connect(self._choose_media)
        self.surface.edited.connect(self._on_surface_edited)
        self.surface.selectionChanged.connect(self._on_shape_selection_changed)
        if hasattr(self.surface, 'displayColorStatusChanged'):
            self.surface.displayColorStatusChanged.connect(
                lambda description: self.color_pipeline_btn.setToolTip(
                    'Precision viewer: {}. Color settings belong to this source; '
                    'the monitor output is still SDR.'.format(description)))
        self.review_records_panel.noteSelected.connect(self._on_review_note_selected)
        self.review_records_panel.saveRequested.connect(self._on_review_note_saved)
        self.review_records_panel.replyRequested.connect(self._on_review_reply_requested)
        if hasattr(self.review_records_panel, 'replyEditRequested'):
            self.review_records_panel.replyEditRequested.connect(
                self._on_review_reply_edit_requested)
        if hasattr(self.review_records_panel, 'replyDeleteRequested'):
            self.review_records_panel.replyDeleteRequested.connect(
                self._on_review_reply_delete_requested)
        if hasattr(self.review_records_panel, 'attachmentAddRequested'):
            self.review_records_panel.attachmentAddRequested.connect(
                self._on_review_attachment_add_requested)
        if hasattr(self.review_records_panel, 'attachmentRemoveRequested'):
            self.review_records_panel.attachmentRemoveRequested.connect(
                self._on_review_attachment_remove_requested)
        self.review_records_panel.createRequested.connect(self._on_new_review_note)
        self.review_records_panel.layerUpdated.connect(self._on_review_layer_updated)
        self.timeline.scrubbed.connect(self._scrub)
        custom_menu = getattr(QtCore.Qt, 'CustomContextMenu', None)
        if custom_menu is None:
            custom_menu = QtCore.Qt.ContextMenuPolicy.CustomContextMenu
        self.timeline.setContextMenuPolicy(custom_menu)
        self.timeline.customContextMenuRequested.connect(self._show_timeline_menu)

        self.in_spin.valueChanged.connect(self._range_changed)
        self.out_spin.valueChanged.connect(self._range_changed)
        # Record one undo step per committed edit, not per keystroke.
        self.in_spin.editingFinished.connect(self._commit_range_undo)
        self.out_spin.editingFinished.connect(self._commit_range_undo)
        self.first_btn.clicked.connect(lambda: self._scrub(self.player.in_frame))
        self.last_btn.clicked.connect(lambda: self._scrub(self.player.out_frame))
        self.prev_btn.clicked.connect(lambda: self._scrub(self.player.current_frame - 1))
        self.next_btn.clicked.connect(lambda: self._scrub(self.player.current_frame + 1))
        self.play_btn.clicked.connect(self.player.toggle)
        self.loop_btn.toggled.connect(self.act_loop.setChecked)
        self.act_loop.toggled.connect(self.loop_btn.setChecked)
        self.sync_btn.toggled.connect(self._toggle_maya_sync)
        self.sync_offset_spin.valueChanged.connect(self._on_sync_offset_changed)
        self.mute_btn.singleClicked.connect(self._show_volume_popup)
        self.mute_btn.doubleClicked.connect(self._toggle_audio_mute)
        self.volume_slider.valueChanged.connect(
            lambda value: self.player.set_volume(value / 100.0))
        self.volume_slider.valueChanged.connect(
            lambda value: self.volume_value_lbl.setText(str(int(value))))
        self.set_in_btn.clicked.connect(self._mark_in)
        self.set_out_btn.clicked.connect(self._mark_out)
        self.reset_range_btn.clicked.connect(self._reset_marked_range)
        self.speed_combo.currentIndexChanged.connect(
            lambda: self._set_speed(float(self.speed_combo.currentData() or 1.0)))

        if self.track_btn is not None:
            self.track_btn.clicked.connect(self._track_forward)
            self.clear_track_btn.clicked.connect(lambda: self._clear_tracking())
            self.correct_track_btn.clicked.connect(self._correct_current_track_frame)
            self.stabilize_view_cb.toggled.connect(self._set_stabilization_preview)

        self.media_import_btn.clicked.connect(self._choose_media)
        self.media_prev_marker_btn.clicked.connect(self._previous_frame_marker)
        self.media_next_marker_btn.clicked.connect(self._next_frame_marker)
        self.clear_crop_btn.clicked.connect(lambda: self.surface.set_crop(None))
        self.crop_overlay_check.toggled.connect(self.act_crop.setChecked)
        self.act_crop.toggled.connect(self.crop_overlay_check.setChecked)

        self.draw_color_picker.currentColorChanged.connect(self._set_drawing_color)
        for mode, button in (
                ('select', self.select_mode_btn),
                ('brush', self.brush_mode_btn),
                ('eraser', self.eraser_mode_btn),
                ('rectangle', self.rect_mode_btn),
                ('ellipse', self.ellipse_mode_btn),
                ('arrow', self.arrow_mode_btn),
                ('text', self.text_mode_btn)):
            button.toggled.connect(
                lambda checked, name=mode: checked and self._set_draw_mode(name))
        for swatch in self.draw_swatches:
            swatch.clicked.connect(
                lambda _checked=False, button=swatch:
                    self._set_drawing_color(button.property('brushColor')))
        for control in (self.draw_width, self.draw_opacity, self.draw_smoothing,
                        self.pressure_size_check, self.pressure_opacity_check):
            if hasattr(control, 'valueChanged'):
                control.valueChanged.connect(self._update_brush_style)
            else:
                control.toggled.connect(self._update_brush_style)
        self._update_brush_style()
        self.draw_width.valueChanged.connect(self._on_draw_size_changed)
        self.clear_drawing_btn.clicked.connect(self.surface.clear_current_drawing)
        self.undo_drawing_btn.clicked.connect(self.surface.undo_drawing_stroke)
        self.delete_selected_btn.clicked.connect(self.surface.delete_selected_strokes)
        self.blank_drawing_btn.clicked.connect(self.surface.create_blank_drawing)
        self.delete_all_notes_btn.clicked.connect(self._delete_all_notes)
        self.prev_drawing_btn.clicked.connect(lambda: self._step_drawing(-1))
        self.next_drawing_btn.clicked.connect(lambda: self._step_drawing(1))
        for control in (self.onion_enable, self.onion_falloff, self.onion_tint):
            control.toggled.connect(self._update_onion_skin)
        for control in (self.onion_before, self.onion_after, self.onion_opacity):
            control.valueChanged.connect(self._update_onion_skin)
        self.import_notes_btn.clicked.connect(self._import_notes)
        self.export_notes_btn.clicked.connect(self._export_notes)
        self.surface.drawingChanged.connect(self._on_drawing_changed)
        self.surface.pixelHovered.connect(self._on_pixel_hovered)
        self.surface.pixelProbesChanged.connect(self._on_pixel_probes_changed)
        self.surface.pixelMagnifierChanged.connect(self._on_pixel_magnifier_changed)
        self.surface.activeAnnotationChanged.connect(self._on_active_annotation_changed)
        self.surface.annotationFramesChanged.connect(
            self.timeline.set_annotation_frames)
        self.surface.tabletPressureChanged.connect(self._on_tablet_pressure)
        self.composition_combo.currentIndexChanged.connect(
            lambda *_: self.surface.set_composition_mode(
                self.composition_combo.currentData()))
        self.composition_combo.currentIndexChanged.connect(
            lambda *_: self.settings.set(
                'view/composition', self.composition_combo.currentData()))
        self.color_btn.clicked.connect(self._show_color_tool)
        self.review_thumbs_check.toggled.connect(self.act_thumbs.setChecked)
        self.act_thumbs.toggled.connect(self.review_thumbs_check.setChecked)
        self.review_wave_check.toggled.connect(self.act_wave.setChecked)
        self.act_wave.toggled.connect(self.review_wave_check.setChecked)
        self.review_flip_h.toggled.connect(self.act_flip_h.setChecked)
        self.act_flip_h.toggled.connect(self.review_flip_h.setChecked)
        self.review_flip_v.toggled.connect(self.act_flip_v.setChecked)
        self.act_flip_v.toggled.connect(self.review_flip_v.setChecked)
        self.tracking_mode.currentIndexChanged.connect(
            lambda *_: self._clear_tracking() if self.tracking_enabled else None)
        self.fit_btn.clicked.connect(self.surface.fit_view)
        self.zoom_out_btn.clicked.connect(lambda: self.surface.zoom_by(0.8))
        self.zoom_in_btn.clicked.connect(lambda: self.surface.zoom_by(1.25))
        self.export_btn.clicked.connect(self._export)

        self.waveform_job.ready.connect(self.timeline.set_waveform)
        self.waveform_job.ready.connect(lambda *_: self._task_end('waveform'))
        self.record_waveform.ready.connect(self._on_record_waveform_ready)
        self.record_waveform.failed.connect(self._on_record_waveform_failed)
        if hasattr(self.player, 'recordAudioStateChanged'):
            self.player.recordAudioStateChanged.connect(
                self._on_record_audio_transport_state)
        if hasattr(self.player, 'recordAudioError'):
            self.player.recordAudioError.connect(
                self._on_record_audio_transport_error)
        self.thumb_job.started.connect(
            lambda total: self.timeline.begin_thumbnails(total, THUMB_LANE_H))
        self.thumb_job.started.connect(self._on_thumbs_started)
        self.thumb_job.produced.connect(self.timeline.add_thumbnail)
        self.thumb_job.produced.connect(lambda *_: self._on_thumb_produced())
        self.thumb_job.finished.connect(self.timeline.end_thumbnails)
        self.thumb_job.finished.connect(lambda: self._task_end('thumbs'))

        self._shortcuts = []
        for keys, callback in (
            ('Up', lambda: self._focus_clip(+1)),
            ('Down', lambda: self._focus_clip(-1)),
            ('Home', lambda: self._scrub(self.player.in_frame)),
            ('End', lambda: self._scrub(self.player.out_frame)),
        ):
            shortcut = QShortcut(QtGui.QKeySequence(keys), self)
            shortcut.setContext(QtCore.Qt.WidgetWithChildrenShortcut)
            shortcut.activated.connect(callback)
            self._shortcuts.append(shortcut)

    # --------------------------------------------------------- media load
    def _choose_open(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, 'Open media or review', self._notes_directory(),
            'Media / FrameReviewer Notes '
            '(*.frsession *.json *.mp4 *.mov *.mkv *.webm *.avi *.m4v '
            '*.jpg *.jpeg *.png *.tga *.exr);;All files (*)')
        if path:
            self._load_path(path, remember_recent=True)

    def _show_playblast_options(self):
        """Native-style option box for Playblast Current Shot.

        A small Qt dialog mirroring the handful of Maya playblast settings
        FrameReviewer uses, persisted and fed to :meth:`_playblast_current_shot`.
        """
        if cmds is None:
            QtWidgets.QMessageBox.warning(self, 'Playblast unavailable', 'Maya is required.')
            return
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle('Playblast Options')
        form = QtWidgets.QFormLayout(dialog)

        source_combo = QtWidgets.QComboBox()
        source_combo.addItem('Current Camera Sequencer shot', 'shot')
        source_combo.addItem('Playback range', 'range')
        source_index = source_combo.findData(
            self.settings.get_str('playblast/source', 'shot'))
        source_combo.setCurrentIndex(max(0, source_index))

        scale = QtWidgets.QSpinBox()
        scale.setRange(10, 100)
        scale.setSuffix(' %')
        scale.setValue(self.settings.get_int('playblast/scale', 100))
        quality = QtWidgets.QSpinBox()
        quality.setRange(1, 100)
        quality.setValue(self.settings.get_int('playblast/quality', 90))
        ornaments = QtWidgets.QCheckBox('Show ornaments')
        ornaments.setChecked(self.settings.get_bool('playblast/ornaments', False))
        offscreen = QtWidgets.QCheckBox('Render off-screen')
        offscreen.setChecked(self.settings.get_bool('playblast/offscreen', True))

        form.addRow('Source', source_combo)
        form.addRow('Scale', scale)
        form.addRow('Quality', quality)
        form.addRow('', ornaments)
        form.addRow('', offscreen)

        buttons = QtWidgets.QDialogButtonBox()
        run_btn = buttons.addButton('Playblast', QtWidgets.QDialogButtonBox.AcceptRole)
        buttons.addButton('Save', QtWidgets.QDialogButtonBox.ApplyRole)
        buttons.addButton('Close', QtWidgets.QDialogButtonBox.RejectRole)
        form.addRow(buttons)

        def persist():
            self.settings.set('playblast/source', source_combo.currentData())
            self.settings.set('playblast/scale', int(scale.value()))
            self.settings.set('playblast/quality', int(quality.value()))
            self.settings.set('playblast/ornaments', bool(ornaments.isChecked()))
            self.settings.set('playblast/offscreen', bool(offscreen.isChecked()))

        def on_click(button):
            role = buttons.buttonRole(button)
            if role == QtWidgets.QDialogButtonBox.ApplyRole:
                persist()
            elif role == QtWidgets.QDialogButtonBox.AcceptRole:
                persist()
                dialog.accept()
            else:
                dialog.reject()

        buttons.clicked.connect(on_click)
        accepted = dialog.exec() if hasattr(dialog, 'exec') else dialog.exec_()
        if accepted:
            self._playblast_current_shot()

    def _playblast_current_shot(self):
        """Capture Maya's active camera/time-slider range as a JPEG sequence."""
        if cmds is None:
            QtWidgets.QMessageBox.warning(self, 'Playblast unavailable', 'Maya is required.')
            return
        if self._closed or (self._export_job is not None and self._export_job.isRunning()):
            return
        settings = self.settings
        use_shot = settings.get_str('playblast/source', 'shot') != 'range'
        percent = int(utils.clamp(settings.get_int('playblast/scale', 100), 10, 100))
        quality = int(utils.clamp(settings.get_int('playblast/quality', 90), 1, 100))
        show_ornaments = settings.get_bool('playblast/ornaments', False)
        off_screen = settings.get_bool('playblast/offscreen', True)
        folder = tempfile.mkdtemp(prefix='framereviewer_playblast_')
        prefix = os.path.join(folder, 'shot')
        try:
            # Prefer the current Camera Sequencer shot. Outside a sequencer
            # shot Maya's playblast defaults to the playback slider range.
            range_args = {}
            if use_shot:
                current = float(cmds.currentTime(query=True))
                shots = []
                for shot in cmds.ls(type='shot') or []:
                    start = float(cmds.shot(shot, query=True, startTime=True))
                    end = float(cmds.shot(shot, query=True, endTime=True))
                    if start <= current <= end:
                        shots.append((end - start, start, end))
                if shots:
                    _, start, end = min(shots)
                    range_args = {'startTime': start, 'endTime': end}
            # Maya's image playblast is portable across Linux, macOS and Windows;
            # FrameReviewer already supports numbered image sequences.
            result = cmds.playblast(
                format='image', compression='jpg', filename=prefix,
                forceOverwrite=True, viewer=False, showOrnaments=show_ornaments,
                offScreen=off_screen, percent=percent, quality=quality,
                clearCache=True, sequenceTime=False, **range_args)
            files = sorted(glob.glob(prefix + '*.jpg') + glob.glob(prefix + '*.jpeg'))
            if not files and result:
                files = sorted(glob.glob(os.path.splitext(str(result))[0] + '*.jpg'))
            if not files:
                raise RuntimeError('Maya did not create playblast frames.')
            self._playblast_path = files[0]
            self._load_path(files[0])
            self._update_reference_actions()
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, 'Playblast failed', str(exc))

    def _build_sequencer_playlist(self):
        """Cache Maya Sequencer shots and show them on one ordered timeline."""
        if cmds is None:
            QtWidgets.QMessageBox.information(
                self, 'Camera Sequencer', 'Open FrameReviewer inside Maya.')
            return
        from ..integrations import maya_sequencer
        shots = maya_sequencer.inventory(cmds)
        if not shots:
            self.status_lbl.setText('No Camera Sequencer shots found in the scene.')
            return
        refresh = QtWidgets.QMessageBox.question(
            self, 'Build Camera Sequencer Playlist',
            'Add {} Sequencer shots as stable review sources?\n\n'
            'Saved, unchanged scenes can reuse verified cached playblasts. '
            'Unsaved scene edits or untitled scenes require fresh renders; '
            'save the scene first for repeatable caching. Existing reviewed '
            'versions are retained.'.format(len(shots)),
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.Cancel,
            QtWidgets.QMessageBox.Yes)
        if refresh != QtWidgets.QMessageBox.Yes:
            return
        progress = QtWidgets.QProgressDialog('Preparing Sequencer playblasts…',
                                             'Cancel remaining shots', 0,
                                             len(shots), self)
        progress.setWindowModality(QtCore.Qt.WindowModal)
        progress.setMinimumDuration(0)
        self._capture_current_review()
        controller = self.session_controller
        sources = maya_sequencer.add_to_session(controller, shots)
        failures = []
        completed = 0
        for index, (shot, source) in enumerate(zip(shots, sources)):
            if self._closed or progress.wasCanceled():
                break
            progress.setLabelText('Playblast {} ({}/{})'.format(
                shot.node, index + 1, len(shots)))
            QtWidgets.QApplication.processEvents()
            try:
                path = maya_sequencer.render(
                    shot, cmds, percent=self.settings.get_int('playblast/scale', 100),
                    quality=self.settings.get_int('playblast/quality', 90),
                    show_ornaments=self.settings.get_bool('playblast/ornaments', False),
                    off_screen=self.settings.get_bool('playblast/offscreen', True))
                maya_sequencer.attach_playblast(source, path)
                controller.dirty = True
                completed += 1
            except (OSError, IOError, RuntimeError, TypeError, ValueError) as exc:
                failures.append('{}: {}'.format(shot.node, exc))
            progress.setValue(index + 1)
        progress.close()
        controller.reorder([shot.source_id for shot in shots])
        if completed:
            self.info = None
            first_ready = next(
                (source for source in sources if source.active_path() and
                 os.path.isfile(source.active_path())), None)
            if first_ready is not None:
                controller.set_current(first_ready.id)
        self.playlist_panel.set_session(controller)
        self._show_playlist()
        if completed:
            sequence_index = self.playlist_panel.mode_combo.findData('sequence')
            self.playlist_panel.mode_combo.setCurrentIndex(sequence_index)
            if not failures:
                self.playlist_panel._emit_composition()
            else:
                self.status_lbl.setText('Some shots are offline; relink missing '
                                        'shots before building the full sequence.')
        self.status_lbl.setText('Sequencer: {} of {} shots with playblasts{}.'.format(
            completed, len(shots), ' · {} need attention'.format(len(failures))
            if failures else ''))
        if failures:
            dialog = QtWidgets.QMessageBox(self)
            dialog.setWindowTitle('Sequencer playblast results')
            dialog.setText('{} shot(s) could not be rendered.'.format(len(failures)))
            dialog.setDetailedText('\n'.join(failures))
            dialog.exec_()
        self._schedule_scene_state_save()

    def _set_reference(self):
        """Mark the currently loaded media as the A/B reference clip."""
        if self.info is None:
            self.status_lbl.setText('Load media first to set it as the reference.')
            return
        self._reference_path = self.info.path
        self.status_lbl.setText(
            'Reference set: ' + os.path.basename(self._reference_path))
        self._update_reference_actions()

    def _toggle_reference_playblast(self):
        """Swap the viewer between the reference clip and the latest playblast."""
        reference, playblast = self._reference_path, self._playblast_path
        if not (reference and playblast):
            self.status_lbl.setText(
                'Set a reference and create a playblast to A/B between them.')
            return
        current = self.info.path if self.info is not None else None
        on_reference = (current and
                        os.path.normpath(current) == os.path.normpath(reference))
        target = playblast if on_reference else reference
        if not os.path.exists(target):
            self.status_lbl.setText('Toggle target no longer exists:\n' + target)
            return
        frame = self.player.current_frame
        self._load_path(target)
        # Hold the playhead across a successful reference swap only. Qt may
        # deliver this after another source switch, scrub or Play action:
        # never move that newer source back to the original A/B playhead.
        loaded = self.info
        if (loaded is not None and os.path.normcase(os.path.normpath(loaded.path)) ==
                os.path.normcase(os.path.normpath(target))):
            generation = self._navigation_generation
            current = self.session_controller.current
            source_id = current.id if current is not None else None

            def _retain_reference_frame():
                if (self._closed or self._shutting_down or
                        self._navigation_generation != generation or
                        self.info is not loaded or self.player.playing):
                    return
                now = self.session_controller.current
                if (now.id if now is not None else None) != source_id:
                    return
                if self.player.frame_count > 0:
                    self.player.seek(max(0, min(frame, self.player.frame_count - 1)))

            QtCore.QTimer.singleShot(0, _retain_reference_frame)
        self._update_reference_actions()

    def _update_reference_actions(self):
        both = bool(self._reference_path and self._playblast_path)
        self.act_toggle_reference.setEnabled(both)
        on_reference = (self.info is not None and self._reference_path and
                        os.path.normpath(self.info.path) ==
                        os.path.normpath(self._reference_path))
        self.act_toggle_reference.setText(
            'Toggle Reference / Playblast'
            + ('  (showing Reference)' if both and on_reference else
               '  (showing Playblast)' if both else ''))

    def _choose_media(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, 'Import media', os.path.expanduser('~'),
            'Media (*.mp4 *.mov *.mkv *.webm *.avi *.m4v '
            '*.jpg *.jpeg *.png *.tga *.exr);;All files (*)')
        if path:
            self._load_path(path, remember_recent=True)

    def _remember_recent(self, path):
        self.settings.add_recent(path)
        self._populate_recent_menu()  # reflect the new entry immediately

    def _populate_recent_menu(self):
        if self._closed:
            return
        try:
            # Recents may be corrected in another Maya process (for example
            # after removing an accidentally persisted QA fixture). Re-read
            # QSettings rather than holding stale menu actions indefinitely.
            self.settings.sync()
            self.recent_menu.clear()
            paths = self.settings.prune_expired_test_recents()
            if not paths:
                empty = self.recent_menu.addAction('No Recent Media')
                empty.setEnabled(False)
                return
            for path in paths:
                label = os.path.basename(path) or path
                present = os.path.exists(path)
                recent_action = self.recent_menu.addAction(
                    label if present else '{}  (unavailable)'.format(label))
                recent_action.setToolTip(
                    path if present else
                    '{}\nCurrently unavailable. Retained for removable or '
                    'network media; reconnect and reopen this menu.'.format(path))
                recent_action.setEnabled(present)
                if present:
                    recent_action.triggered.connect(
                        lambda _checked=False, media_path=path:
                            self._open_recent(media_path))
            self.recent_menu.addSeparator()
            self.recent_menu.addAction('Clear Recent Media', self._clear_recent_media)
        except RuntimeError:
            # The menu's C++ object was torn down (workspace-control close or
            # hot reload) while its aboutToShow slot was still connected.
            return

    def _open_recent(self, path):
        if os.path.exists(path):
            self._load_path(path, remember_recent=True)
            return
        # The path may have vanished AFTER opening the menu. Preserve true
        # user opens for offline disks; only historical expired QA artifacts
        # are safe to remove automatically.
        if self.settings._expired_test_recent(path):
            self.settings.remove_recent(path)
        self._populate_recent_menu()
        self.status_lbl.setText(
            'Recent media is currently unavailable; original history kept '
            'for reconnection: {}'.format(path))

    def _clear_recent_media(self):
        self.settings.clear_recent()
        self._populate_recent_menu()

    def _load_path(self, path, remember_recent=False):
        """Resolve a media path; only explicit artist opens enter Open Recent.

        Auto-restores and test-driven internal load calls use the default
        False. The file chooser, Open Recent and viewport drop explicitly opt
        in; recents must never become a log of every temporary decoder path.
        """
        if self._closed or (self._export_job is not None and self._export_job.isRunning()):
            return
        try:
            resolved = self.source_resolver.resolve(path)
            if resolved.kind == 'session':
                self._open_session(resolved.local_path,
                                   remember_recent=remember_recent)
                return
            if resolved.kind == 'notes':
                self._open_notes_path(resolved.local_path,
                                      remember_recent=remember_recent)
                return
            if resolved.kind == 'folder':
                if not resolved.candidates:
                    raise ValueError('No supported media in this folder.')
                self._queue_playlist_paths(resolved.candidates)
                path = resolved.candidates[0]
                resolved = self.source_resolver.resolve(path)
            if resolved.kind in ('audio', 'color'):
                raise ValueError('Open this asset from its audio or color controls.')
            if not resolved.exists:
                raise IOError('Missing media: ' + resolved.authored_path)
            info = self._probe_source_media(
                resolved.local_path, sequence_pattern=resolved.sequence_pattern,
                ask_exr_selection=True)
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, 'Import failed', str(exc))
            return
        self._load(info)
        if (remember_recent and self.info is not None and
                os.path.realpath(os.path.abspath(self.info.path)) ==
                os.path.realpath(os.path.abspath(info.path))):
            self._remember_recent(info.path)

    def _probe_source_media(self, path, sequence_pattern=None,
                            ask_exr_selection=False):
        """Reopen a saved native EXR part/layer, or explicitly ask on import.

        A layer is a representation of the SAME logical Source: stable review
        and ShotGrid IDs survive changing channels. FFmpeg cannot safely
        choose an ambiguous EXR layer; no first-layer guess is permitted.
        """
        from ..media import exr
        source = self.session_controller.source_for_path(path)
        selection = (source.metadata if source is not None else {}) or {}
        explicit = bool(selection.get('exr_selection_explicit'))
        kwargs = ({'exr_layer': selection.get('exr_layer'),
                   'exr_part': selection.get('exr_part')}
                  if exr.is_exr(path) and explicit else {})
        try:
            info = ffmpegtools.probe(
                self.ffprobe, QtGui.QImageReader, path,
                sequence_pattern=sequence_pattern, **kwargs)
            if explicit:
                info.metadata['exr_selection_explicit'] = True
            return self._exr_display_canvas_info(info)
        except RuntimeError:
            if not ask_exr_selection or not exr.is_exr(path):
                raise
            views = exr.available_views(path)
            if len(views) < 2:
                raise
        return self._ask_exr_view(path, views, sequence_pattern)

    @staticmethod
    def _exr_display_canvas_info(info):
        """Treat verified EXR displayWindow as the native review canvas.

        Decode channels still belong to the absolute dataWindow. A single
        immutable source FramePayload translates them into a display-local
        raster; crop, drawing and pixel selection operate in those same
        coordinates. The EXR absolute origin is retained for inspection.
        """
        from ..media import exr
        from ..media.window_geometry import DisplayCanvasPlacement
        metadata = dict(getattr(info, 'metadata', {}) or {})
        if metadata.get('exr_display_canvas_rectified'):
            if tuple(metadata.get('display_window', ())[2:]) != (
                    info.width, info.height):
                raise ValueError('Restored EXR canvas dimensions are inconsistent.')
            return info
        if (not getattr(info, 'is_sequence', False) or
                not exr.is_exr(info.path) or
                not metadata.get('exr_header_verified') or
                not metadata.get('data_window') or
                not metadata.get('display_window')):
            return info
        data = tuple(metadata['data_window'])
        display = tuple(metadata['display_window'])
        if data == display == (0, 0, info.width, info.height):
            return info
        placement = DisplayCanvasPlacement(data, display)
        if (info.width, info.height) != data[2:]:
            raise ValueError('EXR probe disagrees with verified native dataWindow.')
        # The full-resolution canvas must be within the same 256 MiB
        # allocation bound used by the actual decoder/rectifier. Be
        # conservative for four floating channels before media is loaded.
        if placement.size[0] * placement.size[1] * 16 > 256 * 1024 * 1024:
            raise ValueError('EXR displayWindow exceeds precision canvas budget.')
        metadata.update(
            exr_display_canvas_rectified=True,
            exr_canvas_origin=list(display[:2]),
            exr_data_raster_size=[info.width, info.height])
        return ffmpegtools.MediaInfo(
            info.path, placement.size[0], placement.size[1], info.fps,
            info.frame_count, sequence_files=info.sequence_files,
            audio=info.audio, frame_numbers=info.frame_numbers,
            fps_rational=info.fps_rational, metadata=metadata,
            missing_policy=info.missing_policy,
            sequence_source_frames=info.sequence_source_frames,
            declared_frames=info.declared_frames)

    def _ask_exr_view(self, path, views, sequence_pattern=None):
        names = ['Part {} · {} · {}×{} · PAR {:g}'.format(
            view['part'], view['layer'] or '<root RGB>', view['width'],
            view['height'], view['pixel_aspect_ratio']) for view in views]
        selected, accepted = QtWidgets.QInputDialog.getItem(
            self, 'Choose EXR Part / Layer',
            'Multiple valid RGB layers exist. Explicitly select the image '
            'to review (native OpenEXR decoder required):', names, 0, False)
        if not accepted:
            raise ValueError('EXR part/layer selection was cancelled.')
        chosen = views[names.index(selected)]
        info = ffmpegtools.probe(
            self.ffprobe, QtGui.QImageReader, path,
            sequence_pattern=sequence_pattern,
            exr_layer=chosen['layer'], exr_part=chosen['part'])
        info.metadata['exr_selection_explicit'] = True
        return self._exr_display_canvas_info(info)

    def _choose_exr_view(self):
        """Change EXR color channels without inventing a second Source ID."""
        if self.info is None or not self.info.is_sequence or self._composition is not None:
            self.status_lbl.setText('Open one EXR sequence to select its part/layer.')
            return
        from ..media import exr
        if not exr.is_exr(self.info.path):
            self.status_lbl.setText('Part/layer selection applies to EXR sources only.')
            return
        views = exr.available_views(self.info.path)
        if not views:
            QtWidgets.QMessageBox.warning(
                self, 'EXR layer selection unavailable',
                'No selectable native RGB(A) parts/layers were verified. '
                'Install compatible OpenEXR Python bindings in Maya; FFmpeg '
                'cannot explicitly select a non-default EXR part/layer.')
            return
        try:
            info = self._ask_exr_view(self.info.path, views)
        except (RuntimeError, ValueError) as exc:
            self.status_lbl.setText('EXR selection unchanged: {}'.format(exc))
            return
        previous = (self.session_controller.current.metadata
                    if self.session_controller.current is not None else {})
        if (previous.get('exr_selection_explicit') and
                previous.get('exr_layer') == info.metadata.get('exr_layer') and
                previous.get('exr_part') == info.metadata.get('exr_part')):
            return  # identical selection: do not schedule recovery
        # Changing the selected EXR channels is a real review-source edit.
        # reconcile_info refreshes technical probe metadata but does not mark
        # source metadata dirty (deliberately, for unchanged navigation). Mark
        # this explicit artist selection so a portable .frsession and its
        # bounded recovery checkpoint actually retain it after Maya reload.
        self.session_controller.update_source_metadata({
            'exr_selection_explicit': True,
            'exr_layer': info.metadata.get('exr_layer'),
            'exr_part': info.metadata.get('exr_part')})
        self._load(info)

    def _recovery_path(self):
        """Use a discoverable recovery file without overwriting an explicit save."""
        controller = self.session_controller
        if controller.path:
            return controller.path + '.autosave.frsession'
        folder = os.path.join(os.path.expanduser('~'), '.framereviewer', 'recovery')
        return os.path.join(folder, controller.session.id + '.frsession')

    def _review_document_for_source(self, source, mapping):
        """One canonical typed document per currently loaded logical source.

        Existing serialized review records are authoritative. Old sessions
        holding only display drawings migrate once without changing source
        frame numbers or replacing existing shape identities.
        """
        stored = (source.review or {}).get('review_records')
        document = (ReviewDocument.from_payload(stored) if isinstance(stored, dict)
                    else ReviewDocument.from_session_source(source, mapping))
        if document.source_id != source.id:
            raise ValueError('Review notes belong to another source: {}'.format(
                document.source_id))
        return document

    def _bind_review_document(self, source):
        self._review_adapter = None
        if source is None or self.info is None or self._composition is not None:
            self.review_records_panel.bind(None)
            return
        try:
            mapping = self.info.frame_mapping()
            bridge = ReviewSurfaceAdapter(
                self._review_document_for_source(source, mapping), mapping)
            bridge.push(self.surface)
        except (ValueError, TypeError, ReviewProjectionError) as exc:
            self.review_records_panel.bind(None)
            self.status_lbl.setText('Could not restore review records: {}'.format(exc))
            return
        self._review_adapter = bridge
        self._refresh_review_records()

    def _refresh_review_records(self, select_id=None):
        bridge = self._review_adapter
        document = bridge.document if bridge is not None else None
        if select_id is None and document is not None:
            select_id = self.review_records_panel.selected_id()
        self.review_records_panel.bind(document, selected_id=select_id)

    def _persist_review_document(self):
        bridge = self._review_adapter
        current = self.session_controller.current
        if (bridge is None or current is None or
                bridge.document.source_id != current.id):
            return False
        mapping = bridge.frame_mapping
        drawings = {
            str(mapping.display_to_source(int(frame))): strokes
            for frame, strokes in self.surface.drawing_data().items()}
        changed = self.session_controller.update_review({
            'review_records': bridge.document.to_payload(),
            'drawings': drawings,
        }, source_id=current.id)
        # Merely capturing or reselecting an unchanged review must not arm a
        # recovery write. SessionController compares the complete snapshot.
        if changed:
            self._schedule_scene_state_save()
        return changed

    def _commit_review_projection(self, edited_frame=None):
        bridge = self._review_adapter
        if bridge is None or self._restoring_review:
            return ()
        selected = self.surface.selected_shape_ids()
        try:
            changed = bridge.pull(self.surface, edited_frame=edited_frame)
        except (ReviewProjectionError, PermissionError, ValueError) as exc:
            # Locked/hidden layers cannot be changed by manipulating their
            # stale surface items, and remote merge generations win atomically.
            bridge.push(self.surface)
            self.status_lbl.setText('Review edit not applied: {}'.format(exc))
            return ()
        if changed:
            self.surface.select_shape_ids(selected)
            self._persist_review_document()
            self._refresh_review_records()
        return changed

    def _on_shape_selection_changed(self, shape_ids):
        bridge = self._review_adapter
        if not bridge or not shape_ids:
            return
        note_id = bridge.note_id_for_shape(shape_ids[0])
        if note_id:
            self.review_records_panel.select_note(note_id)

    def _on_review_note_selected(self, note_id):
        bridge = self._review_adapter
        note = bridge.document.get(note_id) if bridge else None
        if note is None:
            return
        frame = bridge._display_frame(note)
        if self.player.current_frame != frame and frame >= 0:
            self._scrub(frame)
        if note.strokes:
            self.surface.select_shape_ids([s['id'] for s in note.strokes])

    def _on_new_review_note(self):
        if self.info is None or self._composition is not None:
            return
        if self._review_adapter is None:
            self._bind_review_document(self.session_controller.current)
        bridge = self._review_adapter
        if bridge is None:
            return
        try:
            frame = int(self.player.current_frame)
            note = bridge.document.add({
                'source_id': bridge.document.source_id,
                'frame': frame,
                'source_frame': bridge._source_frame(frame),
                'body': 'Review note', 'status': 'open', 'priority': 'normal'})
            bridge.push(self.surface)
            self._persist_review_document()
            self._refresh_review_records(note.id)
        except (ValueError, PermissionError, ReviewProjectionError) as exc:
            self.status_lbl.setText('Could not create review note: {}'.format(exc))

    def _on_review_note_saved(self, note_id, fields):
        bridge = self._review_adapter
        note = bridge.document.get(note_id) if bridge else None
        if note is None:
            return
        changes = {key: value for key, value in fields.items()
                   if getattr(note, key) != value}
        if not changes:
            return
        try:
            bridge.document.update(note_id, **changes)
            bridge.push(self.surface)
            self._persist_review_document()
            self._refresh_review_records(note_id)
        except (PermissionError, ValueError, KeyError,
                ReviewProjectionError) as exc:
            self.status_lbl.setText('Could not edit review note: {}'.format(exc))
            self._refresh_review_records(note_id)

    def _on_review_reply_requested(self, note_id, body):
        """Persist discussion locally; remote Flow writes always need consent."""
        bridge = self._review_adapter
        if (bridge is None or not body.strip() or
                bridge.document.get(note_id) is None or
                self.review_records_panel.selected_id() != note_id):
            return
        # Reply publication is independent of the main note's Apply Changes
        # button; do not discard uncommitted body/category/status edits.
        staged_fields = self.review_records_panel.current_fields()
        try:
            bridge.document.add_reply(note_id, body.strip())
            self._persist_review_document()
            self.review_records_panel.mark_reply_sent(note_id)
            self._refresh_review_records(note_id)
            self.review_records_panel.restore_fields(note_id, staged_fields)
        except (PermissionError, ValueError, KeyError,
                ReviewProjectionError) as exc:
            # Keep the draft if the layer is locked or persistence is rejected.
            self.status_lbl.setText('Could not add review reply: {}'.format(exc))

    def _on_review_reply_edit_requested(self, note_id, reply_id, body,
                                        expected_body, expected_modified):
        """Edit only an unchanged, unsynced local reply; never remote Flow."""
        bridge = self._review_adapter
        note = bridge.document.get(note_id) if bridge is not None else None
        if (note is None or self.review_records_panel.selected_id() != note_id):
            return
        original = next((r for r in note.replies if r.id == reply_id), None)
        if original is None or original.body == body:
            return  # stale or unchanged editing is never a review autosave
        staged_fields = self.review_records_panel.current_fields()
        try:
            bridge.document.update_reply(
                note_id, reply_id, body, expected_body=expected_body,
                expected_modified=expected_modified)
            self._persist_review_document()
            acknowledge = getattr(
                self.review_records_panel, 'mark_reply_updated', None)
            if callable(acknowledge):
                acknowledge(note_id, reply_id, body)
            self._refresh_review_records(note_id)
            self.review_records_panel.restore_fields(note_id, staged_fields)
        except (PermissionError, ValueError, KeyError, ReviewProjectionError) as exc:
            # The draft remains editable so the artist may inspect conflicts
            # or a locked layer without losing work.
            self.status_lbl.setText('Could not edit local reply: {}'.format(exc))

    def _on_review_reply_delete_requested(self, note_id, reply_id,
                                          expected_body, expected_modified):
        """Delete only a verified unsynced reply from the local review record."""
        bridge = self._review_adapter
        note = bridge.document.get(note_id) if bridge is not None else None
        if (note is None or self.review_records_panel.selected_id() != note_id or
                all(reply.id != reply_id for reply in note.replies)):
            return
        staged_fields = self.review_records_panel.current_fields()
        try:
            bridge.document.delete_reply(
                note_id, reply_id, expected_body=expected_body,
                expected_modified=expected_modified)
            self._persist_review_document()
            acknowledge = getattr(
                self.review_records_panel, 'mark_reply_deleted', None)
            if callable(acknowledge):
                acknowledge(note_id, reply_id)
            self._refresh_review_records(note_id)
            self.review_records_panel.restore_fields(note_id, staged_fields)
        except (PermissionError, ValueError, KeyError, ReviewProjectionError) as exc:
            self.status_lbl.setText('Could not delete local reply: {}'.format(exc))

    def _review_attachment_context(self, note_id, source_id):
        """Require the same active Source and a saved portable session."""
        bridge = self._review_adapter
        controller = self.session_controller
        current = controller.current
        if (self._closed or self._composition is not None or
                bridge is None or current is None or current.id != source_id or
                bridge.document.source_id != source_id or
                self.review_records_panel.selected_id() != note_id or
                bridge.document.get(note_id) is None):
            raise ValueError('Review Source or selected note changed; select it again.')
        path = controller.path
        if (not path or not os.path.isabs(path) or
                not path.lower().endswith('.frsession') or
                not os.path.isfile(path) or os.path.islink(path)):
            raise ValueError('Save this review as a named .frsession before attaching files.')
        return bridge.document, os.path.dirname(path)

    def _on_review_attachment_add_requested(self, note_id, source_path,
                                            kind, source_id, expected_revision):
        """Copy a validated local asset; remote publication is never implicit."""
        staged_fields = self.review_records_panel.current_fields()
        try:
            document, session_dir = self._review_attachment_context(note_id, source_id)
            document.add_local_attachment(
                note_id, source_path, session_dir=session_dir,
                expected_source_id=source_id, expected_revision=expected_revision,
                kind=kind)
            self._persist_review_document()
            self._refresh_review_records(note_id)
            self.review_records_panel.restore_fields(note_id, staged_fields)
        except (PermissionError, ValueError, KeyError, OSError, IOError,
                ReviewProjectionError) as exc:
            self.status_lbl.setText('Could not add local review attachment: {}'.format(exc))

    def _on_review_attachment_remove_requested(self, note_id, attachment_id,
                                               expected_uri, source_id,
                                               expected_revision):
        """Unlink an unsent local reference, not any artist/source asset."""
        staged_fields = self.review_records_panel.current_fields()
        try:
            document, _session_dir = self._review_attachment_context(note_id, source_id)
            document.remove_local_attachment(
                note_id, attachment_id, expected_uri=expected_uri,
                expected_source_id=source_id,
                expected_revision=expected_revision)
            self._persist_review_document()
            self._refresh_review_records(note_id)
            self.review_records_panel.restore_fields(note_id, staged_fields)
        except (PermissionError, ValueError, KeyError, OSError, IOError,
                ReviewProjectionError) as exc:
            self.status_lbl.setText('Could not remove local review attachment: {}'.format(exc))

    def _on_review_layer_updated(self, layer_id, updates):
        bridge = self._review_adapter
        layer = bridge.document.layer(layer_id) if bridge else None
        if layer is None:
            return
        changes = {key: value for key, value in updates.items()
                   if getattr(layer, key) != value}
        if not changes:
            return
        try:
            bridge.document.update_layer(layer_id, **changes)
            bridge.push(self.surface)
            self._persist_review_document()
            self._refresh_review_records()
        except (PermissionError, ValueError, ReviewProjectionError) as exc:
            self.status_lbl.setText('Could not change review layer: {}'.format(exc))
            self._refresh_review_records()

    def _shotgrid_review_context(self):
        """Return immutable remote publication context from the active source.

        Never publish a note inferred solely from the current movie filename.
        The Flow panel also verifies that its selected remote Version matches
        the persisted Source.metadata.shotgrid identity.
        """
        source = self.session_controller.current
        bridge = self._review_adapter
        document = (bridge.document if bridge is not None and source is not None and
                    bridge.document.source_id == source.id else None)
        return source, document, self.review_records_panel.selected_id()

    def _link_shotgrid_version(self, version, site, project, shot, path):
        """Attach real Flow Version identity to an existing logical Source."""
        source = self.session_controller.current
        if source is None or self.info is None or not version.get('id'):
            return
        # Stale async remote result must not attach to whichever local Source
        # happened to become active after a separate user-initiated load.
        if os.path.normcase(os.path.abspath(self.info.path)) != os.path.normcase(
                os.path.abspath(path)):
            return
        from ..integrations.shotgrid.client import normalize_site
        reference = {
            'site': normalize_site(site),
            'version_id': int(version['id']),
            'version_code': version.get('code') or '',
            'version_status': version.get('sg_status_list'),
            'project_id': int(project['id']) if project and project.get('id') else None,
            'shot_id': int(shot['id']) if shot and shot.get('id') else None,
        }
        self.session_controller.update_source_metadata({'shotgrid': reference},
                                                       source_id=source.id)
        self._schedule_scene_state_save()

    def _on_shotgrid_published(self, report, outbox):
        """Acknowledge notes only if their content still matches publication.

        A reviewer can edit/switch sources while ShotGrid is processing. A
        successful remote write is never proof that newer local edits are
        synced. Only the same stable source/version/note ID AND the exact
        published content hash can receive a remote reference.
        """
        for publication in report.results:
            if (publication.kind != 'note' or publication.state != 'done' or
                    not publication.remote_id or not publication.note_id):
                continue
            source = self.session_controller.session.source(publication.source_id)
            if source is None:
                continue
            identity = (source.metadata or {}).get('shotgrid') or {}
            if (identity.get('site') != outbox.site or
                    identity.get('version_id') != publication.version_id):
                continue
            bridge = (self._review_adapter if self._review_adapter is not None and
                      self._review_adapter.document.source_id == source.id else None)
            try:
                document = (bridge.document if bridge is not None else
                            self._review_document_for_source(source, None))
                note = document.get(publication.note_id)
                if note is None:
                    continue
                content = note.body.rstrip() + '\n\n' + (
                    '[FrameReviewer-Review:{}]'.format(publication.id))
                canonical = json.dumps(['FrameReviewer', content],
                                       ensure_ascii=False, sort_keys=True,
                                       separators=(',', ':'))
                checksum = hashlib.sha256(canonical.encode('utf-8')).hexdigest()
                if checksum != publication.content_hash:
                    continue  # edited since enqueue, retain local dirty note
                document.update(note.id, remote_ref={
                    'site': outbox.site,
                    'type': 'Note',
                    'id': publication.remote_id,
                    'version_id': publication.version_id})
                document.mark_synced([note.id])
                self.session_controller.update_review({
                    'review_records': document.to_payload()}, source_id=source.id)
                if bridge is not None:
                    selected = self.surface.selected_shape_ids()
                    bridge.push(self.surface)
                    self.surface.select_shape_ids(selected)
                    self._refresh_review_records(note.id)
                self._schedule_scene_state_save()
            except (ValueError, TypeError, PermissionError, ReviewProjectionError):
                # A conflict/locked layer or bad legacy payload must remain
                # locally dirty for explicit resolution, not silently vanish.
                continue

    def _capture_current_review(self):
        """Save live widget state in the current Source using source-frame keys."""
        if (self.info is None or self._restoring_review or
                self._restoring_scene_state or self._composition is not None):
            return
        controller = self.session_controller
        source = controller.current
        if source is None:
            return
        # External scripts and old sessions may call set_drawing_data()
        # directly, without emitting PlaybackSurface.edited. Reconcile such
        # deliberate differences before serializing the authoritative records.
        self._commit_review_projection()
        mapping = self.info.frame_mapping()
        def to_source(display_index):
            return mapping.display_to_source(display_index)
        drawing_data = self.surface.drawing_data()
        drawings = {str(to_source(int(frame))): strokes
                    for frame, strokes in drawing_data.items()}
        review_data = {
            'frame': int(self.player.current_frame),
            'source_frame': to_source(self.player.current_frame),
            'in_frame': int(self.player.in_frame),
            'out_frame': int(self.player.out_frame),
            'source_in': to_source(self.player.in_frame),
            'source_out': to_source(self.player.out_frame),
            'markers': sorted(int(frame) for frame in self._markers),
            'source_markers': sorted(to_source(frame) for frame in self._markers),
            'drawings': drawings,
            'crop': list(self.surface.crop()) if self.surface.crop() else None,
            'flip_h': bool(self.act_flip_h.isChecked()),
            'flip_v': bool(self.act_flip_v.isChecked()),
        }
        if (self._review_adapter is not None and
                self._review_adapter.document.source_id == source.id):
            review_data['review_records'] = self._review_adapter.document.to_payload()
        controller.update_review(review_data, source_id=source.id)

    def _restore_current_review(self, source):
        """Restore per-source annotations/crop/marks after decoder replacement."""
        review = source.review
        if not review or self.info is None:
            return
        self._restoring_review = True
        try:
            mapping = self.info.frame_mapping()
            def to_display(source_key, display_key, default):
                if source_key in review:
                    return mapping.source_to_display(review[source_key])
                return int(review.get(display_key, default))
            lo = to_display('source_in', 'in_frame', 0)
            hi = to_display('source_out', 'out_frame', self.player.frame_count - 1)
            self._set_marked_range(lo, max(lo, hi))
            markers = (mapping.source_to_display(frame)
                       for frame in review.get('source_markers',
                                               review.get('markers', [])))
            self._markers = set(markers)
            self.timeline.set_markers(self._markers)
            self.act_flip_h.setChecked(bool(review.get('flip_h', False)))
            self.act_flip_v.setChecked(bool(review.get('flip_v', False)))
            drawings = review.get('drawings')
            if isinstance(drawings, dict):
                display_drawings = {
                    mapping.source_to_display(int(frame)): strokes
                    for frame, strokes in drawings.items()}
                self.surface.set_drawing_data(display_drawings, replace=True)
            self._bind_review_document(source)
            frame = to_display('source_frame', 'frame', 0)
            self.player.seek(max(0, min(self.player.frame_count - 1, frame)))
            crop = review.get('crop')
            if crop:
                expected_id = source.id
                expected_info = self.info
                expected_generation = self._navigation_generation
                expected_crop_edit = self._crop_edit_generation
                def restore_crop():
                    if (not self._closed and not self._shutting_down and
                            self._navigation_generation == expected_generation and
                            self._crop_edit_generation == expected_crop_edit and
                            self.info is expected_info and
                            self.session_controller.current is not None and
                            self.session_controller.current.id == expected_id):
                        if self.tracking_enabled:
                            # A deferred scene restore must not overwrite the
                            # saved track's correct crop at the new playhead.
                            self._apply_tracked_crop()
                        else:
                            self.surface.set_crop(crop, notify=False)
                restore_crop()
                QtCore.QTimer.singleShot(0, restore_crop)
                QtCore.QTimer.singleShot(250, restore_crop)
            saved_track = review.get('tracking_v1')
            if saved_track:
                self._restore_tracking_record(source.id, saved_track)
            saved_stabilization = review.get('stabilization_preview')
            if (isinstance(saved_stabilization, dict) and
                    saved_stabilization.get('enabled')):
                self._set_stabilization_preview(True, persist=False)
            self._reset_undo()
        finally:
            self._restoring_review = False

    def _choose_session(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, 'Open review session', self._notes_directory(),
            'FrameReviewer Sessions (*.frsession);;All Files (*)')
        if path:
            self._open_session(path, remember_recent=True)

    def _choose_recovery(self):
        folder = os.path.dirname(self._recovery_path())
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, 'Recover autosaved review', folder,
            'FrameReviewer Recovery (*.frsession);;All Files (*)')
        if path:
            self._open_session(path, remember_recent=True)

    def _timeline_issues(self, result, title):
        if not result.issues:
            return
        lines = ['[{}] {}: {}'.format(issue['severity'], issue['code'],
                                       issue['message']) for issue in result.issues]
        self.status_lbl.setText('{}: {} interchange notices'.format(
            title, len(lines)))
        dialog = QtWidgets.QMessageBox(self)
        dialog.setWindowTitle(title)
        dialog.setIcon(QtWidgets.QMessageBox.Warning)
        dialog.setText('{} interchange notices; inspect unsupported edits before delivery.'.format(
            len(lines)))
        dialog.setDetailedText('\n'.join(lines))
        dialog.exec_()

    def _import_timeline(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, 'Import edit timeline', self._notes_directory(),
            'Edit Timelines (*.edl *.otio *.otioz);;All Files (*)')
        if not path:
            return
        from ..media import interchange
        try:
            if path.lower().endswith('.edl'):
                with open(path, 'r', encoding='utf-8-sig') as stream:
                    data = stream.read()
                fps = (self.info.fps_rational if self.info is not None else '24')
                result = interchange.import_edl(data, fps=fps)
            else:
                result = interchange.import_otio(path)
        except (OSError, IOError, TypeError, ValueError) as exc:
            QtWidgets.QMessageBox.warning(self, 'Timeline import failed', str(exc))
            return
        if not result.ok or result.session is None:
            self._timeline_issues(result, 'Timeline import failed')
            return
        # Preview warnings are not consent to publish a lossy approximation.
        # Keep the original import diagnostics across Save Session As/reopen so
        # the durable v4 exporter can prove that no omitted transition, edit or
        # track was silently represented as a hard cut.
        result.session.metadata.setdefault('interchange', {})[
            'import_issues'] = copy.deepcopy(result.issues)
        self._capture_current_review()
        self.player.stop_all(wait=True)
        self.info = None
        self.surface.clear()
        controller = self.session_controller
        controller.replace_session(result.session)
        if result.composition is not None:
            controller.set_view(interchange_composition=result.composition.to_dict(),
                                comparison_mode=result.composition.mode,
                                composition_active=True)
        self.playlist_panel.set_session(controller)
        source = controller.current
        path_to_load = controller.resolved_path(source) if source is not None else None
        if path_to_load and os.path.isfile(path_to_load):
            self._load_path(path_to_load)
            # An imported edit is a *record* timeline, not a collection of
            # unrelated sources. Apply the actual trims/gaps immediately;
            # simply opening its first reel would preview the wrong material.
            if result.composition is not None:
                self.playlist_panel._emit_composition()
        else:
            self._set_media_window_title(path)
            self.status_lbl.setText('Timeline imported with offline sources. Relink from Playlist.')
        self._show_playlist()
        self._timeline_issues(result, 'Timeline imported')
        self._schedule_scene_state_save()

    def _export_timeline(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, 'Export edit timeline', self._notes_directory(),
            'Edit Decision List (*.edl);;OpenTimelineIO (*.otio)')
        if not path:
            return
        from ..media import interchange
        session = self.session_controller.session
        composition = self._composition
        if composition is None and session.view.get('interchange_composition'):
            try:
                composition = composition_mod.Composition.from_dict(
                    session.view['interchange_composition'])
            except (ValueError, TypeError, KeyError):
                composition = None
        try:
            if path.lower().endswith('.otio'):
                result = interchange.export_otio(session, composition)
            else:
                if not path.lower().endswith('.edl'):
                    path += '.edl'
                result = interchange.export_edl(session, composition)
            if not result.ok:
                self._timeline_issues(result, 'Timeline export rejected')
                return
            lossy = interchange.lossy_export_issues(result)
            if lossy:
                details = '\n'.join('{}: {}'.format(
                    issue.get('code'), issue.get('message')) for issue in lossy)
                if QtWidgets.QMessageBox.question(
                        self, 'Confirm lossy timeline export',
                        'This EDL/OTIO cannot represent every edit exactly. '
                        'Publishing it may change source timing, overlaps or '
                        'transitions:\n\n{}\n\nExport this explicitly '
                        'approximate interchange anyway?'.format(details),
                        QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                        QtWidgets.QMessageBox.No) != QtWidgets.QMessageBox.Yes:
                    self.status_lbl.setText('Timeline export cancelled: '
                                            'unapproved lossy edits.')
                    return
            interchange.write_interchange_atomic(path, result)
        except (OSError, IOError, TypeError, ValueError, ImportError) as exc:
            QtWidgets.QMessageBox.warning(self, 'Timeline export failed', str(exc))
            return
        self._timeline_issues(result, 'Timeline export')
        if result.ok:
            self.status_lbl.setText('Exported timeline: {}'.format(path))

    def _open_session(self, path, remember_recent=False):
        try:
            controller = self.session_controller
            session = controller.open(path)
        except (OSError, IOError, ValueError, TypeError) as exc:
            QtWidgets.QMessageBox.warning(self, 'Open session failed', str(exc))
            return
        # The previous window media belongs to the previous session. Leave its
        # decoder teardown to _load without capturing it into the new session.
        self.info = None
        self.playlist_panel.set_session(controller)
        if remember_recent:
            self._remember_recent(path)
        self._set_media_window_title(path)
        mode = session.view.get('comparison_mode', 'sequence')
        selected = set(session.view.get('selected_source_ids') or [])
        self.playlist_panel.set_suspended(True)
        try:
            idx = self.playlist_panel.mode_combo.findData(mode)
            if idx >= 0:
                self.playlist_panel.mode_combo.setCurrentIndex(idx)
            self.playlist_panel.set_comparison_parameters(
                session.view.get('comparison_parameters') or {})
            for item in self.playlist_panel._items():
                item.setSelected(item.data(QtCore.Qt.UserRole) in selected)
        finally:
            self.playlist_panel.set_suspended(False)
        current = controller.current
        if current is not None:
            resolved = controller.resolved_path(current)
            if resolved and os.path.isfile(resolved):
                self._load_path(resolved)
            else:
                self.player.stop_all(wait=True)
                self.surface.clear()
                self.surface.set_placeholder_visible(True)
                self._set_media_window_title(path)
                self.status_lbl.setText('Session loaded; current media is missing. Relink in Playlist.')
        if session.view.get('composition_active', False):
            # The visible Playlist is Source-owned (one row per logical
            # Source), but an editorial record is OCCURRENCE-owned. A saved
            # CMX/OTIO can repeat one Source/path in multiple distinct cuts;
            # asking the playlist to emit its unique rows after Session open
            # silently collapses two record occurrences into a single source
            # player, defeating preview/export parity and signed trim IDs.
            saved = session.view.get('interchange_composition')
            restored_record = False
            if isinstance(saved, dict):
                try:
                    edit = composition_mod.Composition.from_dict(saved)
                    if (edit.mode == composition_mod.SEQUENCE and
                            edit.record_timeline and len(edit.members) >= 2):
                        occurrence_paths = []
                        for occurrence in edit.members:
                            logical = session.source(occurrence.source_id)
                            path_for_edit = (controller.resolved_path(logical)
                                             if logical is not None else None)
                            if not path_for_edit or not os.path.isfile(path_for_edit):
                                raise ValueError('Record occurrence {} cannot '
                                                 'resolve original Source {}.'
                                                 .format(len(occurrence_paths) + 1,
                                                         occurrence.source_id))
                            occurrence_paths.append(path_for_edit)
                        self._compose_sequence(occurrence_paths)
                        restored_record = True
                except (ValueError, TypeError, KeyError, OSError) as exc:
                    # Never substitute an accidental 1-Source preview for an
                    # unreconstructable imported edit. The Session remains
                    # open for a deliberate artist relink/recovery action.
                    self._composition = None
                    self.status_lbl.setText('Saved record preview unavailable: '
                                            + str(exc))
                    restored_record = True
            if not restored_record and len(self.playlist_panel.effective_paths()) > 1:
                self.playlist_panel._emit_composition()

    def _save_session(self):
        if not self.session_controller.path:
            return self._save_session_as()
        self._capture_current_review()
        self._remember_session_view()
        try:
            path = self.session_controller.save()
            self.status_lbl.setText('Session saved: {}'.format(path))
            return path
        except (OSError, IOError, TypeError, ValueError) as exc:
            QtWidgets.QMessageBox.warning(self, 'Save session failed', str(exc))
            return None

    def _save_session_as(self):
        default = (self.session_controller.path or
                   os.path.join(self._notes_directory(), 'Review.frsession'))
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, 'Save review session', default,
            'FrameReviewer Sessions (*.frsession)')
        if not path:
            return None
        self._capture_current_review()
        self._remember_session_view()
        try:
            saved = self.session_controller.save(path)
            self.status_lbl.setText('Session saved: {}'.format(saved))
            return saved
        except (OSError, IOError, TypeError, ValueError) as exc:
            QtWidgets.QMessageBox.warning(self, 'Save session failed', str(exc))
            return None

    def _remember_session_view(self):
        if self.playlist_panel is not None:
            selected = [self.session_controller.source_for_path(path)
                        for path in self.playlist_panel.selected_paths()]
            self.session_controller.set_view(
                comparison_mode=self.playlist_panel.composition_mode(),
                selected_source_ids=[source.id for source in selected if source],
                composition_active=self._composition is not None,
                comparison_parameters=self.playlist_panel.comparison_parameters())

    def _activate_source(self, source_id):
        source = self.session_controller.session.source(source_id)
        if source is None:
            return
        path = self.session_controller.resolved_path(source)
        if not path or not os.path.isfile(path):
            self._relink_source(source_id)
            return
        self._capture_current_review()
        # Do not let _load capture the old decoder into the newly selected ID.
        self.info = None
        self.session_controller.set_current(source_id)
        self._load_path(path)

    def _switch_source_role(self, source_id, role):
        """Switch versions through Session; keep notes, IDs, range, and crop."""
        current_id = (self.session_controller.current.id
                      if self.session_controller.current else None)
        if self.info is not None and current_id == source_id:
            self._capture_current_review()
        try:
            self.session_controller.choose_representation(source_id, role)
            source = self.session_controller.session.source(source_id)
            path = self.session_controller.resolved_path(source)
            if not path or not os.path.isfile(path):
                raise IOError('The selected representation is unavailable. Use Relink.')
        except (ValueError, KeyError, IOError, OSError) as exc:
            self.status_lbl.setText(str(exc))
            self.playlist_panel._sync_role_combo()
            return
        self.playlist_panel.set_session(self.session_controller)
        if current_id == source_id:
            # A version switch keeps this source ID; _load will restore the
            # source-frame mapped review state after probing the new decoder.
            self._load_path(path)
        elif self._composition is not None:
            self.playlist_panel._emit_composition()
        self._schedule_scene_state_save()

    def _relink_source(self, source_id):
        """Change a representation path without replacing note/source IDs."""
        source = self.session_controller.session.source(source_id)
        if source is None:
            return
        old_path = self.session_controller.resolved_path(source) or source.active_path()
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, 'Relink {}'.format(source.name), os.path.dirname(old_path or ''),
            'Media (*.mp4 *.mov *.mkv *.webm *.avi *.m4v '
            '*.jpg *.jpeg *.png *.tga *.exr);;All Files (*)')
        if not path:
            return
        try:
            resolution = self.source_resolver.resolve(path)
            if not resolution.exists or resolution.kind not in ('media', 'sequence'):
                raise ValueError('Select an existing media file or image sequence.')
            info = self._probe_source_media(
                resolution.local_path,
                sequence_pattern=resolution.sequence_pattern,
                ask_exr_selection=True)
        except (OSError, IOError, ValueError, RuntimeError) as exc:
            QtWidgets.QMessageBox.warning(self, 'Relink failed', str(exc))
            return
        old_current = self.session_controller.current
        if old_current is not None and self.info is not None:
            self._capture_current_review()
        self.session_controller.reconcile_info(
            info, source_id=source_id, role=source.active_role,
            select=(source_id == self.session_controller.session.current_source_id))
        self.playlist_panel.set_session(self.session_controller)
        if source_id == self.session_controller.session.current_source_id:
            self._load(info)
        # EDLs often import as offline reels. Once the last missing media is
        # relinked, resume the preserved *record* timeline, not a free-running
        # first clip. Do not attempt partial compositions (they fail probing).
        if (self.session_controller.session.view.get('composition_active') and
                self.session_controller.session.view.get('interchange_composition') and
                all(os.path.isfile(path) for path in
                    self.playlist_panel.effective_paths())):
            self.playlist_panel._emit_composition()
        self._schedule_scene_state_save()

    def _install_session_snapshot(self, payload):
        try:
            self.session_controller.restore_snapshot(payload)
        except (ValueError, TypeError):
            return False
        self.playlist_panel.set_session(self.session_controller)
        return True

    def _on_comparison_parameters_changed(self, parameters):
        self.session_controller.set_view(comparison_parameters=dict(parameters))
        if self._composition is not None:
            self._composition.parameters.update(parameters)
        active = getattr(self.player, '_source', None)
        if active is not None and hasattr(active, 'set_comparison'):
            active.set_comparison(**parameters)
            self.player.seek(self.player.current_frame)
        self._schedule_scene_state_save()

    def _align_comparison_view_geometry(self, source, mode):
        """Keep Qt physical geometry aligned after an in-place mode switch.

        GRID intentionally stretches each tile and uses square canvas pixels.
        The other modes use member one's original physical reference field,
        including reference-space selected A/B and Flicker. Reusing warm
        decoders must not leave GRID's PAR=1 behind when switching to A/B.
        This is presentation only; no Source review, Session or notes mutate.
        """
        geometries = getattr(source, '_member_geometries', ())
        if not geometries or geometries[0] is None:
            return
        try:
            from fractions import Fraction
            ratios = [Fraction(str(item[2])) for item in geometries
                      if item is not None]
            if not ratios:
                return
            mixed = len(set(ratios)) > 1
            # GRID has a square-pixel cell canvas even when *every* member
            # independently declares the SAME non-square PAR; the tiles are
            # stretched intentionally, not placed into a reference field.
            target = 1.0 if mode == composition_mod.GRID else float(ratios[0])
            previous = self.surface.image_geometry()
            self.surface.set_image_geometry(target, previous.rotation)
            if mode == composition_mod.GRID:
                self.status_lbl.setToolTip(
                    'Grid stretches members into square-pixel cells. '
                    'Managed output still requires verified input geometry, '
                    'native color and a supported output format.')
            elif mixed and mode in (composition_mod.A_B, composition_mod.FLICKER):
                self.status_lbl.setToolTip(
                    'A/B and Flicker use the first Source physical field. '
                    'Unequal-PAR managed export requires an actually presented '
                    'native-precision reference-field frame; Qt-only fallback '
                    'cannot prove matching output.')
            elif mixed:
                self.status_lbl.setToolTip(
                    'Live CPU comparison rectifies source-specific PAR to '
                    'the first Source physical image field; outside-member pixels '
                    'are transparent. Managed export has its own strict gate.')
        except (TypeError, ValueError, OverflowError, ZeroDivisionError):
            # Invalid source geometry must remain rejected by export preflight;
            # never replace its display interpretation with a guessed PAR.
            return

    def _load(self, info):
        self._navigation_generation += 1
        if not self._scene_restore_loading:
            scene_state.invalidate_pending_restore(self)
        info = self._exr_display_canvas_info(info)
        old_source = self.session_controller.current
        if old_source is not None and self.info is not None:
            self._capture_current_review()
        # Session restore / role switch must interpret the sequence using the
        # previously selected *logical Source* policy before frame mapping,
        # decoder allocation and review-note projection are constructed.
        if not self._applying_composition and getattr(info, 'is_sequence', False):
            associated = (self.session_controller.source_for_path(info.path)
                          or self.session_controller.current)
            if (associated is not None and info.declared_frames is None and
                    os.path.normpath(associated.active_path() or '') ==
                    os.path.normpath(info.path) and
                    associated.timing.get('declared_frames') is not None):
                # Restoring a named range such as 999-1009x2#.exr must recover
                # absent leading/trailing frames, not infer a shorter range
                # from only the images still on disk.
                info = info.with_authored_timeline(
                    associated.timing['declared_frames'])
            policy = ((associated.review or {}).get('missing_frame_policy', 'skip')
                      if associated is not None else 'skip')
            if policy not in self.missing_actions:
                policy = 'skip'
            info = info.with_missing_policy(policy)
        # A synthetic composite MediaInfo describes a view, not a media
        # representation. Only normal source loads update source metadata.
        source = (self.session_controller.current if self._applying_composition
                  else self.session_controller.reconcile_info(
                      info, source_id=getattr(info, 'source_id', None)))
        if source is not None and not self._applying_composition:
            # Precision FramePayload and legacy review annotations now refer to
            # the same logical source, independent of the filename/role/path.
            info.source_id = source.id
        # Pending timeline callbacks refer to the previous source's frames.
        # Loading another clip must discard them before any decoder callbacks.
        self._maya_read_timer.stop()
        self._maya_write_timer.stop()
        self._queued_player_frame = None
        self._queued_maya_frame = None
        if self.sync_btn.isChecked():
            self.sync_btn.setChecked(False)
        self._cancel_tracking_job()
        self._suspend_undo = True   # loading clears drawings; not an undo step
        self._review_adapter = None
        self.review_records_panel.bind(None)
        self.info = info
        # Audio delivery always requires a new, source-specific opt-in;
        # changing media must never reuse a prior selected soundtrack.
        self.export_preserve_audio_cb.setChecked(False)
        self._sync_export_audio_controls()
        self._sync_missing_frame_menu()
        self.surface.set_placeholder_visible(False)
        self._clear_tracking(update=False)
        self.surface.set_stabilization_offset(None)
        self._stabilization_plan = None
        self.stabilize_view_cb.blockSignals(True)
        self.stabilize_view_cb.setChecked(False)
        self.stabilize_view_cb.blockSignals(False)
        self.waveform_job.cancel()
        self._invalidate_record_waveform()
        self.thumb_job.cancel()
        self._task_end('thumbs')
        self._task_end('waveform')
        self._waveform_ready = self._thumbs_ready = False
        self.timeline.clear_waveform()
        self.timeline.clear_thumbnails()
        self.timeline.clear_cache_ranges()
        self.timeline.clear_cached_frames()
        self._markers.clear()
        self.timeline.set_markers(())

        for act in (self.act_flip_h, self.act_flip_v):
            act.blockSignals(True)
            act.setChecked(False)
            act.blockSignals(False)
        self.review_flip_h.setChecked(False)
        self.review_flip_v.setChecked(False)
        self.surface.set_flip(False, False)
        self._current_flip_h = False
        self._current_flip_v = False

        active_audio = ((source.audio or {}).get('route') if source is not None and
                        not self._applying_composition else None)
        self._apply_source_color_pipeline(source)
        self.player.load(info, audio_route=active_audio or AudioRoute())
        # The primary viewport, annotations, crop guides and pixel inspector
        # share one PAR+orientation matrix. Decoder pixels stay native; do not
        # bake a display-only quarter-turn into source IDs or review geometry.
        self._apply_view_geometry(info, source)
        self.player.set_loop_mode(self._current_loop_mode())
        # J/K/L shuttle speed and direction persist across media switches.
        self.player.set_speed(float(self.player.speed or 1.0))

        self._set_media_window_title(info.path)
        self.media_lbl.setToolTip(info.path)
        audio_description = getattr(info, 'audio_description', None)
        if audio_description is None:
            # Defensive compatibility for a MediaInfo instance created by a
            # stale pre-reload callback. New loads use the current class.
            audio_description = 'Audio metadata unavailable'
        self.media_lbl.setText(
            '{} × {}\n{:g} fps  ·  {} frames\nAudio: {}'.format(
                info.width, info.height, info.fps, info.frame_count,
                audio_description))
        if getattr(info, 'is_sequence', False):
            missing = info.missing_frame_count
            if missing:
                label = '{} missing source frames; policy: {}'.format(
                    missing, info.missing_policy)
                self.media_lbl.setToolTip(info.path + '\n' + label)
                self.status_lbl.setToolTip(label)
        self.out_w_le.setText(str(info.width))
        self.out_h_le.setText(str(info.height))

        self.timeline.set_frame_count(info.frame_count, info.fps)
        self.timeline.set_range(0, info.frame_count - 1)
        self.timeline.set_frame(0)
        for spin, value in ((self.in_spin, 0), (self.out_spin, info.frame_count - 1)):
            spin.blockSignals(True)
            spin.setRange(0, info.frame_count - 1)
            spin.setValue(value)
            spin.blockSignals(False)
        self.export_range_lbl.setText(
            'Frames 0 to {}  ·  {} frames'.format(
                info.frame_count - 1, info.frame_count))

        self._set_enabled(True)
        if not HAVE_MULTIMEDIA and not info.is_sequence:
            self.status_lbl.setText('QtMultimedia unavailable. Using stable frame stepping.')
        else:
            self.status_lbl.setText('Ready. Choose a workflow from the left toolbar.')
        if self.act_thumbs.isChecked():
            self._request_thumbnails()
        if self.act_wave.isChecked():
            self._request_waveform()

        # Start each clip with a clean history reflecting its loaded state.
        self._suspend_undo = False
        self._reset_undo()

        # Put keyboard focus on the viewer so transport/tool shortcuts fire
        # immediately after a clip loads, without a stray click first.
        self.surface.setFocus()

        # Record the new source into the scene's fileInfo right away.
        self._schedule_scene_state_save()

        # Keep the current file in the playlist as its default item (normal
        # loads only; during composition the window is driving the list).
        if self.playlist_panel is not None and getattr(info, 'path', None):
            if not self._applying_composition:
                self.playlist_panel.add_paths(
                    [info.path], active=info.path, emit=False)
            else:
                self.playlist_panel.set_active_path(info.path)
        if not self._applying_composition and source is not None:
            self._restore_current_review(source)
            if self._review_adapter is None:
                self._bind_review_document(source)
        # A direct single-Source load is NEVER still the old comparison/record
        # timeline, even if that Source happened to be a member of it. Retaining
        # the old Composition here would let an Export UI action unknowingly
        # deliver OLD multi-source pixels after an artist switched back to a
        # single native plate. Explicit composition construction owns its own
        # short-lived _applying_composition flag, and only that path may retain
        # comparison members or their signed record-timeline segments.
        comp = self._composition
        if (not self._applying_composition or
                comp is None or not any(
                m.source_id == info.path or
                (self.session_controller.source_for_path(info.path) is not None and
                 m.source_id == self.session_controller.source_for_path(info.path).id)
                for m in comp.members)):
            self._comparison_presented_receipt = None
            self._clear_composition()
        elif comp is not None:
            if comp.mode == composition_mod.SEQUENCE:
                self.timeline.set_segments(
                    [seg.to_tuple() for seg in comp.segments()])
        self._sync_missing_frame_menu()
        self._update_reference_actions()

    def _apply_view_geometry(self, info, source=None):
        """Present decoded native pixels at correct pixel aspect/orientation.

        Orientation is a source/session view choice, never silently applied to
        managed output, color data or the native annotation coordinate system.
        Unsupported/stale media metadata defaults visibly to square pixels.
        """
        par = getattr(info, 'pixel_aspect_ratio', 1.0)
        if self._composition is not None:
            rotation = self.session_controller.session.view.get(
                'comparison_view_rotation', 0)
        else:
            rotation = ((source.review or {}).get('view_rotation', 0)
                        if source is not None else 0)
        try:
            self.surface.set_image_geometry(par, rotation)
        except (ValueError, TypeError, OverflowError) as exc:
            self.surface.set_image_geometry(1.0, 0)
            self.status_lbl.setToolTip('Unsupported source image geometry: {}'.format(exc))
        metadata = getattr(info, 'metadata', {}) or {}
        window = metadata.get('data_window')
        display = metadata.get('display_window')
        expected = (0, 0, info.width, info.height)
        if metadata.get('exr_display_canvas_rectified'):
            self.surface.frame_item.setToolTip(
                'Native EXR dataWindow {} rectified onto displayWindow {}. '
                'Image annotations and crop use display-local coordinates; '
                'signed EXR pixel origins remain available in pixel inspection. '
                'Current managed exports reject noncanonical windows.'.format(
                    tuple(window), tuple(display)))
        elif (window is not None and tuple(window) != expected or
                display is not None and tuple(display) != expected):
            self.surface.frame_item.setToolTip(
                'EXR native data/display windows are preserved as metadata; '
                'this Qt raster view currently uses data-window-local '
                'coordinates. Offset/display-canvas placement and managed '
                'export require explicit geometric rectification.')

    @staticmethod
    def _unsafe_exr_delivery(info):
        """Return the unsupported EXR feature instead of silently flattening."""
        from ..media import exr
        if info is None or not exr.is_exr(info.path):
            return ''
        meta = info.metadata or {}
        actual = (0, 0, info.width, info.height)
        for key in ('data_window', 'display_window'):
            value = meta.get(key)
            if value is not None and tuple(value) != actual:
                return '{} differs from zero-origin output raster'.format(key)
        if meta.get('exr_part_count', 1) != 1:
            return 'native multipart EXR selection is not preserved by FFmpeg output'
        if meta.get('exr_selection_explicit') or meta.get('exr_layer') not in (None, ''):
            return 'named/explicit EXR color layer is not verified in FFmpeg output'
        return ''

    def _set_view_rotation(self, rotation):
        """Rotate image/overlays as one affine viewport without rewriting notes.

        This is a genuine source/session view edit; unchanged rotation never
        changes session state or schedules autosave. Source/Working/Display
        export rejects unsupported rotated presentation instead of dropping it.
        """
        if self.info is None:
            return
        rotation = int(rotation) % 360
        if rotation not in (0, 90, 180, 270):
            return
        current = self.surface.image_geometry()
        if rotation == current.rotation:
            return
        if self._stabilization_plan is not None:
            self._set_stabilization_preview(False)
        self.surface.set_image_geometry(current.pixel_aspect, rotation)
        if self._composition is not None:
            before = dict(self.session_controller.session.view)
            self.session_controller.set_view(comparison_view_rotation=rotation)
            changed = self.session_controller.session.view != before
        else:
            source = self.session_controller.current
            changed = bool(source is not None and
                           self.session_controller.update_review(
                               {'view_rotation': rotation}, source_id=source.id))
        if changed:
            self._schedule_scene_state_save()
        self.status_lbl.setText(
            'View rotated {}° clockwise (display-only; rotated export '
            'must be explicitly supported).'.format(rotation))

    def _rotate_view(self, delta):
        if self.info is not None:
            self._set_view_rotation(self.surface.image_geometry().rotation + delta)

    def _sync_missing_frame_menu(self):
        """Reflect the actual source timeline without editing settings/state."""
        info = self.info
        available = (info is not None and info.is_sequence and
                     self._composition is None)
        mode = (getattr(info, 'missing_policy', 'skip') if available else 'skip')
        for name, act in self.missing_actions.items():
            act.blockSignals(True)
            act.setChecked(name == mode)
            act.setEnabled(available)
            act.blockSignals(False)

    def _effective_color_state(self, source=None):
        """Declare EXR input as linear unless the Source explicitly overrides."""
        if source is None:
            source = self.session_controller.current
        data = copy.deepcopy(source.color or {}) if source is not None else {}
        path = (source.active_path() if source is not None else
                self.info.path if self.info is not None else '')
        if not data.get('input_space'):
            data['input_space'] = ('linear' if str(path).lower().endswith('.exr')
                                   else 'sRGB')
        return ColorPipelineState.from_dict(data)

    def _apply_source_color_pipeline(self, source=None):
        state = self._effective_color_state(source)
        if hasattr(self.surface, 'set_color_pipeline_state'):
            self.surface.set_color_pipeline_state(state)
        return state

    def _show_color_pipeline(self):
        """Edit source-owned interpretation; do not conflate it with 8-bit grade."""
        source = self.session_controller.current
        if source is None or self.info is None or self._composition is not None:
            return
        state = self._effective_color_state(source)
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle('Source Color Pipeline')
        dialog.resize(420, 280)
        layout = QtWidgets.QVBoxLayout(dialog)
        info = QtWidgets.QLabel(
            'Precision EXR uses source → working → display before the monitor '
            'preview. OCIO is optional; unsupported configurations are reported, '
            'never silently treated as managed. Specify source alpha association '
            'before exporting RGBA media.')
        info.setWordWrap(True)
        layout.addWidget(info)
        form = QtWidgets.QFormLayout()
        controls = {}
        for key, title, values in (
                ('input_space', 'Input', ('linear', 'sRGB', 'raw')),
                ('working_space', 'Working', ('linear', 'sRGB')),
                ('display_space', 'Display space', ('sRGB', 'linear')),
                ('alpha_mode', 'Alpha association',
                 ('straight', 'premultiplied', 'opaque'))):
            combo = QtWidgets.QComboBox()
            for value in values:
                combo.addItem(value, value)
            current = getattr(state, key)
            if combo.findData(current) < 0:
                combo.addItem(current, current)
            combo.setCurrentIndex(combo.findData(current))
            controls[key] = combo
            form.addRow(title, combo)
        for key, title in (('config', 'OCIO config path'),
                           ('display', 'OCIO display'), ('view', 'OCIO view'),
                           ('look', 'OCIO look')):
            field = QtWidgets.QLineEdit(str(getattr(state, key) or ''))
            if key == 'config':
                field.setPlaceholderText('Optional; leave blank for built-in sRGB/linear')
            controls[key] = field
            form.addRow(title, field)
        layout.addLayout(form)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        result = dialog.exec_() if hasattr(dialog, 'exec_') else dialog.exec()
        if result != QtWidgets.QDialog.Accepted:
            return
        values = state.to_dict()
        for key, field in controls.items():
            values[key] = (field.currentData() if isinstance(field, QtWidgets.QComboBox)
                           else field.text().strip())
        try:
            updated = ColorPipelineState.from_dict(values)
            if (updated.config and not os.path.isfile(os.path.expanduser(
                    os.path.expandvars(updated.config)))):
                raise ValueError('OCIO config file not found.')
            if (updated.display or updated.view or updated.look) and not updated.config:
                raise ValueError('An OCIO config is required for a display/view/look.')
        except (ValueError, TypeError) as exc:
            QtWidgets.QMessageBox.warning(self, 'Invalid color pipeline', str(exc))
            return
        if updated.to_dict() == state.to_dict():
            return  # no-op: do not mark the source dirty or autosave
        source.color = updated.to_dict()
        self.session_controller.dirty = True
        self._apply_source_color_pipeline(source)
        self._schedule_scene_state_save()

    def _set_missing_frame_policy(self, policy):
        """Rebuild one source's frame map, retaining notes and source identity.

        No independent clock/timeline: Player and FrameMapping both receive
        the same dense display indices whose source numbers may include holes.
        """
        source = self.session_controller.current
        info = self.info
        if (source is None or info is None or not info.is_sequence or
                self._composition is not None or policy not in self.missing_actions):
            self._sync_missing_frame_menu()
            return
        if policy == getattr(info, 'missing_policy', 'skip'):
            return
        try:
            changed_info = info.with_missing_policy(policy)
        except ValueError as exc:
            self.status_lbl.setText('Missing-frame setting unavailable: {}'.format(exc))
            self._sync_missing_frame_menu()
            return
        self._capture_current_review()
        self.session_controller.update_review({'missing_frame_policy': policy},
                                              source_id=source.id)
        self._load(changed_info)
        self.status_lbl.setText('Sequence missing frames: {} ({} holes).'.format(
            policy, changed_info.missing_frame_count))

    def _schedule_scene_state_save(self):
        """Debounced Maya snapshot and portable session recovery checkpoint."""
        if (self._closed or getattr(self, '_restoring_review', False) or
                getattr(self, '_restoring_scene_state', False)):
            return
        self._scene_state_timer.start()

    def _write_scene_state(self):
        if (self._closed or getattr(self, '_restoring_review', False) or
                getattr(self, '_restoring_scene_state', False)):
            return
        self._capture_current_review()
        if self.session_controller.has_unsaved_changes():
            try:
                self.session_controller.save_recovery(self._recovery_path())
            except (OSError, IOError, TypeError, ValueError) as exc:
                self.status_lbl.setText('Session recovery failed: {}'.format(exc))
        # The kBeforeSave scene callback writes Maya fileInfo when the scene is
        # actually saved. No reason to mark the Maya scene dirty every 2 min.

    def _restore_scene_state_on_launch(self):
        """On open, reload whatever media this scene stored."""
        if cmds is None:
            return
        state = scene_state.load_scene_state()
        if state is None:
            return
        generation = self._scene_restore_generation
        QtCore.QTimer.singleShot(
            0, lambda: scene_state.apply_state(self, state)
            if not self._closed and self._scene_restore_generation == generation
            else None)

    def _set_enabled(self, on):
        widgets = [self.timeline, self.in_spin, self.out_spin, self.first_btn, self.prev_btn,
                   self.play_btn, self.next_btn, self.last_btn, self.loop_btn,
                   self.sync_btn, self.sync_offset_spin,
                   self.set_in_btn, self.set_out_btn,
                   self.reset_range_btn, self.speed_combo,
                   self.fps_combo, self.out_w_le, self.out_h_le,
                   self.mute_btn, self.volume_slider, self.draw_color_btn,
                   self.draw_width, self.draw_width_slider,
                   self.draw_opacity, self.draw_opacity_slider,
                   self.draw_smoothing, self.draw_smoothing_slider,
                   self.pressure_size_check, self.pressure_opacity_check,
                   self.brush_mode_btn, self.eraser_mode_btn,
                   self.rect_mode_btn, self.ellipse_mode_btn,
                   self.arrow_mode_btn, self.text_mode_btn,
                   self.delete_selected_btn,
                   self.clear_drawing_btn, self.undo_drawing_btn,
                   self.blank_drawing_btn, self.delete_all_notes_btn,
                   self.prev_drawing_btn, self.next_drawing_btn,
                   self.import_notes_btn, self.export_notes_btn,
                   self.composition_combo, self.color_btn, self.color_pipeline_btn,
                   self.export_color_stage, self.fit_btn,
                   self.zoom_out_btn, self.zoom_in_btn,
                   self.clear_crop_btn, self.crop_overlay_check,
                   self.review_thumbs_check, self.review_wave_check,
                   self.review_flip_h, self.review_flip_v,
                   self.tracking_mode,
                   self.media_prev_marker_btn, self.media_next_marker_btn,
                   self.export_btn]
        widgets += list(self.draw_swatches)
        if self.track_btn is not None:
            widgets += [self.track_btn, self.clear_track_btn]
        for widget in widgets:
            widget.setEnabled(on)
        if not tracking.HAVE_CV2:
            self.track_btn.setEnabled(False)
            self.clear_track_btn.setEnabled(False)
            self.tracking_mode.setEnabled(False)
        for act in (self.act_export, self.act_thumbs, self.act_wave, self.act_crop,
                    self.act_loop, self.act_flip_h, self.act_flip_v,
                    self.act_toggle_marker, self.act_previous_marker,
                    self.act_next_marker, self.act_clear_markers,
                    self.act_set_reference):
            act.setEnabled(on)

    # ----------------------------------------------------------- playback
    def player_set_loop(self, enabled):
        if enabled and self.act_ping_pong.isChecked():
            self.act_ping_pong.setChecked(False)
        self.player.set_loop_mode('loop' if enabled else 'once')
        self.settings.set('playback/loop_mode', 'loop' if enabled else 'once')

    def _current_loop_mode(self):
        if self.act_ping_pong.isChecked():
            return 'ping_pong'
        return 'loop' if self.act_loop.isChecked() else 'once'

    def _set_ping_pong(self, enabled):
        if enabled:
            self.act_loop.setChecked(False)
        mode = 'ping_pong' if enabled else self._current_loop_mode()
        self.player.set_loop_mode(mode)
        self.settings.set('playback/loop_mode', mode)

    def _play_reverse(self):
        previous = float(self.player.speed)
        self._set_speed(max(-8.0, previous * 2.0) if previous < 0 and
                        self.player.playing else -1.0)
        if not self.player.playing:
            self.player.play()

    def _play_forward(self):
        previous = float(self.player.speed)
        self._set_speed(min(8.0, previous * 2.0) if previous > 0 and
                        self.player.playing else 1.0)
        if not self.player.playing:
            self.player.play()

    def _support_report(self):
        host = {'qt': QtCore.qVersion()}
        if cmds is not None:
            try:
                host['maya'] = str(cmds.about(version=True))
            except (RuntimeError, ValueError):
                host['maya'] = 'unavailable'
        report = report_json(session=self.session_controller.session,
                             player=self.player, host=host)
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle('FrameReviewer Capabilities / Support')
        dialog.resize(680, 560)
        layout = QtWidgets.QVBoxLayout(dialog)
        details = QtWidgets.QPlainTextEdit()
        details.setReadOnly(True)
        details.setPlainText(report)
        layout.addWidget(details)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close)
        copy_button = buttons.addButton('Copy Redacted Report',
                                        QtWidgets.QDialogButtonBox.ActionRole)
        copy_button.clicked.connect(
            lambda: QtWidgets.QApplication.clipboard().setText(report))
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec_()

    def _set_pixel_magnifier_visible(self, enabled):
        """User-opt-in read-only inspection, never an annotation/Session edit."""
        self.surface.set_pixel_magnifier(bool(enabled), scale=12, radius=4)
        if not enabled:
            self.pixel_magnifier_panel.hide()

    def _on_pixel_magnifier_changed(self, reading):
        """Present latest display pixels, with original precision only if proven.

        This floating viewport child deliberately ignores mouse input; even
        over a crop/brush/pin the original pixel hit-test stays unchanged.
        """
        if (not reading or not self.act_pixel_magnifier.isChecked() or
                not isinstance(reading, dict)):
            self.pixel_magnifier_panel.hide()
            return
        raster = reading.get('image')
        if not isinstance(raster, QtGui.QImage) or raster.isNull():
            self.pixel_magnifier_panel.hide()
            return
        sample = reading.get('sample') or {}
        if not isinstance(sample, dict):
            self.pixel_magnifier_panel.hide()
            return
        generation = reading.get('generation')
        last = getattr(self, '_pixel_magnifier_generation', -1)
        if (type(generation) is int and generation < last):
            return  # late callback may not replace newer presented pixels
        if type(generation) is int:
            self._pixel_magnifier_generation = generation
        self.pixel_magnifier_image.setPixmap(QtGui.QPixmap.fromImage(raster))
        center = reading.get('center') or (None, None)
        if len(center) != 2:
            center = (None, None)
        pixel_text = 'Display ({}, {}) · 8-bit preview'.format(*center)
        if sample.get('source_rgba') is not None:
            values = sample['source_rgba']
            native_text = 'Native {} {} · signed frame {} · RGBA {}'.format(
                sample.get('source_dtype', '?'),
                sample.get('source_stage', '?'),
                sample.get('source_frame', '?'),
                ', '.join('{:.5g}'.format(float(v)) for v in values))
            raw = sample.get('native_storage_rgba')
            if raw is not None:
                native_text += ('\nOriginal unnormalized channel storage RGBA: {}'
                                .format(tuple(raw)))
        elif sample.get('exr_padding'):
            native_text = 'EXR canvas padding · no original sample'
        else:
            native_text = 'No independently measured native HDR sample'
        self.pixel_magnifier_details.setText(
            'Pixel {} : {}'.format(center[0], center[1]))
        self.pixel_magnifier_panel.setToolTip(
            pixel_text + '\n' + native_text + '\n'
            'Nearest-neighbor Display pixel grid only; not GPU HDR monitor proof.')
        self.pixel_magnifier_panel.adjustSize()
        viewport = self.surface.viewport()
        self.pixel_magnifier_panel.move(
            max(4, viewport.width() - self.pixel_magnifier_panel.width() - 12), 12)
        self.pixel_magnifier_panel.show()
        self.pixel_magnifier_panel.raise_()

    def _on_pixel_hovered(self, data):
        if self.surface.pixel_probes():
            return  # Active A/B probes follow the presented frame, not the cursor.
        if not data:
            self.pixel_lbl.clear()
            return
        if 'source_rgba' in data:
            rgba = data['source_rgba']
            display_status = (self.surface.display_color_status()
                              if hasattr(self.surface, 'display_color_status')
                              else '8-bit preview')
            self.pixel_lbl.setText(
                '({},{})  {:.3f}  {:.3f}  {:.3f}  {:.3f}'.format(
                    data['source_x'], data['source_y'], *rgba))
            self.pixel_lbl.setToolTip(
                '{} {} / {} | real source frame {} | original RGBA {}\n'
                'Displayed SDR RGBA: {}, {}, {}, {} ({}; the monitor is not '
                'an HDR hardware display).'.format(
                    data['source_stage'], data['source_dtype'],
                    data['source_alpha_mode'], data['source_frame'], rgba,
                    data['r'], data['g'], data['b'], data['a'], display_status))
            if data.get('exr_absolute_x') is not None:
                self.pixel_lbl.setToolTip(
                    self.pixel_lbl.toolTip() +
                    '\nEXR absolute pixel ({}, {}), displayWindow origin ({}, {})'.format(
                        data['exr_absolute_x'], data['exr_absolute_y'],
                        *data['exr_canvas_origin']))
        else:
            self.pixel_lbl.setText(
                '({},{})  R:{:03} G:{:03} B:{:03} A:{:03}'.format(
                    data['display_x'], data['display_y'],
                    data['r'], data['g'], data['b'], data['a']))
            self.pixel_lbl.setToolTip(
                '{} | source ({}, {}) | frame {}'.format(
                    data['stage'], data['source_x'], data['source_y'], data['frame']))
            if data.get('exr_padding'):
                self.pixel_lbl.setToolTip(
                    self.pixel_lbl.toolTip() +
                    '\nEXR display canvas padding, no original native pixel '
                    'at absolute ({}, {}).'.format(
                        data['exr_absolute_x'], data['exr_absolute_y']))
        members = self._original_comparison_pixel_details(data)
        if members:
            self.pixel_lbl.setToolTip(self.pixel_lbl.toolTip() + '\n\n' + members)

    def _on_comparison_frame_presented(self, frame):
        """Capture the sole authoritative presented-picture lineage receipt.

        This happens AFTER Player has delivered the current frame and any
        immutable precision payload to PlaybackSurface, never on navigation.
        The QPixmap cacheKey distinguishes a different frame at the same
        timeline index (reload, source/representation switch or mode redraw).
        """
        source = getattr(self.player, '_source', None)
        pixmap = self.surface.frame_item.pixmap()
        if source is None or pixmap.isNull():
            self._comparison_presented_receipt = None
        else:
            self._comparison_presented_receipt = (
                source, int(frame), int(pixmap.cacheKey()))

    def _original_comparison_pixel_details(self, data):
        """Cached native A/B/Grid samples at a presented image coordinate.

        Display pixels may be a shared working composite. These originals
        have distinct input-space transforms and must never be confused with
        a physically calibrated difference or HDR monitor readback. This is
        read-only UI inspection: no seeks, media jobs or recovery autosaves.
        """
        if not data or self._composition is None:
            return ''
        source = getattr(self.player, '_source', None)
        inspect = getattr(source, 'inspect_member_pixels', None)
        pixmap = self.surface.frame_item.pixmap()
        if not callable(inspect) or pixmap.isNull():
            return ''
        # For the live CompositeSource, a transport seek can advance the
        # playhead before async media pixels arrive. Never associate a newly
        # requested signed source frame with old displayed pixels. Tests and
        # external inspection stubs without a real CompositeSource retain the
        # public read-only inspection contract.
        from ..playback.composite_source import CompositeSource
        if isinstance(source, CompositeSource):
            receipt = self._comparison_presented_receipt
            if (receipt is None or receipt[0] is not source or
                    receipt[1] != int(self.player.current_frame) or
                    receipt[2] != int(pixmap.cacheKey())):
                return 'Original comparison source: pending matching presented frame; no stale sample.'
        # source_x/y can be native full-res for HDR samples. The actual
        # display canvas is the stable geometry to normalize against.
        x = data.get('display_pixel_x', data.get('source_x', -1))
        y = data.get('display_pixel_y', data.get('source_y', -1))
        if x is None or y is None:
            return ''
        try:
            coordinates = ((float(x) + .5) / pixmap.width(),
                           (float(y) + .5) / pixmap.height())
            if isinstance(source, CompositeSource):
                items = inspect(self.player.current_frame, coordinates,
                                mode='visible')
            else:
                # Existing read-only adapters provide the original two-arg
                # inspection contract and have no composed pixel-owner mask.
                # Do not make their working native samples vanish merely
                # because the new CompositeSource API adds an opt-in mode.
                items = inspect(self.player.current_frame, coordinates)
        except (RuntimeError, ValueError, TypeError, IndexError, KeyError):
            return ''
        if not items:
            return 'Comparison original: no visible member under this pixel.'
        lines = ['Visible comparison pixel contributors (original ungraded values):']
        for item in items:
            label = str(item.get('source_id', item.get('member_index', '?')))
            rgba = item.get('source_rgba')
            movie_precision = item.get('movie_precision')
            source_color = item.get('source_color') or {}
            if not isinstance(source_color, dict):
                source_color = (source_color.to_dict()
                                if hasattr(source_color, 'to_dict') else {})
            if rgba is None:
                reason = item.get('decoder_status') or item.get('status')
                if item.get('exr_absolute') is not None:
                    reason = '{} at signed EXR pixel {}'.format(
                        reason, item['exr_absolute'])
                lines.append('{} · {} (no invented native HDR sample)'.format(
                    label, reason))
            else:
                lines.append('{} · signed frame {} · ({}, {}) · {} {} · {}'
                             ' · RGBA {:.4g}, {:.4g}, {:.4g}, {:.4g}'.format(
                    label, item['source_frame'], item['x'], item['y'],
                    item['source_dtype'], item['source_stage'],
                    source_color.get('input_space', 'unspecified input'), *rgba))
                if item.get('native_storage_rgba') is not None:
                    lines.append('  Original channel storage {} (source dtype, '
                                 'not converted Display RGB).'.format(
                                     tuple(item['native_storage_rgba'])))
                if movie_precision:
                    lines.append('  {} · decoded movie SDR samples; not '
                                 'scene-linear HDR or hardware monitor proof.'
                                 .format(movie_precision))
        lines.append('Original values may use different input color spaces; '
                     'comparison output is shared working light.')
        return '\n'.join(lines)

    def _on_pixel_probes_changed(self, samples):
        """Re-sample pinned locations only from the actual presented pixels.

        Precision deltas are meaningful in matching source space or in the
        composited working space. Differently interpreted source plates are
        compared in the displayed SDR domain rather than as raw float RGB.
        """
        samples = {key: value for key, value in (samples or {}).items() if value}
        if not samples:
            self.pixel_lbl.clear()
            return
        parts = []
        details = []
        for slot in ('A', 'B'):
            data = samples.get(slot)
            if data is None:
                continue
            rgba = data.get('source_rgba')
            if rgba is not None:
                parts.append('{}({},{}) {:.3f}/{:.3f}/{:.3f}'.format(
                    slot, data['source_x'], data['source_y'], *rgba[:3]))
                details.append('{}: {} {} frame {} RGBA {}\n'.format(
                    slot, data['source_stage'], data['source_dtype'],
                    data['source_frame'], rgba))
            else:
                parts.append('{}({},{}) {:03}/{:03}/{:03}'.format(
                    slot, data['source_x'], data['source_y'],
                    data['r'], data['g'], data['b']))
                details.append('{}: Display 8-bit RGBA {}, {}, {}, {}\n'.format(
                    slot, data['r'], data['g'], data['b'], data['a']))
            if data.get('exr_absolute_x') is not None:
                details.append(
                    '{}: signed EXR pixel ({}, {}), {}\n'.format(
                        slot, data['exr_absolute_x'], data['exr_absolute_y'],
                        ('display-only padding, no native source sample'
                         if data.get('exr_padding') else
                         'original native source sample')))
        if len(samples) == 2 and all(key in samples for key in ('A', 'B')):
            a, b = samples['A'], samples['B']
            comparable = (a.get('source_rgba') is not None and
                          b.get('source_rgba') is not None and
                          a.get('source_stage') == b.get('source_stage') and
                          (a.get('source_id') == b.get('source_id') or
                           a.get('source_stage') == 'working'))
            if comparable:
                delta = [float(a['source_rgba'][i]) - float(b['source_rgba'][i])
                         for i in range(3)]
                stage = a['source_stage']
                parts.append('Δ{} {:.3f}/{:.3f}/{:.3f}'.format(stage, *delta))
            else:
                delta = [a[c] - b[c] for c in ('r', 'g', 'b')]
                parts.append('Δdisplay {:d}/{:d}/{:d}'.format(*delta))
                stage = 'display SDR 8-bit (source spaces differ or lack precision)'
            details.append('A − B: {} RGB delta {}; values are not a hardware HDR readback.'.format(
                stage, delta))
        self.pixel_lbl.setText('  |  '.join(parts))
        for slot in ('A', 'B'):
            if slot in samples:
                extra = self._original_comparison_pixel_details(samples[slot])
                if extra:
                    details.append('\n{} position:\n{}'.format(slot, extra))
        self.pixel_lbl.setToolTip(''.join(details) + '\n' +
                                  self.surface.display_color_status())

    def _show_playback_diagnostics(self):
        snapshot = self.player.diagnostics_snapshot()
        if self.info is not None:
            snapshot['source_timeline'] = {
                'path': self.info.path,
                'source_frame': self.info.frame_mapping().display_to_source(
                    self.player.current_frame),
                'missing_frame_policy': getattr(self.info, 'missing_policy', 'skip'),
                'missing_frame_count': getattr(self.info, 'missing_frame_count', 0),
            }
            sequence = getattr(self.player, '_source', None)
            if sequence is not None and hasattr(sequence, 'frame_status'):
                snapshot['source_timeline']['current_status'] = sequence.frame_status(
                    self.player.current_frame)
        if hasattr(self.surface, 'display_color_status'):
            snapshot['viewer_color_status'] = self.surface.display_color_status()
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle('Playback Diagnostics')
        dialog.resize(560, 420)
        layout = QtWidgets.QVBoxLayout(dialog)
        text = QtWidgets.QPlainTextEdit()
        text.setReadOnly(True)
        text.setPlainText(json.dumps(snapshot, indent=2, sort_keys=True))
        layout.addWidget(text, 1)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close)
        copy_btn = buttons.addButton('Copy', QtWidgets.QDialogButtonBox.ActionRole)
        copy_btn.clicked.connect(
            lambda: QtWidgets.QApplication.clipboard().setText(text.toPlainText()))
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec_() if hasattr(dialog, 'exec_') else dialog.exec()

    def _maya_frame(self):
        if cmds is None:
            return None
        try:
            return float(cmds.currentTime(query=True))
        except Exception:
            return None

    def _toggle_maya_sync(self, enabled):
        if not enabled:
            self._maya_sync_timer.stop()
            self._maya_write_timer.stop()
            self._maya_read_timer.stop()
            self._queued_maya_frame = None
            self._queued_player_frame = None
            self._sync_offset = None
            self._last_maya_frame = None
            return
        maya_frame = self._maya_frame()
        if maya_frame is None or self.info is None:
            self.sync_btn.blockSignals(True)
            self.sync_btn.setChecked(False)
            self.sync_btn.blockSignals(False)
            self.status_lbl.setText('Maya timeline sync is unavailable.')
            return
        # This value never changes until Sync is toggled off and on again.
        self._sync_offset = float(self.player.current_frame) - maya_frame
        self._last_maya_frame = maya_frame
        self._maya_sync_timer.start()

    def _on_sync_offset_changed(self, *args):
        self.settings.set('sync/offset', int(self.sync_offset_spin.value()))
        if not self.sync_btn.isChecked() or self.info is None:
            return
        # Re-align immediately: force the next poll to accept Maya's frame, then
        # nudge both directions so the new offset takes effect without waiting.
        self._last_maya_frame = None
        if self.sync_player_to_maya.isChecked():
            self._sync_from_maya()
        elif self.sync_maya_to_player.isChecked():
            self._sync_maya_from_player(self.player.current_frame)

    def _sync_from_maya(self):
        if (self._closed or self._shutting_down or
                not self.sync_btn.isChecked() or self._sync_updating or
                not self.sync_player_to_maya.isChecked() or self.info is None):
            return
        # When FrameReviewer is playing, it is the timeline master. Accepting
        # our own delayed currentTime writes as new Maya seeks creates a
        # feedback loop and restarts FFmpeg workers repeatedly.
        if self.player.playing and self.sync_maya_to_player.isChecked():
            return
        if self._maya_write_timer.isActive() or self._queued_maya_frame is not None:
            return
        maya_frame = self._maya_frame()
        if maya_frame is None or maya_frame == self._last_maya_frame:
            return
        self._last_maya_frame = maya_frame
        target = int(round(maya_frame + self._sync_offset - self.sync_offset_spin.value()))
        target = int(utils.clamp(target, 0, self.player.frame_count - 1))
        self._queued_player_frame = target
        # Multiple slider events collapse into one seek every 55ms. Starting
        # the single-shot again on each 20ms Maya poll would postpone playback
        # indefinitely until scrubbing stopped.
        if not self._maya_read_timer.isActive():
            self._maya_read_timer.start(55)

    def _flush_maya_scrub(self):
        target = self._queued_player_frame
        self._queued_player_frame = None
        if (target is None or self._closed or self._shutting_down or
                self.info is None or
                not self.sync_btn.isChecked() or
                not self.sync_player_to_maya.isChecked() or
                (self.player.playing and self.sync_maya_to_player.isChecked()) or
                target == self.player.current_frame):
            return
        self._sync_updating = True
        try:
            if self.player.playing:
                self.player.pause()
            self.player.seek(target)
        finally:
            self._sync_updating = False

    def _sync_maya_from_player(self, frame):
        if (self._closed or self._shutting_down or
                not self.sync_btn.isChecked() or self._sync_updating or
                not self.sync_maya_to_player.isChecked() or
                self._sync_offset is None or cmds is None):
            return
        maya_frame = float(frame) - self._sync_offset + self.sync_offset_spin.value()
        self._queued_maya_frame = maya_frame
        if not self._maya_write_timer.isActive():
            # At most 10 Maya evaluation updates/s while video is playing.
            # Manual frame stepping still propagates on the next Qt turn.
            self._maya_write_timer.start(100 if self.player.playing else 0)

    def _flush_maya_write(self):
        maya_frame = self._queued_maya_frame
        self._queued_maya_frame = None
        if (maya_frame is None or self._closed or self._shutting_down or
                cmds is None or
                not self.sync_btn.isChecked() or
                not self.sync_maya_to_player.isChecked()):
            return
        if abs(float(maya_frame) - float(self._last_maya_frame or 0)) < .5:
            return
        self._sync_updating = True
        try:
            cmds.currentTime(maya_frame, edit=True)
            self._last_maya_frame = maya_frame
        except Exception:
            pass
        finally:
            self._sync_updating = False

    def _set_marked_range(self, in_frame, out_frame):
        """Commit paired mark edits in one transaction (no signal races)."""
        if self.info is None:
            return
        last = self.player.frame_count - 1
        start = int(utils.clamp(in_frame, 0, last))
        end = int(utils.clamp(out_frame, start, last))
        self.in_spin.blockSignals(True)
        self.out_spin.blockSignals(True)
        try:
            self.in_spin.setValue(start)
            self.out_spin.setValue(end)
        finally:
            self.in_spin.blockSignals(False)
            self.out_spin.blockSignals(False)
        self._range_changed()
        self._commit_range_undo()
        self._schedule_scene_state_save()

    def _mark_in(self):
        if self.info is None:
            return
        frame = self.player.current_frame
        self._set_marked_range(frame, max(frame, self.out_spin.value()))

    def _mark_out(self):
        if self.info is None:
            return
        frame = self.player.current_frame
        self._set_marked_range(min(self.in_spin.value(), frame), frame)

    def _reset_marked_range(self):
        if self.info is not None:
            self._set_marked_range(0, self.player.frame_count - 1)

    def _toggle_frame_marker(self):
        if self.info is None:
            return
        frame = self.player.current_frame
        if frame in self._markers:
            self._markers.remove(frame)
        else:
            self._markers.add(frame)
        self.timeline.set_markers(self._markers)

    def _previous_frame_marker(self):
        previous = [frame for frame in self._markers
                    if frame < self.player.current_frame]
        if previous:
            self._scrub(max(previous))

    def _next_frame_marker(self):
        following = [frame for frame in self._markers
                     if frame > self.player.current_frame]
        if following:
            self._scrub(min(following))

    def _clear_frame_markers(self):
        self._markers.clear()
        self.timeline.set_markers(())

    def _set_speed(self, value):
        self.player.set_speed(value)
        if self.speed_combo.currentData() != value:
            index = self.speed_combo.findData(value)
            if index >= 0:
                self.speed_combo.blockSignals(True)
                self.speed_combo.setCurrentIndex(index)
                self.speed_combo.blockSignals(False)
        act = self.speed_actions.get(value)
        if act is not None and not act.isChecked():
            act.setChecked(True)

    def _scrub(self, frame):
        self._navigation_generation += 1
        if self._restoring_scene_state:
            scene_state.invalidate_pending_restore(self)
        if self.player.playing:
            self.player.pause()
        self.player.seek(frame)

    def _on_frame_changed(self, frame):
        if (self._comparison_presented_receipt is not None and
                self._comparison_presented_receipt[1] != int(frame)):
            self._comparison_presented_receipt = None
        previous = self._history_frame
        self._history_frame = int(frame)
        if (previous != frame and not self._suspend_undo and
                self.info is not None):
            if self._navigation_step is not None:
                # Consecutive seeks, timeline drags and playback are one
                # navigation step; redo must land on the latest frame.
                self._navigation_step['end'] = int(frame)
            elif self._recent_drawing_edit:
                navigation = {'start': previous, 'end': int(frame)}
                self._undo.push(
                    lambda n=navigation: self._apply_frame_state(n['start']),
                    lambda n=navigation: self._apply_frame_state(n['end']))
                self._navigation_step = navigation
                self._recent_drawing_edit = False
        self.frame_lbl.setText('{} / {}'.format(frame, self.player.frame_count - 1))
        if self.info is not None:
            frame_source = self.info.frame_mapping().display_to_source(frame)
            sequence = getattr(self.player, '_source', None)
            status = (sequence.frame_status(frame)
                      if sequence is not None and hasattr(sequence, 'frame_status')
                      else {})
            if status.get('kind') == 'hole':
                detail = 'Intentional sequence hole — {} policy'.format(
                    status.get('policy', 'unknown'))
            elif status.get('kind') == 'decode_error':
                detail = 'Decoder failure — {}'.format(status.get('reason', 'unknown'))
            else:
                detail = 'Available source frame'
            record_bin = self._record_waveform_bins.get(int(frame))
            identity = None
            if (sequence is not None and
                    self._composition is not None and
                    self._composition.mode == composition_mod.SEQUENCE and
                    self._composition.record_timeline and
                    hasattr(sequence, 'record_frame_identity')):
                try:
                    identity = sequence.record_frame_identity(int(frame))
                except (ValueError, IndexError, KeyError, TypeError):
                    identity = None
            if identity is not None:
                kind = identity.get('kind')
                if kind == 'record_gap':
                    detail = ('Intentional record gap · no source decode; '
                              'viewer black is opaque, managed export gap '
                              'is transparent')
                elif identity.get('source_id') is None:
                    detail = '{} · no source sample'.format(kind)
                else:
                    detail = ('edit #{}, Source {}, original frame {}, decoder '
                              'index {}, status {}'.format(
                                  identity['occurrence_index'] + 1,
                                  identity['source_id'],
                                  identity['source_frame'],
                                  identity.get('decode_index'), kind))
                    if identity.get('source_kind'):
                        detail += ' ({})'.format(identity['source_kind'])
                self.frame_lbl.setToolTip('Record {} · {}'.format(frame, detail))
            elif (record_bin is not None and
                    self._record_waveform_edit_key == self._record_audio_edit_key()):
                if record_bin['kind'] == 'record_gap':
                    detail = 'Record gap · independently verified PCM silence'
                else:
                    detail = ('Occurrence {} · Source {} · original frame {} '
                              '· selected {} · samples {}–{}'.format(
                                  record_bin['occurrence_id'],
                                  record_bin['source_id'],
                                  record_bin['source_frame'],
                                  record_bin['source_label'],
                                  record_bin['start_sample'],
                                  record_bin['end_sample'] - 1))
                self.frame_lbl.setToolTip('Record {} · {}. Physical sound '
                                          'device audition is not verified.'.format(
                                              frame, detail))
            else:
                self.frame_lbl.setToolTip('Source frame {} · {}'.format(
                    frame_source, detail))
            if (record_bin is not None and identity is not None and
                    self._record_waveform_edit_key == self._record_audio_edit_key()):
                self.frame_lbl.setToolTip(
                    self.frame_lbl.toolTip() +
                    ' · Selected PCM: {} · sample frames {}–{} · verified '
                    'waveform only; physical playback unverified'.format(
                        record_bin['source_label'], record_bin['start_sample'],
                        record_bin['end_sample'] - 1))
        self.timeline.set_frame(frame)
        self.surface.set_current_frame(frame)
        self._sync_maya_from_player(frame)
        if self.tracking_enabled:
            self._apply_tracked_crop()
            self._refresh_tracking_quality()
        self._apply_stabilization_frame(frame)
        # A moving playhead is navigation, not an edit. Scene save/shutdown
        # still captures its final position without generating autosaves.

    def _on_playing_changed(self, playing):
        if playing:
            self._navigation_generation += 1
            if self._restoring_scene_state:
                scene_state.invalidate_pending_restore(self)
            # Ignore a pending Maya scrub queued just before the Play click.
            # Otherwise its delayed handler pauses/reseeks the newly started
            # decoder and was reproducibly unstable in scrub→Play sequences.
            self._maya_read_timer.stop()
            self._queued_player_frame = None
        self.play_btn.setIcon(self._transport_icon('SP_MediaPause' if playing else 'SP_MediaPlay'))

    def _range_changed(self, *args):
        self.player.set_range(self.in_spin.value(), self.out_spin.value())
        self.timeline.set_range(self.player.in_frame, self.player.out_frame)
        if self.info is not None:
            count = self.player.out_frame - self.player.in_frame + 1
            self.export_range_lbl.setText(
                'Frames {} to {}  ·  {} frames'.format(
                    self.player.in_frame, self.player.out_frame, count))

    # ------------------------------------------------------------- undo/redo
    def _on_surface_edited(self, frame):
        """A drawing/erase change was committed; record it as one undo step."""
        if self._suspend_undo:
            return
        self._commit_review_projection(edited_frame=frame)
        before = self._drawing_snapshot
        after = self.surface.snapshot_drawings()
        if before == after:
            return
        self._navigation_step = None
        self._undo.push(
            lambda b=before, f=frame: self._apply_drawing_state(b, f),
            lambda a=after, f=frame: self._apply_drawing_state(a, f),
            frame)
        self._drawing_snapshot = after
        self._recent_drawing_edit = True

    def _apply_frame_state(self, frame):
        self._scrub(frame)

    def _apply_drawing_state(self, snapshot, frame):
        # Jump to the frame the edit happened on, then restore its strokes.
        self._suspend_undo = True
        try:
            if frame is not None and frame >= 0:
                self._scrub(frame)
            self.surface.restore_drawings(snapshot)
            self._commit_review_projection()
        finally:
            self._suspend_undo = False
        self._drawing_snapshot = self.surface.snapshot_drawings()

    def _commit_range_undo(self, *args):
        if self._suspend_undo or self.info is None:
            return
        new = (self.in_spin.value(), self.out_spin.value())
        if new == self._range_snapshot:
            return
        old = self._range_snapshot
        self._navigation_step = None
        self._recent_drawing_edit = False
        self._undo.push(lambda o=old: self._apply_range(o),
                        lambda n=new: self._apply_range(n), None)
        self._range_snapshot = new

    def _apply_range(self, pair):
        self._suspend_undo = True
        try:
            self._set_marked_range(pair[0], pair[1])
        finally:
            self._suspend_undo = False
        self._range_snapshot = pair

    def _reset_undo(self):
        self._undo.clear()
        self._drawing_snapshot = self.surface.snapshot_drawings()
        self._range_snapshot = (self.in_spin.value(), self.out_spin.value())
        self._history_frame = self.player.current_frame
        self._recent_drawing_edit = False
        self._navigation_step = None

    def _undo_action(self):
        self._perform_history_action(self._undo.undo)

    def _redo_action(self):
        self._perform_history_action(self._undo.redo)

    def _perform_history_action(self, action):
        self._navigation_step = None
        self._recent_drawing_edit = False
        self._suspend_undo = True
        try:
            action()
        finally:
            self._suspend_undo = False

    def _show_timeline_menu(self, position):
        """Expose the shared View actions beside the timeline."""
        menu = QtWidgets.QMenu(self.timeline)
        menu.addAction(self.act_thumbs)
        menu.addAction(self.act_wave)
        menu.addSeparator()
        menu.addAction('Fit Session', self.timeline.fit_session)
        menu.addAction('Fit Marked Range', self.timeline.fit_marked_range)
        menu.addAction('Zoom In', lambda: self.timeline.zoom_by(1.6))
        menu.addAction('Zoom Out', lambda: self.timeline.zoom_by(1.0 / 1.6))
        global_position = self.timeline.mapToGlobal(position)
        if hasattr(menu, 'exec'):
            menu.exec(global_position)
        else:
            menu.exec_(global_position)

    # ------------------------------------------------------- view toggles
    def _show_tool(self, name):
        page = self._tool_pages.get(name)
        if page is None:
            return
        self.tool_stack.setCurrentWidget(page)
        titles = {
            'media': ('Media', 'Source information & navigation', 'V'),
            'playlist': ('Playlist', 'Sources & comparison', 'P'),
            'crop': ('Crop', 'Frame & output region', 'C'),
            'draw': ('Draw', 'Annotations & markup', 'D'),
            'track': ('Track', 'Motion tracking', 'T'),
            'review': ('Review', 'Viewer & overlays', 'R'),
            'export': ('Export', 'Deliverables & output', 'X'),
            'shotgrid': ('Flow / ShotGrid', 'Production versions', 'G'),
        }
        title, subtitle, shortcut = titles.get(name, (name.title(), '', ''))
        self.tool_title.setText(title)
        self.tool_subtitle.setText(subtitle)
        self.tool_header_icon.setPixmap(self._icon(name).pixmap(21, 21))
        self.tool_shortcut_hint.setText(shortcut)
        self.tool_shortcut_hint.setVisible(bool(shortcut))
        button = self._tool_buttons.get(name)
        if button is not None:
            button.setChecked(True)
        self.surface.set_tool('draw' if name == 'draw' else
                              'crop' if name in ('crop', 'track') else 'select')
        self.act_side.setChecked(True)

    def _toggle_side(self, visible):
        self.side.setVisible(visible)

    def _toggle_toolbar(self, visible):
        self.toolbar.setVisible(visible)

    def _toggle_timeline(self, visible):
        self.timeline.setVisible(visible)

    def _toggle_transport(self, visible):
        self.transport_bar.setVisible(visible)

    def _toggle_statusbar(self, visible):
        self.status_bar.setVisible(visible)

    def _set_drawing_color(self, color):
        color = QtGui.QColor(color)
        if not color.isValid():
            return
        self.surface.set_drawing_style(color=color)
        self.draw_color_btn.setStyleSheet(
            'QToolButton { background:%s; border:1px solid %s; '
            'border-radius:3px; }' % (color.name(), style.SWATCH_BORDER))
        if self.draw_color_picker.currentColor() != color:
            self.draw_color_picker.blockSignals(True)
            self.draw_color_picker.setCurrentColor(color)
            self.draw_color_picker.blockSignals(False)
        self.settings.set('draw/color', color.name())

    def _update_brush_style(self, *args):
        self.surface.set_drawing_style(
            width=self.draw_width.value(),
            eraser_size=self._draw_tool_sizes['eraser'],
            opacity=self.draw_opacity.value() / 100.0,
            smoothing=self.draw_smoothing.value(),
            pressure_size=self.pressure_size_check.isChecked(),
            pressure_opacity=self.pressure_opacity_check.isChecked())

    def _on_draw_size_changed(self, value):
        mode = self._current_draw_mode
        self._draw_tool_sizes[mode] = int(value)
        key = 'draw/width' if mode == 'brush' else 'draw/width/' + mode
        self.settings.set(key, int(value))
        self._update_brush_style()

    def _set_draw_mode(self, mode):
        spec = TOOL_BY_ID.get(mode, TOOL_BY_ID['brush'])
        mode = spec.id
        self._current_draw_mode = mode
        self.surface.set_draw_mode(mode)
        self.draw_tip_title.setText(spec.settings_title)
        self.draw_width_caption.setText(spec.size_label)
        maximum = spec.max_size
        self.draw_width.blockSignals(True)
        self.draw_width_slider.blockSignals(True)
        self.draw_width.setRange(1, maximum)
        self.draw_width_slider.setRange(1, maximum)
        self.draw_width.setValue(self._draw_tool_sizes.get(mode, 3))
        self.draw_width_slider.setValue(self._draw_tool_sizes.get(mode, 3))
        self.draw_width_slider.blockSignals(False)
        self.draw_width.blockSignals(False)
        is_select = mode == 'select'
        self.draw_settings_stack.setCurrentIndex(0 if is_select else 1)
        self.draw_color_panel.setEnabled(mode != 'eraser')
        self.draw_opacity_panel.setEnabled(mode != 'eraser')
        self.draw_smoothing_panel.setEnabled(mode == 'brush')
        self.pressure_opacity_check.setEnabled(mode != 'eraser')
        self.pressure_card.setEnabled(not is_select)
        self._update_brush_style()

    def _activate_draw_mode(self, mode):
        """Open annotation tools and select a drawing mode from a shortcut."""
        buttons = {
            'brush': self.brush_mode_btn,
            'eraser': self.eraser_mode_btn,
        }
        self._show_tool('draw')
        button = buttons[mode]
        if button.isChecked():
            self._set_draw_mode(mode)
        else:
            button.setChecked(True)

    def _on_tablet_pressure(self, pressure):
        value = int(round(utils.clamp(float(pressure), 0.0, 1.0) * 100))
        self.pressure_meter.setValue(value)
        self.tablet_status_lbl.setText(
            'Tablet pressure active' if value > 0 else
            'Mouse input · tablet pressure ready')

    def _on_drawing_changed(self, count):
        if not count:
            self.drawing_lbl.setText('No active annotation')

    def _on_active_annotation_changed(self, key_frame, count):
        if key_frame < 0:
            self.drawing_lbl.setText('No active annotation')
            return
        if not count:
            self.drawing_lbl.setText('Blank key on frame {}'.format(key_frame))
            return
        held = key_frame != self.player.current_frame
        self.drawing_lbl.setText(
            '{} stroke{}, {} frame {}'.format(
                count, '' if count == 1 else 's',
                'held from' if held else 'key on', key_frame))

    def _delete_all_notes(self):
        """Confirm and remove every annotation key as one undoable edit."""
        if not self.surface.drawing_frames():
            return
        result = QtWidgets.QMessageBox.question(
            self, 'Delete All Notes',
            'Delete every annotation and blank annotation frame?\n\n'
            'You can undo this action.',
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No)
        if result == QtWidgets.QMessageBox.Yes:
            self.surface.clear_drawings()

    def _step_drawing(self, direction):
        frames = self.surface.drawing_frames()
        current = self.player.current_frame
        candidates = ([frame for frame in frames if frame < current]
                      if direction < 0 else
                      [frame for frame in frames if frame > current])
        if candidates:
            self._scrub(max(candidates) if direction < 0 else min(candidates))

    def _toggle_onion(self, enabled):
        self.onion_enable.setChecked(bool(enabled))

    def _update_onion_skin(self, *_):
        value = self.onion_opacity.value()
        self.onion_opacity_label.setText('{}%'.format(value))
        self.act_onion.blockSignals(True)
        self.act_onion.setChecked(self.onion_enable.isChecked())
        self.act_onion.blockSignals(False)
        self.surface.set_onion_skin(
            enabled=self.onion_enable.isChecked(),
            before=self.onion_before.value(),
            after=self.onion_after.value(),
            opacity=value / 100.0,
            falloff=self.onion_falloff.isChecked(),
            tint=self.onion_tint.isChecked())
        for key, setting in (
                ('enabled', self.onion_enable.isChecked()),
                ('before', self.onion_before.value()),
                ('after', self.onion_after.value()),
                ('opacity', value),
                ('falloff', self.onion_falloff.isChecked()),
                ('tint', self.onion_tint.isChecked())):
            self.settings.set('onion/' + key, setting)

    def _notes_directory(self):
        if self.info is not None and self.info.path:
            return os.path.dirname(self.info.path)
        return os.path.expanduser('~')

    def _install_notes_payload(self, payload, replace=True):
        """Import schema v1/v2 + rich records into the current logical source.

        A note file may originate in a different session with another Source.id.
        Rebinding never changes a note's real source frame or original shape
        identity; source identity becomes the destination logical source.
        A merge stages all records before updating the viewer/session, so one
        malformed or locked record cannot leave a partially imported review.
        """
        source = self.session_controller.current
        if source is None or self.info is None or self._composition is not None:
            raise ValueError('Open a single review source before importing notes.')
        mapping = self.info.frame_mapping()
        incoming = ReviewDocument.from_payload(payload)
        data = incoming.to_payload()
        data.pop('review_records', None)
        data['source']['id'] = source.id
        data['source']['start_frame'] = int(mapping.first_source)
        data['source']['path'] = self.info.path
        for note in data.get('notes', []):
            note['source_id'] = source.id
            note['frame'] = int(mapping.source_to_display(int(note['source_frame'])))
        incoming = ReviewDocument.from_payload(data)
        if replace:
            staged = incoming
        else:
            original = (self._review_adapter.document if self._review_adapter is not None
                        else self._review_document_for_source(source, mapping))
            staged = ReviewDocument.from_payload(original.to_payload())
            for layer in incoming.layers:
                if staged.layer(layer.id) is None:
                    staged.add_layer(layer)
            for note in incoming.notes:
                data = note.to_dict()
                if staged.get(note.id) is not None:
                    data['derived_from'] = note.id
                    data['id'] = str(uuid.uuid4())
                    data.pop('remote_ref', None)
                    for stroke in data['strokes']:
                        stroke['derived_from'] = stroke['id']
                        stroke['id'] = str(uuid.uuid4())
                staged.add(data)
        previous = self._review_adapter
        selected = self.review_records_panel.selected_id()
        bridge = ReviewSurfaceAdapter(staged, mapping)
        try:
            bridge.push(self.surface)
        except Exception:
            if previous is not None:
                previous.push(self.surface)
            raise
        self._review_adapter = bridge
        self._persist_review_document()
        self._refresh_review_records(selected)
        self._reset_undo()
        return len(incoming.notes)

    def _export_notes(self):
        self._commit_review_projection()
        drawings = self.surface.drawing_data()
        bridge = self._review_adapter
        if not drawings and (bridge is None or not bridge.document.notes):
            QtWidgets.QMessageBox.information(
                self, 'Export notes', 'There are no annotations to export.')
            return
        source_name = (os.path.splitext(os.path.basename(self.info.path))[0]
                       if self.info is not None else 'frame-review')
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, 'Export notes',
            os.path.join(self._notes_directory(), source_name + '-notes.json'),
            'FrameReviewer Notes (*.json)')
        if not path:
            return
        if not path.lower().endswith('.json'):
            path += '.json'
        payload = (bridge.document.to_surface_payload() if bridge is not None else
                   note_serialization.build_payload(drawings, ({
                'path': self.info.path,
                'width': self.info.width,
                'height': self.info.height,
                'frame_count': self.info.frame_count,
                'fps': self.info.fps,
            } if self.info is not None else {})))
        try:
            note_serialization.write_payload(path, payload)
        except (IOError, OSError, TypeError, ValueError) as exc:
            QtWidgets.QMessageBox.critical(self, 'Export notes failed', str(exc))
            return
        self.status_lbl.setText(
            'Exported {} annotation key{}.'.format(
                len(drawings), '' if len(drawings) == 1 else 's'))

    def _open_notes_path(self, path, remember_recent=False):
        """Restore the media and annotations recorded in a notes JSON."""
        if self._closed or (self._export_job is not None and self._export_job.isRunning()):
            return
        try:
            payload = note_serialization.read_payload(path)
            source, drawings = note_serialization.parse_payload(payload)
            if (not isinstance(source, dict) or
                    not isinstance(source.get('path'), str) or
                    not source['path'].strip()):
                raise ValueError('The notes file has no source media path.')
            original = os.path.expanduser(os.path.expandvars(source['path']))
            folder = os.path.dirname(os.path.abspath(path))
            paths = ([os.path.join(folder, original)] if not os.path.isabs(original) else [])
            paths += [self.source_resolver.mapper.map_existing(original), original,
                      os.path.join(folder, os.path.basename(original))]
            media = next((candidate for candidate in paths if os.path.isfile(candidate)), None)
            if media is None:
                raise IOError('Source media missing: ' + source['path'])
            info = ffmpegtools.probe(self.ffprobe, QtGui.QImageReader, media)
        except (IOError, OSError, TypeError, ValueError, KeyError, OverflowError) as exc:
            QtWidgets.QMessageBox.critical(self, 'Open notes failed', str(exc))
            return
        self._load(info)
        try:
            self._install_notes_payload(payload, replace=True)
        except (TypeError, ValueError, PermissionError, ReviewProjectionError) as exc:
            QtWidgets.QMessageBox.critical(self, 'Open notes failed', str(exc))
            return
        if remember_recent:
            self._remember_recent(path)
        if self.session_controller.current is not None:
            self.session_controller.current.notes_ref = path
            self._capture_current_review()
        self.status_lbl.setText('Opened {} with {} note keys.'.format(
            os.path.basename(media), len(drawings)))

    def _import_notes(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, 'Import notes', self._notes_directory(),
            'FrameReviewer Notes (*.json);;JSON files (*.json)')
        if not path:
            return
        try:
            payload = note_serialization.read_payload(path)
            _source, drawings = note_serialization.parse_payload(payload)
        except (IOError, OSError, TypeError, ValueError, KeyError) as exc:
            QtWidgets.QMessageBox.critical(self, 'Import notes failed', str(exc))
            return
        replace = True
        if self.surface.drawing_frames():
            answer = QtWidgets.QMessageBox.question(
                self, 'Import notes',
                'Replace existing notes?\n\nChoose No to merge imported strokes '
                'with the current notes.',
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No |
                QtWidgets.QMessageBox.Cancel,
                QtWidgets.QMessageBox.No)
            if answer == QtWidgets.QMessageBox.Cancel:
                return
            replace = answer == QtWidgets.QMessageBox.Yes
        try:
            self._install_notes_payload(payload, replace=replace)
        except (TypeError, ValueError, PermissionError, ReviewProjectionError) as exc:
            QtWidgets.QMessageBox.critical(self, 'Import notes failed', str(exc))
            return
        self.status_lbl.setText(
            'Imported {} annotation key{}.'.format(
                len(drawings), '' if len(drawings) == 1 else 's'))

    def _set_muted(self, muted):
        self.player.set_muted(muted)
        self.mute_btn.setIcon(self._icon('muted' if muted else 'volume'))
        self.mute_btn.setToolTip(
            '{} · Click for volume · Double-click to {}'.format(
                'Muted' if muted else 'Audio on',
                'unmute' if muted else 'mute'))
        self.settings.set('playback/muted', bool(muted))

    def _apply_audio_route(self, route):
        """Persist the selected audio source on the active logical Source."""
        source = self.session_controller.current
        if source is None or self.info is None:
            return
        route = (route if isinstance(route, AudioRoute)
                 else AudioRoute.from_dict(route))
        previous = ((source.audio or {}).get('route')
                    if isinstance(source.audio, dict) else None)
        if previous == route.to_dict():
            return False  # No recovery for identical audio choices.
        if previous is None:
            # No explicit route yet: selecting the same effective default
            # player route merely discovers codec/channel metadata. Discovery
            # alone is not a user-authored review edit or autosave event.
            current = self.player.audio_route
            intent = ('external_path', 'stream_index', 'delay_ms',
                      'output_layout')
            if all(getattr(current, key) == getattr(route, key)
                   for key in intent):
                return False
        if not isinstance(source.audio, dict):
            source.audio = {}
        source.audio['route'] = route.to_dict()
        self.session_controller.dirty = True
        self.player.set_audio_route(route)
        # The waveform belongs to the *effective selected soundtrack*, not to
        # the movie's first arbitrary audio track. Route edits invalidate its
        # cached view; changing the playhead does not.
        self.waveform_job.cancel()
        self._invalidate_record_waveform()
        self._waveform_ready = False
        self.timeline.clear_waveform()
        if self.act_wave.isChecked():
            self._request_waveform()
        self._schedule_scene_state_save()
        return True

    def _select_embedded_audio_stream(self):
        """Choose a stable audio ordinal, preserving FFprobe's container ID.

        Qt5 cannot reliably audition non-default tracks; the selection is
        still available for opt-in, independently verified Display MP4 export.
        This is source-owned review intent, never a parallel audio decoder.
        """
        import subprocess
        from ..playback.audio_routing import probe_audio_streams
        if (self.info is None or self.info.is_sequence or
                self._composition is not None or
                self.session_controller.current is None):
            self.status_lbl.setText('Select an ordinary movie Source to choose audio.')
            return
        if self.player.audio_route.external_path:
            self.status_lbl.setText('Remove external audio before selecting '
                                    'an embedded movie track.')
            return

        def bounded_probe(args):
            options = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=15, env=ffmpegtools.media_environment())
            options.update(ffmpegtools._no_window_kwargs())
            result = subprocess.run(args, **options)
            if result.returncode or len(result.stdout) > 256 * 1024:
                raise ValueError('Audio stream metadata is unavailable or oversized.')
            return result.stdout

        try:
            tracks = probe_audio_streams(self.ffprobe, self.info.path,
                                         runner=bounded_probe)
        except (ValueError, OSError, RuntimeError, TypeError,
                subprocess.SubprocessError) as exc:
            QtWidgets.QMessageBox.warning(self, 'Audio tracks unavailable', str(exc))
            return
        if not tracks:
            self.status_lbl.setText('This movie has no verified embedded audio tracks.')
            return
        labels = []
        for track in tracks:
            language = track.get('language') or 'unknown language'
            description = track.get('title') or track.get('channel_layout') or \
                          '{} channels'.format(track.get('channels') or '?')
            labels.append('Track {} · #{} · {} · {} · {}'.format(
                track['audio_index'] + 1, track['stream_index'],
                language, description, track.get('codec') or 'unknown codec'))
        old = self.player.audio_route
        initial = min(old.stream_index, len(labels) - 1)
        selected, accepted = QtWidgets.QInputDialog.getItem(
            self, 'Select Embedded Audio Track',
            'Track choice applies to this Source. Maya Qt5 may audition only '
            'the default track; managed export will verify the selected one:',
            labels, initial, False)
        if not accepted:
            return
        track = tracks[labels.index(selected)]
        params = old.to_dict()
        params.update(stream_index=track['audio_index'],
                      sample_rate=track['sample_rate'],
                      channels=track['channels'],
                      channel_layout=track['channel_layout'])
        if self._apply_audio_route(AudioRoute.from_dict(params)):
            self.status_lbl.setText(
                'Selected embedded audio track {} (container stream #{}). '
                'Non-default Qt5 playback is backend-dependent; managed MP4 '
                'export independently verifies the requested stream.'.format(
                    track['audio_index'] + 1, track['stream_index']))

    def _attach_external_audio(self):
        if self.session_controller.current is None:
            QtWidgets.QMessageBox.information(
                self, 'Attach audio', 'Open media before attaching an audio file.')
            return
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, 'Attach external audio', self._notes_directory(),
            'Audio (*.wav *.aiff *.aif *.flac *.mp3 *.ogg *.m4a);;All Files (*)')
        if not path:
            return
        resolved = self.source_resolver.resolve(path)
        if resolved.kind != 'audio' or not resolved.exists:
            QtWidgets.QMessageBox.warning(self, 'Attach audio',
                                          'Please choose an existing audio file.')
            return
        old = self.player.audio_route
        self._apply_audio_route(AudioRoute(
            external_path=resolved.local_path, delay_ms=old.delay_ms))
        self.status_lbl.setText('External audio: {}'.format(
            os.path.basename(resolved.local_path)))

    def _remove_external_audio(self):
        if self.session_controller.current is None:
            return
        previous = self.player.audio_route
        self._apply_audio_route(AudioRoute(delay_ms=previous.delay_ms))
        self.status_lbl.setText('Using embedded media audio.')

    def _set_audio_delay(self):
        if self.session_controller.current is None:
            return
        before = self.player.audio_route
        value, accepted = QtWidgets.QInputDialog.getInt(
            self, 'Audio synchronization',
            'Audio delay (ms; positive = sound later):',
            before.delay_ms, -60000, 60000, 1)
        if accepted:
            params = before.to_dict()
            params['delay_ms'] = value
            self._apply_audio_route(AudioRoute.from_dict(params))
            self.status_lbl.setText('Audio delay: {:+d} ms'.format(value))

    def _show_audio_routing_diagnostics(self):
        """Inspect effective backend/clock state without claiming device parity.

        Read-only: opening support diagnostics never dirties a Source or
        schedules a recovery snapshot, even while playback is active.
        """
        snapshot = self.player.diagnostics_snapshot()
        telemetry = snapshot.get('audio_routing', {})
        record_pcm = (self.player.record_audio_capabilities()
                      if hasattr(self.player, 'record_audio_capabilities')
                      else {'state': 'unavailable', 'reason':
                            'No verified record-audio device backend'})
        route = self.player.audio_route
        source = self.session_controller.current
        detail = {
            'logical_source_id': source.id if source is not None else None,
            'media_audio_probe': dict(self.info.audio) if self.info is not None else {},
            'requested_route': route.to_dict(),
            'effective_route': telemetry,
            'record_pcm': record_pcm,
            'transport': {'frame': self.player.current_frame,
                          'fps_rational': list(self.info.frame_mapping().fps_rational)
                          if self.info is not None else None,
                          'playback_policy': self.player.playback_policy,
                          'playing': self.player.playing,
                          'speed': self.player.speed},
            'limitations': [
                'Qt5/Maya cannot select a non-default embedded soundtrack '
                'or apply programmable channel matrices.',
                'A/V drift is measured only while the separate audio clock '
                'is active and stable. Hardware speaker latency is uncalibrated.',
                'Verified v4 record WAV in the same durable image directory '
                'is supported; live audible record PCM is opt-in and depends '
                'on the local Qt output device and supported exact source plan.',
                'A verified record PCM sample buffer is not proof that a '
                'physical speaker played those samples with zero drift or '
                'calibrated video latency. Unsupported offsets, effects and '
                'retimes are refused.',
            ],
        }
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle('Audio Routing & Synchronization')
        dialog.resize(650, 450)
        layout = QtWidgets.QVBoxLayout(dialog)
        label = QtWidgets.QLabel(
            'Requested routing versus effective playback. This is diagnostic '
            'information, not proof of audible device routing or sample-accurate '
            'speaker synchronization.')
        label.setWordWrap(True)
        layout.addWidget(label)
        report = QtWidgets.QPlainTextEdit(dialog)
        report.setObjectName('frameReviewerAudioRoutingReport')
        report.setReadOnly(True)
        report.setPlainText(json.dumps(detail, indent=2, sort_keys=True,
                                       ensure_ascii=False, default=str))
        layout.addWidget(report)
        actions = QtWidgets.QHBoxLayout()
        copy_btn = QtWidgets.QPushButton('Copy Report', dialog)
        copy_btn.clicked.connect(
            lambda: QtWidgets.QApplication.clipboard().setText(
                report.toPlainText()))
        close_btn = QtWidgets.QPushButton('Close', dialog)
        close_btn.clicked.connect(dialog.accept)
        actions.addStretch(1)
        actions.addWidget(copy_btn)
        actions.addWidget(close_btn)
        layout.addLayout(actions)
        dialog.exec_() if hasattr(dialog, 'exec_') else dialog.exec()

    def _toggle_audio_mute(self):
        self._set_muted(not self.player.muted)

    def _show_volume_popup(self, *args):
        size = self.volume_menu.sizeHint()
        anchor = self.mute_btn.mapToGlobal(QtCore.QPoint(
            (self.mute_btn.width() - size.width()) // 2,
            -size.height()))
        self.volume_menu.popup(anchor)

    def _current_screen(self):
        """The QScreen the player currently sits on (not always the primary)."""
        gui = QtGui.QGuiApplication
        try:
            center = self.mapToGlobal(self.rect().center())
            screen = gui.screenAt(center)
            if screen is not None:
                return screen
        except Exception:
            pass
        try:
            screen = self.window().screen()
            if screen is not None:
                return screen
        except Exception:
            pass
        return gui.primaryScreen()

    def _toggle_fullscreen(self, enabled):
        # Fullscreen the player's own top-level window on whichever monitor it is
        # on, rather than letting Qt default to the primary display.
        window = self.window() or self
        if enabled:
            self._pre_fullscreen_geometry = window.geometry()
            screen = self._current_screen()
            if screen is not None:
                # Moving the window onto the target screen first makes Qt choose
                # that screen for fullscreen.
                window.setGeometry(screen.geometry())
            window.showFullScreen()
        else:
            window.showNormal()
            geometry = getattr(self, '_pre_fullscreen_geometry', None)
            if geometry is not None:
                window.setGeometry(geometry)
                self._pre_fullscreen_geometry = None

    def _show_color_tool(self):
        """Open (or raise) the non-destructive color-correction tool."""
        if self.color_dialog is None:
            self.color_dialog = ColorCorrectionDialog(self)
            self.color_dialog.valuesChanged.connect(self._on_color_changed)
        self.color_dialog.show()
        self.color_dialog.raise_()
        self.color_dialog.activateWindow()

    def _show_scopes(self):
        """Show asynchronous image scopes; never block the playback event loop."""
        dialog = self.scopes_dialog
        if dialog is None or getattr(dialog, '_closed', False):
            try:
                from ..viewer.scopes import ScopesDialog
                dialog = ScopesDialog(self)
            except (ImportError, RuntimeError, ValueError) as exc:
                QtWidgets.QMessageBox.warning(self, 'Image scopes unavailable', str(exc))
                return
            self.scopes_dialog = dialog
            dialog.stageChanged.connect(lambda *_: self._update_scopes())
            dialog.memberChanged.connect(lambda *_: self._update_scopes())
            dialog.domainChanged.connect(lambda *_: self._update_scopes())
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        self._update_scopes()

    def _update_scopes(self, presented_frame=None):
        """Scope the presented image in its actual selected pixel stage.

        Source/Working use float FramePayload before SDR conversion, never an
        8-bit QPixmap mislabeled HDR. Display uses the actual graded SDR image
        visible in the viewer, so legacy preview and monitor fallbacks are
        represented honestly. Requests are cancelled on stale/failed frames.
        """
        dialog = self.scopes_dialog
        if dialog is None or not dialog.isVisible() or getattr(dialog, '_closed', False):
            return
        composition_source = getattr(self.player, '_source', None)
        if callable(getattr(dialog, 'set_members', None)):
            if (self._composition is not None and
                    callable(getattr(composition_source, 'member_payload', None))):
                visible = self._composition.visible_members
                dialog.set_members([
                    '{} · {}'.format(chr(ord('A') + index), member.name)
                    for index, member in enumerate(visible)])
            else:
                dialog.set_members(())
        pixmap = self.surface.frame_item.pixmap()
        if pixmap.isNull():
            dialog.cancel_pending()
            return
        stage = getattr(dialog, 'selected_stage', 'display')
        member = getattr(dialog, 'selected_member', -1)
        if member >= 0:
            if stage == 'display':
                dialog.cancel_pending()
                dialog.status_label.setText(
                    'Individual Display scopes are not a rendered composite '
                    'view. Select Source or Working (before shared grade), '
                    'or select Composited output for actual SDR Display.')
                return
            native = composition_source.member_payload(self.player.current_frame,
                                                       member)
            if native is None:
                dialog.cancel_pending()
                dialog.status_label.setText(
                    'Member {} has no original precision sample for this '
                    'presented frame (hole, decode error or SDR-only member).'
                    .format(chr(ord('A') + member)))
                return
            if stage == 'source' and native.stage != 'source':
                dialog.cancel_pending()
                dialog.status_label.setText(
                    'Member {} is already a {} payload; its original Source '
                    'samples are not reversible.'.format(chr(ord('A') + member),
                                                       native.stage))
                return
            try:
                local = None
                if stage != native.stage:
                    from dataclasses import replace
                    from ..viewer.color import ColorPipelineState
                    base = self._effective_color_state()
                    member_color = composition_source.member_color_state(member, native)
                    member_color = (member_color.to_dict()
                                    if hasattr(member_color, 'to_dict') else
                                    dict(member_color or {}))
                    data = base.to_dict()
                    for key in ('input_space', 'config', 'ocio_context',
                                'bypass_input'):
                        if key in member_color:
                            data[key] = member_color[key]
                    local = replace(ColorPipelineState.from_dict(data),
                                    bypass_grade=True)
                dialog.submit_frame(native, stage=stage, color_state=local)
            except (RuntimeError, TypeError, ValueError, ImportError) as exc:
                dialog.cancel_pending()
                dialog.status_label.setText(
                    'Member {} precision scopes unavailable: {}'.format(
                        chr(ord('A') + member), exc))
            return
        if stage in ('source', 'working'):
            payload = getattr(self.surface, '_precision_payload', None)
            if payload is None:
                dialog.cancel_pending()
                dialog.status_label.setText(
                    '{} scopes unavailable: presented frame has no native '
                    'precision samples (8-bit/display or decoder fallback).'.format(
                        stage.title()))
                return
            if payload.stage == 'working' and stage == 'source':
                dialog.cancel_pending()
                dialog.status_label.setText(
                    'Source scopes unavailable: this composition is already '
                    'in shared working light. Choose Working for precision '
                    'or Display for actual SDR output.')
                return
            try:
                dialog.submit_frame(
                    payload, stage=stage, color_state=(
                        getattr(self.surface, '_color_pipeline_state', None)
                        if payload.stage != stage else None))
            except (RuntimeError, TypeError, ValueError) as exc:
                dialog.cancel_pending()
                dialog.status_label.setText(
                    '{} scopes unavailable: {}'.format(stage.title(), exc))
            return
        source = self.session_controller.current
        source_id = source.id if source is not None else ''
        source_frame = (self.player.current_frame if presented_frame is None
                        else int(presented_frame))
        if self.info is not None and self._composition is None:
            source_frame = self.info.frame_mapping().display_to_source(source_frame)
        dialog.submit_frame(pixmap, source_id=source_id, source_frame=source_frame)

    def _on_color_changed(self, params, enabled):
        self.surface.set_color_correction(params if enabled else None)
        self._update_scopes()

    def _show_playlist(self):
        """Reveal the Playlist tool in the Properties sidebar."""
        if self.playlist_panel is not None and self.info is not None:
            self.playlist_panel.set_active_path(self.info.path)
        self.act_side.setChecked(True)
        self._show_tool('playlist')

    def _queue_playlist_paths(self, paths):
        """Add paths (e.g. a ShotGrid shot's versions) to the Playlist and show it."""
        if not paths or self.playlist_panel is None:
            return
        self.playlist_panel.add_paths(paths)
        self._show_playlist()

    def _composition_source_ids(self, paths, mode):
        if mode == composition_mod.SEQUENCE:
            # A saved EDL/OTIO may deliberately repeat one logical Source in
            # multiple editorial occurrences. Generic playlist path matching
            # consumes identities once and otherwise returns an untrusted path
            # as the second ID; that silently loses the imported record edit.
            # Accept repeated IDs ONLY when each occurrence is independently
            # bound to its exact saved Source and resolved media path.
            saved = self.session_controller.session.view.get(
                'interchange_composition')
            if isinstance(saved, dict):
                try:
                    candidate = composition_mod.Composition.from_dict(saved)
                    if (candidate.mode == composition_mod.SEQUENCE and
                            len(candidate.members) == len(paths)):
                        ids = [member.source_id for member in candidate.members]
                        if all(
                                (self.session_controller.session.source(sid)
                                 is not None) and
                                os.path.normcase(os.path.normpath(
                                    self.session_controller.resolved_path(
                                        self.session_controller.session.source(sid))
                                    or '')) ==
                                os.path.normcase(os.path.normpath(path))
                                for sid, path in zip(ids, paths)):
                            return ids
                except (TypeError, ValueError, KeyError, OSError):
                    pass  # Never trust stale/imported IDs over actual media.
        from .comparison_identity import resolve_comparison_ids
        return resolve_comparison_ids(
            paths, mode, self.playlist_panel, self.session_controller)

    def _on_composition_changed(self, paths, mode):
        """Apply the playlist's composition live (no Compose button).

        ``paths`` are the effective clips (the selection, or all when nothing is
        selected). One clip plays normally; two or more compose per ``mode``:
        Sequence concatenates image sequences into one continuous timeline;
        Grid and Difference/Inverted render through the software compositor.
        """
        if self._applying_composition:
            return
        mode = composition_mod.validate_mode(mode)
        if (mode == composition_mod.SEQUENCE and self.playlist_panel is not None
                and self.playlist_panel.paths()):
            # A selected row means 'inspect/compare this source' in other modes;
            # a sequence ALWAYS spans the full ordered playlist. Reordering
            # updates Session.sources and immediately reconstructs its segments.
            paths = self.playlist_panel.paths()
        paths = [p for p in (paths or []) if p]
        if not paths:
            return
        # Playlist rows have stable identities, even when an imported EDL
        # refers to the same movie more than once with different edit trims.
        # A path lookup alone would collapse every occurrence onto edit one.
        member_ids = self._composition_source_ids(paths, mode)
        if (self._composition is not None and
                [member.source_id for member in self._composition.members] == member_ids and
                mode != composition_mod.SEQUENCE and
                self._composition.mode != composition_mod.SEQUENCE):
            # Switching comparison modes only changes the compositor. Keep the
            # current frame, cached member frames and decoder processes warm.
            parameters = self.playlist_panel.comparison_parameters()
            self._composition.mode = mode
            self._composition.parameters.update(parameters)
            active = getattr(self.player, '_source', None)
            if active is not None and hasattr(active, 'set_comparison'):
                # Re-presentation at the SAME frame may have different visible
                # source contributors; discard the prior image receipt now.
                self._comparison_presented_receipt = None
                if (mode in (composition_mod.A_B, composition_mod.FLICKER) and
                        callable(getattr(active, 'enable_integrity_proof', None))):
                    # Warm Blend/Grid → selected-source switches cannot certify
                    # their old SHA-less native cache by changing a mode flag.
                    # Explicitly invalidate and re-decode with stable original
                    # file proof before any managed selected-plate export.
                    active.enable_integrity_proof()
                active.set_comparison(mode, **parameters)
                self._align_comparison_view_geometry(active, mode)
                self.player.seek(self.player.current_frame)
                self.session_controller.set_view(comparison_mode=mode)
                self._schedule_scene_state_save()
                return
        self._capture_current_review()
        self._applying_composition = True
        try:
            if (len(paths) == 1 and not
                    (mode == composition_mod.SEQUENCE and
                     self.session_controller.session.view.get(
                         'interchange_composition'))):
                self._compose_single(paths[0])
            elif mode == composition_mod.SEQUENCE:
                self._compose_sequence(paths)
            else:
                self._compose_multi(paths, mode)
        finally:
            self._applying_composition = False
            # Record audio intent is strictly scoped to SEQUENCE composition;
            # a direct Source/review mode change must clear the opt-in instead
            # of silently inheriting a different edit's soundtrack.
            self._sync_export_audio_controls()

    def _probe_all(self, paths, mode=None):
        infos = []
        identities = self._composition_source_ids(
            paths, mode or (self.playlist_panel.composition_mode()
                            if self.playlist_panel is not None else
                            composition_mod.SEQUENCE))
        for path, source_id in zip(paths, identities):
            try:
                info = self._probe_source_media(path)
                source = self.session_controller.session.source(source_id)
                if info.is_sequence and source is not None:
                    if (info.declared_frames is None and
                            source.timing.get('declared_frames') is not None):
                        info = info.with_authored_timeline(
                            source.timing['declared_frames'])
                    policy = (source.review or {}).get('missing_frame_policy', 'skip')
                    info = info.with_missing_policy(policy)
                # These immutable MediaInfo.with_* constructors produce new
                # probes. Bind the logical identity LAST so a sparse policy
                # cannot silently return the first duplicate-path Source.
                if source is not None:
                    info.source_id = source.id
                infos.append(info)
            except Exception as exc:
                self.status_lbl.setText('Could not probe {}: {}'.format(
                    os.path.basename(path), exc))
                return None
        return infos

    def _comparison_color_states(self, infos):
        """One source's input interpretation must not bleed into another.

        Working/display grade is shared across the comparison, but the
        input-space/OCIO source profile remains attached to each logical Source.
        """
        profiles = {}
        for index, info in enumerate(infos):
            associated = self.session_controller.session.source(
                getattr(info, 'source_id', None))
            if associated is None:
                associated = self.session_controller.source_for_path(info.path)
            data = self._effective_color_state(associated).to_dict()
            if associated is None:
                data['input_space'] = ('linear' if info.path.lower().endswith('.exr')
                                       else 'sRGB')
            profiles[index] = data
            if associated is not None:
                profiles[associated.id] = data
        return profiles

    def _compose_single(self, path):
        """Play one clip normally (no composition)."""
        was_composite = self._composition is not None
        current = getattr(self.info, 'path', None)
        identities = self._composition_source_ids(
            [path], self.playlist_panel.composition_mode()
            if self.playlist_panel is not None else composition_mod.SEQUENCE)
        target_id = identities[0] if identities else None
        self._clear_composition()
        if (not was_composite and current and
                os.path.normpath(current) == os.path.normpath(path) and
                (self.session_controller.current is None or
                 self.session_controller.current.id == target_id)):
            return   # already showing exactly this single clip
        info = self._probe_all([path], self.playlist_panel.composition_mode()
                               if self.playlist_panel is not None else None)
        if info:
            self._load(info[0])

    def _record_v4_picture_preview_ids(self, infos, composition, rate):
        """Opt into exact v4 picture preview only for bounded proven EXR cuts.

        A legacy SEQUENCE may have overlapping events, mixed movie decoders,
        unstated alpha association or different native image fields. Those
        still use the established preview; they must not inherit a claim of
        parity with managed v4 Working/Display export. Source IDs and edit
        occurrence IDs are deliberately different even for repeated plates.
        No Session state changes or decoded pixels are produced by this probe.
        """
        from fractions import Fraction
        from ..media import exr
        from ..viewer.color import MAX_PREVIEW_PIXELS
        if (not composition.record_timeline or
                not 2 <= len(composition.members) <= 8 or
                not 1 <= composition.total_frames() <= 120 or
                not infos):
            return None
        first_size = (infos[0].width, infos[0].height)
        if first_size[0] <= 0 or first_size[1] <= 0 or (
                first_size[0] * first_size[1] > MAX_PREVIEW_PIXELS):
            # An intentionally downsampled Qt preview is not an exact native
            # pixel parity receipt. Large plates retain legacy rendering.
            return None
        source_ids = [getattr(info, 'source_id', None) for info in infos]
        occurrence_ids = [item.source_id for item in composition.members]
        if (any(not isinstance(item, str) or not item for item in source_ids) or
                (source_ids != occurrence_ids and
                 source_ids != list(dict.fromkeys(occurrence_ids)))):
            return None
        for info in infos:
            source = self.session_controller.session.source(info.source_id)
            native_rate = (Fraction(*info.fps_rational)
                           if info.fps_rational else
                           Fraction(str(info.fps)).limit_denominator(1001000))
            if (source is None or not info.is_sequence or
                    not exr.is_exr(info.path) or
                    self._unsafe_exr_delivery(info) or
                    (info.width, info.height) != first_size or
                    abs(float(info.pixel_aspect_ratio) - 1.0) > 1e-6 or
                    not isinstance(source.color, dict) or
                    source.color.get('alpha_mode') not in (
                        'opaque', 'straight', 'premultiplied') or
                    any(not exr.is_exr(path) for path in info.sequence_files) or
                    native_rate != rate):
                return None
        seen_end = -1
        for index, member in enumerate(composition.members):
            if (type(member.session_offset) is not int or
                    member.session_offset <= seen_end or
                    member.session_offset < 0 or
                    member.frame_count < 1 or
                    member.rate is not None and Fraction(member.rate) != 1):
                return None
            seen_end = member.session_offset + member.frame_count - 1
            if seen_end >= 120:
                return None
            reference = source_ids[index] if source_ids == occurrence_ids else member.source_id
            if reference != member.source_id:
                return None
        return tuple('record-edit-{:03d}-{}'.format(
            index, member.source_id)
            for index, member in enumerate(composition.members))

    def _compose_sequence(self, paths):
        infos = self._probe_all(paths, composition_mod.SEQUENCE)
        if infos is None:
            return
        member_ids = self._composition_source_ids(paths, composition_mod.SEQUENCE)
        serialized = self.session_controller.session.view.get('interchange_composition')
        imported = None
        alignment_issues = []
        if serialized:
            try:
                candidate = composition_mod.Composition.from_dict(serialized)
                if ([member.source_id for member in candidate.members] == member_ids
                        and candidate.mode == composition_mod.SEQUENCE):
                    from ..media.time_alignment import align_composition
                    imported, alignment_issues = align_composition(
                        self.session_controller.session, candidate, infos)
            except (ValueError, TypeError, KeyError):
                imported = None
        members = [composition_mod.CompositionMember(
            id, i.frame_count, os.path.basename(path))
            for id, path, i in zip(member_ids, paths, infos)]
        self._composition = imported or composition_mod.Composition(
            composition_mod.SEQUENCE, members)
        if len(infos) > 1 or imported is not None:
            from ..playback.composite_source import CompositeSource
            from fractions import Fraction
            first = infos[0]
            session_fps = (self.session_controller.session.metadata.get('interchange', {})
                           .get('fps_rational') or first.fps_rational or first.fps)
            session_fps_fraction = CompositeSource._fps_fraction(session_fps)
            record_preview_ids = self._record_v4_picture_preview_ids(
                infos, self._composition, session_fps_fraction)
            source = CompositeSource(
                infos, composition_mod.SEQUENCE, self,
                self.player.cache_policy, self.ffmpeg,
                composition=self._composition, session_fps=session_fps,
                record_preview_v4=record_preview_ids is not None,
                record_occurrence_ids=record_preview_ids,
                integrity_proof=record_preview_ids is not None)
            if hasattr(source, 'set_source_color_states'):
                source.set_source_color_states(self._comparison_color_states(infos))
            # Comparison decoder and primary surface share one display
            # interpretation; the composite itself stays working-linear.
            source.set_color_state(self._effective_color_state(
                self.session_controller.session.source(
                    self._composition.members[0].source_id)))
            total = source.frame_count
            placeholder = ffmpegtools.MediaInfo(
                first.path, first.width, first.height, first.fps, total,
                sequence_files=first.sequence_files,
                sequence_source_frames=first.sequence_source_frames,
                frame_numbers=list(range(total)),
                fps_rational=(session_fps_fraction.numerator,
                              session_fps_fraction.denominator))
            # This method is also a public internal entry point used by
            # playlist restores/tests, not only through _on_composition_changed.
            # Preserve the explicitly constructed multi-source identity while
            # _load initializes its placeholder; direct artist single-source
            # _load_path still clears a stale composition unconditionally.
            previous_build = self._applying_composition
            self._applying_composition = True
            try:
                self._load(placeholder)
            finally:
                self._applying_composition = previous_build
            self.player.load_frames_source(
                source, float(source._session_fps), source.frame_count,
                first.width, first.height)
            self._apply_view_geometry(first)
            self.timeline.set_segments(
                [seg.to_tuple() for seg in self._composition.segments()])
        elif infos:
            self._load(infos[0])
            self.timeline.clear_segments()
        self.status_lbl.setText(
            'Sequence: {} clips concatenated ({} frames). Up/Down jump to '
            'the next/previous clip.'.format(len(members),
                                             self.player.frame_count))
        if alignment_issues:
            self.status_lbl.setText('Sequence loaded with {} media timecode '
                                    'alignment warnings.'.format(len(alignment_issues)))
            self.status_lbl.setToolTip('\n'.join(alignment_issues))

    def _compose_multi(self, paths, mode):
        """Grid/difference through the software multi-source compositor."""
        infos = self._probe_all(paths, mode)
        if infos is None:
            return
        member_ids = self._composition_source_ids(paths, mode)
        members = [composition_mod.CompositionMember(
            source_id, i.frame_count, os.path.basename(p))
            for source_id, p, i in zip(member_ids, paths, infos)]
        self._composition = composition_mod.Composition(mode, members)

        from ..playback.composite_source import CompositeSource
        from fractions import Fraction
        total = max(i.frame_count for i in infos)
        first = infos[0]
        session_rate = (Fraction(*first.fps_rational)
                        if first.fps_rational else
                        Fraction(str(first.fps)).limit_denominator(1001000))
        # Build the window UI/state with a placeholder, then drive the player
        # from the composite source so grid/difference actually render.
        placeholder = ffmpegtools.MediaInfo(
            first.path, first.width, first.height, float(session_rate), total,
            sequence_files=first.sequence_files,
            sequence_source_frames=first.sequence_source_frames,
            frame_numbers=list(range(total)),
            fps_rational=(session_rate.numerator, session_rate.denominator))
        previous_build = self._applying_composition
        self._applying_composition = True
        try:
            self._load(placeholder)
        finally:
            self._applying_composition = previous_build
        source = CompositeSource(
            infos, mode, self, self.player.cache_policy, self.ffmpeg,
            composition=self._composition,
            parameters=self.playlist_panel.comparison_parameters(),
            session_fps=session_rate,
            # Artist-approved managed selected-source delivery must never
            # freeze newly replaced on-disk EXR bytes as though they were the
            # already reviewed cached native plate. Normal Sequence playback
            # retains its original no-hash performance contract.
            integrity_proof=(mode in (composition_mod.A_B,
                                      composition_mod.FLICKER)))
        if hasattr(source, 'set_source_color_states'):
            source.set_source_color_states(self._comparison_color_states(infos))
        source.set_color_state(self._effective_color_state(
            self.session_controller.session.source(
                self._composition.members[0].source_id)))
        self.player.load_frames_source(source, float(session_rate), source.frame_count,
                                       first.width, first.height)
        # All comparisons share the first Source's native coordinate system.
        # Only a mixed-PAR GRID substitutes square canvas geometry. Keep the
        # Session-owned view rotation, including after warm mode switching.
        self._apply_view_geometry(first)
        self._align_comparison_view_geometry(source, mode)
        self.timeline.clear_segments()
        label = {composition_mod.GRID: 'Grid',
                 composition_mod.DIFFERENCE: 'Difference',
                 composition_mod.INVERTED_DIFFERENCE:
                     'Inverted difference'}.get(mode, mode)
        self.status_lbl.setText('{}: compositing {} clips ({} frames).'.format(
            label, len(infos), total))

    def _concatenated_info(self, paths, infos):
        """One synthetic MediaInfo whose frames are every clip's, back to back.

        Used for sequence composition of image-sequence clips: the existing
        SequenceSource then plays the whole playlist continuously. FPS and
        display size come from the first clip (mixed-rate clips play at the
        first clip's rate).
        """
        files = []
        for info in infos:
            files.extend(info.sequence_files)
        first = infos[0]
        total = len(files)
        return ffmpegtools.MediaInfo(
            first.path, first.width, first.height, first.fps, total,
            sequence_files=files, frame_numbers=list(range(total)))

    def _clear_composition(self):
        self._composition = None
        self.timeline.clear_segments()
        self._sync_export_audio_controls()

    def _sync_export_audio_controls(self):
        """Audio choice is delivery intent, never an autosaved review edit."""
        enabled = (self.info is not None and not self.info.is_sequence and
                   self._composition is None and
                   self.export_color_stage.currentData() == 'display')
        self.export_preserve_audio_cb.setEnabled(bool(enabled))
        if not enabled:
            self.export_preserve_audio_cb.setChecked(False)
        record = (self._composition is not None and
                  self._composition.mode == composition_mod.SEQUENCE and
                  self.info is not None and
                  self.export_color_stage.currentData() in ('working', 'display'))
        self.export_record_audio_cb.setEnabled(bool(record))
        if not record:
            self.export_record_audio_cb.setChecked(False)
            self._record_audio_override_key = None
            self._record_audio_overrides = {}
        self.record_audio_routes_btn.setEnabled(
            bool(record and self.export_record_audio_cb.isChecked()))
        self.record_audio_overlay_btn.setEnabled(
            bool(record and self.export_record_audio_cb.isChecked()))
        self.record_audio_layers_btn.setEnabled(
            bool(record and self.export_record_audio_cb.isChecked()))
        self.record_audio_preview_btn.setEnabled(
            bool(record and self.export_record_audio_cb.isChecked()))
        self.record_audio_audition_btn.setEnabled(
            bool(record and self.export_record_audio_cb.isChecked()))
        allow_mov = (bool(record) and self.export_record_audio_cb.isChecked() and
                     self.export_color_stage.currentData() == 'display')
        self.export_record_av_cb.setEnabled(allow_mov)
        if not allow_mov:
            self.export_record_av_cb.setChecked(False)
        self.export_record_rgba16_cb.setEnabled(allow_mov)
        if not allow_mov:
            self.export_record_rgba16_cb.setChecked(False)
        if (getattr(self, '_record_waveform_edit_key', None) is not None and
                (not record or not self.export_record_audio_cb.isChecked() or
                 self._record_waveform_edit_key != self._record_audio_edit_key())):
            self._invalidate_record_waveform()

    def _record_audio_edit_key(self):
        """Bind ephemeral audio choices to EXACT edit occurrences, not paths."""
        comp = self._composition
        if comp is None or comp.mode != composition_mod.SEQUENCE:
            return None
        return tuple((m.source_id, m.name, m.source_in, m.frame_count,
                      m.session_offset, m.trim_in, m.trim_out, str(m.rate),
                      m.hold) for m in comp.visible_members)

    def _saved_record_audio_routes(self):
        """Read only matching, typed Session intent; never trust it as proof."""
        from .record_audio_intent import restore_intent
        saved = self.session_controller.session.view.get('record_audio_intent')
        if saved is None:
            return {}
        return restore_intent(self._composition, saved)

    def _choose_record_audio_routes(self):
        """Explicit per-occurrence output sound choices, without Session edits.

        Audio is still frozen/probed and independently verified at export.
        This is not a claim of real-time multi-clip speaker synchronization.
        """
        if not (self.export_record_audio_cb.isEnabled() and
                self.export_record_audio_cb.isChecked() and
                self._record_audio_edit_key() is not None):
            return
        from ..playback.audio_routing import probe_audio_streams
        comp = self._composition
        fingerprint = self._record_audio_edit_key()
        saved_compatible = True
        try:
            saved = self._saved_record_audio_routes()
        except ValueError as exc:
            QtWidgets.QMessageBox.warning(self, 'Record audio routes',
                                          'Saved edit soundtrack choices cannot '
                                          'be applied to these edits: {}. Select '
                                          'new routes and clear or replace the '
                                          'stale saved intent.'.format(exc))
            saved = {}
            saved_compatible = False
        previous = (self._record_audio_overrides
                    if fingerprint == self._record_audio_override_key else saved)
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle('Record audio by edit occurrence')
        dialog.setMinimumWidth(720)
        layout = QtWidgets.QVBoxLayout(dialog)
        description = QtWidgets.QLabel(
            'Each occurrence has an independent soundtrack. These choices '
            'never change Source audio or speaker playback. You may explicitly '
            'remember them as editorial Session intent below. External sound must be '
            'zero-origin 48 kHz stereo PCM WAV, aligned to the source frame '
            'clock; every file and output sample is independently checked.')
        description.setWordWrap(True)
        layout.addWidget(description)
        rows = QtWidgets.QFormLayout()
        editors = []

        def pick_path(edit):
            path, _ = QtWidgets.QFileDialog.getOpenFileName(
                dialog, 'Select independently verified PCM WAV',
                os.path.dirname(edit.text()) if edit.text() else '',
                'PCM WAV (*.wav)')
            if path:
                edit.setText(path)

        try:
            for index, member in enumerate(comp.visible_members):
                source = self.session_controller.session.source(member.source_id)
                if source is None:
                    raise ValueError('Record occurrence has a missing Source.')
                media_path = self.session_controller.resolved_path(source)
                info = ffmpegtools.probe(self.ffprobe, QtGui.QImageReader, media_path)
                selected = previous.get(index, {})
                combo = QtWidgets.QComboBox(dialog)
                combo.addItem('Use current Source audio route', 'source')
                if info.is_sequence:
                    combo.addItem('Explicit silence (image edit)', 'silence')
                else:
                    for track in probe_audio_streams(self.ffprobe, media_path)[:4]:
                        ordinal = track['audio_index']
                        combo.addItem('Embedded audio track {} (stream #{})'.format(
                            ordinal, track['stream_index']), 'embedded:{}'.format(ordinal))
                combo.addItem('External verified PCM WAV…', 'external')
                if selected.get('kind') == 'external':
                    initial = 'external'
                elif selected.get('kind') == 'embedded':
                    initial = 'embedded:{}'.format(selected.get('stream_index'))
                elif selected.get('kind') == 'silence':
                    initial = 'silence'
                else:
                    initial = 'source'
                found = combo.findData(initial)
                if found >= 0:
                    combo.setCurrentIndex(found)
                edit = QtWidgets.QLineEdit(dialog)
                edit.setPlaceholderText('Absolute path to zero-origin PCM WAV')
                edit.setText(selected.get('external_path', ''))
                browse = QtWidgets.QPushButton('Browse…', dialog)
                browse.clicked.connect(lambda checked=False, e=edit: pick_path(e))
                combo.currentIndexChanged.connect(
                    lambda unused=None, c=combo, e=edit, b=browse:
                    (e.setEnabled(c.currentData() == 'external'),
                     b.setEnabled(c.currentData() == 'external')))
                edit.setEnabled(combo.currentData() == 'external')
                browse.setEnabled(combo.currentData() == 'external')
                line = QtWidgets.QWidget(dialog)
                line_layout = QtWidgets.QHBoxLayout(line)
                line_layout.setContentsMargins(0, 0, 0, 0)
                line_layout.addWidget(combo, 2)
                line_layout.addWidget(edit, 2)
                line_layout.addWidget(browse)
                rows.addRow('Edit {} · {} · record {}–{}'.format(
                    index + 1, source.name, member.session_offset,
                    member.session_offset + member.frame_count - 1), line)
                editors.append((combo, edit))
        except (OSError, ValueError, RuntimeError) as exc:
            QtWidgets.QMessageBox.warning(self, 'Record audio routes', str(exc))
            dialog.deleteLater()
            return
        layout.addLayout(rows)
        remember = QtWidgets.QCheckBox(
            'Remember these per-edit sound choices in the review Session', dialog)
        remember.setToolTip(
            'An explicit editorial document change: marks the Session dirty '
            'and allows Save/Save As and bounded recovery. Unchecked removes '
            'previously remembered audio choices; otherwise export-only choices '
            'never autosave.')
        remember.setChecked(
            saved_compatible and
            self.session_controller.session.view.get('record_audio_intent') is not None)
        layout.addWidget(remember)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel,
            parent=dialog)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        accepted = (dialog.exec() if hasattr(dialog, 'exec') else dialog.exec_())
        if accepted == QtWidgets.QDialog.Accepted:
            overrides = {}
            for index, (combo, edit) in enumerate(editors):
                choice = str(combo.currentData())
                if choice == 'external':
                    overrides[index] = {'kind': 'external',
                                        'external_path': edit.text().strip(),
                                        'stream_index': 0}
                elif choice == 'silence':
                    overrides[index] = {'kind': 'silence'}
                elif choice.startswith('embedded:'):
                    overrides[index] = {'kind': 'embedded',
                                        'stream_index': int(choice.split(':')[1])}
            if remember.isChecked():
                from .record_audio_intent import freeze_intent
                try:
                    saved_intent = freeze_intent(comp, overrides)
                except ValueError as exc:
                    QtWidgets.QMessageBox.warning(self, 'Record audio intent',
                                                  str(exc))
                    dialog.deleteLater()
                    return
            else:
                saved_intent = None
            before_intent = self.session_controller.session.view.get(
                'record_audio_intent')
            if before_intent != saved_intent:
                # Only explicitly remembered/cleared editorial intent is a
                # Session mutation. Merely previewing or exporting never is.
                self.session_controller.set_view(record_audio_intent=saved_intent)
                self._schedule_scene_state_save()
            self._record_audio_override_key = fingerprint
            self._record_audio_overrides = overrides
            self._invalidate_record_waveform()
        dialog.deleteLater()

    def _choose_record_audio_overlay(self):
        """Explicit editorial audio-only event; never infer overlap from reels.

        Only one separately proven zero-origin 48 kHz PCM source is supported.
        It can overlap the selected edit sound at exact record frames; gains
        0/0.5/1 have a declared integer mixing rule, never Qt device DSP.
        """
        if (not self.record_audio_overlay_btn.isEnabled() or
                self._composition is None or
                self._composition.mode != composition_mod.SEQUENCE):
            return
        from .record_audio_overlay_intent import (
            freeze_overlay_intent, restore_overlay_intent)
        key = self._record_audio_edit_key()
        persisted = self.session_controller.session.view.get(
            'record_audio_overlay_intent')
        compatible = True
        try:
            saved = restore_overlay_intent(self._composition, persisted)
        except ValueError as exc:
            compatible = False
            saved = None
            QtWidgets.QMessageBox.warning(self, 'Audio-only record event',
                'Previously saved music-bed intent is incompatible with '
                'these cuts: {}. Select new timing and explicitly replace '
                'or clear the stale saved event.'.format(exc))
        choice = (self._record_overlay_choice
                  if self._record_overlay_key == key else saved)
        if isinstance(choice, list):
            # Never silently drop a second or third authored event by opening
            # the legacy one-event dialog; use the explicit ordered editor.
            self._choose_record_audio_layers()
            return
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle('One verified audio-only record event')
        dialog.setMinimumWidth(600)
        layout = QtWidgets.QVBoxLayout(dialog)
        summary = QtWidgets.QLabel(
            'One external canonical zero-origin 48 kHz stereo PCM WAV can '
            'overlap the already selected edit audio, using the 24000/1001 '
            'record clock. An authored video gap becomes audible ONLY where '
            'this explicitly chosen event is present. No crossfade, retime, '
            'unverified delay, automation curve or extra audio output clock.')
        summary.setWordWrap(True)
        layout.addWidget(summary)
        active = QtWidgets.QCheckBox('Include one audio-only event', dialog)
        active.setChecked(choice is not None)
        layout.addWidget(active)
        form = QtWidgets.QFormLayout()
        path_edit = QtWidgets.QLineEdit(dialog)
        path_edit.setText((choice or {}).get('external_path', ''))
        path_edit.setPlaceholderText('Canonical absolute .wav file')
        browse = QtWidgets.QPushButton('Browse…', dialog)
        def browse_external():
            path, _ = QtWidgets.QFileDialog.getOpenFileName(
                dialog, 'Choose verified external 48k stereo PCM',
                os.path.dirname(path_edit.text()) if path_edit.text() else '',
                'PCM WAV (*.wav)')
            if path:
                path_edit.setText(path)
        browse.clicked.connect(browse_external)
        path_row = QtWidgets.QHBoxLayout()
        path_row.addWidget(path_edit, 1)
        path_row.addWidget(browse)
        form.addRow('Original audio file', path_row)
        start = QtWidgets.QSpinBox(dialog)
        start.setRange(0, 119)
        start.setValue((choice or {}).get('record_start_frame', 0))
        form.addRow('First record frame', start)
        original = QtWidgets.QSpinBox(dialog)
        original.setRange(0, 119)
        original.setValue((choice or {}).get('source_in_frame', 0))
        form.addRow('Original audio-in frame', original)
        duration = QtWidgets.QSpinBox(dialog)
        duration.setRange(1, 120)
        duration.setValue((choice or {}).get('duration_frames',
                                            self._composition.total_frames()))
        form.addRow('Length in record frames', duration)
        gain = QtWidgets.QComboBox(dialog)
        for numerator, name in ((2, '1.0 · Unity'),
                                (1, '0.5 · Half gain'),
                                (0, '0.0 · Muted event')):
            gain.addItem(name, numerator)
        gain.setCurrentIndex(max(0, gain.findData(
            (choice or {}).get('gain_num', 2))))
        form.addRow('Exact constant PCM gain', gain)
        layout.addLayout(form)
        for widget in (path_edit, browse, start, original, duration, gain):
            active.toggled.connect(widget.setEnabled)
            widget.setEnabled(active.isChecked())
        remember = QtWidgets.QCheckBox(
            'Remember this audio-only record event in the review Session', dialog)
        remember.setChecked(compatible and persisted is not None)
        layout.addWidget(remember)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel,
            parent=dialog)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        accepted = (dialog.exec() if hasattr(dialog, 'exec') else dialog.exec_())
        if accepted == QtWidgets.QDialog.Accepted:
            next_choice = (dict(
                external_path=path_edit.text().strip(),
                record_start_frame=start.value(),
                source_in_frame=original.value(),
                duration_frames=duration.value(),
                gain_num=gain.currentData(), gain_den=2)
                if active.isChecked() else None)
            try:
                verified_intent = freeze_overlay_intent(
                    self._composition, next_choice)
            except ValueError as exc:
                QtWidgets.QMessageBox.warning(dialog, 'Audio-only record event',
                                              str(exc))
                dialog.deleteLater()
                return
            next_saved = verified_intent if remember.isChecked() and next_choice else None
            if persisted != next_saved:
                self.session_controller.set_view(
                    record_audio_overlay_intent=next_saved)
                self._schedule_scene_state_save()
            self._record_overlay_key = key
            self._record_overlay_choice = next_choice
            self._invalidate_record_waveform()
        dialog.deleteLater()

    def _choose_record_audio_layers(self):
        """Edit an ordered, bounded set of independently authored PCM-only cuts.

        This editor owns *intent*, not source PCM/PTS evidence. Each selected
        file is independently frozen and rechecked in the shared v4 queue.
        Authoring order is part of the exact saturated-mix semantics.
        """
        if (not self.record_audio_layers_btn.isEnabled() or
                self._composition is None or
                self._composition.mode != composition_mod.SEQUENCE):
            return
        from .record_audio_overlay_intent import (
            freeze_overlay_intent, restore_overlay_intent)
        key = self._record_audio_edit_key()
        persisted = self.session_controller.session.view.get(
            'record_audio_overlay_intent')
        compatible = True
        try:
            saved = restore_overlay_intent(self._composition, persisted)
        except (ValueError, TypeError, KeyError) as exc:
            compatible = False
            saved = None
            QtWidgets.QMessageBox.warning(
                self, 'Ordered record PCM layers',
                'Saved audio-only events no longer match these exact edits: '
                '{}. Explicitly replace or clear the stale Session intent.'.format(exc))
        choice = (self._record_overlay_choice
                  if self._record_overlay_key == key else saved)
        layers = ([dict(event) for event in choice] if isinstance(choice, list)
                  else [dict(choice)] if isinstance(choice, dict) else [])
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle('Ordered verified audio-only record layers (max 3)')
        dialog.setMinimumWidth(820)
        layout = QtWidgets.QVBoxLayout(dialog)
        explanation = QtWidgets.QLabel(
            'Each row is one separately proven canonical zero-origin 48 kHz '
            'stereo s16 WAV. Record start, original source in and duration are '
            'integer 24000/1001 frames (exactly 2,002 samples each). Rows '
            'are mixed TOP TO BOTTOM on the existing cut soundtrack, with '
            'per-sample signed truncation and saturation after EACH row. '
            'Changing row order can change clipping. Source gaps remain silent '
            'unless an explicitly positioned row covers them. No fades, '
            'crossfades, retiming, implicit media sync or additional clock.')
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        table = QtWidgets.QTableWidget(0, 5, dialog)
        table.setObjectName('recordAudioLayersTable')
        table.setHorizontalHeaderLabels([
            'Canonical WAV file', 'Record start', 'Source in',
            'Length (frames)', 'Gain (0 / 0.5 / 1)'])
        table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setStretchLastSection(False)
        table.setColumnWidth(0, 380)
        for col in (1, 2, 3, 4):
            table.setColumnWidth(col, 100)
        table.setMinimumHeight(190)
        layout.addWidget(table)

        def append_layer(event=None):
            if table.rowCount() >= 3:
                return
            event = event or {}
            row = table.rowCount()
            table.insertRow(row)
            values = (
                event.get('external_path', ''),
                str(event.get('record_start_frame', 0)),
                str(event.get('source_in_frame', 0)),
                str(event.get('duration_frames', min(120,
                    self._composition.total_frames()))),
                {0: '0', 1: '0.5', 2: '1'}.get(event.get('gain_num', 2), '1'))
            for col, value in enumerate(values):
                table.setItem(row, col, QtWidgets.QTableWidgetItem(value))
            table.setCurrentCell(row, 0)

        def rows():
            # Reading the visible table, not stale hidden state, makes row
            # order and every artist edit part of the frozen source plan.
            result = []
            for row in range(table.rowCount()):
                def value(col):
                    item = table.item(row, col)
                    return item.text().strip() if item else ''
                gain_text = value(4)
                if gain_text not in ('0', '0.5', '1'):
                    raise ValueError('Layer {} gain must be exactly 0, 0.5 or 1.'
                                     .format(row + 1))
                try:
                    start, source_in, duration = (
                        int(value(col)) for col in (1, 2, 3))
                except ValueError:
                    raise ValueError('Layer {} record/source/length must be '
                                     'integer video frames.'.format(row + 1))
                result.append(dict(
                    external_path=value(0), record_start_frame=start,
                    source_in_frame=source_in, duration_frames=duration,
                    gain_num={'0': 0, '0.5': 1, '1': 2}[gain_text],
                    gain_den=2))
            return result

        for layer in layers:
            append_layer(layer)
        actions = QtWidgets.QHBoxLayout()
        add = QtWidgets.QPushButton('Add WAV event', dialog)
        remove = QtWidgets.QPushButton('Remove selected', dialog)
        up = QtWidgets.QPushButton('Move earlier', dialog)
        down = QtWidgets.QPushButton('Move later', dialog)
        browse = QtWidgets.QPushButton('Browse selected WAV…', dialog)
        actions.addWidget(add)
        actions.addWidget(remove)
        actions.addWidget(up)
        actions.addWidget(down)
        actions.addWidget(browse)
        layout.addLayout(actions)

        def selected():
            return table.currentRow()

        def remove_selected():
            at = selected()
            if at >= 0:
                table.removeRow(at)
                if table.rowCount():
                    table.setCurrentCell(min(at, table.rowCount()-1), 0)

        def move(delta):
            at = selected()
            target = at + delta
            if at < 0 or target < 0 or target >= table.rowCount():
                return
            for col in range(table.columnCount()):
                a, b = table.takeItem(at, col), table.takeItem(target, col)
                table.setItem(at, col, b)
                table.setItem(target, col, a)
            table.setCurrentCell(target, 0)

        def browse_selected():
            at = selected()
            if at < 0:
                return
            previous = table.item(at, 0).text() if table.item(at, 0) else ''
            selected_path, _ = QtWidgets.QFileDialog.getOpenFileName(
                dialog, 'Select original canonical stereo PCM WAV',
                os.path.dirname(previous) if previous else '', 'PCM WAV (*.wav)')
            if selected_path:
                table.setItem(at, 0, QtWidgets.QTableWidgetItem(selected_path))

        add.clicked.connect(lambda *_: append_layer())
        remove.clicked.connect(remove_selected)
        up.clicked.connect(lambda *_: move(-1))
        down.clicked.connect(lambda *_: move(1))
        browse.clicked.connect(browse_selected)
        remember = QtWidgets.QCheckBox(
            'Remember this exact ordered audio-only edit list in the review Session',
            dialog)
        remember.setChecked(compatible and persisted is not None)
        layout.addWidget(remember)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel,
            parent=dialog)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        accepted_choice = []

        def accept_verified_intent():
            try:
                chosen = rows()
                if chosen:
                    freeze_overlay_intent(self._composition, chosen)
                accepted_choice[:] = chosen
            except (ValueError, TypeError) as exc:
                QtWidgets.QMessageBox.warning(
                    dialog, 'Invalid audio-only record layers', str(exc))
                return
            dialog.accept()

        buttons.accepted.connect(accept_verified_intent)
        result = (dialog.exec() if hasattr(dialog, 'exec') else dialog.exec_())
        if result == QtWidgets.QDialog.Accepted:
            next_choice = accepted_choice or None
            next_saved = (freeze_overlay_intent(self._composition, next_choice)
                          if remember.isChecked() and next_choice else None)
            if persisted != next_saved:
                self.session_controller.set_view(
                    record_audio_overlay_intent=next_saved)
                self._schedule_scene_state_save()
            self._record_overlay_key = key
            self._record_overlay_choice = next_choice
            self._invalidate_record_waveform()
        dialog.deleteLater()

    def _invalidate_record_waveform(self):
        """A new edit/route cannot retain the old waveform's sound identity."""
        preview = getattr(self, 'record_waveform', None)
        if preview is not None:
            preview.cancel()
        self._record_audition_spec = None
        player = getattr(self, 'player', None)
        if player is not None and hasattr(player, 'disable_record_audio'):
            player.disable_record_audio()
        self._record_waveform_bins = {}
        self._record_waveform_edit_key = None
        if hasattr(self, 'timeline'):
            self.timeline.clear_waveform()
        if hasattr(self, '_tasks') and 'waveform' in self._tasks:
            self._task_end('waveform')

    def _audition_record_sound(self):
        """User-mediated attempt to play proven PCM through Qt (no device SLA)."""
        capabilities = (self.player.record_audio_capabilities()
                        if hasattr(self.player, 'record_audio_capabilities') else {})
        if capabilities.get('state') in ('preparing', 'ready', 'playing', 'paused'):
            self._invalidate_record_waveform()
            self.status_lbl.setText('Record audio audition stopped; source playback unchanged.')
            return
        self._preview_record_waveform(audition=True)

    def _preview_record_waveform(self, audition=False):
        """Explicitly authorize a *read-only* offline verified record sound view.

        This is separate from source audio device playback and cannot publish
        an export WAV. A large soundtrack/probe is processed on a QThread after
        full immutable selection is frozen; colour provenance is confirmed only
        by the artist, never silently inferred from FFprobe.
        """
        if (self._closed or not self.export_record_audio_cb.isEnabled() or
                not self.export_record_audio_cb.isChecked() or
                self._composition is None or
                self._composition.mode != composition_mod.SEQUENCE):
            return
        try:
            frozen = self._composite_export_snapshot()
            if frozen.get('version') != 4 or frozen.get('audio_policy') != 'wav_pcm_v1':
                raise ValueError('Record PCM must be explicitly selected first.')
            full_count = self._composition.total_frames()
            if not 1 <= full_count <= 120 or full_count != self.player.frame_count:
                raise ValueError('Record waveform must match the actual bounded '
                                 'viewer record clock before decoding sound.')
            route_notes = []
            movie_color_notes = []
            for member, route in zip(frozen['members'], frozen['record_audio']['routes']):
                if member['input_kind'] == 'movie':
                    color = member['movie_color_provenance']
                    movie_color_notes.append(
                        '{}: {} fps, input {}, YUV {} / {}, opaque, codec {}'.format(
                            member['occurrence_id'],
                            member['movie_proof']['avg_frame_rate'],
                            color['input_space'], color['yuv_matrix'],
                            color['yuv_range'], member['movie_proof']['codec']))
                if route['kind'] == 'external':
                    detail = 'external 48k PCM {} · SHA256 {}'.format(
                        route['external_path'],
                        route['external_proof']['source_sha256'])
                elif route['kind'] == 'embedded':
                    detail = 'embedded audio ordinal {} (stream #{})'.format(
                        route['audio_ordinal'], route['stream_index'])
                else:
                    detail = 'explicit silence'
                route_notes.append('{}: {}'.format(member['occurrence_id'], detail))
            notes = '\n'.join(route_notes)
            overlays = frozen['record_audio'].get('overlays')
            if overlays is None:
                legacy_overlay = frozen['record_audio'].get('overlay')
                overlays = [legacy_overlay] if legacy_overlay is not None else []
            for overlay in overlays:
                notes += ('\nAUDIO-ONLY OVERLAP {}: {}, SHA256 {}, record '
                          '{} +{} frames, source in {}, gain {}/{}. '
                          'Picture gaps are audible where this explicit '
                          'event overlaps.'.format(
                              overlay['occurrence_id'],
                              overlay['external_path'],
                              overlay['external_proof']['source_sha256'],
                              overlay['record_start_frame'],
                              overlay['duration_frames'],
                              overlay['source_in_frame'],
                              overlay['gain_num'], overlay['gain_den']))
            movie_notes = ('\n\nExplicitly confirm these original movie '
                           'decoder/input-color interpretations:\n' +
                           '\n'.join(movie_color_notes)) if movie_color_notes else ''
            if QtWidgets.QMessageBox.question(
                    self, 'Audition verified record PCM' if audition else
                    'Verify record waveform',
                    'Independently decode these exact original sound sources '
                    'for a read-only, per-frame record waveform?\n\n{}{}\n\n'
                    '{}'.format(
                        notes, movie_notes,
                        ('The frozen PCM will also be sent to the local Qt '
                         'audio output device after verification, when the '
                         'device supports 48 kHz stereo s16. Play/pause/seek '
                         'follow the primary video clock; physical output '
                         'latency and drift are NOT certified.' if audition else
                         'No pictures or sound are exported or played here. '
                         'This is not calibrated speaker synchronization.')),
                    QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                    QtWidgets.QMessageBox.No) != QtWidgets.QMessageBox.Yes:
                return
            for member in frozen['members']:
                if member['input_kind'] == 'movie':
                    # The trusted v4 record plan refuses unconfirmed YUV
                    # provenance. Explicit approval above is required for
                    # this preview's own in-memory frozen snapshot only.
                    member['movie_color_provenance'].update(
                        confirmed=True, origin='user')
            first = frozen['members'][0]
            rate = frozen['session_fps']
            stage = self.export_color_stage.currentData()
            if stage not in ('working', 'display'):
                raise ValueError('Record waveform needs a supported color stage.')
            # This path is an immutable spec marker, not a destination: the
            # waveform worker is read-only and NEVER starts an ExportJob.
            output = os.path.join(
                os.path.dirname(first['source_path']),
                '.framereviewer-waveform-preview.####.' +
                ('exr' if stage == 'working' else 'png'))
            context = dict(
                path=output, sequence=True,
                source_path=first['source_path'], source_id=first['source_id'],
                start=0, end=full_count - 1, count=full_count,
                fps=rate, source_fps=rate, out_w=first['width'],
                out_h=first['height'], color_stage=stage,
                color=first['color'], composite_export=frozen,
                speed=1.0, tracking_enabled=False, burnin={}, crop=None,
                flip_h=False, flip_v=False, note_ids=[],
                movie_audio_policy='drop')
            spec = ExportSpec.from_context(
                context, source_path=first['source_path'],
                source_id=first['source_id'])
            # Fail before leaving UI on an actually unsupported edit. Only the
            # final FFmpeg sample reads should happen on the background worker.
            from ..exporting.spec import managed_record_timeline_plan
            managed_record_timeline_plan(spec)
            self.waveform_job.cancel()
            self._invalidate_record_waveform()
            self._waveform_ready = False
            self._record_waveform_edit_key = self._record_audio_edit_key()
            self._record_audition_spec = spec if audition else None
            self._task_begin('waveform', 'Verifying record PCM', determinate=False)
            self.record_waveform.request(spec, self.ffmpeg)
            self.timeline.setToolTip('Independently verifying selected PCM '
                                    'sources against the frozen record clock…')
        except (ValueError, TypeError, RuntimeError, OSError) as exc:
            self._invalidate_record_waveform()
            QtWidgets.QMessageBox.warning(self, 'Record PCM waveform rejected', str(exc))

    def _on_record_waveform_ready(self, generation, result):
        if (self._closed or generation != self.record_waveform.generation or
                self._record_waveform_edit_key != self._record_audio_edit_key()):
            return
        bins = result.get('bins') or []
        if (len(bins) != self.player.frame_count or
                [item.get('record_frame') for item in bins] != list(range(len(bins)))):
            self._on_record_waveform_failed(generation,
                'Selected PCM does not match the whole record playhead clock.')
            return
        self._record_waveform_bins = {
            item['record_frame']: item for item in bins}
        self.timeline.set_waveform([item['peak'] for item in bins],
                                   exact_record=True)
        self.timeline.setToolTip(
            'Verified exact 24000/1001 record PCM: per-cut source selection, '
            'repeated occurrences, true gaps and explicit audio-only overlays. '
            'This is a waveform visualization, NOT live speaker/device synchronization.')
        self._waveform_ready = True
        self._task_end('waveform')
        self.status_lbl.setText(
            'Verified record waveform: {} stereo samples, {} record frames. '
            'No audio-device playback claimed.'.format(
                result['sample_frames'], len(bins)))
        self._on_frame_changed(self.player.current_frame)
        if self._record_audition_spec is not None:
            spec = self._record_audition_spec
            self._record_audition_spec = None
            enable = getattr(self.player, 'enable_record_audio', None)
            if enable is None or not enable(spec, self.ffmpeg):
                self.status_lbl.setText(
                    'Record waveform verified; audible Qt playback was refused '
                    'by device or immutable transport constraints.')

    def _on_record_audio_transport_state(self, state):
        if self._closed:
            return
        if state in ('preparing', 'ready', 'playing', 'paused'):
            self.record_audio_audition_btn.setText('Stop record audio audition')
            if state == 'ready':
                self.status_lbl.setText('Record PCM verified; Qt audio ready. '
                                        'Start video Play to audition; hardware '
                                        'latency is uncalibrated.')
            elif state == 'playing':
                self.status_lbl.setText('Record PCM routed to local Qt sound '
                                        'output using the video record clock; '
                                        'physical A/V sync is NOT certified.')
        else:
            self.record_audio_audition_btn.setText('Audition verified record audio…')
            if state == 'unsupported':
                self.status_lbl.setText('Record audio device or media route '
                                        'is unsupported; no sound started.')

    def _on_record_audio_transport_error(self, reason):
        if not self._closed:
            self.status_lbl.setText('Record sound refused: {}'.format(reason))

    def _on_record_waveform_failed(self, generation, error):
        if self._closed or generation != self.record_waveform.generation:
            return
        self._invalidate_record_waveform()
        self._waveform_ready = False
        self.timeline.setToolTip('Record PCM waveform was not verified: {}'.format(error))
        self.status_lbl.setText('Record PCM visualization refused: {}'.format(error))

    def _focus_clip(self, direction):
        """Up/Down: focus the next/previous clip of a sequence composition.

        With no active sequence composition this preserves the original
        behavior: Up jumps to the In point, Down to the Out point.
        """
        comp = self._composition
        if comp is None or comp.mode != composition_mod.SEQUENCE or not comp.is_active():
            self._scrub(self.player.in_frame if direction > 0
                        else self.player.out_frame)
            return
        frame = self.player.current_frame
        start, end = (comp.next_clip_range(frame) if direction > 0
                      else comp.prev_clip_range(frame))
        self._set_marked_range(start, end)
        self._scrub(start)

    def _toggle_thumbnails(self, visible):
        self.timeline.set_thumbnails_visible(visible)
        if visible and self.info is not None and not self._thumbs_ready:
            self._request_thumbnails()

    def _toggle_waveform(self, visible):
        self.timeline.set_waveform_visible(visible)
        if visible and self.info is not None and not self._waveform_ready:
            self._request_waveform()

    def _request_thumbnails(self):
        self._thumbs_ready = True
        aspect = self.info.width / float(max(1, self.info.height))
        cell = max(16, THUMB_LANE_H * aspect)
        inner = max(1, self.timeline.width() - 16)
        count = int(utils.clamp(inner / cell, 6, 16))
        count = min(count, self.info.frame_count)
        # begin_thumbnails is driven by the job's `started` signal.
        self.thumb_job.request(self.info, count, THUMB_LANE_H)

    def _request_waveform(self):
        self._waveform_ready = True
        if self.info.is_sequence or self._composition is not None:
            self.timeline.set_waveform([])
            self.timeline.setToolTip('No verified sequence/composite soundtrack; '
                                    'waveform deliberately omitted.')
            return
        route = self.player.audio_route
        path = route.external_path or self.info.path
        stream_index = route.stream_index if not route.external_path else 0
        if not path or not os.path.isfile(path):
            self.timeline.set_waveform([])
            self.timeline.setToolTip('Selected soundtrack is unavailable: no waveform.')
            return
        # A selected nondefault track is visualized by FFmpeg even on a Qt5
        # playback device unable to audition it. Do not infer it was heard.
        self.timeline.setToolTip(
            'Mono visualization: {} audio stream {} with delay {:+d} ms. '
            'Qt5 audition of nondefault embedded tracks is backend-dependent; '
            'the waveform does not certify speaker routing.'.format(
                'external' if route.external_path else 'embedded',
                stream_index, route.delay_ms))
        # A waveform cache hit may emit `ready` synchronously. Register the
        # busy task first, otherwise its completion races and leaves a spinner.
        self._task_begin('waveform', 'Audio waveform', determinate=False)
        try:
            self.waveform_job.request(path, self.info.duration,
                                      stream_index=stream_index,
                                      delay_ms=route.delay_ms)
        except (ValueError, TypeError, OSError) as exc:
            self._task_end('waveform')
            self._waveform_ready = False
            self.timeline.set_waveform([])
            self.status_lbl.setText('Audio waveform unavailable: {}'.format(exc))
            return

    # --------------------------------------------------------------- crop
    def _on_crop_changed(self, rect):
        # A real artist crop edit wins over any still-pending deferred scene /
        # review restore, even if it is the SAME logical Source and image.
        # Silent set_crop(..., notify=False) during restore does not increment.
        self._crop_edit_generation += 1
        if self._stabilization_plan is not None and not self._applying_tracked:
            self._set_stabilization_preview(False)
        crop = self.surface.crop()
        if crop is None:
            self.crop_lbl.setText('None (full frame)')
            if self.tracking_enabled and not self._applying_tracked:
                self._clear_tracking()
            return
        x, y, w, h = crop
        self.crop_lbl.setText('{}, {}  ·  {} × {}'.format(x, y, w, h))
        if not self._applying_tracked:
            self.out_w_le.setText(str(w))
            self.out_h_le.setText(str(h))
            if self.tracking_enabled:
                center = self._nearest_center(self.player.current_frame)
                if center:
                    self.tracking_size = (w, h)
                    self.tracking_offset = QtCore.QPointF(
                        x + w / 2.0 - center[0], y + h / 2.0 - center[1])
                    self._update_tracking_overlay()
                    self._persist_tracking_record()

    def _on_flip_changed(self, *args):
        horizontal = self.act_flip_h.isChecked()
        vertical = self.act_flip_v.isChecked()
        changed_h = horizontal != getattr(self, '_current_flip_h', False)
        changed_v = vertical != getattr(self, '_current_flip_v', False)
        if (changed_h or changed_v) and self._stabilization_plan is not None:
            self._set_stabilization_preview(False)
        # An in-flight optical flow job captured the previous flip flags.
        # Letting its result arrive afterward would put the crop into the
        # previous coordinate system (and could overwrite an edited crop).
        if (changed_h or changed_v) and self._tracking_job is not None:
            self._cancel_tracking_job()
            self.track_btn.setEnabled(self.info is not None and tracking.HAVE_CV2)
            self.tracking_mode.setEnabled(tracking.HAVE_CV2)
            self.tracking_lbl.setText('Tracking cancelled after flip')
        self._applying_tracked = True
        try:
            self.surface.set_flip(horizontal, vertical)
        finally:
            self._applying_tracked = False
        self._current_flip_h, self._current_flip_v = horizontal, vertical
        if changed_h or changed_v:
            self._reset_undo()
        if self.tracking_enabled:
            # Tracking is expressed in *display* space. Flipping the viewer
            # mirrors the stored points rather than invalidating the track.
            width = self.info.width if self.info is not None else 0
            height = self.info.height if self.info is not None else 0
            if changed_h or changed_v:
                self.tracked_centers = {
                    key: (width - cx if changed_h else cx,
                          height - cy if changed_v else cy)
                    for key, (cx, cy) in self.tracked_centers.items()}
                self.tracked_quads = {
                    # Preserve the corner orientation after mirroring. Merely
                    # mirroring each point (without permuting it) silently
                    # unflips a perspective-warped export.
                    key: [(width - quad[i][0] if changed_h else quad[i][0],
                           height - quad[i][1] if changed_v else quad[i][1])
                          for i in ((2, 3, 0, 1) if changed_h and changed_v else
                                    (1, 0, 3, 2) if changed_h else
                                    (3, 2, 1, 0) if changed_v else
                                    (0, 1, 2, 3))]
                    for key, quad in self.tracked_quads.items()}
                if changed_h:
                    self.tracking_offset.setX(-self.tracking_offset.x())
                if changed_v:
                    self.tracking_offset.setY(-self.tracking_offset.y())
                self._apply_tracked_crop()
                self._persist_tracking_record()

    # ----------------------------------------------------------- tracking
    def _apply_stabilization_frame(self, dense_frame):
        """View-only camera movement; the native image/annotations stay bound."""
        plan = self._stabilization_plan
        if plan is None or self.info is None:
            return
        # A late seek/reload signal from an old decoder cannot move the NEW
        # Source's scene with a stale camera transform. FrameMapping clamps
        # out-of-range dense indices, so check the range BEFORE mapping.
        current = self.session_controller.current
        if (current is None or current.id != plan.source_id or
                type(dense_frame) is not int or
                not 0 <= dense_frame < self.info.frame_count):
            self.surface.set_stabilization_offset(None)
            self._stabilization_plan = None
            self.stabilize_view_cb.blockSignals(True)
            self.stabilize_view_cb.setChecked(False)
            self.stabilize_view_cb.blockSignals(False)
            return
        label = self.info.frame_mapping().display_to_source(dense_frame)
        if label not in plan.source_frames:
            # A tracked range never licenses an approximation outside it.
            self.surface.set_stabilization_offset(None)
            return
        self.surface.set_stabilization_offset(plan.offset(label))

    def _set_stabilization_preview(self, enabled, persist=True):
        """Enable only source-verified translation preview, fail without edits."""
        enabled = bool(enabled)
        if enabled:
            source = self.session_controller.current
            if (source is None or self.info is None or
                    self._composition is not None or
                    not self.tracking_enabled or not self.tracked_centers):
                reason = 'A single Source and a completed tracking pass are required.'
                plan = None
            else:
                from ..viewer.stabilization import StabilizationPreview
                mapping = self.info.frame_mapping()
                labels = [mapping.display_to_source(index)
                          for index in sorted(self.tracked_centers)]
                saved = source.review.get('stabilization_preview')
                anchor = ((saved or {}).get('anchor_source_frame')
                          if isinstance(saved, dict) else None)
                try:
                    plan = StabilizationPreview.from_tracking(
                        source.review.get('tracking_v1'), self.info, source.id,
                        labels, enabled=True,
                        flip_h=self.act_flip_h.isChecked(),
                        flip_v=self.act_flip_v.isChecked(),
                        pixel_aspect=self.surface.image_geometry().pixel_aspect,
                        rotation=self.surface.image_geometry().rotation,
                        anchor_source_frame=anchor)
                except (TypeError, ValueError, KeyError) as exc:
                    reason = str(exc)
                    plan = None
            if plan is None:
                # Restore checkbox to the last real accepted state without
                # changing review, stopping playback or generating autosave.
                self.stabilize_view_cb.blockSignals(True)
                self.stabilize_view_cb.setChecked(False)
                self.stabilize_view_cb.blockSignals(False)
                self.status_lbl.setText('Stabilization preview refused: {}'.format(reason))
                return False
            if self._stabilization_plan is not None and \
                    self._stabilization_plan.source_frames == plan.source_frames:
                return False
            self._stabilization_plan = plan
            self.stabilize_view_cb.blockSignals(True)
            self.stabilize_view_cb.setChecked(True)
            self.stabilize_view_cb.blockSignals(False)
            self._apply_stabilization_frame(self.player.current_frame)
            self.status_lbl.setText(
                'Stabilization preview active for {} verified source frames; '
                'native plate/annotations move together. Not baked into export.'
                .format(len(plan.source_frames)))
            state = {'enabled': True,
                     'anchor_source_frame': plan.anchor_source_frame}
        else:
            was_active = self._stabilization_plan is not None
            self._stabilization_plan = None
            self.surface.set_stabilization_offset(None)
            self.stabilize_view_cb.blockSignals(True)
            self.stabilize_view_cb.setChecked(False)
            self.stabilize_view_cb.blockSignals(False)
            if not was_active:
                return False
            state = None
        if persist and self.session_controller.current is not None and \
                self._composition is None:
            if self.session_controller.update_review(
                    {'stabilization_preview': state}):
                self._schedule_scene_state_save()
        return True

    def _persist_tracking_record(self):
        """Only an actual authored track/edit changes portable Source state."""
        source = self.session_controller.current
        if (source is None or self.info is None or not self.tracking_enabled or
                self._restoring_review or self._applying_composition or
                self._applying_tracked):
            return False
        from .tracking_state import freeze_tracking
        try:
            record = freeze_tracking(
                self.info, source.id, self.tracked_centers, self.tracked_quads,
                self.tracking_size, self.tracking_base_size,
                (self.tracking_offset.x(), self.tracking_offset.y()),
                self.tracking_mode.currentData(),
                self.act_flip_h.isChecked(), self.act_flip_v.isChecked(),
                quality=self.tracked_quality)
        except (ValueError, TypeError, KeyError) as exc:
            self.status_lbl.setText('Tracking not saved: {}'.format(exc))
            return False
        changed = self.session_controller.update_review(
            {'tracking_v1': record}, source_id=source.id)
        if changed:
            self._schedule_scene_state_save()
        return changed

    def _restore_tracking_record(self, source_id, record):
        """Rebind persisted keys only to exact present signed source frames."""
        if self.info is None or self.session_controller.current is None or \
                self.session_controller.current.id != source_id:
            return False
        from .tracking_state import restore_tracking
        try:
            data = restore_tracking(
                self.info, source_id, record,
                self.act_flip_h.isChecked(), self.act_flip_v.isChecked())
        except (ValueError, TypeError, KeyError) as exc:
            self.tracking_lbl.setText('Saved track unavailable')
            self.tracking_lbl.setToolTip(str(exc))
            return False
        old = self.tracking_mode.blockSignals(True)
        try:
            index = self.tracking_mode.findData(data['mode'])
            if index < 0:
                return False
            self.tracking_mode.setCurrentIndex(index)
        finally:
            self.tracking_mode.blockSignals(old)
        self.tracked_centers = data['centers']
        self.tracked_quads = data['quads']
        self.tracked_quality = data.get('quality', {})
        self.tracking_size = data['crop_size']
        self.tracking_base_size = data['base_size']
        self.tracking_offset = QtCore.QPointF(*data['offset'])
        self.tracking_enabled = True
        self.tracking_lbl.setToolTip('Source-frame mapped; no nearest-frame guessing.')
        self.tracking_lbl.setText('{} saved source-frame tracks'.format(
            len(data['centers'])))
        self._apply_tracked_crop()
        self._refresh_tracking_quality()
        return True

    def _cancel_tracking_job(self):
        job = self._tracking_job
        self._tracking_job = None
        if job is None:
            return
        try:
            if job.isRunning():
                job.requestInterruption()
                if hasattr(job, 'stop_now'):
                    job.stop_now()
        except RuntimeError:
            pass  # Already deleted by Qt's finished/deleteLater lifecycle.
        for signal in (job.progress, job.result, job.error,
                       getattr(job, 'quality_ready', None)):
            if signal is None:
                continue
            try:
                signal.disconnect()
            except (RuntimeError, TypeError):
                pass
        self._task_end('track')

    def _clear_tracking(self, update=True):
        if self._stabilization_plan is not None:
            self._set_stabilization_preview(False, persist=update)
        was_tracked = bool(self.tracking_enabled)
        source = self.session_controller.current
        had_saved_track = bool(source is not None and
                               source.review.get('tracking_v1'))
        if update:
            self._cancel_tracking_job()
            if not self._closed:
                self.track_btn.setEnabled(self.info is not None and tracking.HAVE_CV2)
                self.tracking_mode.setEnabled(tracking.HAVE_CV2)
        self.tracking_enabled = False
        self.tracked_centers = {}
        self.tracked_quads = {}
        self.tracked_quality = {}
        self.tracking_size = None
        self.tracking_base_size = None
        self.tracking_offset = QtCore.QPointF()
        if self.tracking_lbl is not None:
            self.tracking_lbl.setText('No tracking')
        if hasattr(self.surface, 'set_tracked_quad'):
            self.surface.set_tracked_quad(None)
        if update and (was_tracked or had_saved_track) and \
                not self._restoring_review and not self._applying_composition and \
                source is not None:
            if self.session_controller.update_review({'tracking_v1': None}):
                self._schedule_scene_state_save()

    def _nearest_center(self, index):
        if not self.tracked_centers:
            return None
        keys = sorted(self.tracked_centers)
        pos = bisect.bisect_left(keys, int(index))
        if pos == 0:
            key = keys[0]
        elif pos == len(keys):
            key = keys[-1]
        else:
            before, after = keys[pos - 1], keys[pos]
            key = before if index - before <= after - index else after
        return self.tracked_centers[key]

    def _nearest_quad(self, index):
        if not self.tracked_quads:
            return None
        key = min(self.tracked_quads, key=lambda number: abs(number - index))
        return self.tracked_quads[key]

    def _transformed_quad(self, index):
        """The tracked quad with the user's later crop move/resize applied."""
        quad = self._nearest_quad(index)
        if not quad:
            return None
        center = self._nearest_center(index)
        if center is None:
            return None
        base_w, base_h = self.tracking_base_size or self.tracking_size or (1, 1)
        w, h = self.tracking_size or (base_w, base_h)
        sx = w / float(max(1, base_w))
        sy = h / float(max(1, base_h))
        return [(center[0] + (x - center[0]) * sx + self.tracking_offset.x(),
                 center[1] + (y - center[1]) * sy + self.tracking_offset.y())
                for x, y in quad]

    def _update_tracking_overlay(self):
        if not hasattr(self.surface, 'set_tracked_quad'):
            return
        if self.tracking_enabled and self.tracking_mode.currentData() != 1:
            self.surface.set_tracked_quad(
                self._transformed_quad(self.player.current_frame))
        else:
            self.surface.set_tracked_quad(None)

    def _refresh_tracking_quality(self):
        """Show real per-frame tracking health, distinguishing held lost keys."""
        if not self.tracking_enabled or not self.tracked_quality:
            return
        frame = self.player.current_frame
        reading = self.tracked_quality.get(frame)
        if reading is None:
            return
        original = (self.info.frame_mapping().display_to_source(frame)
                    if self.info is not None else frame)
        if reading.get('lost'):
            label = 'Tracking LOST · source {} · held last valid pose'.format(original)
        elif reading.get('status') == 'manual':
            label = 'Manual track correction · source {}'.format(original)
        elif reading.get('status') == 'reference':
            label = 'Track reference · source {} · no motion measured'.format(original)
        else:
            confidence = reading.get('confidence')
            label = 'Tracking · source {} · confidence {}'.format(
                original, '{:.0%}'.format(confidence) if confidence is not None
                else 'unavailable')
        self.tracking_lbl.setText(label)
        self.tracking_lbl.setToolTip(
            'Optical-flow measurements, not estimated accuracy/probability:\n' +
            json.dumps(reading, sort_keys=True, indent=2, default=str))

    def _apply_tracking_revision(self, source_id, saved):
        """Undo one explicit correction without crossing logical Source IDs."""
        source = self.session_controller.current
        if source is None or source.id != source_id or self.info is None:
            return
        self._clear_tracking(update=False)
        changed = self.session_controller.update_review(
            {'tracking_v1': copy.deepcopy(saved)}, source_id=source_id)
        if saved:
            self._restore_tracking_record(source_id, saved)
        if changed:
            self._schedule_scene_state_save()

    def _correct_current_track_frame(self):
        """Bake a manual translation key; do not interpolate unknown motion."""
        if not self.tracking_enabled or self.info is None:
            return
        source = self.session_controller.current
        frame = self.player.current_frame
        if (source is None or self.tracking_mode.currentData() != 1 or
                frame not in self.tracked_centers or
                frame not in self.tracked_quads or
                frame not in self.tracked_quality):
            self.status_lbl.setText(
                'Manual correction requires a measured translation track '
                'at the current source frame.')
            return
        delta = (float(self.tracking_offset.x()),
                 float(self.tracking_offset.y()))
        if max(abs(delta[0]), abs(delta[1])) < 1e-8:
            self.status_lbl.setText(
                'Move the tracked crop first to define a manual correction.')
            return
        if self._stabilization_plan is not None:
            self._set_stabilization_preview(False)
        before = copy.deepcopy(source.review.get('tracking_v1'))
        if not before:
            self.status_lbl.setText('Save the track before correcting a key.')
            return
        previous_centers = copy.deepcopy(self.tracked_centers)
        previous_quads = copy.deepcopy(self.tracked_quads)
        previous_quality = copy.deepcopy(self.tracked_quality)
        old_offset = QtCore.QPointF(self.tracking_offset)
        self.tracked_centers[frame] = (
            previous_centers[frame][0] + delta[0],
            previous_centers[frame][1] + delta[1])
        self.tracked_quads[frame] = [
            (x + delta[0], y + delta[1])
            for x, y in previous_quads[frame]]
        self.tracking_offset = QtCore.QPointF()
        reading = dict(self.tracked_quality[frame])
        reading.update(status='manual', lost=False, confidence=None)
        self.tracked_quality[frame] = reading
        if not self._persist_tracking_record():
            self.tracked_centers = previous_centers
            self.tracked_quads = previous_quads
            self.tracked_quality = previous_quality
            self.tracking_offset = old_offset
            self._apply_tracked_crop()
            self.status_lbl.setText('Manual correction not accepted; track unchanged.')
            return
        after = copy.deepcopy(source.review['tracking_v1'])
        self._undo.push(
            lambda sid=source.id, state=before:
                self._apply_tracking_revision(sid, state),
            lambda sid=source.id, state=after:
                self._apply_tracking_revision(sid, state))
        self._apply_tracked_crop()
        self._refresh_tracking_quality()
        self.status_lbl.setText(
            'Manual correction set for source frame {} only; other '
            'frames remain unmodified.'.format(
                self.info.frame_mapping().display_to_source(frame)))

    def _apply_tracked_crop(self):
        fallback = self.surface.crop()
        if fallback is None:
            return
        center = self._nearest_center(self.player.current_frame)
        if center is None:
            return
        w, h = self.tracking_size or (fallback[2], fallback[3])
        quad = self._transformed_quad(self.player.current_frame)
        if self.tracking_mode.currentData() != 1 and quad:
            cx = (min(p[0] for p in quad) + max(p[0] for p in quad)) / 2.0
            cy = (min(p[1] for p in quad) + max(p[1] for p in quad)) / 2.0
            w = int(utils.clamp(round(max(p[0] for p in quad) -
                                        min(p[0] for p in quad)), 2, self.info.width))
            h = int(utils.clamp(round(max(p[1] for p in quad) -
                                        min(p[1] for p in quad)), 2, self.info.height))
        else:
            cx = center[0] + self.tracking_offset.x()
            cy = center[1] + self.tracking_offset.y()
        x = int(utils.clamp(round(cx - w / 2.0), 0, self.info.width - w))
        y = int(utils.clamp(round(cy - h / 2.0), 0, self.info.height - h))
        self._applying_tracked = True
        try:
            self.surface.set_crop((x, y, w, h))
        finally:
            self._applying_tracked = False
        self._update_tracking_overlay()

    def _track_forward(self):
        crop = self.surface.crop()
        if crop is None or self.info is None:
            return
        start, end = self.player.current_frame, self.player.out_frame
        if end <= start:
            return
        # Validate the new crop/plate range BEFORE pausing playback or
        # cancelling an existing track. Refused requests are no-ops, not
        # navigation mutations, and cannot needlessly disturb an artist.
        job = TrackingJob(
            self.ffmpeg, self.info.path, self.info.sequence_files, self.info.fps,
            start, end, crop, self.act_flip_h.isChecked(), self.act_flip_v.isChecked(),
            QtCore.QCoreApplication.instance(), mode=self.tracking_mode.currentData(),
            frame_size=(self.info.width, self.info.height),
            frame_numbers=(self.info.frame_numbers if self.info.is_sequence else None),
            existing_source_frames=(self.info.sequence_source_frames
                                    if self.info.is_sequence else None))
        try:
            job.validate_range()
        except (OSError, ValueError, IndexError) as exc:
            self.tracking_lbl.setText('Tracking not started: unsafe frame range')
            self.status_lbl.setText('Tracking unavailable: {}'.format(exc))
            job.deleteLater()
            return
        if self._stabilization_plan is not None:
            self._set_stabilization_preview(False)
        self.player.pause()
        self._cancel_tracking_job()
        self.tracked_quality = {}
        # A cancelled reader can finish after this window is gone.
        self._tracking_job = job
        job.progress.connect(
            lambda p: (self.tracking_lbl.setText('Tracking… {}%'.format(p)),
                       self._task_update('track', p))
            if self._tracking_job is job and not self._closed else None)
        job.result.connect(
            lambda centers, size, quads: self._on_tracking_result(centers, size, quads)
            if self._tracking_job is job and not self._closed else None)
        if hasattr(job, 'quality_ready'):
            job.quality_ready.connect(
                lambda values: self._on_tracking_quality(job, values))
        job.error.connect(
            lambda m: (self.tracking_lbl.setText('Tracking failed'),
                       self.status_lbl.setText('Tracking failed: ' + html.escape(m)))
            if self._tracking_job is job and not self._closed else None)
        job.finished.connect(
            lambda: self._on_tracking_job_finished(job))
        job.finished.connect(job.deleteLater)
        job.start()
        self.track_btn.setEnabled(False)
        self.tracking_mode.setEnabled(False)
        self._task_begin('track', 'Tracking')
        self.status_lbl.setText('Tracking crop…')

    def _on_tracking_job_finished(self, job):
        if self._tracking_job is not job:
            return
        self._tracking_job = None
        self._task_end('track')
        if not self._closed:
            self.track_btn.setEnabled(self.info is not None and tracking.HAVE_CV2)
            self.tracking_mode.setEnabled(tracking.HAVE_CV2)

    def _on_tracking_quality(self, job, values):
        if self._tracking_job is not job or self._closed:
            return
        self.tracked_quality = copy.deepcopy(values) if isinstance(values, dict) else {}

    def _on_tracking_result(self, centers, crop_size, quads=None):
        if self._closed or self._tracking_job is None:
            return
        self.tracked_centers = centers
        self.tracked_quads = quads or {}
        self.tracking_size = crop_size
        self.tracking_base_size = crop_size
        self.tracking_offset = QtCore.QPointF()
        self.tracking_enabled = bool(centers)
        self.tracking_lbl.setText('{} tracked frames · {}'.format(
            len(centers), self.tracking_mode.currentText()))
        self._apply_tracked_crop()
        self._persist_tracking_record()
        self._refresh_tracking_quality()
        lost = sum(bool(value.get('lost'))
                   for value in self.tracked_quality.values())
        self.status_lbl.setText(
            'Tracking complete: {} frames, {} motion losses (held). '
            'Move/resize crop to offset it.'.format(len(centers), lost))

    # ----------------------------------------------------------- exporting
    def _parse_out_res(self):
        try:
            w, h = int(self.out_w_le.text()), int(self.out_h_le.text())
            if w < 2 or h < 2:
                raise ValueError
            return w, h
        except ValueError:
            raise ValueError('Enter a valid output resolution.')

    # ------------------------------------------------- status-bar progress
    def _task_begin(self, name, label, determinate=True):
        """Register a running task so the status bar shows its progress.

        ``determinate`` tasks report 0-100 via :meth:`_task_update`; otherwise
        the bar runs in busy mode until :meth:`_task_end`. Tasks reference-count,
        so concurrent work (thumbnails + waveform) shares the one bar cleanly.
        """
        self._tasks[name] = {'label': label, 'value': 0, 'determinate': determinate}
        if name not in self._task_order:
            self._task_order.append(name)
        self._task_refresh()

    def _task_update(self, name, value):
        task = self._tasks.get(name)
        if task is None:
            return
        task['value'] = int(utils.clamp(value, 0, 100))
        task['determinate'] = True
        self._task_refresh()

    def _task_end(self, name):
        self._tasks.pop(name, None)
        if name in self._task_order:
            self._task_order.remove(name)
        self._task_refresh()

    def _task_refresh(self):
        if self._closed:
            return
        active = [n for n in self._task_order if n in self._tasks]
        if not active:
            self.progress.reset()
            self.progress.setFormat('')
            self.progress.hide()
            return
        # Prefer a determinate task to show a real percentage; fall back to the
        # first busy one. A suffix notes any other task running at the same time.
        determinate = [n for n in active if self._tasks[n]['determinate']]
        name = determinate[0] if determinate else active[0]
        task = self._tasks[name]
        suffix = '  (+{})'.format(len(active) - 1) if len(active) > 1 else ''
        if task['determinate']:
            self.progress.setRange(0, 100)
            self.progress.setValue(task['value'])
            self.progress.setFormat('{}{}  %p%'.format(task['label'], suffix))
        else:
            self.progress.setRange(0, 0)  # busy / indeterminate
            self.progress.setFormat('{}{}'.format(task['label'], suffix))
        self.progress.show()

    def _on_thumbs_started(self, total):
        self._thumb_total = max(1, int(total))
        self._thumb_done = 0
        self._task_begin('thumbs', 'Timeline thumbnails')

    def _on_thumb_produced(self):
        self._thumb_done += 1
        self._task_update('thumbs', 100.0 * self._thumb_done / self._thumb_total)

    def _init_persistent_exports(self):
        """Attach durable source-stage exports to the existing ExportJob.

        The journal is deliberately outside the Maya scene/session. Pending
        work persists between launches; interrupted exports need an explicit
        Retry. A corrupt or contested journal is never reset or overwritten.
        """
        destination = os.environ.get('FRAMEREVIEWER_EXPORT_QUEUE') or os.path.join(
            os.path.expanduser('~'), '.framereviewer', 'exports', 'queue.json')
        try:
            self._export_queue_store = ExportQueueStore(destination)
            self._export_runner = ExportQueueRunner(
                self._export_queue_store,
                existing_export_job_factory(
                    self.ffmpeg, parent=QtCore.QCoreApplication.instance()),
                on_change=self._on_export_task_change,
                on_idle=self._on_export_queue_idle,
                ffprobe=self.ffprobe)
        except (OSError, IOError, TypeError, ValueError, RuntimeError) as exc:
            self._export_queue_store = self._export_runner = None
            self.export_queue_list.addItem('Queue unavailable: {}'.format(exc))
        self._refresh_export_queue_ui()

    def _refresh_export_queue_ui(self):
        store = self._export_queue_store
        if store is None:
            for control in (self.export_queue_start_btn,
                            self.export_queue_cancel_btn,
                            self.export_queue_retry_btn,
                            self.export_queue_open_btn):
                control.setEnabled(False)
            return
        current = self._selected_export_id()
        self.export_queue_list.blockSignals(True)
        try:
            self.export_queue_list.clear()
            for task in reversed(store.tasks()):
                label = '{} · {} · {}%'.format(
                    os.path.basename(task.spec.path), task.state.title(),
                    task.progress)
                item = QtWidgets.QListWidgetItem(label)
                item.setData(QtCore.Qt.UserRole, task.id)
                item.setToolTip(task.error or task.spec.path)
                self.export_queue_list.addItem(item)
                if task.id == current:
                    self.export_queue_list.setCurrentItem(item)
        finally:
            self.export_queue_list.blockSignals(False)
        self._update_export_queue_actions()

    def _selected_export_id(self):
        current = self.export_queue_list.currentItem()
        return (current.data(QtCore.Qt.UserRole) if current is not None else None)

    def _update_export_queue_actions(self):
        store = self._export_queue_store
        runner = self._export_runner
        if store is None or runner is None:
            return
        task_id = self._selected_export_id()
        task = store.get(task_id) if task_id else None
        self.export_queue_start_btn.setEnabled(
            runner.active_id is None and store.next_pending() is not None)
        self.export_queue_cancel_btn.setEnabled(
            task is not None and task.state in ('pending', 'running', 'committing'))
        self.export_queue_retry_btn.setEnabled(
            task is not None and task.state in ('failed', 'crashed', 'cancelled'))
        self.export_queue_open_btn.setEnabled(
            task is not None and task.state == 'succeeded')

    def _on_export_task_change(self, task):
        if self._closed:
            return
        self._refresh_export_queue_ui()
        runner = self._export_runner
        self._export_job = runner.active_job if runner else None
        if task.state in ('running', 'committing', 'cancelling'):
            self._task_begin('export', 'Exporting') if 'export' not in self._tasks else None
            self._task_update('export', task.progress)
        elif task.state == 'failed':
            self.status_lbl.setText('Export failed: {}'.format(task.error))
        elif task.state == 'succeeded':
            self.status_lbl.setText('Export verified: {}'.format(task.spec.path))

    def _on_export_queue_idle(self):
        if self._closed:
            return
        self._export_job = None
        self._task_end('export')
        self._refresh_export_queue_ui()

    def _start_export_queue(self):
        if self._export_runner is None:
            return
        try:
            self._export_runner.start_next()
            self._update_export_queue_actions()
        except (RuntimeError, ValueError, OSError) as exc:
            self.status_lbl.setText('Cannot start export queue: {}'.format(exc))

    def _cancel_queued_export(self):
        tid = self._selected_export_id()
        if tid and self._export_runner is not None:
            try:
                self._export_runner.cancel(tid)
            except (RuntimeError, ValueError) as exc:
                self.status_lbl.setText('Cannot cancel export: {}'.format(exc))

    def _retry_queued_export(self):
        tid = self._selected_export_id()
        if tid and self._export_runner is not None:
            try:
                self._export_runner.retry(tid, start=True)
            except (RuntimeError, ValueError, OSError) as exc:
                self.status_lbl.setText('Cannot retry export: {}'.format(exc))

    def _reveal_queued_export(self):
        tid = self._selected_export_id()
        if not tid or self._export_queue_store is None:
            return
        task = self._export_queue_store.get(tid)
        if task.state != 'succeeded':
            return
        folder = os.path.dirname(task.spec.path)
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(folder))

    def _composite_export_snapshot(self):
        """Freeze each actual playlist member, rather than preview-only pixels.

        The managed renderer receives immutable source frame maps, distinct
        input color profiles and the same rational member clock/mode as the
        CompositeSource. Never infer identity from a repeated media filename.
        """
        from fractions import Fraction
        composition = self._composition
        members = composition.visible_members if composition is not None else ()
        if composition is None or not 2 <= len(members) <= 8:
            raise ValueError('Managed composition requires 2–8 source members.')
        is_record = composition.mode == composition_mod.SEQUENCE
        include_record_audio = bool(is_record and
                                    self.export_record_audio_cb.isChecked())
        saved_audio_routes = {}
        if include_record_audio:
            # Stale persisted audio routes cannot silently become a different
            # repeated Source's sound merely because filenames still match.
            saved_audio_routes = self._saved_record_audio_routes()
        if is_record:
            # Actual EDL/OTIO composition describes edit occurrences, not a
            # path list. v4 supports hard cuts/gaps only; never interpret
            # overlapping segments as silently flattened transitions.
            segments = composition.segments()
            previous_end = -1
            for segment in segments:
                if (segment.start <= previous_end or segment.start < 0 or
                        segment.end >= 120):
                    raise ValueError('Managed EDL/OTIO record output supports '
                                     'only ordered, nonoverlapping hard cuts '
                                     'within 120 record frames; transitions '
                                     'require separate verified delivery.')
                previous_end = segment.end
        if self.surface.crop() or self.act_flip_h.isChecked() or self.act_flip_v.isChecked():
            raise ValueError('Managed composite export with crop or flip is not '
                             'yet color/geometry verified. Clear crop and flips first.')
        if self.surface.image_geometry().rotation:
            raise ValueError('Reset display-only view rotation before managed '
                             'composite output; output rotation is not supported.')
        if self.tracking_enabled or self.player.speed != 1:
            raise ValueError('Tracking and speed changes are not yet supported '
                             'for managed comparisons.')
        session_rate = (getattr(getattr(self.player, '_source', None),
                                '_session_fps', None) or self.info.fps)
        rate = Fraction(session_rate).limit_denominator(1001000)
        snapshots = []
        physical_par_required = False
        mixed_movie_required = False
        record_media = []
        audio_routes = []
        for index, member in enumerate(members):
            logical = self.session_controller.session.source(member.source_id)
            if logical is None:
                raise ValueError('Comparison member {} is missing its stable Source.'
                                 .format(member.source_id))
            path = self.session_controller.resolved_path(logical)
            if not path or not os.path.isfile(path):
                raise ValueError('Comparison Source {} is not an available file.'
                                 .format(member.source_id))
            info = ffmpegtools.probe(self.ffprobe, QtGui.QImageReader, path)
            if is_record:
                record_media.append(info)
            selected_audio = ((logical.audio or {}).get('route') or {}) if \
                include_record_audio else {}
            if not isinstance(selected_audio, dict):
                raise ValueError('Record audio route metadata is not a verified mapping.')
            occurrence_override = None
            if (include_record_audio and
                    self._record_audio_override_key == self._record_audio_edit_key()):
                occurrence_override = self._record_audio_overrides.get(index)
            elif include_record_audio:
                occurrence_override = saved_audio_routes.get(index)
            if occurrence_override:
                # An explicit edit choice is a one-off export intent. Do not
                # mutate the repeated Source's global playback audio route.
                kind = occurrence_override.get('kind')
                if kind == 'external':
                    selected_audio = dict(
                        external_path=occurrence_override.get('external_path', ''),
                        stream_index=0, delay_ms=0, output_layout='source')
                elif kind == 'embedded':
                    selected_audio = dict(
                        external_path='',
                        stream_index=occurrence_override['stream_index'],
                        delay_ms=0, output_layout='source')
                elif kind == 'silence':
                    selected_audio = {}
                else:
                    raise ValueError('Unknown per-edit record audio override.')
            external_audio = selected_audio.get('external_path') or ''
            if include_record_audio and (
                    selected_audio.get('delay_ms', 0) != 0 or
                    selected_audio.get('output_layout', 'source') not in
                    ('source', 'stereo')):
                raise ValueError('Record audio does not support independent '
                                 'delay or programmable output routing.')
            if external_audio and (not isinstance(external_audio, str) or
                                   not os.path.isabs(external_audio) or
                                   os.path.splitext(external_audio)[1].lower() != '.wav'):
                raise ValueError('Record audio does not support external files '
                                 'unless they are independently proven absolute '
                                 'zero-origin PCM WAV sources.')
            movie_proof = None
            if not info.is_sequence:
                from ..exporting.spec import freeze_managed_composite_movie_proof
                allow_movie_audio = include_record_audio
                if include_record_audio and external_audio:
                    # External sound is not evidence of any embedded movie
                    # stream. Only request the strict v2 movie-audio proof
                    # when the source actually contains an audio stream.
                    from ..playback.audio_routing import probe_audio_streams
                    allow_movie_audio = bool(probe_audio_streams(
                        self.ffprobe, os.path.abspath(path)))
                movie_proof = freeze_managed_composite_movie_proof(
                    os.path.abspath(path), ffprobe=self.ffprobe,
                    allow_audio=allow_movie_audio)
                mixed_movie_required = True
            if info.is_sequence and (info.declared_frames is None and
                    logical.timing.get('declared_frames') is not None):
                info = info.with_authored_timeline(logical.timing['declared_frames'])
            if info.is_sequence:
                info = info.with_missing_policy(
                    (logical.review or {}).get('missing_frame_policy', 'skip'))
                exr_issue = self._unsafe_exr_delivery(info)
                if exr_issue:
                    raise ValueError('Member {} cannot be color-correctly exported: {}'.format(
                        member.source_id, exr_issue))
            if (logical.review or {}).get('view_rotation', 0):
                raise ValueError('Managed comparison cannot export rotated member {}.'
                                 .format(member.source_id))
            par = Fraction(str(info.pixel_aspect_ratio)).limit_denominator(1000000)
            physical_par_required = physical_par_required or par != 1
            if len(info.frame_numbers) > 1000000:
                raise ValueError('Comparison source timeline is too large.')
            color_state = self._effective_color_state(logical)
            frozen_color = color_state.to_dict()
            # The supported ColorPipelineState includes alpha association;
            # older source adapters can expose it only as an attribute. Freeze
            # a real declared mode rather than raising KeyError or guessing
            # that an alpha-bearing native EXR is opaque.
            alpha_association = frozen_color.get(
                'alpha_mode', getattr(color_state, 'alpha_mode', None))
            if alpha_association not in ('opaque', 'straight', 'premultiplied'):
                raise ValueError('Comparison Source {} has no verified alpha '
                                 'association.'.format(member.source_id))
            frozen_color['alpha_mode'] = alpha_association
            if movie_proof is not None:
                # One verified opaque YUV video can have a source-owned alpha
                # mode defaulted to 'straight'. No alpha actually exists in
                # these supported formats; freeze opaque explicitly, not an
                # invented transparent movie channel. Artist confirmation is
                # required below before this plan can be enqueued.
                frozen_color['alpha_mode'] = 'opaque'
                labels = logical.frame_mapping().source_frames
                if len(labels) != movie_proof['frame_count'] or any(
                        b != a + 1 for a, b in zip(labels, labels[1:])):
                    raise ValueError('Movie {} has a non-consecutive or stale '
                                     'source frame mapping; refresh its Source '
                                     'metadata before export.'.format(member.source_id))
            else:
                labels = list(info.frame_numbers)
            member_dict = member.to_dict()
            member_dict.update(
                # `CompositionMember.frame_count` describes RECORD duration in
                # EDL edits. The underlying Source length is distinct and
                # stays identical across repeated edit occurrences.
                frame_count=len(labels),
                source_path=os.path.abspath(info.path),
                width=info.width, height=info.height,
                pixel_aspect=('{}/{}'.format(par.numerator, par.denominator)
                              if par != 1 or movie_proof is not None else 1),
                seq_files=([os.path.abspath(p) for p in info.sequence_files]
                           if info.is_sequence else []),
                frame_numbers=list(labels),
                existing_frame_numbers=(list(info.sequence_source_frames)
                                        if info.is_sequence else []),
                source_fps=(movie_proof['avg_frame_rate'] if movie_proof else
                            str(Fraction(*info.fps_rational)
                                if info.fps_rational else
                                Fraction(str(info.fps)).limit_denominator(1001000))),
                missing_frame_policy=(info.missing_policy if info.is_sequence
                                      else 'hold'),
                alpha_mode=frozen_color['alpha_mode'], color=frozen_color)
            if movie_proof is not None:
                stream_yuv = {'bt709': 'bt709', 'bt470bg': 'bt601',
                              'smpte170m': 'bt601', 'bt2020nc': 'bt2020'}
                member_dict.update(
                    input_kind='movie', movie_proof=movie_proof,
                    movie_color_provenance={
                        'confirmed': False, 'origin': 'unconfirmed',
                        'input_space': frozen_color['input_space'],
                        'alpha_mode': 'opaque',
                        'yuv_matrix': stream_yuv[movie_proof['color_space']],
                        'yuv_range': ('full' if movie_proof['color_range'] == 'pc'
                                      else 'limited')})
            else:
                member_dict['input_kind'] = 'image_sequence'
            if is_record:
                segment = segments[index]
                # An EDL occurrence may use a different clip/event title for
                # the same Source. Freeze stable Source metadata separately
                # from occurrence_id, preventing a harmless different clip
                # label from appearing to change its pixel/color identity.
                member_dict['name'] = logical.name
                expected_speed = Fraction(member_dict['source_fps']) / rate
                frozen_speed = (member.rate if member.rate is not None
                                else expected_speed)
                if frozen_speed != expected_speed:
                    raise ValueError('Managed record export does not support '
                                     'retimed edit {}. Its source frames per '
                                     'record frame must match the actual '
                                     'source/session FPS rational.'.format(index))
                member_dict.update(
                    occurrence_id='record-edit-{:03d}-{}'.format(
                        index, member.source_id),
                    session_offset=segment.start,
                    record_length=segment.count,
                    rate='{}/{}'.format(frozen_speed.numerator,
                                        frozen_speed.denominator))
                if include_record_audio:
                    if occurrence_override and occurrence_override['kind'] == 'silence':
                        if movie_proof is not None:
                            raise ValueError('Movie edit audio cannot be silently '
                                             'dropped; only image edits may '
                                             'select explicit record silence.')
                        audio_routes.append(dict(
                            occurrence_id=member_dict['occurrence_id'],
                            kind='silence'))
                    elif external_audio:
                        from ..exporting.external_audio_proof import (
                            freeze_external_record_audio_proof)
                        proof = freeze_external_record_audio_proof(
                            external_audio, ffprobe=self.ffprobe)
                        if selected_audio.get('stream_index', 0) != 0:
                            raise ValueError('Record external PCM WAV has only '
                                             'one verified audio stream (ordinal zero).')
                        audio_routes.append(dict(
                            occurrence_id=member_dict['occurrence_id'],
                            kind='external', external_path=external_audio,
                            audio_ordinal=0, stream_index=0,
                            external_proof=proof))
                    elif movie_proof is None:
                        # Image plates contain no synchronized soundtrack.
                        # Their silence is an explicit part of the requested
                        # record output, not an inferred missing movie track.
                        audio_routes.append(dict(
                            occurrence_id=member_dict['occurrence_id'],
                            kind='silence'))
                    else:
                        ordinal = selected_audio.get('stream_index', 0)
                        tracks = movie_proof.get('audio_tracks') or []
                        if (type(ordinal) is not int or ordinal < 0 or
                                ordinal >= len(tracks)):
                            raise ValueError('Record edit {} lacks its selected '
                                             'verified embedded audio ordinal.'
                                             .format(index + 1))
                        audio_routes.append(dict(
                            occurrence_id=member_dict['occurrence_id'],
                            kind='embedded', audio_ordinal=ordinal,
                            stream_index=tracks[ordinal]['stream_index']))
            for window_key in ('data_window', 'display_window'):
                if (isinstance(info.metadata, dict) and
                        info.metadata.get(window_key) is not None):
                    member_dict[window_key] = info.metadata[window_key]
            snapshots.append(member_dict)
        if is_record:
            from ..media.delivery_gate import validate_record_delivery
            validate_record_delivery(
                self.session_controller.session, composition, record_media)
            # A precision v4 record preview is only authoritative while the
            # actual source-backed frame PRESENTED in the Maya viewport still
            # equals the unchanged original EXR on disk. The durable worker
            # separately freezes every output-range source; this UI gate
            # certifies only the currently presented artist-reviewed frame,
            # never pretends that an unviewed range was visually inspected.
            # True gaps/outside-trim cards have no original HDR sample to hash.
            from ..playback.composite_source import CompositeSource
            active = getattr(self.player, '_source', None)
            if (isinstance(active, CompositeSource) and
                    active.composition is composition and
                    getattr(active, '_record_preview_v4', False)):
                current_frame = int(self.player.current_frame)
                address = active.record_frame_identity(current_frame)
                occurrence = address.get('occurrence_index')
                mapped = address.get('decode_index')
                if type(occurrence) is int and type(mapped) is int:
                    pixmap = self.surface.frame_item.pixmap()
                    receipt = self._comparison_presented_receipt
                    original_proof = active.verified_record_file_proof(
                        current_frame)
                    correct_disk_source = False
                    if (isinstance(original_proof, dict) and
                            0 <= occurrence < len(snapshots)):
                        member_proof = snapshots[occurrence]
                        labels = list(member_proof.get(
                            'existing_frame_numbers') or [])
                        files = list(member_proof.get('seq_files') or [])
                        real_label = original_proof.get('source_frame')
                        if (len(labels) == len(files) and
                                real_label in labels and
                                original_proof.get('member_source_id') ==
                                members[occurrence].source_id):
                            frozen_file = files[labels.index(real_label)]
                            correct_disk_source = (
                                os.path.realpath(os.path.abspath(frozen_file)) ==
                                original_proof.get('path'))
                    if not (correct_disk_source and receipt is not None and
                            receipt[0] is active and receipt[1] == current_frame and
                            not pixmap.isNull() and
                            receipt[2] == int(pixmap.cacheKey())):
                        raise ValueError(
                            'Record Working preview versus managed delivery '
                            'requires the currently presented native EXR frame '
                            'AND its unchanged SHA-proven original Source. '
                            'A changed, stale, evicted or display-only plate '
                            'must be explicitly reloaded and presented before '
                            'another verified record export.')
        # A width/height mismatch is a genuine native PHYSICAL field mismatch
        # even when both plates declare PAR=1 or the same non-square ratio.
        # Legacy v1 lacks per-member source_geometry and stretches both
        # rasters to the entire output field; the live CPU precision viewer
        # instead preserves centred member coverage. Select independently
        # probed v2 physical_field_v1 for ALL image-only comparisons with
        # different native extents, including A/B/Flicker and Wipe/Grid.
        # Do not change the v4 record or v3 mixed movie contracts.
        if not is_record and not mixed_movie_required and snapshots:
            first_size = (int(snapshots[0]['width']),
                          int(snapshots[0]['height']))
            physical_par_required = physical_par_required or any(
                (int(item['width']), int(item['height'])) != first_size
                for item in snapshots[1:])
        if (not is_record and composition.mode in (
                composition_mod.A_B, composition_mod.FLICKER) and
                len({(int(item['width']), int(item['height']),
                      Fraction(str(item['pixel_aspect'])))
                     for item in snapshots}) > 1):
            # Different native dimensions are a geometry mismatch EVEN if PAR
            # ratios agree: the old raw selected-source preview stretches the
            # narrower plate, while v1/v2 Working delivery may use reference
            # physical coverage. It is safe to queue ONLY after
            # this exact comparison object has genuinely presented an owned
            # Working float reference-field raster for the CURRENT frame.
            # Capability alone or a requested seek is never a presentation
            # receipt; a Qt-only fallback cannot prove precision pixel parity.
            from ..playback.composite_source import CompositeSource
            from ..core.frame_payload import FramePayload
            active = getattr(self.player, '_source', None)
            receipt = self._comparison_presented_receipt
            pixmap = self.surface.frame_item.pixmap()
            current_frame = int(self.player.current_frame)
            original = (active._precision_cache.get(current_frame)
                        if isinstance(active, CompositeSource) else None)
            source_proof = (active.verified_selected_file_proof(current_frame)
                            if isinstance(active, CompositeSource) and
                            callable(getattr(active, 'verified_selected_file_proof', None))
                            else None)
            proof_matches = False
            if isinstance(source_proof, dict):
                selected = source_proof.get('selected_member_index')
                if type(selected) is int and 0 <= selected < len(snapshots):
                    chosen = snapshots[selected]
                    real_labels = list(chosen.get('existing_frame_numbers') or [])
                    real_files = list(chosen.get('seq_files') or [])
                    native_label = source_proof.get('source_frame')
                    if (source_proof.get('member_source_id') ==
                            members[selected].source_id and
                            len(real_labels) == len(real_files) and
                            native_label in real_labels):
                        path = real_files[real_labels.index(native_label)]
                        proof_matches = (os.path.realpath(os.path.abspath(path)) ==
                                         source_proof.get('path'))
            parity_proven = (
                isinstance(active, CompositeSource) and
                active.composition is composition and
                bool(getattr(active, 'selected_physical_parity', False)) and
                proof_matches and
                isinstance(original, FramePayload) and
                original.stage == 'working' and
                bool(original.color_metadata.get('selected_member_rectified')) and
                receipt is not None and receipt[0] is active and
                receipt[1] == current_frame and not pixmap.isNull() and
                receipt[2] == int(pixmap.cacheKey()))
            if not parity_proven:
                raise ValueError('Nonidentical-geometry A/B or Flicker export requires '
                                 'an actually presented, precision-correct '
                                 'reference-field frame AND unchanged SHA-proven '
                                 'original Source bytes. Reload a modified '
                                 'Source and present the latest frame; stale, '
                                 'unavailable or Qt-only previews cannot '
                                 'enter the durable queue.')
        if mixed_movie_required or is_record:
            if physical_par_required:
                raise ValueError('Mixed movie / record-timeline delivery '
                                 'requires square-pixel members. Unequal PAR '
                                 'is only verified for native EXR comparisons.')
            for item in snapshots:
                if item['input_kind'] == 'image_sequence' and (
                        os.path.splitext(item['source_path'])[1].lower() != '.exr'
                        or any(os.path.splitext(filename)[1].lower() != '.exr'
                               for filename in item['seq_files'])):
                    raise ValueError('Mixed / record output requires native '
                                     'EXR image members (not PNG/TIFF) to prove '
                                     'per-source square pixel geometry.')
                item['pixel_aspect'] = '1/1'
        if is_record:
            size = (snapshots[0]['width'], snapshots[0]['height'])
            if any((item['width'], item['height']) != size for item in snapshots):
                raise ValueError('Managed hard-cut record output requires '
                                 'identical original source raster sizes; '
                                 'implicit per-cut stretching is unverified.')
        if physical_par_required:
            # Explicit opt-in v2: EXR windows/PAR are verified again against
            # EACH decoded source frame by the durable export worker.
            for member in snapshots:
                if (os.path.splitext(member['source_path'])[1].lower() != '.exr' or
                        any(os.path.splitext(path)[1].lower() != '.exr'
                            for path in member['seq_files'])):
                    raise ValueError(
                        'Unequal-PAR managed comparisons require every member '
                        'to be a native EXR image sequence. Member {} has '
                        'unverifiable PAR/window geometry.'.format(
                            member['source_id']))
                par = Fraction(str(member['pixel_aspect'])).limit_denominator(1000000)
                ratio = '{}/{}'.format(par.numerator, par.denominator)
                size = [member['width'], member['height']]
                full = [0, 0, size[0], size[1]]
                member['pixel_aspect'] = ratio
                member['source_geometry'] = {
                    'version': 1, 'size': size, 'pixel_aspect': ratio,
                    'data_window': list(member.get('data_window', full)),
                    'display_window': list(member.get('display_window', full)),
                    'rotation': 0}
        mode_keys = {
            'a_b': ('active',), 'flicker': ('flicker_frames',),
            'blend': ('blend',),
            'wipe': ('wipe', 'orientation'), 'split': ('orientation',),
            'checker': ('checker_size',), 'over': (), 'grid': (),
            'difference': (), 'inverted_difference': ()}
        frozen = {'version': (4 if is_record else
                            3 if mixed_movie_required else
                            2 if physical_par_required else 1),
                'geometry_model': (
                    'square_pixel_v1' if mixed_movie_required or is_record else
                    ('grid_stretch_v1' if composition.mode == 'grid'
                     else 'physical_field_v1') if physical_par_required else None),
                'mode': composition.mode,
                'parameters': ({} if is_record else
                               {key: copy.deepcopy(value)
                                for key, value in composition.parameters.items()
                                if key in mode_keys.get(composition.mode, ())}),
                'session_fps': str(rate),
                'audio_policy': ('wav_pcm_v1' if include_record_audio else 'drop'),
                'members': snapshots}
        if include_record_audio:
            frozen['record_audio'] = {
                'version': 1, 'file': 'record_audio.wav',
                'sample_rate': 48000, 'channels': 2,
                'sample_format': 's16le', 'routes': audio_routes}
            from .record_audio_overlay_intent import restore_overlay_intent
            persisted_overlay = restore_overlay_intent(
                composition, self.session_controller.session.view.get(
                    'record_audio_overlay_intent'))
            overlay_choice = (self._record_overlay_choice
                              if self._record_overlay_key == self._record_audio_edit_key()
                              else persisted_overlay)
            if overlay_choice is not None:
                from ..exporting.external_audio_proof import (
                    freeze_external_record_audio_proof)
                ordered = (overlay_choice if isinstance(overlay_choice, list)
                           else [overlay_choice])
                if not 1 <= len(ordered) <= 3:
                    raise ValueError('Record audio supports 1–3 explicit '
                                     'ordered audio-only events, never an '
                                     'unbounded implicit music mix.')
                frozen_overlays = []
                for i, layer in enumerate(ordered):
                    if (layer['record_start_frame'] +
                            layer['duration_frames'] > composition.total_frames()):
                        raise ValueError('Audio-only event {} extends beyond '
                                         'the actual record playhead; no silent '
                                         'trim.'.format(i+1))
                    proof = freeze_external_record_audio_proof(
                        layer['external_path'], ffprobe=self.ffprobe)
                    frozen_overlays.append(dict(
                        layer, occurrence_id='record-overlay-{:03d}'.format(i+1),
                        kind='external', audio_ordinal=0, stream_index=0,
                        external_proof=proof))
                if isinstance(overlay_choice, list):
                    frozen['record_audio']['overlays'] = frozen_overlays
                else:
                    # Preserve every existing v1 Session, queue and receipt.
                    frozen['record_audio']['overlay'] = frozen_overlays[0]
        want_record_mov = (getattr(self, 'export_record_av_cb', None) is not None
                           and self.export_record_av_cb.isChecked())
        want_record_rgba16 = (
            getattr(self, 'export_record_rgba16_cb', None) is not None and
            self.export_record_rgba16_cb.isChecked())
        if want_record_mov or want_record_rgba16:
            if (not is_record or not include_record_audio or
                    self.export_color_stage.currentData() != 'display'):
                raise ValueError('Record MOV/RGBA16 NUT requires explicit '
                                 'verified PCM and Display SDR PNG sequence output.')
            from ..exporting.record_av_mux import freeze_record_av_sources
            first = snapshots[0]
            # Freeze ORIGINAL movie/EXR bytes ONCE for both optional derivative
            # formats, before either derivative flag is in the frozen plan.
            # This spec never writes output or self-validates an absent flag.
            provisional = ExportSpec.from_context(dict(
                source_path=first['source_path'], source_id=first['source_id'],
                path=os.path.join(os.path.dirname(first['source_path']),
                                  '.record-av-source-proof.####.png'),
                start=self.player.in_frame, end=self.player.out_frame,
                count=self.player.out_frame - self.player.in_frame + 1,
                fps=str(rate), source_fps=str(rate),
                out_w=first['width'], out_h=first['height'],
                color_stage='display', sequence=True,
                composite_export=frozen),
                source_path=first['source_path'], source_id=first['source_id'])
            source_files = freeze_record_av_sources(provisional)
        if want_record_mov:
            from ..exporting.record_av_mux import CONFIG
            frozen['record_av'] = dict(
                CONFIG, source_files=copy.deepcopy(source_files))
        if want_record_rgba16:
            from ..exporting.record_rgba16_mux import CONFIG
            frozen['record_rgba16'] = dict(
                CONFIG, source_files=copy.deepcopy(source_files))
        return frozen

    def _export_composition(self):
        """Queue one bounded, provenance-rich comparison from the actual UI."""
        try:
            stage = self.export_color_stage.currentData() or 'source'
            if stage == 'source':
                raise ValueError('A composition has no single original Source '
                                 'color stage; select Working EXR or Display.')
            if self._export_runner is None:
                raise ValueError('Durable export queue is unavailable.')
            out_w, out_h = self._parse_out_res()
            out_w, out_h = max(2, out_w - out_w % 2), max(2, out_h - out_h % 2)
            frozen = self._composite_export_snapshot()
            if frozen['version'] == 2:
                if frozen['mode'] != 'grid':
                    from fractions import Fraction
                    first = frozen['members'][0]
                    par = Fraction(first['pixel_aspect'])
                    out_w = (first['width'] * par.numerator * 2 +
                             par.denominator) // (2 * par.denominator)
                    out_h = first['height']
                if out_w < 1 or out_h < 1:
                    raise ValueError('Invalid physical-field comparison output size.')
            if (self.fps_combo.currentData() != 'source' and
                    frozen['session_fps'] != '24'):
                raise ValueError('Precision composites retain their exact '
                                 'session FPS. Select Source frame rate; '
                                 'composite resampling is not yet verified.')
            if self.player.out_frame - self.player.in_frame + 1 > 10000:
                raise ValueError('Managed composite export is limited to 10,000 frames.')
        except (ValueError, OSError, RuntimeError) as exc:
            QtWidgets.QMessageBox.warning(self, 'Managed composition export', str(exc))
            return
        dialog = ExportOptionsDialog(self, self.info.path,
                                     os.path.dirname(self.info.path), stage=stage)
        with_record_pcm = (frozen['version'] == 4 and
                           frozen.get('audio_policy') == 'wav_pcm_v1')
        with_record_mov = (with_record_pcm and
                           frozen.get('record_av') is not None)
        with_record_rgba16 = (with_record_pcm and
                              frozen.get('record_rgba16') is not None)
        if frozen['version'] in (2, 3, 4):
            # Do not offer unsupported MP4 and then reject the artist after
            # confirmation. Exact physical and mixed delivery is image-only.
            dialog.video_cb.setChecked(False)
            dialog.video_cb.setEnabled(False)
            dialog.sequence_cb.setChecked(True)
            dialog.warning.setText(
                'Managed image-sequence delivery uses one durable folder: '
                + ('native EXR pixel aspect will be rectified to square pixels.'
                   if frozen['version'] == 2 else
                   'EDL/OTIO cuts/gaps use the frozen per-edit record clock; '
                   'transparent gaps and source color are retained. '
                   + ('An explicitly selected, independently verified 48 kHz '
                      'stereo PCM WAV will be published INSIDE the same atomic '
                      'image-sequence folder, with source and output checks. '
                      + ('An opt-in 8-bit SDR lossless RGB/PCM MOV derivative '
                         'will also be independently decoded and published '
                         'in that same folder, with transparent PNG pixels '
                         'composited onto black and no HDR/alpha claim.'
                         if with_record_mov else '')
                      + ('An additional lossless FFV1 16-bit straight RGBA '
                         'and 48 kHz PCM NUT movie will be published in the '
                         'SAME folder, with exact 24000/1001 timestamps and '
                         'independent full pixel/alpha/sample checks. It is '
                         'Display SDR, NOT source HDR; third-party NUT '
                         'player support is limited.'
                         if with_record_rgba16 else '')
                      if with_record_pcm else 'Audio is omitted.')
                   if frozen['version'] == 4 else
                   'the explicitly confirmed CFR movie and EXR members will '
                   'be composited in working light; original audio is omitted.'))
            dialog.warning.show()
        result = dialog.exec() if hasattr(dialog, 'exec') else dialog.exec_()
        if result != QtWidgets.QDialog.Accepted:
            return
        movie_details = []
        if frozen['version'] in (3, 4):
            for member in frozen['members']:
                if member['input_kind'] == 'movie':
                    proof = member['movie_proof']
                    yuv = member['movie_color_provenance']
                    movie_details.append(
                        '{} — {} fps, {} frames, input {}, YUV {} / {}, '
                        'opaque, codec {}, {}. The source file and all '
                        'frame timestamps were probed.'.format(
                            member['source_id'], proof['avg_frame_rate'],
                            proof['frame_count'], yuv['input_space'],
                            yuv['yuv_matrix'], yuv['yuv_range'],
                            proof['codec'], proof['pix_fmt']))
        audio_details = []
        if with_record_pcm:
            for route in frozen['record_audio']['routes']:
                if route['kind'] == 'silence':
                    audio_details.append('{}: EXPLICIT SILENCE (image edit)'.format(
                        route['occurrence_id']))
                elif route['kind'] == 'external':
                    proof = route['external_proof']
                    audio_details.append(
                        '{}: EXTERNAL zero-origin PCM WAV {}, stream #{}, '
                        '48 kHz stereo; source SHA256 {}, {} sample frames '
                        '(original file frozen/independently rechecked)'.format(
                            route['occurrence_id'], route['external_path'],
                            route['stream_index'], proof['source_sha256'],
                            proof['sample_frames']))
                else:
                    audio_details.append('{}: embedded audio ordinal {}, '
                                         'container stream #{}, unshifted '
                                         '48 kHz stereo PCM'.format(
                                             route['occurrence_id'],
                                             route['audio_ordinal'],
                                             route['stream_index']))
            overlays = frozen['record_audio'].get('overlays')
            if overlays is None:
                legacy_overlay = frozen['record_audio'].get('overlay')
                overlays = [legacy_overlay] if legacy_overlay is not None else []
            for overlay in overlays:
                audio_details.append(
                    '{}: EXTRA AUDIO-ONLY OVERLAP, independently verified '
                    'WAV {}, SHA256 {}, record frames {}–{}, original in {}, '
                    'constant gain {}/2, saturated signed PCM mix; explicitly '
                    'audible over source gaps. No crossfade or retime.'.format(
                        overlay['occurrence_id'], overlay['external_path'],
                        overlay['external_proof']['source_sha256'],
                        overlay['record_start_frame'],
                        overlay['record_start_frame'] +
                        overlay['duration_frames'] - 1,
                        overlay['source_in_frame'], overlay['gain_num']))
            if len(overlays) > 1:
                audio_details.append(
                    'ORDERED MIX: apply the preceding {} separately authored '
                    'WAV events in exactly this order with signed PCM16 '
                    'saturation after EACH step. Reordering can change '
                    'clipping; no commutative floating-point mix is claimed.'
                    .format(len(overlays)))
        audio_message = ('Record PCM sidecar, selected per occurrence:\n' +
                         '\n'.join(audio_details) + '\nGaps without an authored '
                         'audio-only event become exact silence. External audio '
                         'is accepted ONLY when separately '
                         'verified from canonical zero-origin PCM. There is no '
                         'crossfade, delay, retime, unverified source clock '
                         'or movie mux.\n\n'
                         if with_record_pcm else 'Audio is explicitly omitted. ')
        if with_record_mov:
            audio_message += (
                '\nOPT-IN RECORD MOV: lossless H.264 RGB 8-bit Display SDR '
                'and exact 48 kHz stereo PCM, built FROM the staged PNG16/WAV '
                'references. Native HDR/16-bit/alpha are NOT retained in MOV; '
                'the explicitly chosen black matte and 8-bit SDR quantization '
                'will be independently pixel-checked after decoding. MOV, WAV '
                'and reference frames publish or rollback together.\n\n')
        if with_record_rgba16:
            audio_message += (
                '\nOPT-IN RECORD RGBA16 NUT: lossless FFV1 16-bit straight '
                'RGBA Display SDR and selected 48 kHz stereo PCM, FROM the '
                'staged PNG16/WAV reference assets. Exact rational '
                '24000/1001 video and 1/48000 video/audio timestamps; every '
                'decoded 16-bit channel, transparent alpha and audio sample '
                'must match independently. This is NOT native HDR/linear EXR '
                'and the NUT container has limited player compatibility. '
                'This folder and its optional MOV publish atomically.\n\n')
        if QtWidgets.QMessageBox.question(
                self, 'Confirm Composite Export',
                'Render the selected comparison/record timeline in {} space using a frozen '
                'color interpretation per member?\n\n{}{}{}Notes are not burned in. Unsupported transforms '
                'fail without replacing '
                'existing outputs.'.format(
                    stage.title(),
                    ('These members are verified image sequences.\n\n'
                     if not movie_details else
                     'Explicitly CONFIRM these movie decoding/input-color '
                     'interpretations (metadata alone is not consent):\n' +
                     '\n'.join(movie_details) + '\n\n'),
                    'Non-square source pixels will be rectified to a '
                    'square-pixel image sequence using frozen native EXR '
                    'geometry; movie output is unsupported.\n\n'
                    if frozen['version'] == 2 else '', audio_message),
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No) != QtWidgets.QMessageBox.Yes:
            return
        if frozen['version'] in (3, 4):
            # `origin=user` is assigned only AFTER this explicit confirmation,
            # never inferred from FFprobe metadata or a saved Source profile.
            for member in frozen['members']:
                if member['input_kind'] == 'movie':
                    member['movie_color_provenance'].update(
                        confirmed=True, origin='user')
        first = frozen['members'][0]
        fps = frozen['session_fps']
        context = dict(
            source_path=first['source_path'], source_id=first['source_id'],
            start=self.player.in_frame, end=self.player.out_frame,
            count=self.player.out_frame - self.player.in_frame + 1,
            source_fps=frozen['session_fps'], fps=fps, out_w=out_w, out_h=out_h,
            color_stage=stage, color=self._effective_color_state(
                self.session_controller.session.source(first['source_id'])).to_dict(),
            composite_export=frozen, speed=1.0, tracking_enabled=False,
            tracking_quads={}, burnin={}, crop=None, flip_h=False,
            flip_v=False, note_ids=[], movie_audio_policy='drop')
        try:
            queued = []
            for path in dialog.paths():
                if frozen['version'] in (2, 3, 4) and '.####.' not in path:
                    raise ValueError('Native-PAR, mixed movie or record composites require '
                                     'Working EXR / Display PNG image-sequence '
                                     'delivery; MP4 is not verified.')
                spec = ExportSpec.from_context(
                    dict(context, path=os.path.abspath(path),
                         sequence='.####.' in path),
                    source_path=first['source_path'], source_id=first['source_id'])
                if with_record_mov:
                    from ..exporting.record_av_mux import validate_record_av
                    validate_record_av(spec, frozen)
                if with_record_rgba16:
                    from ..exporting.record_rgba16_mux import validate_record_rgba16
                    validate_record_rgba16(spec, frozen)
                queued.append(self._export_runner.enqueue(
                    spec, seq_files=(), width=first['width'],
                    height=first['height'],
                    overwrite=bool(dialog.overwrite_cb.isChecked())))
        except (RuntimeError, ValueError, OSError, TypeError) as exc:
            QtWidgets.QMessageBox.critical(self, 'Composite export rejected', str(exc))
            return
        self.player.pause()
        self.status_lbl.setText('Queued {} managed composite output(s).'.format(
            len(queued)))
        self._start_export_queue()

    def _frozen_managed_movie_audio(self):
        """Freeze the explicitly selected embedded track, not a device route.

        Audio can accompany verified CFR Display MP4 only with an unchanged
        rational video clock, zero start offsets and an unambiguous embedded
        source track. This is a bounded read-only FFprobe, never a silent
        adaptation of a user-selected external soundtrack or audio delay.
        """
        import subprocess
        from fractions import Fraction
        if self.info is None or self.info.is_sequence or self._composition is not None:
            raise ValueError('Managed soundtrack preservation needs one movie source.')
        route = self.player.audio_route
        if (route.external_path or route.delay_ms or
                route.output_layout not in ('source', 'stereo')):
            raise ValueError('External audio, audio delay and forced audio routing '
                             'cannot be verified in managed delivery. Clear '
                             'these playback choices or export video-only.')
        args = [self.ffprobe, '-v', 'error', '-show_entries',
                'stream=index,codec_type,codec_name,channels,sample_rate,time_base,start_time',
                '-of', 'json', self.info.path]
        kwargs = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                      timeout=20, env=ffmpegtools.media_environment())
        kwargs.update(ffmpegtools._no_window_kwargs())
        try:
            response = subprocess.run(args, **kwargs)
            if response.returncode or len(response.stdout) > 256 * 1024:
                raise ValueError('Source audio/PTS metadata unavailable or oversized.')
            streams = json.loads(response.stdout.decode('utf-8'))['streams']
            video_tracks = [s for s in streams if s.get('codec_type') == 'video']
            if len(video_tracks) != 1:
                raise ValueError('Audio-preserving managed delivery requires '
                                 'exactly one source video stream.')
            video = video_tracks[0]
            if abs(Fraction(str(video['start_time']))) > Fraction(1, 1000):
                raise ValueError('Movie video PTS starts away from zero; '
                                 'managed soundtrack alignment is unverified.')
            tracks = [s for s in streams if s.get('codec_type') == 'audio']
            selected = tracks[route.stream_index]
            input_rate = int(selected['sample_rate'])
            channels = int(selected['channels'])
            source_time_base = str(Fraction(selected['time_base']))
            if (input_rate not in (44100, 48000) or channels != 2 or
                    abs(Fraction(str(selected['start_time']))) >
                    Fraction(1, 1000)):
                raise ValueError('Selected audio must be stereo at 44.1/48 kHz '
                                 'with a zero start offset. No unverified '
                                 'mono/surround remixes are performed.')
        except (KeyError, IndexError, TypeError, ZeroDivisionError,
                OverflowError, OSError, UnicodeError, StopIteration,
                subprocess.SubprocessError, json.JSONDecodeError) as exc:
            raise ValueError('Cannot establish selected embedded audio identity: {}'
                             .format(exc))
        frozen = {
            'version': 1, 'confirmed': True, 'source': 'embedded',
            'stream_index': int(selected['index']), 'codec': 'aac',
            'sample_rate': 48000, 'channels': 2, 'bit_rate': 192000,
            'offset_ms': 0, 'alignment': 'cfr_frame_count',
            'source_time_base': source_time_base,
            'source_sample_rate': input_rate}
        return frozen

    def _export_stabilized(self):
        """Bounded, opt-in native precision stabilization via the ONE queue.

        Stage only verified linear Working float EXR. The capture model is the
        same frozen signed tracking transform as the live preview. Preview
        camera shifts and note overlays must never be mistaken for baked output.
        """
        from fractions import Fraction
        plan = self._stabilization_plan
        source = self.session_controller.current
        try:
            if (plan is None or source is None or self.info is None or
                    not self.info.is_sequence or self._composition is not None):
                raise ValueError('A single native EXR Source with a verified '
                                 'stabilization preview is required.')
            if (self.export_color_stage.currentData() != 'working' or
                    self.fps_combo.currentData() != 'source' or
                    self.player.speed != 1.0):
                raise ValueError('Stabilization currently delivers only full '
                                 'native-resolution Working float EXR at exact '
                                 'original source FPS, normal speed.')
            mapping = self.info.frame_mapping()
            start, end = self.player.in_frame, self.player.out_frame
            if (start < 0 or end >= self.info.frame_count or
                    end < start or end - start + 1 > 60):
                raise ValueError('Stabilized output is bounded to 1–60 original '
                                 'consecutive display frames.')
            labels = [mapping.display_to_source(index)
                      for index in range(start, end + 1)]
            if (any(label not in plan.source_frames for label in labels) or
                    plan.anchor_source_frame not in labels):
                raise ValueError('Selected export range must be completely '
                                 'covered by the measured stabilization and '
                                 'contain its original fixed anchor frame.')
            if ((self.surface.image_geometry().rotation != 0) or
                    abs(self.info.pixel_aspect_ratio - 1.0) > 1e-7):
                raise ValueError('Baked stabilization requires unrotated '
                                 'square-pixel original EXR geometry.')
            existing = dict(zip(self.info.sequence_source_frames,
                                self.info.sequence_files))
            if any(label not in existing for label in labels):
                raise ValueError('The marked range contains an absent original '
                                 'source plate; held pixels are not delivered.')
            from ..exporting.stabilized_renderer import (
                freeze_stabilized_export, validate_stabilized_export)
            color = self._effective_color_state(source).to_dict()
            frozen = freeze_stabilized_export(
                self.info, source.id, source.review.get('tracking_v1'),
                labels, {label: os.path.abspath(existing[label])
                         for label in labels}, source_color=color,
                alpha_mode=color.get('alpha_mode'),
                flip_h=self.act_flip_h.isChecked(),
                flip_v=self.act_flip_v.isChecked(), rotation=0,
                anchor_source_frame=plan.anchor_source_frame)
            validate_stabilized_export(
                frozen, info=self.info, source_id=source.id,
                tracking_v1=source.review['tracking_v1'])
            if any(tuple(row['offset']) != plan.offset(row['source_frame'])
                   for row in frozen['frames']):
                raise ValueError('Managed stabilization transform is not '
                                 'identical to the accepted Qt preview.')
            exact_fps = str(Fraction(*self.info.frame_mapping().fps_rational))
        except (ValueError, RuntimeError, OSError, KeyError, TypeError) as exc:
            QtWidgets.QMessageBox.warning(self, 'Stabilized export unavailable',
                                          str(exc))
            return
        dialog = ExportOptionsDialog(
            self, self.info.path, os.path.dirname(self.info.path), stage='working')
        dialog.video_cb.setChecked(False)
        dialog.video_cb.setEnabled(False)
        dialog.sequence_cb.setChecked(True)
        dialog.warning.setText(
            'Verified Working float32 RGBA EXR only. Native source pixels and '
            'negative/HDR values remain scene-linear. Each tracked translation '
            'is baked with genuine zero-alpha unsupported edges. Preview crop, '
            'annotations, display LUT, audio, and burn-ins are NOT included.')
        dialog.warning.show()
        choice = dialog.exec() if hasattr(dialog, 'exec') else dialog.exec_()
        if choice != QtWidgets.QDialog.Accepted:
            return
        paths = dialog.paths()
        if (len(paths) != 1 or not paths[0].lower().endswith('.####.exr')):
            QtWidgets.QMessageBox.warning(self, 'Stabilized export unavailable',
                                          'Select only a Working EXR image sequence.')
            return
        if QtWidgets.QMessageBox.question(
                self, 'Confirm baked stabilization',
                'Render {} original signed source frames using the immutable '
                'verified translation track for logical Source {}?\n\n'
                'Anchor frame: {}. FPS: {}. Frame size: {} × {}.\n'
                'RGBA float Working output; edges outside actual plate pixels '
                'will be transparent. This is NOT the cropped or color-display '
                'preview and has NO audio/notes.\n\n'
                'Queue one verified native EXR sequence?'.format(
                    len(labels), source.id, plan.anchor_source_frame,
                    exact_fps, self.info.width, self.info.height),
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No) != QtWidgets.QMessageBox.Yes:
            return
        context = dict(
            path=os.path.abspath(paths[0]), source_path=os.path.abspath(self.info.path),
            source_id=source.id, start=start, end=end, count=len(labels),
            fps=exact_fps, source_fps=exact_fps,
            out_w=self.info.width, out_h=self.info.height,
            color_stage='working', sequence=True, color=color,
            frame_numbers=list(mapping.source_frames),
            existing_frame_numbers=list(self.info.sequence_source_frames),
            missing_frame_policy=self.info.missing_policy,
            stabilization_export=frozen,
            speed=1.0, tracking_enabled=False, tracking_quads={},
            flip_h=self.act_flip_h.isChecked(),
            flip_v=self.act_flip_v.isChecked(), crop=None, burnin={}, note_ids=[])
        try:
            spec = ExportSpec.from_context(context, source_path=self.info.path,
                                           source_id=source.id)
            # A build without the proven stabilized QThread + durable receipt
            # MUST fail here, never fall through to ordinary EXR delivery.
            from ..exporting.spec import validate_managed_export
            validate_managed_export(spec, (), (self.info.width, self.info.height))
            self._export_runner.enqueue(
                spec, seq_files=(), width=self.info.width,
                height=self.info.height,
                overwrite=bool(dialog.overwrite_cb.isChecked()))
        except (RuntimeError, ValueError, OSError, TypeError) as exc:
            QtWidgets.QMessageBox.warning(self, 'Stabilized export rejected', str(exc))
            return
        self.player.pause()
        self.status_lbl.setText('Queued verified Working EXR stabilization '
                                'through the durable export queue.')
        self._start_export_queue()

    def _export(self):
        if self.info is None or self._export_runner is None:
            return
        if self._composition is not None:
            return self._export_composition()
        if self._stabilization_plan is not None:
            return self._export_stabilized()
        if self.tracking_enabled and self.tracked_quality:
            # The optical-flow worker holds the last real transform when it
            # loses its features. Such a held estimate is honest for review,
            # but silently delivering it as tracked/corrected media is not.
            missing_motion = sorted(
                self.info.frame_mapping().display_to_source(index)
                for index, metric in self.tracked_quality.items()
                if (self.player.in_frame <= index <= self.player.out_frame and
                    metric.get('lost')))
            if missing_motion:
                QtWidgets.QMessageBox.warning(
                    self, 'Tracking delivery needs correction',
                    'Motion was lost at original source frame(s): {}{}. '
                    'The preview held the last measured pose rather than '
                    'inventing a transform. Retrack/correct or disable '
                    'tracking before exporting; nothing was queued.'.format(
                        ', '.join(str(frame) for frame in missing_motion[:10]),
                        '…' if len(missing_motion) > 10 else ''))
                return
        exr_issue = self._unsafe_exr_delivery(self.info)
        if exr_issue:
            QtWidgets.QMessageBox.warning(
                self, 'EXR output not yet geometrically verified',
                'This source uses an EXR feature the current FFmpeg export '
                'cannot preserve: {}. No video or managed output was queued.'
                .format(exr_issue))
            return
        geometry = self.surface.image_geometry()
        stage = self.export_color_stage.currentData() or 'source'
        managed_image = bool(self.info.is_sequence and stage in ('working', 'display'))
        view_geometry = bool(geometry.rotation or
                             abs(geometry.pixel_aspect - 1.0) > 1e-6)
        managed_effects = bool(managed_image and (
            view_geometry or self.surface.crop() or
            self.act_flip_h.isChecked() or self.act_flip_v.isChecked()))
        if view_geometry and not managed_image:
            QtWidgets.QMessageBox.warning(
                self, 'Unverified export geometry',
                'This source is displayed with rotation {}° and pixel aspect '
                '{:g}. Select Working or Display image-sequence export to '
                'bake these into verified square-pixel pixels. Legacy Source, '
                'movie and composite delivery still reject this geometry.'
                .format(geometry.rotation, geometry.pixel_aspect))
            return
        managed_geometry = None
        if managed_effects:
            from fractions import Fraction
            from ..exporting.spec import managed_geometry_plan
            # Pixel aspect is frozen as an explicit rational. The backend,
            # queue and receipt repeat the same sample-domain calculations;
            # never infer a second geometry from export widget dimensions.
            crop = self.surface.crop() or (0, 0, self.info.width, self.info.height)
            if any(type(value) is not int for value in crop):
                QtWidgets.QMessageBox.warning(
                    self, 'Invalid native crop', 'Export crop must use integer source pixels.')
                return
            par = Fraction(str(geometry.pixel_aspect)).limit_denominator(1000000)
            square_width = (crop[2] * par.numerator * 2 +
                            par.denominator) // (2 * par.denominator)
            natural_size = ((crop[3], square_width)
                            if geometry.rotation in (90, 270)
                            else (square_width, crop[3]))
            managed_geometry = {
                'version': 1, 'input_size': [self.info.width, self.info.height],
                'pixel_aspect': str(par), 'rotation': geometry.rotation,
                'resample': 'linear'}
            message = (
                'Bake the confirmed source geometry into a square-pixel '
                '{}-stage image sequence?\n\n'
                'Native source: {} × {}, pixel aspect {}\n'
                'Native post-flip crop: {}\n'
                'Flip horizontal / vertical: {} / {}\n'
                'Clockwise rotation: {}°\n'
                'Required raster: {} × {} (no additional resize)\n\n'
                'Order: flip → crop → float linear-light PAR correction → '
                'rotate. Working EXR retains float HDR; Display PNG quantizes '
                'after its color transform. Movie output is not available.'
            ).format(stage.title(), self.info.width, self.info.height, par,
                     tuple(crop), self.act_flip_h.isChecked(),
                     self.act_flip_v.isChecked(), geometry.rotation, *natural_size)
            if QtWidgets.QMessageBox.question(
                    self, 'Confirm managed geometry', message,
                    QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                    QtWidgets.QMessageBox.No) != QtWidgets.QMessageBox.Yes:
                return
            out_w, out_h = natural_size
            # Validate the *same* immutable plan that the queue will validate
            # before opening the output chooser, not after destructive work.
            preflight = ExportSpec.from_context(dict(
                path=os.path.abspath(os.path.join(
                    os.path.dirname(self.info.path),
                    'geometry-preview.####.' + ('exr' if stage == 'working' else 'png'))),
                source_path=self.info.path,
                color_stage=stage, sequence=True,
                start=self.player.in_frame, end=self.player.out_frame,
                count=self.player.out_frame - self.player.in_frame + 1,
                fps=self.info.fps, source_fps=self.info.fps,
                out_w=out_w, out_h=out_h,
                crop=self.surface.crop(),
                flip_h=self.act_flip_h.isChecked(),
                flip_v=self.act_flip_v.isChecked(),
                speed=self.player.speed,
                tracking_enabled=self.tracking_enabled,
                tracking_quads=dict(self.tracked_quads),
                managed_geometry_export=managed_geometry))
            try:
                managed_geometry_plan(preflight, (self.info.width, self.info.height))
            except (ValueError, RuntimeError, TypeError) as exc:
                QtWidgets.QMessageBox.warning(
                    self, 'Managed geometry unavailable', str(exc))
                return
        else:
            try:
                out_w, out_h = self._parse_out_res()
            except ValueError as exc:
                QtWidgets.QMessageBox.warning(self, 'Export settings', str(exc))
                return
            # H.264 (yuv420p) needs even dimensions on the legacy/movie path.
            out_w = max(2, out_w - out_w % 2)
            out_h = max(2, out_h - out_h % 2)
        dialog = ExportOptionsDialog(self, self.info.path, os.path.dirname(self.info.path))
        if managed_geometry is not None:
            dialog.video_cb.setChecked(False)
            dialog.video_cb.setEnabled(False)
            dialog.video_cb.setToolTip('Geometry-correct managed movies are not supported.')
            dialog.sequence_cb.setChecked(True)
        result = dialog.exec() if hasattr(dialog, 'exec') else dialog.exec_()
        if result != QtWidgets.QDialog.Accepted:
            return
        if managed_geometry is not None and (not dialog.paths() or
                any(not path.lower().endswith(
                    '.####.' + ('exr' if stage == 'working' else 'png'))
                    for path in dialog.paths())):
            QtWidgets.QMessageBox.warning(
                self, 'Managed geometry output',
                'Only the verified image-sequence format for this color '
                'stage can preserve the requested geometry.')
            return
        self._commit_review_projection()
        mapping = self.info.frame_mapping()
        doc = self._review_adapter.document if self._review_adapter else None
        selected_note_ids = ([note.id for note in doc.notes
                              if self.player.in_frame <=
                              mapping.source_to_display(note.source_frame) <=
                              self.player.out_frame] if doc is not None else [])
        current_source = self.session_controller.current
        export_fps = self.info.fps if self.fps_combo.currentData() == 'source' else 24.0
        stage = self.export_color_stage.currentData() or 'source'
        managed_movie = not self.info.is_sequence and stage != 'source'
        # A movie's codec/metadata cannot establish scene-referred input
        # interpretation. Confirm a frozen source color assumption and the
        # deliberate absence of audio rather than silently claiming management.
        movie_provenance = None
        movie_audio_export = None
        if managed_movie:
            if self.export_preserve_audio_cb.isChecked():
                if (stage != 'display' or len(dialog.paths()) != 1 or
                        not dialog.paths()[0].lower().endswith('.mp4') or
                        self.fps_combo.currentData() != 'source' or
                        self.player.speed != 1.0):
                    QtWidgets.QMessageBox.warning(
                        self, 'Managed soundtrack unavailable',
                        'Preserve audio requires one Display MP4 at the source '
                        'frame rate and normal playback speed. Deselect '
                        'image-sequence outputs or disable Preserve audio.')
                    return
                try:
                    movie_audio_export = self._frozen_managed_movie_audio()
                except (ValueError, RuntimeError) as exc:
                    QtWidgets.QMessageBox.warning(
                        self, 'Managed soundtrack unavailable', str(exc))
                    return
            color_state = self._effective_color_state(current_source)
            input_space = color_state.input_space
            matrix = None
            video_range = None
            pixel_format = str(self.info.pixel_format or '').casefold()
            if pixel_format.startswith(('yuv', 'yuva', 'nv', 'p010', 'p016')):
                tags = self.info.color_tags or {}
                matrix = {'bt709': 'bt709', 'bt470bg': 'bt601',
                          'smpte170m': 'bt601',
                          'bt2020nc': 'bt2020'}.get(tags.get('color_space'))
                video_range = {'tv': 'limited', 'pc': 'full'}.get(
                    tags.get('color_range'))
                if matrix is None:
                    matrix, accepted = QtWidgets.QInputDialog.getItem(
                        self, 'Confirm YUV Matrix',
                        'Movie metadata does not identify a YUV matrix. '
                        'Select the source interpretation:',
                        ['bt709', 'bt601', 'bt2020'], 0, False)
                    if not accepted:
                        return
                if video_range is None:
                    video_range, accepted = QtWidgets.QInputDialog.getItem(
                        self, 'Confirm YUV Range',
                        'Movie metadata does not identify the range. '
                        'Select the source interpretation:',
                        ['limited', 'full'], 0, False)
                    if not accepted:
                        return
            message = (
                'Managed {} movie export requires a confirmed input color space.\n\n'
                'Interpret this source as: {}\n'
                'Source alpha association: {}\n'
                'YUV matrix/range: {} / {}\n'
                'Decode constant-frame-rate video with exact source-frame mapping.\n'
                '{}\n\n'
                'Continue with this frozen interpretation?'.format(
                    stage.title(), input_space, color_state.alpha_mode,
                    matrix or 'not YUV', video_range or 'not YUV',
                    ('Preserve embedded audio stream #{}, convert to stereo '
                     '48 kHz AAC, sample-trim to the selected CFR frame range; '
                     'no external source/delay is applied.'.format(
                         movie_audio_export['stream_index'])
                     if movie_audio_export else
                     'Audio will NOT be included in the managed output.')))
            answer = QtWidgets.QMessageBox.question(
                self, 'Confirm Managed Movie Color', message,
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No)
            if answer != QtWidgets.QMessageBox.Yes:
                return
            movie_provenance = {'origin': 'user', 'confirmed': True,
                                'input_space': input_space,
                                'alpha_mode': color_state.alpha_mode}
            if matrix is not None:
                movie_provenance.update(yuv_matrix=matrix, yuv_range=video_range)
        snapshot = dict(
            crop=self.surface.crop(), out_w=out_w, out_h=out_h,
            start=self.player.in_frame, end=self.player.out_frame,
            count=self.player.out_frame - self.player.in_frame + 1,
            fps=export_fps, source_fps=self.info.fps, speed=self.player.speed,
            flip_h=self.act_flip_h.isChecked(), flip_v=self.act_flip_v.isChecked(),
            tracking_enabled=self.tracking_enabled, centers=dict(self.tracked_centers),
            tracking_mode=self.tracking_mode.currentData(),
            tracking_quads={key: list(points)
                            for key, points in self.tracked_quads.items()},
            tracking_base_size=self.tracking_base_size,
            tracking_size=self.tracking_size,
            tracking_offset=(self.tracking_offset.x(), self.tracking_offset.y()),
            note_ids=selected_note_ids,
            color=self._effective_color_state(current_source).to_dict(),
            frame_numbers=list(self.info.frame_numbers),
            existing_frame_numbers=list(getattr(self.info, 'sequence_source_frames', [])),
            missing_frame_policy=getattr(self.info, 'missing_policy', 'skip'),
            managed_movie=managed_movie,
            managed_geometry_export=managed_geometry,
            movie_color_provenance=movie_provenance,
            movie_audio_policy=('preserve' if movie_audio_export else 'drop')
                               if managed_movie else None,
            movie_audio_export=movie_audio_export,
            source_frames={'start': mapping.display_to_source(self.player.in_frame),
                           'end': mapping.display_to_source(self.player.out_frame)})
        overwrite = bool(dialog.overwrite_cb.isChecked())
        try:
            queued = []
            source_id = (self.session_controller.current.id
                         if self.session_controller.current is not None else '')
            for path in dialog.paths():
                ctx = dict(snapshot, path=os.path.abspath(path),
                           source_id=source_id, source_path=self.info.path,
                           sequence=('.####.' in path),
                           color_stage=stage)
                spec = ExportSpec.from_context(ctx, source_path=self.info.path,
                                               source_id=source_id)
                queued.append(self._export_runner.enqueue(
                    spec, seq_files=self.info.sequence_files,
                    width=self.info.width, height=self.info.height,
                    overwrite=overwrite))
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, 'Queue export failed', str(exc))
            return
        self.player.pause()
        self.status_lbl.setText('Queued {} export(s).'.format(len(queued)))
        self._start_export_queue()

    def _export_next(self):
        if not self._export_queue:
            self._export_job = None
            self._task_end('export')
            self._set_enabled(True)
            if not self._export_failed:
                folders = list(dict.fromkeys(os.path.dirname(p) for p in self._export_done))
                links = ['<a href="{}" style="color:{}">{}</a>'.format(
                    QtCore.QUrl.fromLocalFile(os.path.abspath(p)).toString(),
                    style.ACCENT_HI, html.escape(os.path.basename(p) or p)) for p in folders]
                self.status_lbl.setText('Export complete. Open: ' + ' | '.join(links))
            return
        context = self._export_queue.pop(0)
        self._set_enabled(False)
        # Label the bar with the file's position in a multi-output queue.
        index = len(self._export_done) + 1
        total = index + len(self._export_queue)
        label = 'Exporting' if total == 1 else 'Exporting {}/{}'.format(index, total)
        self._task_begin('export', label)
        self.status_lbl.setText('Exporting {}…'.format(os.path.basename(context['path'])))
        job = ExportJob(self.ffmpeg, self.info.path, self.info.sequence_files,
                        self.info.width, self.info.height, context,
                        QtCore.QCoreApplication.instance())
        job.progress.connect(lambda v: self._task_update('export', v))
        job.result.connect(lambda p=context['path']: self._on_export_done(p))
        job.error.connect(self._on_export_error)
        job.finished.connect(job.deleteLater)
        self._export_job = job
        job.start()

    def _on_export_done(self, path):
        self._export_done.append(path)
        self._export_job = None
        self._export_next()

    def _on_export_error(self, message):
        self._export_failed = True
        self._export_queue = []
        self._export_job = None
        self._task_end('export')
        self._set_enabled(True)
        self.status_lbl.setText('Export failed: ' + html.escape(message))
        QtWidgets.QMessageBox.critical(self, 'Export failed', message)

    # ---------------------------------------------------------- lifecycle
    def _about(self):
        from .. import __version__
        from .about import AboutDialog
        AboutDialog.show_about(self, __version__)

    def keyPressEvent(self, event):
        if event.key() == QtCore.Qt.Key_Escape and self.isFullScreen():
            self.act_fullscreen.setChecked(False)
            event.accept()
            return
        QtWidgets.QDialog.keyPressEvent(self, event)

    def _apply_macos_tool_window(self):
        """Use native utility-window chrome only outside a workspace control."""
        if sys.platform != 'darwin':
            return
        tool_flag = getattr(QtCore.Qt, 'Tool', None)
        if tool_flag is None:
            tool_flag = QtCore.Qt.WindowType.Tool
        self.setWindowFlags(self.windowFlags() | tool_flag)
        attribute = getattr(QtCore.Qt, 'WA_MacAlwaysShowToolWindow', None)
        if attribute is None and hasattr(QtCore.Qt, 'WidgetAttribute'):
            attribute = getattr(
                QtCore.Qt.WidgetAttribute, 'WA_MacAlwaysShowToolWindow', None)
        if attribute is not None:
            self.setAttribute(attribute, True)

    @staticmethod
    def _stop_thread(job, timeout=2500):
        if job is None:
            return True
        try:
            if not job.isRunning():
                return True
            job.requestInterruption()
            if hasattr(job, 'stop_now'):
                job.stop_now()
            return bool(job.wait(timeout))
        except RuntimeError:
            return True

    def shutdown(self):
        """Idempotently stop every callback, process and worker owned by the UI."""
        if self._shutting_down:
            return False
        if self._closed:
            if self._shutdown_clean:
                return True
            # First teardown may time out while a decoder is exiting. Keep
            # references and recheck on subsequent shutdown/reload calls.
            self._shutting_down = True
            try:
                pending = self._shutdown_pending_jobs
                self._shutdown_pending_jobs = []
                clean = True
                for job in pending:
                    stopped = self._stop_thread(job)
                    if not stopped:
                        self._shutdown_pending_jobs.append(job)
                    clean = clean and stopped
                clean = bool(self.player.shutdown()) and clean
                self._shutdown_clean = clean
                if clean and type(self)._instance is self:
                    type(self)._instance = None
                return clean
            finally:
                self._shutting_down = False
        self._shutting_down = True
        clean = True
        try:
            scene_state.invalidate_pending_restore(self)
            try:
                self._save_preferences()
            except (RuntimeError, TypeError):
                pass
            # Persist the current review state (including the player's current
            # time) into the scene fileInfo and the last-session store before
            # the player tears down, so closing the window keeps the playhead.
            self._capture_current_review()
            self._remember_session_view()
            if self.session_controller.has_unsaved_changes():
                try:
                    self.session_controller.save_recovery(self._recovery_path())
                except (OSError, IOError, TypeError, ValueError):
                    pass
            scene_state.save_to_scene(self)
            self._closed = True
            self._scene_persistence.remove()
            self._scene_state_timer.stop()
            self._maya_sync_timer.stop()
            self._maya_write_timer.stop()
            self._maya_read_timer.stop()
            self._queued_maya_frame = None
            self._queued_player_frame = None
            self.mute_btn._click_timer.stop()
            self.volume_menu.close()
            self.draw_color_menu.close()
            if self.color_dialog is not None:
                self.color_dialog.close()
            if self.scopes_dialog is not None:
                self.scopes_dialog.shutdown()
                self.scopes_dialog.close()
                self.scopes_dialog = None
            # The playlist panel is embedded in the side panel; it tears down
            # with the window and needs no explicit close.
            self.waveform_job.cancel(wait=True)
            if not self.record_waveform.shutdown(wait=True, timeout_ms=3000):
                # QThread lifetime remains separately retained until it has
                # finished; a closed Qt window must not destroy a live thread.
                clean = False
            self.thumb_job.cancel(wait=True)

            tracking_job = self._tracking_job
            self._cancel_tracking_job()
            if not self._stop_thread(tracking_job):
                self._shutdown_pending_jobs.append(tracking_job)
                clean = False

            export_job = self._export_job
            self._export_job = None
            runner = self._export_runner
            if runner is not None:
                # Keep the runner's finished/verification callbacks connected
                # while cancellation drains. An interrupted export remains
                # journalled; it must never be reported as successful simply
                # because Maya closed its window.
                runner.shutdown(cancel_active=True)
            elif export_job is not None:
                for signal in (export_job.progress, export_job.result,
                               export_job.error):
                    try:
                        signal.disconnect()
                    except (RuntimeError, TypeError):
                        pass
            if not self._stop_thread(export_job, 3500):
                self._shutdown_pending_jobs.append(export_job)
                clean = False
            elif runner is not None and runner.active_id is not None:
                # A stopped QThread can still have a queued finished signal.
                # Finalize the durable cancellation exactly once; the late
                # signal sees no active task and becomes a no-op.
                runner._finished(runner.active_id)
            if getattr(self, 'shotgrid_panel', None) is not None:
                clean = bool(self.shotgrid_panel.shutdown()) and clean
            clean = bool(self.player.shutdown()) and clean
        finally:
            self._shutdown_clean = clean
            if clean and type(self)._instance is self:
                type(self)._instance = None
            self._shutting_down = False
        return clean

    def closeEvent(self, event):
        if not self.shutdown():
            event.ignore()
            return
        QtWidgets.QDialog.closeEvent(self, event)

    def dockCloseEventTriggered(self):
        """Maya workspace controls do not reliably deliver QWidget.closeEvent."""
        self.shutdown()

    @classmethod
    def restore_ui(cls):
        """Rebuild the widget into Maya's current restored workspace control."""
        instance = getattr(cls, '_instance', None)
        if instance is not None and getattr(instance, '_shutdown_clean', None) is False:
            raise RuntimeError(
                'FrameReviewer cannot be restored while background workers '
                'are still stopping. Retry shutdown after they exit.')
        if instance is not None and not getattr(instance, '_closed', True):
            return instance
        window = cls(parent=maya_main_window())
        cls._instance = window
        if not add_to_current_maya_layout(window):
            window.deleteLater()
            cls._instance = None
            return cls.show_ui(dockable=True)
        QtWidgets.QWidget.setVisible(window, True)
        window._set_media_window_title()
        return window

    @classmethod
    def show_ui(cls, dockable=True, area='right', floating=True):
        instance = getattr(cls, '_instance', None)
        if instance is not None and getattr(instance, '_shutdown_clean', None) is False:
            raise RuntimeError(
                'FrameReviewer is shutting down background workers. '
                'A new window cannot be opened until shutdown is complete.')
        if instance is not None and not getattr(instance, '_closed', True):
            try:
                instance.show()
                instance.raise_()
                instance.activateWindow()
                return instance
            except RuntimeError:
                instance = None
        cls._instance = None

        use_docking = bool(dockable and HAVE_MAYA_DOCKING and cmds is not None)
        # Retired workspace callbacks import the old package. Remove that
        # control before Maya tries to restore the new package in the same UI.
        old_control = 'FramestoreVideoCropperWorkspaceControl'
        if use_docking and cmds.workspaceControl(
                old_control, query=True, exists=True):
            try:
                cmds.deleteUI(old_control, control=True)
            except RuntimeError:
                pass
        if use_docking and cmds.workspaceControl(
                cls._workspace_control, query=True, exists=True):
            try:
                cmds.deleteUI(cls._workspace_control, control=True)
            except RuntimeError:
                pass

        # Clean up orphaned Python widgets left by interrupted development
        # reloads, even when their original class object no longer exists.
        app = QtWidgets.QApplication.instance()
        if app is not None:
            for widget in app.allWidgets():
                try:
                    if widget.objectName() == cls._object_name:
                        widget.close()
                        widget.deleteLater()
                except RuntimeError:
                    pass

        window = cls(parent=maya_main_window())
        cls._instance = window
        if use_docking:
            window.show(
                dockable=True, area=area, floating=bool(floating), retain=True,
                uiScript='import framereviewer; framereviewer.restore()',
                closeCallback='import framereviewer; framereviewer.shutdown()')
        else:
            window._apply_macos_tool_window()
            QtWidgets.QDialog.show(window)
        window._set_media_window_title()
        window.raise_()
        window.activateWindow()
        return window


def shutdown_ui(delete_workspace=False):
    """Stop the live UI and optionally remove its Maya workspace control."""
    clean = True
    instance = getattr(FrameReviewerWindow, '_instance', None)
    if instance is not None:
        try:
            clean = instance.shutdown()
            if not clean:
                # Keep the QObject hierarchy alive while QThreads still own
                # callbacks into it. The caller (reload()) will refuse to
                # purge modules. Deleting the widget here would invalidate
                # QObject receivers and can terminate Maya natively.
                return False
            instance.close()
            instance.deleteLater()
        except RuntimeError:
            pass
        FrameReviewerWindow._instance = None
    if (delete_workspace and cmds is not None and
            cmds.workspaceControl(FrameReviewerWindow._workspace_control,
                                  query=True, exists=True)):
        try:
            cmds.deleteUI(FrameReviewerWindow._workspace_control, control=True)
        except RuntimeError:
            clean = False
    app = QtWidgets.QApplication.instance()
    if app is not None:
        app.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
        app.processEvents(QtCore.QEventLoop.ExcludeUserInputEvents)
        app.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
    return clean


