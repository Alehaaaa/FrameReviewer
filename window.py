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
import html
import json
import os
import shutil
import tempfile
import glob
import sys

from .qtcompat import (QtCore, QtGui, QtWidgets, Signal, QAction, QActionGroup, QShortcut,
                       HAVE_MULTIMEDIA, HAVE_MAYA_DOCKING,
                       MayaQWidgetDockableMixin, maya_main_window,
                       add_to_current_maya_layout, cmds)
from . import style, utils, ffmpegtools, tracking
from .surface import PlaybackSurface
from .timeline import MarkedTimelineSlider
from .player import Player
from .waveform import WaveformJob
from .thumbnails import ThumbnailStripJob
from .tracking import TrackingJob
from .export import ExportOptionsDialog, ExportJob
from .shotgrid_ui import ShotGridPanel
from .settings import Settings
from .undo import UndoStack
from . import scene_state
from .scene_state import ScenePersistence
from .color import ColorCorrectionDialog

THUMB_LANE_H = 36
SPEEDS = (0.25, 0.5, 1.0, 1.5, 2.0)
ICON_DIR = os.path.join(os.path.dirname(__file__), 'icons')


class VolumeButton(QtWidgets.QToolButton):
    singleClicked = Signal()
    doubleClicked = Signal()

    def __init__(self, parent=None):
        QtWidgets.QToolButton.__init__(self, parent)
        self._ignore_release = False
        self._click_timer = QtCore.QTimer(self)
        self._click_timer.setSingleShot(True)
        self._click_timer.timeout.connect(self.singleClicked.emit)

    def mouseReleaseEvent(self, event):
        QtWidgets.QToolButton.mouseReleaseEvent(self, event)
        if event.button() != QtCore.Qt.LeftButton:
            return
        if self._ignore_release:
            self._ignore_release = False
            return
        interval = QtWidgets.QApplication.instance().doubleClickInterval()
        self._click_timer.start(interval)

    def mouseDoubleClickEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton:
            self._click_timer.stop()
            self._ignore_release = True
            self.doubleClicked.emit()
            event.accept()
            return
        QtWidgets.QToolButton.mouseDoubleClickEvent(self, event)


class CompactStack(QtWidgets.QStackedWidget):
    """A stack whose layout follows the current page, not its largest page."""

    def __init__(self, parent=None):
        QtWidgets.QStackedWidget.__init__(self, parent)
        self.currentChanged.connect(lambda *_: self.updateGeometry())

    def sizeHint(self):
        page = self.currentWidget()
        return page.sizeHint() if page is not None else QtCore.QSize()

    def minimumSizeHint(self):
        page = self.currentWidget()
        return page.minimumSizeHint() if page is not None else QtCore.QSize()


def _menu_item_option(widget, text='', selected=False):
    """A QStyleOptionMenuItem initialised for drawing one item through QStyle."""
    option = QtWidgets.QStyleOptionMenuItem()
    option.initFrom(widget)
    option.rect = widget.rect()
    option.menuRect = widget.rect()
    option.menuItemType = QtWidgets.QStyleOptionMenuItem.Normal
    option.checkType = QtWidgets.QStyleOptionMenuItem.NotCheckable
    option.text = text
    option.menuHasCheckableItems = False
    option.maxIconWidth = 0
    try:
        option.reservedShortcutWidth = 0
    except AttributeError:
        option.tabWidth = 0                 # older Qt
    option.state = QtWidgets.QStyle.State_Enabled
    if selected:
        option.state |= (QtWidgets.QStyle.State_Selected |
                         QtWidgets.QStyle.State_MouseOver)
    return option


class _OptionBoxRow(QtWidgets.QWidget):
    """One menu row painted as a single native menu item with an option box.

    Painting the whole item through QStyle (``CE_MenuItem``) in one widget gives
    the exact platform text margin and a highlight that spans edge to edge, then
    the option-box square is drawn on top at the far right. A left release hit
    tests which half was clicked. One widget avoids the seam, the gap at the
    right edge and the text shift that two sub-widgets produced.
    """
    primaryClicked = Signal()
    optionsClicked = Signal()
    BOX_WIDTH = 30

    def __init__(self, text, parent=None):
        super(_OptionBoxRow, self).__init__(parent)
        self._text = text
        self._hover = False
        self.setMouseTracking(True)
        self.setSizePolicy(QtWidgets.QSizePolicy.MinimumExpanding,
                           QtWidgets.QSizePolicy.Fixed)

    def sizeHint(self):
        option = _menu_item_option(self, self._text)
        metrics = self.fontMetrics()
        content = QtCore.QSize(
            metrics.horizontalAdvance(self._text) + 24 + self.BOX_WIDTH,
            metrics.height())
        return self.style().sizeFromContents(
            QtWidgets.QStyle.CT_MenuItem, option, content, self)

    def _box_rect(self):
        return QtCore.QRect(self.width() - self.BOX_WIDTH, 0,
                            self.BOX_WIDTH, self.height())

    def enterEvent(self, event):
        self._hover = True
        self.update()

    def leaveEvent(self, event):
        self._hover = False
        self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton:
            if self._box_rect().contains(event.pos()):
                self.optionsClicked.emit()
            elif self.rect().contains(event.pos()):
                self.primaryClicked.emit()
        super(_OptionBoxRow, self).mouseReleaseEvent(event)

    def paintEvent(self, event):
        painter = QtWidgets.QStylePainter(self)
        # Full-width menu item: highlight spans edge to edge, text sits on the
        # platform's menu gutter exactly like the neighbouring items. The menu
        # insets a widget-action's rect by ~1px; overdraw past our own edges
        # (clipped to the menu) so the highlight reaches the same extent as the
        # native items around it.
        option = _menu_item_option(self, self._text, self._hover)
        # Extend only the right edge: CE_MenuItem anchors its text to rect.left,
        # so growing the right keeps the text aligned with the other items while
        # the highlight reaches the same right extent they do.
        option.rect = self.rect().adjusted(0, 0, 2, 0)
        option.menuRect = option.rect
        painter.drawControl(QtWidgets.QStyle.CE_MenuItem, option)
        # The option box square, drawn on top at the far right.
        box = self._box_rect()
        color = (self.palette().highlightedText().color() if self._hover
                 else self.palette().text().color())
        pen = QtGui.QPen(color)
        pen.setWidth(1)
        painter.setPen(pen)
        center = box.center()
        painter.drawRect(center.x() - 5, center.y() - 5, 10, 10)
        painter.drawRect(center.x() - 2, center.y() - 2, 4, 4)


