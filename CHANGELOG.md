# Changelog

## Unreleased

- Renamed the application, its About dialog and Maya workspace identifiers to FrameReviewer.
- Added configurable UI region visibility and renamed the side panel to Properties.
- Added independent timeline bookmarks and dominant-color note indicators.
- Added 1-point position, 2-point similarity, 3-point affine, and perspective tracking with smoothed quadrilateral preview and export.
- Corrected crop, annotation and tracking overlay behavior under image flips.
- Improved rolling FFmpeg decoding, cache reuse and end-of-range behavior.
- Improved audio playback for FFmpeg fallback in Maya's Qt5 environment and seek/loop synchronization.
- Coalesced rapid manual seeks, used the audio clock to keep playback smooth during video cache misses, and deferred Qt multimedia teardown beyond sender callbacks.
- Throttled Maya timeline synchronization to avoid evaluation feedback during Play, and retained running workers across interrupted shutdown/reload until they can be stopped safely.

## 3.0.0

- Initial FrameReviewer package name and Maya entrypoints.
