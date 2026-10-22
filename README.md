# FrameReviewer

A media review and crop tool for Autodesk Maya, with frame-accurate FFmpeg playback, in/out marks, review annotations, animated crop tracking, and export.

FrameReviewer is a Python package designed to live in Maya's scripts directory.

## Features

- Media playback for video clips and image sequences, including scrubbing, variable speed, marked-range looping, volume/mute, and a rolling FFmpeg frame cache.
- Optional thumbnail and waveform timeline lanes, colored review-note keys, and independent frame bookmarks.
- Review drawing tools (brush, eraser, rectangle, ellipse, and arrow), undo/redo, import/export of note JSON.
- Notes Select tool: click or Shift-click individual strokes, drag a marquee to select multiple strokes, move by dragging the selection, scale using four corner handles (Shift locks the aspect ratio), and rotate using the top circular handle (Shift snaps to 15°). Delete/Backspace removes selected strokes; Escape deselects. Transforms modify stroke coordinates and participate in note undo/redo.
- Onion skin for annotated keyframes, with independent previous/next depth (0–20), opacity (0–100%), optional distance falloff, warm/cool directional tinting, and persistent controls under Properties → Draw. Ghost strokes never modify exported notes.
- **File → Open** (Ctrl+O) opens media or a FrameReviewer notes JSON, resolving the associated source clip and loading its annotations.
- **File → Playblast Current Shot** captures the current Maya Camera Sequencer shot (or the playback range when no shot is active) as a temporary JPEG image sequence and immediately loads it.
- Raster PNG icons bundled alongside the original SVGs for reliable rendering across Maya/Qt installations.
- Crop controls with horizontal/vertical flips and adjustable output resolution.
- OpenCV-powered crop tracking: position (1 point), rotation/scale (2 points), affine (3 points), and perspective using at least four image features. Tracks include filtering of bad matches and optional rectification on export.
- UI controls for Toolbar, Properties, Timeline, Playback Controls, and Status Bar visibility, remembered per user.
- Optional Flow Production Tracking / ShotGrid integration when available in your studio environment.

## Requirements

| Dependency | Purpose |
| --- | --- |
| Autodesk Maya with PySide2 or PySide6 | Host UI, docking, Maya timeline synchronization |
| Python 3 compatible with your Maya release | Runtime |
| FFmpeg **and** FFprobe available on PATH | Probe, cache, timeline media decoding, and export |
| QtMultimedia | Audio playback and optional native video playback |
| NumPy and OpenCV (`cv2`) | **Optional:** tracking and perspective export |
| ShotGrid Python API / studio credentials | **Optional:** Flow Production Tracking browser |

If OpenCV or NumPy cannot be imported in Maya, the tracking interface is disabled with an explanation; other review tools remain available.

FFmpeg binaries may be set explicitly:

```bash
export FRAMEREVIEWER_FFMPEG=/path/to/ffmpeg
export FRAMEREVIEWER_FFPROBE=/path/to/ffprobe
```

For existing installations, `VIDEOCROPPER_FFMPEG` and `VIDEOCROPPER_FFPROBE` are accepted as compatibility fallbacks.

## Install in Maya

1. Place the **entire** `framereviewer` folder inside a directory on your `MAYA_SCRIPT_PATH` or directly in your user Maya scripts directory. Preserve its `__init__.py` and `icons/` folder.
2. Start Maya and run:

   ```python
   import framereviewer
   framereviewer.show()
   ```

3. For a shelf button, use the same two-line Python command. To load UI updates while developing:

   ```python
   import framereviewer
   framereviewer.reload()
   ```

The package's `restore()` and `shutdown()` entrypoints manage Maya workspace controls and background workers. Use these entrypoints rather than deleting the widget directly.

**Important:** The package import is lowercase `framereviewer`. Folder names on case-sensitive filesystems must match. Existing shelf scripts that import `videocropper` or refer to `VideoCropperWindow` must be updated to `framereviewer` and `FrameReviewerWindow`.

## Review and tracking workflow

1. Import media, open a saved notes JSON through **File → Open**, or choose **File → Playblast Current Shot** in Maya. Use the bottom timeline or arrow keys to navigate, and mark In/Out with `[` and `]`.
2. Open **Properties → Crop**, draw the crop in the viewer, and adjust it as needed.
3. Open **Properties → Track**. Choose position, similarity, affine, or perspective, then select **Track Forward**. Tracking requires image texture in the selected crop for reliable optical flow.
4. Play or scrub to inspect the tracked crop. You may adjust its position and size afterward.
5. Export the marked range and choose dimensions and destination in the export dialog.

Perspective mode needs multiple matched features; three points define an affine transform, while full homography requires four or more non-collinear correspondences. If features are not reliable, the crop holds its last good estimate instead of jumping to a false location.

## Playback troubleshooting

- **No audio:** check that QtMultimedia can load in Maya, the clip has a supported audio stream, and the volume is not muted. FFmpeg frame playback and audio playback use separate components.
- **Playback stalls or flashes an error:** confirm that FFmpeg/FFprobe are executable within Maya's environment. Run the binaries from a shell with the same studio environment to reveal dynamic-library conflicts.
- **Maya closes after repeated scrubbing/Play:** avoid unloading Python modules with active decoder threads. Use `framereviewer.shutdown()` before reloading the package. Report the Maya version, Qt binding, codec, and a minimal reproduction.
- **Tracking disabled:** install compatible OpenCV and NumPy builds in Maya's own Python environment, avoiding incompatible global Python packages.

## Icon assets

Icons are shipped as both SVG and 96×96 PNG in `icons/`. The GUI prefers PNG to avoid QtSvg plugin problems on studio machines; SVG sources are retained for editing. To rebuild PNGs after changing SVGs, install `cairosvg` and run `python tools/bake_icons.py` from the repository root.

## Development

```bash
python -m compileall -q framereviewer
```

The package deliberately avoids bundling FFmpeg, studio credentials, and vendor libraries. FFmpeg decode workers own subprocess handles; decoded Qt images are transferred to the main thread before conversion to `QPixmap`. Keep that lifetime/thread distinction when changing playback.

### Package layout

| Module | Responsibility |
| --- | --- |
| `window.py`, `surface.py`, `timeline.py` | GUI, annotation and crop overlays, timeline |
| `player.py`, `frames.py` | Playback state and timing |
| `rolling_cache.py`, `ffmpegtools.py` | Rolling FFmpeg decode and media probing |
| `tracking.py`, `export.py` | Optical flow/corner tracking and export |
| `shotgrid.py`, `shotgrid_ui.py` | Optional Flow Production Tracking integration |
| `settings.py`, `qtcompat.py` | Preferences and Qt compatibility |

## Status

Active Maya pipeline tool. Playback and tracking changes should be verified within the specific Maya build and with representative studio footage before broader deployment.