class OptionBoxAction(QtWidgets.QWidgetAction):
    """A menu action with a native-style option box (Maya's square on the right).

    Subclassing QWidgetAction and building the widget in ``createWidget`` lets a
    QMenu own the widget's lifetime (each menu that shows the action gets its own
    instance), which avoids the dangling-QMenu crash of a hand-placed widget.
    """
    primaryTriggered = Signal()
    optionsTriggered = Signal()

    def __init__(self, text, parent=None):
        super(OptionBoxAction, self).__init__(parent)
        self._text = text

    def createWidget(self, parent):
        row = _OptionBoxRow(self._text, parent)
        row.primaryClicked.connect(lambda: self._fire(self.primaryTriggered))
        row.optionsClicked.connect(lambda: self._fire(self.optionsTriggered))
        return row

    def _fire(self, signal):
        # Close the open menu without touching a possibly-dead QMenu reference,
        # then emit so the receiver can run (and open dialogs) cleanly.
        popup = QtWidgets.QApplication.activePopupWidget()
        if popup is not None:
            popup.close()
        signal.emit()


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
        self._closed = False
        self._shutting_down = False
        self._shutdown_clean = None
        self._shutdown_pending_jobs = []
        self._markers = set()

        # The one persisted-state store, created before any widget so their
        # build-time signals can read and write it without ordering guards.
        self.settings = Settings()

        # tracking state
        self.tracking_enabled = False
        self.tracked_centers = {}
        self.tracked_quads = {}
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

        # status-bar task progress (export, tracking, thumbnails, waveform, ...)
        self._tasks = {}
        self._task_order = []
        self._thumb_total = 1
        self._thumb_done = 0

        # undo/redo of drawings, erasings and range edits
        self._undo = UndoStack()
        self._drawing_snapshot = {}
        self._range_snapshot = (0, 0)
        self._suspend_undo = False
        self._history_frame = 0
        self._recent_drawing_edit = False
        self._navigation_step = None
        self._current_draw_mode = 'brush'
        self._draw_tool_sizes = {
            'brush': 6, 'eraser': 24,
            'rectangle': 3, 'ellipse': 3, 'arrow': 3}
        # Shared by the main Tools menu, tool rail and its context menu.
        self._tool_specs = (
            ('media', 'Media', 'Media and source information (V)'),
            ('crop', 'Crop', 'Crop tool (C)'),
            ('draw', 'Draw', 'Draw review annotations (D)'),
            ('track', 'Track', 'Track the crop forward (T)'),
            ('review', 'Review', 'Review overlays and display tools (R)'),
            ('export', 'Export', 'Export and output settings (X)'),
            ('shotgrid', 'Flow / ShotGrid', 'Flow / ShotGrid review versions (G)'),
        )

        self.surface = PlaybackSurface()
        # Keyboard shortcuts are window-subtree scoped (WidgetWithChildrenShortcut)
        # so they work while docked. That needs a focus target inside the panel:
        # let both the window and the viewer accept focus.
        self.setFocusPolicy(QtCore.Qt.StrongFocus)
        self.surface.setFocusPolicy(QtCore.Qt.StrongFocus)
        self._build_ui()
        # Flat, non-native buttons everywhere in the rail and side panel: the
        # normal state declares an explicit background so Qt does not fall back
        # to the platform's beveled/3D button frame.
        self.setStyleSheet('''
            QFrame#vcToolRail {{ background: {panel}; border-right: 1px solid {border}; }}
            QToolButton#vcToolButton {{ background: transparent;
                border: 1px solid transparent; border-radius: 3px; }}
            QToolButton#vcToolButton:hover {{ background: {hover}; }}
            QToolButton#vcToolButton:checked {{ background: {accent};
                border-color: {accent_hi}; }}

            QWidget#vcSide {{ background: {bg}; border-left: 1px solid {border}; }}
            QWidget#vcSide QToolButton {{ background: transparent;
                border: 1px solid transparent; border-radius: 3px; }}
            QWidget#vcSide QToolButton:hover {{ background: {hover}; }}
            QWidget#vcSide QToolButton:checked, QWidget#vcSide QToolButton:pressed {{
                background: {down}; border-color: {border}; }}
            QWidget#vcSide QPushButton {{ background: {control}; color: {text};
                border: 1px solid {border}; border-radius: 4px;
                min-height: 26px; padding: 2px 10px; }}
            QWidget#vcSide QPushButton:hover {{ background: {hover}; }}
            QWidget#vcSide QPushButton:pressed {{ background: {down}; }}
            QWidget#vcSide QPushButton:disabled {{ color: {dim}; }}
            QWidget#vcSide QPushButton[role="primary"] {{ background: {primary};
                color: {text}; border-color: {primary_border}; font-weight: bold; }}
            QWidget#vcSide QPushButton[role="primary"]:hover {{ background: {primary_hover}; }}
            QWidget#vcSide QPushButton[role="danger"] {{ background: {danger};
                color: {danger_text}; border-color: {danger_border}; }}
            QWidget#vcSide QPushButton[role="danger"]:hover {{ background: {danger_hover}; }}
            /* Utility tier: least-important actions read quieter and smaller
               than standard/primary buttons so weight tracks importance. */
            QWidget#vcSide QPushButton[role="utility"] {{ background: transparent;
                color: {dim}; border: 1px solid transparent;
                min-height: 21px; padding: 1px 8px; font-weight: normal; }}
            QWidget#vcSide QPushButton[role="utility"]:hover {{ background: {hover};
                color: {text}; border-color: {border}; }}
            QWidget#vcSide QPushButton[role="utility"]:pressed {{ background: {down}; }}
            QWidget#vcSide QPushButton[role="utility"]:disabled {{
                color: {dim}; background: transparent; border-color: transparent; }}
            QWidget#vcSide QToolButton[toolMode="true"] {{ background: {control};
                border: 1px solid {border}; border-radius: 4px; }}
            QWidget#vcSide QToolButton[toolMode="true"]:hover {{ background: {hover}; }}
            QWidget#vcSide QToolButton[toolMode="true"]:checked {{
                background: {toolmode}; border-color: {toolmode_border}; }}
            QWidget#vcSide QStackedWidget, QWidget#vcSide QScrollArea {{
                background: transparent; border: none; }}
            QWidget#vcSide #vcPageBody {{ background: transparent; }}
            QWidget#vcSide QFrame#vcCard {{ background: {panel};
                border: 1px solid {border}; border-radius: 6px; }}
            QLabel#vcCardTitle {{ color: {dim}; font-weight: bold; }}
            QToolButton#vcDisclosure {{ color: {text}; font-weight: bold;
                border: none; padding: 2px 0; text-align: left; }}
            QWidget#vcSide QToolButton#vcDisclosure:hover,
            QWidget#vcSide QToolButton#vcDisclosure:checked,
            QWidget#vcSide QToolButton#vcDisclosure:pressed {{
                color: {text}; background: transparent; border: none; }}
            QLabel#vcToolSubtitle {{ color: {dim}; padding-left: 6px; }}
            QLabel[dim="true"] {{ color: {dim}; }}
        '''.format(panel=style.PANEL, border=style.BORDER, hover=style.CONTROL_HOVER,
                   accent=style.ACCENT, accent_hi=style.ACCENT_HI, control=style.CONTROL,
                   down=style.CONTROL_DOWN, text=style.TEXT, dim=style.TEXT_DIM,
                   bg=style.BG, primary=style.PRIMARY, primary_hover=style.PRIMARY_HOVER,
                   primary_border=style.PRIMARY_BORDER, danger=style.DANGER,
                   danger_hover=style.DANGER_HOVER, danger_text=style.DANGER_TEXT,
                   danger_border=style.DANGER_BORDER, toolmode=style.TOOLMODE_CHECKED,
                   toolmode_border=style.TOOLMODE_CHECKED_BORDER))

        self.player = Player(self.ffmpeg, self.surface, self)
        self.waveform_job = WaveformJob(self.ffmpeg, self)
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
        # Debounced writer that keeps the scene's fileInfo current as review
        # state changes, so any scene save (or window close) carries it and it
        # travels with the file. ~1s after the last change, never during a load.
        self._scene_state_timer = QtCore.QTimer(self)
        self._scene_state_timer.setSingleShot(True)
        self._scene_state_timer.setInterval(1000)
        self._scene_state_timer.timeout.connect(self._write_scene_state)
        self.color_dialog = None            # lazily created color-correction tool

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
        self.act_loop.setChecked(settings.get_bool('playback/loop', True))
        self.player.set_loop(self.act_loop.isChecked())
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
            self._draw_tool_sizes[mode] = int(utils.clamp(
                settings.get_int(key, default), 1, 150 if mode == 'eraser' else 100))
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
        self._splitter.setSizes([880, 300])
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
        self.status_bar = QtWidgets.QWidget()
        status_row = QtWidgets.QHBoxLayout(self.status_bar)
        status_row.setContentsMargins(4, 1, 4, 1)
        status_row.setSpacing(8)
        status_row.addWidget(self.progress, 0, QtCore.Qt.AlignVCenter)
        status_row.addWidget(self.status_lbl, 1, QtCore.Qt.AlignVCenter)
        root.addWidget(self.status_bar)

    def _build_workspace(self):
        workspace = QtWidgets.QWidget()
        row = QtWidgets.QHBoxLayout(workspace)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(3)

        rail = QtWidgets.QFrame()
        rail.setObjectName('vcToolRail')
        self.toolbar = rail
        rail.setFixedWidth(43)
        tools = QtWidgets.QVBoxLayout(rail)
        tools.setContentsMargins(3, 4, 3, 4)
        tools.setSpacing(2)
        self._tool_buttons = {}
        self._tool_group = QtWidgets.QButtonGroup(self)
        self._tool_group.setExclusive(True)
        for key, _name, tooltip in self._tool_specs:
            button = QtWidgets.QToolButton()
            button.setObjectName('vcToolButton')
            button.setAutoRaise(True)          # flat; the stylesheet draws state
            button.setIcon(self._icon(key))
            button.setIconSize(QtCore.QSize(22, 22))
            button.setToolTip(tooltip)
            button.setCheckable(True)
            button.setFixedSize(35, 35)
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

        # Conventional top-level order: project I/O, editing, playback,
        # presentation, tools, then help. This keeps pointer travel predictable
        # and avoids splitting related display options across Review/Window.
        file_menu = bar.addMenu('File')
        self.act_open = action(file_menu, 'Open Media or Review…', self._choose_open,
                               shortcut='Ctrl+O')
        self.recent_menu = file_menu.addMenu('Recent Media')
        self.recent_menu.aboutToShow.connect(self._populate_recent_menu)
        self._populate_recent_menu()  # build once so it is never empty on show
        file_menu.addSeparator()
        self.act_import = action(file_menu, 'Import Media…', self._choose_media, shortcut='I')
        self._add_option_box_item(
            file_menu, 'Playblast Current Shot',
            self._playblast_current_shot, self._show_playblast_options)
        notes_menu = file_menu.addMenu('Review Notes')
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
        action(edit_menu, 'Delete Selected Notes', self.surface.delete_selected_strokes)
        action(edit_menu, 'Clear Notes on Current Frame', self.surface.clear_current_drawing)
        action(edit_menu, 'Create Blank Annotation Frame', self.surface.create_blank_drawing)
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
        self.act_loop = action(play_menu, 'Loop', self.player_set_loop, checkable=True,
                               shortcut='L')
        action(play_menu, 'Set In Point', self._mark_in, shortcut='[')
        action(play_menu, 'Set Out Point', self._mark_out, shortcut=']')
        action(play_menu, 'Reset In / Out', self._reset_marked_range, shortcut='Shift+R')
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
        speed_menu = play_menu.addMenu('Speed')
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

        view_menu = bar.addMenu('View')
        action(view_menu, 'Fit Media to View', self.surface.fit_view, shortcut='F')
        action(view_menu, 'Zoom In', lambda: self.surface.zoom_by(1.25), shortcut='Ctrl++')
        action(view_menu, 'Zoom Out', lambda: self.surface.zoom_by(0.8), shortcut='Ctrl+-')
        self.act_fullscreen = action(view_menu, 'Full Screen', self._toggle_fullscreen,
                                     checkable=True, checked=False, shortcut='F11')
        view_menu.addSeparator()
        self.act_toolbar = action(view_menu, 'Tool Rail', self._toggle_toolbar,
                                  checkable=True, checked=True)
        self.act_side = action(view_menu, 'Properties Panel', self._toggle_side,
                               checkable=True, checked=False)
        self.act_timeline = action(view_menu, 'Timeline', self._toggle_timeline,
                                   checkable=True, checked=True)
        self.act_transport = action(view_menu, 'Playback Controls', self._toggle_transport,
                                    checkable=True, checked=True)
        self.act_statusbar = action(view_menu, 'Status Bar', self._toggle_statusbar,
                                    checkable=True, checked=True)
        view_menu.addSeparator()
        self.act_thumbs = action(view_menu, 'Timeline Thumbnails', self._toggle_thumbnails,
                                 checkable=True, checked=True)
        self.act_wave = action(view_menu, 'Audio Waveform', self._toggle_waveform,
                               checkable=True, checked=True)
        self.act_onion = action(view_menu, 'Neighboring Notes (Onion Skin)',
                                self._toggle_onion, checkable=True,
                                shortcut='Ctrl+Shift+O')
        self.act_crop = action(view_menu, 'Crop Overlay', self.surface.set_crop_visible,
                               checkable=True, checked=True)
        view_menu.addSeparator()
        self.act_flip_h = action(view_menu, 'Flip Horizontally', self._on_flip_changed,
                                 checkable=True, shortcut='Shift+X')
        self.act_flip_v = action(view_menu, 'Flip Vertically', self._on_flip_changed,
                                 checkable=True, shortcut='Shift+Y')

        tools_menu = bar.addMenu('Tools')
        for key, name, tooltip in self._tool_specs:
            shortcut = {'media': 'V', 'crop': 'C', 'draw': 'D', 'track': 'T',
                        'review': 'R', 'export': 'X', 'shotgrid': 'G'}[key]
            act = action(tools_menu, name, lambda selected=key: self._show_tool(selected),
                         shortcut=shortcut)
            act.setToolTip(tooltip)
        tools_menu.addSeparator()
        action(tools_menu, 'Brush', lambda: self._activate_draw_mode('brush'),
               shortcut='B')
        action(tools_menu, 'Eraser', lambda: self._activate_draw_mode('eraser'),
               shortcut='E')

        help_menu = bar.addMenu('Help')
        action(help_menu, 'About FrameReviewer', self._about)
        return bar

    def _build_side(self):
        side = QtWidgets.QWidget()
        side.setObjectName('vcSide')
        side.setMinimumWidth(270)
        side.setMaximumWidth(380)
        column = QtWidgets.QVBoxLayout(side)
        column.setContentsMargins(8, 6, 8, 6)
        column.setSpacing(5)

        header = QtWidgets.QHBoxLayout()
        header.setSpacing(2)
        self.tool_title = QtWidgets.QLabel('MEDIA')
        title_font = self.tool_title.font()
        title_font.setBold(True)
        self.tool_title.setFont(title_font)
        close = QtWidgets.QToolButton()
        close.setText('×')
        close.setToolTip('Close tool pane')
        close.clicked.connect(lambda: self.act_side.setChecked(False))
        self.tool_subtitle = QtWidgets.QLabel('Source and clip information')
        self.tool_subtitle.setObjectName('vcToolSubtitle')
        self.tool_subtitle.setTextFormat(QtCore.Qt.PlainText)
        header.addWidget(self.tool_title)
        header.addWidget(self.tool_subtitle, 1)
        header.addWidget(close)
        column.addLayout(header)

        self.tool_stack = QtWidgets.QStackedWidget()
        column.addWidget(self.tool_stack, 1)
        self._tool_pages = {}

        def page(name):
            scroll = QtWidgets.QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
            scroll.viewport().setAutoFillBackground(False)
            widget = QtWidgets.QWidget()
            widget.setObjectName('vcPageBody')
            layout = QtWidgets.QVBoxLayout(widget)
            layout.setContentsMargins(0, 0, 4, 0)
            layout.setSpacing(6)
            scroll.setWidget(widget)
            self.tool_stack.addWidget(scroll)
            self._tool_pages[name] = scroll
            return layout

        def section(layout, title, collapsible=False, collapsed=False):
            """Build a compact card with optional progressive disclosure."""
            card = QtWidgets.QFrame()
            card.setObjectName('vcCard')
            card_layout = QtWidgets.QVBoxLayout(card)
            card_layout.setContentsMargins(8, 5, 8, 7)
            card_layout.setSpacing(4)
            if collapsible:
                heading = QtWidgets.QToolButton()
                heading.setObjectName('vcDisclosure')
                heading.setText(title)
                heading.setCheckable(True)
                heading.setChecked(not collapsed)
                heading.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
                heading.setArrowType(
                    QtCore.Qt.DownArrow if not collapsed else QtCore.Qt.RightArrow)
            else:
                heading = QtWidgets.QLabel(title)
                heading.setObjectName('vcCardTitle')
            card_layout.addWidget(heading)

            content = QtWidgets.QWidget()
            inner = QtWidgets.QVBoxLayout(content)
            inner.setContentsMargins(0, 0, 0, 0)
            inner.setSpacing(4)
            card_layout.addWidget(content)
            inner.heading = heading
            inner.content = content
            inner.card = card
            if collapsible:
                content.setVisible(not collapsed)

                def toggle(opened, body=content, button=heading):
                    body.setVisible(opened)
                    button.setArrowType(QtCore.Qt.DownArrow if opened
                                        else QtCore.Qt.RightArrow)

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
        self.select_mode_btn = QtWidgets.QToolButton()
        self.select_mode_btn.setIcon(self._icon('select'))
        self.select_mode_btn.setToolTip('Select, marquee, move, scale, rotate notes')
        self.brush_mode_btn = QtWidgets.QToolButton()
        self.eraser_mode_btn = QtWidgets.QToolButton()
        self.rect_mode_btn = QtWidgets.QToolButton()
        self.ellipse_mode_btn = QtWidgets.QToolButton()
        self.arrow_mode_btn = QtWidgets.QToolButton()
        self.brush_mode_btn.setIcon(self._icon('brush'))
        self.eraser_mode_btn.setIcon(self._icon('eraser'))
        self.rect_mode_btn.setIcon(self._icon('rectangle'))
        self.ellipse_mode_btn.setIcon(self._icon('ellipse'))
        self.arrow_mode_btn.setIcon(self._icon('arrow'))
        mode_buttons = (
            self.select_mode_btn, self.brush_mode_btn, self.eraser_mode_btn, self.rect_mode_btn,
            self.ellipse_mode_btn, self.arrow_mode_btn)
        tool_names = ('Select', 'Brush', 'Eraser', 'Rectangle', 'Ellipse', 'Arrow')
        for button, tool_name in zip(mode_buttons, tool_names):
            button.setCheckable(True)
            button.setAutoRaise(False)
            button.setProperty('toolMode', True)
            button.setFixedSize(34, 32)
            button.setIconSize(QtCore.QSize(19, 19))
            button.setToolTip(tool_name)
            button.setAccessibleName(tool_name)
            tool_row.addWidget(button)
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
        range_box = section(export_page, 'Export Range')
        self.export_range_lbl = QtWidgets.QLabel('No media loaded')
        range_box.addWidget(self.export_range_lbl)
        self.export_btn = QtWidgets.QPushButton('Export Media…')
        self.export_btn.setIcon(self._icon('export'))
        self.export_btn.setProperty('role', 'primary')
        self.export_btn.setMinimumHeight(30)
        export_page.addWidget(self.export_btn)
        export_page.addStretch(1)

        # ShotGrid lives in its own scrollable panel widget rather than the
        # shared page() scaffold, so the browser/notes manage their own layout.
        sg_scroll = QtWidgets.QScrollArea()
        sg_scroll.setWidgetResizable(True)
        sg_scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.shotgrid_panel = ShotGridPanel(
            load_path=self._load_path, settings=self.settings, parent=self)
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
        self.player.cacheChanged.connect(self.timeline.set_cache_ranges)
        self.player.bufferProgressChanged.connect(self.timeline.set_buffer_progress)
        self.player.playingChanged.connect(self._on_playing_changed)
        self.player.errorText.connect(
            lambda m: self.status_lbl.setText('Playback error: ' + html.escape(m)))

        self.surface.cropChanged.connect(self._on_crop_changed)
        self.surface.scrubRequested.connect(lambda d: self._scrub(self.player.current_frame + d))
        self.surface.mediaDropped.connect(self._load_path)
        self.surface.importRequested.connect(self._choose_media)
        self.surface.edited.connect(self._on_surface_edited)
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
                ('arrow', self.arrow_mode_btn)):
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
        self.thumb_job.started.connect(
            lambda total: self.timeline.begin_thumbnails(total, THUMB_LANE_H))
        self.thumb_job.started.connect(self._on_thumbs_started)
        self.thumb_job.produced.connect(self.timeline.add_thumbnail)
        self.thumb_job.produced.connect(lambda *_: self._on_thumb_produced())
        self.thumb_job.finished.connect(lambda: self._task_end('thumbs'))

        self._shortcuts = []
        for keys, callback in (
            ('Up', lambda: self._scrub(self.player.in_frame)),
            ('Down', lambda: self._scrub(self.player.out_frame)),
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
            '(*.json *.mp4 *.mov *.mkv *.webm *.avi *.m4v '
            '*.jpg *.jpeg *.png *.tga *.exr);;All files (*)')
        if path:
            self._load_path(path)

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
            self._load_path(files[0])
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, 'Playblast failed', str(exc))

    def _choose_media(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, 'Import media', os.path.expanduser('~'),
            'Media (*.mp4 *.mov *.mkv *.webm *.avi *.m4v '
            '*.jpg *.jpeg *.png *.tga *.exr);;All files (*)')
        if path:
            self._load_path(path)

    def _remember_recent(self, path):
        self.settings.add_recent(path)
        self._populate_recent_menu()  # reflect the new entry immediately

    def _populate_recent_menu(self):
        if self._closed:
            return
        try:
            self.recent_menu.clear()
            paths = self.settings.recent_files()
            if not paths:
                empty = self.recent_menu.addAction('No Recent Media')
                empty.setEnabled(False)
                return
            for path in paths:
                label = os.path.basename(path) or path
                recent_action = self.recent_menu.addAction(label)
                recent_action.setToolTip(path)
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
            self._load_path(path)
            return
        self.settings.remove_recent(path)
        self._populate_recent_menu()
        QtWidgets.QMessageBox.warning(
            self, 'Missing media', 'This recent file no longer exists:\n' + path)

    def _clear_recent_media(self):
        self.settings.clear_recent()
        self._populate_recent_menu()

    def _load_path(self, path):
        """Shared entry point for the file dialog, the placeholder, and drops."""
        if path.lower().endswith('.json'):
            self._open_notes_path(path)
            return
        if self._closed or (self._export_job is not None and self._export_job.isRunning()):
            return
        try:
            info = ffmpegtools.probe(self.ffprobe, QtGui.QImageReader, path)
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, 'Import failed', str(exc))
            return
        self._load(info)

    def _load(self, info):
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
        self.info = info
        self.surface.set_placeholder_visible(False)
        self._clear_tracking(update=False)
        self.waveform_job.cancel()
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

        self.player.load(info)
        self._remember_recent(info.path)
        self.player.set_loop(self.act_loop.isChecked())
        self.player.set_speed(float(self.speed_combo.currentData() or 1.0))

        self._set_media_window_title(info.path)
        self.media_lbl.setToolTip(info.path)
        self.media_lbl.setText('{} × {}\n{:g} fps  ·  {} frames'.format(
            info.width, info.height, info.fps, info.frame_count))
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

    def _schedule_scene_state_save(self):
        """Queue a debounced write of review state into the scene's fileInfo."""
        if (cmds is None or self._closed or
                getattr(self, '_restoring_scene_state', False)):
            return
        self._scene_state_timer.start()

    def _write_scene_state(self):
        if (cmds is None or self._closed or
                getattr(self, '_restoring_scene_state', False)):
            return
        scene_state.save_to_scene(self)

    def _restore_scene_state_on_launch(self):
        """On open, reload whatever media this scene stored."""
        if cmds is None:
            return
        state = scene_state.load_scene_state()
        if state is None:
            return
        QtCore.QTimer.singleShot(
            0, lambda: None if self._closed else scene_state.apply_state(self, state))

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
                   self.arrow_mode_btn,
                   self.delete_selected_btn,
                   self.clear_drawing_btn, self.undo_drawing_btn,
                   self.blank_drawing_btn, self.delete_all_notes_btn,
                   self.prev_drawing_btn, self.next_drawing_btn,
                   self.import_notes_btn, self.export_notes_btn,
                   self.composition_combo, self.color_btn, self.fit_btn,
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
                    self.act_next_marker, self.act_clear_markers):
            act.setEnabled(on)

    # ----------------------------------------------------------- playback
    def player_set_loop(self, enabled):
        self.player.set_loop(enabled)

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
        if (not self.sync_btn.isChecked() or self._sync_updating or
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
        if (target is None or self._closed or self.info is None or
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
        if (not self.sync_btn.isChecked() or self._sync_updating or
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
        if (maya_frame is None or self._closed or cmds is None or
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
        if self.player.playing:
            self.player.pause()
        self.player.seek(frame)

    def _on_frame_changed(self, frame):
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
        self.timeline.set_frame(frame)
        self.surface.set_current_frame(frame)
        self._sync_maya_from_player(frame)
        if self.tracking_enabled:
            self._apply_tracked_crop()
        # Keep the scene's stored playhead current (debounced; fires once the
        # frame settles, never during continuous playback).
        self._schedule_scene_state_save()

    def _on_playing_changed(self, playing):
        if playing:
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
            'media': ('MEDIA', 'Clip & source'),
            'crop': ('CROP', 'Frame region'),
            'draw': ('DRAW', 'Annotations'),
            'track': ('TRACK', 'Crop motion'),
            'review': ('REVIEW', 'Display'),
            'export': ('EXPORT', 'Output'),
            'shotgrid': ('FLOW / SHOTGRID', 'Review versions'),
        }
        title, subtitle = titles.get(name, (name.upper(), ''))
        self.tool_title.setText(title)
        self.tool_subtitle.setText(subtitle)
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
        self._current_draw_mode = mode
        self.surface.set_draw_mode(mode)
        labels = {
            'select': ('Selection', 'Size'),
            'brush': ('Brush Tip', 'Size'),
            'eraser': ('Eraser Tip', 'Diameter'),
            'rectangle': ('Rectangle Outline', 'Width'),
            'ellipse': ('Ellipse Outline', 'Width'),
            'arrow': ('Arrow Outline', 'Width')}
        heading, label = labels[mode]
        self.draw_tip_title.setText(heading)
        self.draw_width_caption.setText(label)
        maximum = 150 if mode == 'eraser' else 100
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

    def _export_notes(self):
        drawings = self.surface.drawing_data()
        if not drawings:
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
        payload = {
            'format': 'frame-review-notes',
            'version': 1,
            'source': ({
                'path': self.info.path,
                'width': self.info.width,
                'height': self.info.height,
                'frame_count': self.info.frame_count,
                'fps': self.info.fps,
            } if self.info is not None else {}),
            'notes': [{'frame': frame, 'strokes': strokes}
                      for frame, strokes in sorted(drawings.items())],
        }
        try:
            with open(path, 'w') as stream:
                json.dump(payload, stream, indent=2, sort_keys=True)
                stream.write('\n')
        except (IOError, OSError, TypeError, ValueError) as exc:
            QtWidgets.QMessageBox.critical(self, 'Export notes failed', str(exc))
            return
        self.status_lbl.setText(
            'Exported {} annotation key{}.'.format(
                len(drawings), '' if len(drawings) == 1 else 's'))

    def _open_notes_path(self, path):
        """Restore the media and annotations recorded in a notes JSON."""
        if self._closed or (self._export_job is not None and self._export_job.isRunning()):
            return
        try:
            if os.path.getsize(path) > 20 * 1024 * 1024:
                raise ValueError('The notes file is larger than 20 MB.')
            with open(path, 'r') as stream:
                payload = json.load(stream)
            if not isinstance(payload, dict) or payload.get('format') != 'frame-review-notes':
                raise ValueError('This is not a FrameReviewer notes file.')
            if int(payload.get('version', 0)) != 1:
                raise ValueError('Unsupported notes file version.')
            source = payload.get('source')
            if (not isinstance(source, dict) or
                    not isinstance(source.get('path'), str) or
                    not source['path'].strip()):
                raise ValueError('The notes file has no source media path.')
            entries = payload.get('notes', [])
            if not isinstance(entries, list):
                raise ValueError('The notes field must be a list.')
            drawings = {}
            for entry in entries:
                if not isinstance(entry, dict) or 'frame' not in entry:
                    raise ValueError('Invalid annotation entry.')
                frame = int(entry['frame'])
                if frame < 0 or frame in drawings:
                    raise ValueError('Invalid or duplicate frame.')
                strokes = entry.get('strokes', [])
                if not isinstance(strokes, list):
                    raise ValueError('Invalid annotation strokes.')
                drawings[frame] = strokes
            original = os.path.expanduser(os.path.expandvars(source['path']))
            folder = os.path.dirname(os.path.abspath(path))
            paths = ([os.path.join(folder, original)] if not os.path.isabs(original) else [])
            paths += [original, os.path.join(folder, os.path.basename(original))]
            media = next((candidate for candidate in paths if os.path.isfile(candidate)), None)
            if media is None:
                raise IOError('Source media missing: ' + source['path'])
            info = ffmpegtools.probe(self.ffprobe, QtGui.QImageReader, media)
        except (IOError, OSError, TypeError, ValueError, KeyError, OverflowError) as exc:
            QtWidgets.QMessageBox.critical(self, 'Open notes failed', str(exc))
            return
        self._load(info)
        try:
            self.surface.set_drawing_data(drawings, replace=True)
        except (TypeError, ValueError) as exc:
            QtWidgets.QMessageBox.critical(self, 'Open notes failed', str(exc))
            return
        self._remember_recent(path)
        self.status_lbl.setText('Opened {} with {} note keys.'.format(
            os.path.basename(media), len(drawings)))

    def _import_notes(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, 'Import notes', self._notes_directory(),
            'FrameReviewer Notes (*.json);;JSON files (*.json)')
        if not path:
            return
        try:
            if os.path.getsize(path) > 20 * 1024 * 1024:
                raise ValueError('The notes file is larger than 20 MB.')
            with open(path, 'r') as stream:
                payload = json.load(stream)
            if not isinstance(payload, dict):
                raise ValueError('The notes file must contain a JSON object.')
            if payload.get('format') != 'frame-review-notes':
                raise ValueError('This is not a FrameReviewer notes file.')
            if int(payload.get('version', 0)) != 1:
                raise ValueError('Unsupported notes file version.')
            entries = payload.get('notes', [])
            if not isinstance(entries, list):
                raise ValueError('The notes field must be a list.')
            drawings = {}
            for entry in entries:
                if not isinstance(entry, dict):
                    raise ValueError('Every annotation key must be an object.')
                drawings[int(entry['frame'])] = entry.get('strokes', [])
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
            self.surface.set_drawing_data(drawings, replace=replace)
        except (TypeError, ValueError) as exc:
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

    def _on_color_changed(self, params, enabled):
        self.surface.set_color_correction(params if enabled else None)

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
        if self.info.is_sequence:
            self.timeline.set_waveform([])
            return
        # No fine-grained progress from a single decode -> a busy indicator.
        self._task_begin('waveform', 'Audio waveform', determinate=False)
        self.waveform_job.request(self.info.path, self.info.duration)

    # --------------------------------------------------------------- crop
    def _on_crop_changed(self, rect):
        crop = self.surface.crop()
        if crop is None:
            self.crop_lbl.setText('None (full frame)')
            if self.tracking_enabled and not self._applying_tracked:
                self._clear_tracking(update=False)
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

    def _on_flip_changed(self, *args):
        horizontal = self.act_flip_h.isChecked()
        vertical = self.act_flip_v.isChecked()
        changed_h = horizontal != getattr(self, '_current_flip_h', False)
        changed_v = vertical != getattr(self, '_current_flip_v', False)
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

    # ----------------------------------------------------------- tracking
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
        for signal in (job.progress, job.result, job.error):
            try:
                signal.disconnect()
            except (RuntimeError, TypeError):
                pass
        self._task_end('track')

    def _clear_tracking(self, update=True):
        if update:
            self._cancel_tracking_job()
            if not self._closed:
                self.track_btn.setEnabled(self.info is not None and tracking.HAVE_CV2)
                self.tracking_mode.setEnabled(tracking.HAVE_CV2)
        self.tracking_enabled = False
        self.tracked_centers = {}
        self.tracked_quads = {}
        self.tracking_size = None
        self.tracking_base_size = None
        self.tracking_offset = QtCore.QPointF()
        if self.tracking_lbl is not None:
            self.tracking_lbl.setText('No tracking')
        if hasattr(self.surface, 'set_tracked_quad'):
            self.surface.set_tracked_quad(None)

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
        self.player.pause()
        self._cancel_tracking_job()
        # A cancelled reader can finish after this window is gone.
        job = TrackingJob(
            self.ffmpeg, self.info.path, self.info.sequence_files, self.info.fps,
            start, end, crop, self.act_flip_h.isChecked(), self.act_flip_v.isChecked(),
            QtCore.QCoreApplication.instance(), mode=self.tracking_mode.currentData(),
            frame_size=(self.info.width, self.info.height))
        self._tracking_job = job
        job.progress.connect(
            lambda p: (self.tracking_lbl.setText('Tracking… {}%'.format(p)),
                       self._task_update('track', p))
            if self._tracking_job is job and not self._closed else None)
        job.result.connect(
            lambda centers, size, quads: self._on_tracking_result(centers, size, quads)
            if self._tracking_job is job and not self._closed else None)
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
        self.status_lbl.setText('Tracking complete. Move/resize the crop to offset it.')

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

    def _export(self):
        if self.info is None or self._export_job is not None:
            return
        try:
            out_w, out_h = self._parse_out_res()
        except ValueError as exc:
            QtWidgets.QMessageBox.warning(self, 'Export settings', str(exc))
            return
        # H.264 (yuv420p) requires even dimensions; round down so a crop like
        # 378x427 does not fail the encoder.
        out_w = max(2, out_w - out_w % 2)
        out_h = max(2, out_h - out_h % 2)
        dialog = ExportOptionsDialog(self, self.info.path, os.path.dirname(self.info.path))
        result = dialog.exec() if hasattr(dialog, 'exec') else dialog.exec_()
        if result != QtWidgets.QDialog.Accepted:
            return
        export_fps = self.info.fps if self.fps_combo.currentData() == 'source' else 24.0
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
            tracking_offset=(self.tracking_offset.x(), self.tracking_offset.y()))
        conflicts = dialog.conflicts()
        try:
            for target in set(conflicts):
                if os.path.isdir(target) and not os.path.islink(target):
                    shutil.rmtree(target)
                elif os.path.lexists(target):
                    os.unlink(target)
            contexts = []
            for path in dialog.paths():
                os.makedirs(os.path.dirname(path), exist_ok=True)
                contexts.append(dict(snapshot, path=path, sequence=('.####.' in path)))
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, 'Export path error', str(exc))
            return
        self.player.pause()
        self._export_queue = contexts
        self._export_done = []
        self._export_failed = False
        self._export_next()

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
        from . import __version__
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
            try:
                self._save_preferences()
            except (RuntimeError, TypeError):
                pass
            # Persist the current review state (including the player's current
            # time) into the scene fileInfo and the last-session store before
            # the player tears down, so closing the window keeps the playhead.
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
            self.waveform_job.cancel(wait=True)
            self.thumb_job.cancel(wait=True)

            tracking_job = self._tracking_job
            self._cancel_tracking_job()
            if not self._stop_thread(tracking_job):
                self._shutdown_pending_jobs.append(tracking_job)
                clean = False

            export_job = self._export_job
            self._export_job = None
            if export_job is not None:
                for signal in (export_job.progress, export_job.result,
                               export_job.error):
                    try:
                        signal.disconnect()
                    except (RuntimeError, TypeError):
                        pass
            if not self._stop_thread(export_job, 3500):
                self._shutdown_pending_jobs.append(export_job)
                clean = False
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


