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

from .qtcompat import QtCore

ORG = 'Framestore'
APP = 'FrameReviewer'
LEGACY_APP = 'VideoCropper'
_MIGRATION_KEY = 'internal/migrated_from_videocropper'

RECENT_KEY = 'files/recent'
RECENT_MAX = 12

_TRUTHY = ('1', 'true', 'yes', 'on')


class Settings(object):
    """Typed wrapper around the tool's QSettings store."""

    def __init__(self):
        self._store = QtCore.QSettings(ORG, APP)
        self._suspended = False
        self._migrate_legacy_settings()

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

    def add_recent(self, path):
        path = os.path.abspath(path)
        paths = [p for p in self.recent_files() if p != path]
        paths.insert(0, path)
        self.set(RECENT_KEY, paths[:RECENT_MAX])

    def remove_recent(self, path):
        self.set(RECENT_KEY, [p for p in self.recent_files() if p != path])

    def clear_recent(self):
        self.remove(RECENT_KEY)
