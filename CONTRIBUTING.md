# Contributing

This tool runs inside Maya's event loop. Please test changes on both PySide2 and PySide6 when possible.

1. Keep FFmpeg subprocess work and image decoding out of the GUI thread.
2. Construct QPixmap objects only in the GUI thread.
3. On file changes, seeks and shutdown, invalidate stale decode callbacks before deleting Qt objects.
4. Preserve the public `framereviewer.show()`, `restore()`, `shutdown()`, and `reload()` APIs.
5. Preserve existing annotation JSON and migrated QSettings data.
6. Run `python -m compileall -q framereviewer` from the parent directory.
7. Reproduce playback bugs with a clip containing audio and with rapid seek/play/pause cycles; include the Maya version and codec in reports.

Never commit studio media, credentials, session tokens, or private pipeline configuration.
