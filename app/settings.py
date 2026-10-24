# -*- coding: utf-8 -*-
"""One typed home for persisted state.

Everything the tool remembers -- view toggles, playback, drawing defaults,
window geometry, recent files, the ShotGrid connection -- goes through here, so
there is a single place that knows how QSettings coerces values (PySide2/6 can
hand back strings), how recent files are capped and de-duplicated, and how
writes are suspended after a preferences reset.

Keys live where they are read, as plain ``'group/name'`` strings; this class
owns the *mechanics* (typing, recents, suspension), not a schema.
"""
from __future__ import absolute_import

import os
import tempfile

from ..core.qtcompat import QtCore

ORG = 'Framestore'
APP = 'FrameReviewer'
LEGACY_APP = 'VideoCropper'
_MIGRATION_KEY = 'internal/migrated_from_videocropper'

RECENT_KEY = 'files/recent'
RECENT_USER_KEY = 'files/recent_user_opened'
RECENT_MAX = 12

_TRUTHY = ('1', 'true', 'yes', 'on')


class Settings(object):
    """Typed wrapper around the tool's QSettings store."""

    def __init__(self):
        # Test subprocesses must never overwrite the artist's persistent
        # recent-file history, pane geometry or playback preferences. The
        # optional path is set ONLY by the Maya suite/test harness; normal
        # Maya still uses the historical native QSettings domain unchanged.
        isolated = os.environ.get('FRAMEREVIEWER_TEST_SETTINGS_DIR', '')
        if isolated:
            if not os.path.isdir(isolated):
                raise ValueError('Isolated FrameReviewer test settings directory '
                                 'does not exist.')
            self._store = QtCore.QSettings(
                os.path.join(isolated, 'FrameReviewer.ini'),
                QtCore.QSettings.IniFormat)
        else:
            self._store = QtCore.QSettings(ORG, APP)
        self._isolated_test_store = bool(isolated)
        self._suspended = False
        if not self._isolated_test_store:
            self._migrate_legacy_settings()
        else:
            # Never import the artist's legacy VideoCropper preferences into
            # an isolated test store, either.
            self._store.setValue(_MIGRATION_KEY, True)

    def _migrate_legacy_settings(self):
        """Copy existing preferences once, preserving any newer values.

        The previous VideoCropper store is intentionally kept so existing
        shelves and Maya workspaces can be upgraded independently.
        """
        if self._store.contains(_MIGRATION_KEY):
            return
        legacy = QtCore.QSettings(ORG, LEGACY_APP)
        for key in legacy.allKeys():
            if not self._store.contains(key):
                self._store.setValue(key, legacy.value(key))
        self._store.setValue(_MIGRATION_KEY, True)
        self._store.sync()

    # -- write suspension -------------------------------------------------
    def suspend(self, suspended=True):
        """Stop persisting writes (used for the rest of a session after reset)."""
        self._suspended = bool(suspended)

    # -- typed reads ------------------------------------------------------
    def get_bool(self, key, default=False):
        value = self._store.value(key, default)
        if isinstance(value, bool):
            return value
        if value is None:
            return bool(default)
        return str(value).strip().lower() in _TRUTHY

    def get_int(self, key, default=0):
        try:
            return int(self._store.value(key, default))
        except (TypeError, ValueError):
            return int(default)

    def get_float(self, key, default=0.0):
        try:
            return float(self._store.value(key, default))
        except (TypeError, ValueError):
            return float(default)

    def get_str(self, key, default=''):
        value = self._store.value(key, default)
        return default if value is None else str(value)

    def get_bytes(self, key):
        """Return a non-empty QByteArray for restoreGeometry/State, else None."""
        value = self._store.value(key)
        if isinstance(value, QtCore.QByteArray) and not value.isEmpty():
            return value
        return None

    def contains(self, key):
        return self._store.contains(key)

    # -- writes (no-ops while suspended) ----------------------------------
    def set(self, key, value):
        if not self._suspended:
            self._store.setValue(key, value)

    def remove(self, key):
        if not self._suspended:
            self._store.remove(key)

    def sync(self):
        self._store.sync()

    def clear(self):
        """Wipe the store. This is the reset itself, so it ignores suspension."""
        self._store.clear()
        # Reset preferences must not silently restore old VideoCropper values
        # when Settings is created again in the next session.
        self._store.setValue(_MIGRATION_KEY, True)
        self._store.sync()

    # -- recent files -----------------------------------------------------
    @staticmethod
    def _expired_test_recent(path):
        """Identify ONLY vanished FrameReviewer QA fixtures, not user media.

        A missing file on a network volume, removable disk or a user's own
        project may return after remount. Never automatically forget it. The
        former test suite wrote specifically named ``fr-*`` temporary-folder
        plates into real macOS QSettings; once these expire they cannot be
        meaningful recent opens. Resolve macOS /var -> /private/var aliases.
        """
        if not isinstance(path, str) or not os.path.isabs(path):
            return False
        temp_root = os.path.realpath(tempfile.gettempdir())
        try:
            absolute = os.path.realpath(path)
            relative = os.path.relpath(absolute, temp_root)
        except (ValueError, OSError):
            return False
        if (relative == '.' or relative.startswith('..' + os.sep) or
                relative == '..' or os.path.exists(path)):
            return False
        parts = relative.split(os.sep)
        return (len(parts) >= 2 and parts[0].startswith('fr-') and
                parts[0] not in ('fr-', 'fr-..'))

    def recent_files(self):
        """De-duplicated, capped list of recent media paths (newest first)."""
        raw = self._store.value(RECENT_KEY, [])
        if not raw:
            return []
        if isinstance(raw, str):
            raw = [raw]
        seen = set()
        paths = []
        for item in raw:
            path = str(item)
            if path and path not in seen:
                seen.add(path)
                paths.append(path)
        return paths[:RECENT_MAX]

    def prune_expired_test_recents(self):
        """One conservative cleanup of expired QA artifacts; keep user order.

        Called only when the artist opens the menu. Do not mutate read-only
        recent_files(), never remove ordinary missing files and never discard
        valid media under the temporary root while it still exists.
        """
        before = self.recent_files()
        # Explicit artist opens (including a genuinely opened temporary
        # directory) are never classified as expired QA artifacts, even when
        # the file later disappears. Old polluted stores predate this marker,
        # so ONLY their narrowly recognized missing fr-* fixtures are pruned.
        raw_explicit = self._store.value(RECENT_USER_KEY, []) or []
        explicit = {raw_explicit} if isinstance(raw_explicit, str) else {
            str(path) for path in raw_explicit}
        after = [path for path in before
                 if path in explicit or not self._expired_test_recent(path)]
        if before != after:
            self.set(RECENT_KEY, after)
            self.sync()
        return after

    def add_recent(self, path):
        path = os.path.abspath(path)
        paths = [p for p in self.recent_files() if p != path]
        paths.insert(0, path)
        self.set(RECENT_KEY, paths[:RECENT_MAX])
        raw = self._store.value(RECENT_USER_KEY, []) or []
        previous = [raw] if isinstance(raw, str) else list(raw)
        opened = [str(item) for item in previous if str(item) != path]
        opened.insert(0, path)
        self.set(RECENT_USER_KEY, opened[:RECENT_MAX])

    def remove_recent(self, path):
        self.set(RECENT_KEY, [p for p in self.recent_files() if p != path])
        raw = self._store.value(RECENT_USER_KEY, []) or []
        previous = [raw] if isinstance(raw, str) else list(raw)
        self.set(RECENT_USER_KEY, [str(p) for p in previous if str(p) != path])

    def clear_recent(self):
        self.remove(RECENT_KEY)
        self.remove(RECENT_USER_KEY)
