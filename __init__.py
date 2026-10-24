# -*- coding: utf-8 -*-
"""FrameReviewer for Maya.

A crop / review tool for Maya. Native QMediaPlayer / FFmpeg streamed playback, an RV-style menu, a
timeline with optional thumbnail and waveform lanes, optical-flow crop tracking,
and ffmpeg export. Cross-platform; nothing bundled -- relies on Maya's PySide
and a system FFmpeg.

Usage in Maya::

    import framereviewer
    framereviewer.show()

``show`` / ``restore`` / ``shutdown`` are the package's public lifecycle API.
``restore`` and ``shutdown`` exist because Maya's workspace control stores them
by name: the control's ``uiScript`` runs ``framereviewer.restore()`` to rebuild
the panel on layout restore, and its close callback runs
``framereviewer.shutdown()`` to tear the UI down. They must live here, at the
import path Maya stores, not only on the window class.
"""
from __future__ import absolute_import

__version__ = '3.0.0'
__all__ = ['show', 'restore', 'shutdown', 'reload', 'FrameReviewerWindow']


def _purge_submodules():
    """Remove cached modules *and* the package attributes pointing at them.

    Deleting only a nested ``sys.modules`` entry is insufficient: Python also
    stores imported packages as attributes on ``framereviewer``. A later
    relative import can therefore recover the stale object and
    mix a newly loaded window with old model classes.
    """
    import importlib
    import sys
    import types

    package = sys.modules[__name__]
    prefix = __name__ + '.'
    modules = [name for name in list(sys.modules) if name.startswith(prefix)]

    # Remove attributes first while their module identities are still known.
    for attr, value in list(vars(package).items()):
        if (isinstance(value, types.ModuleType) and
                getattr(value, '__name__', '').startswith(prefix)):
            try:
                delattr(package, attr)
            except AttributeError:
                pass

    # Children before parents prevents a partially cached package hierarchy.
    for name in sorted(modules, key=lambda item: item.count('.'), reverse=True):
        sys.modules.pop(name, None)
    importlib.invalidate_caches()
    return modules


def show():
    """Create (or re-create) and raise the FrameReviewer window."""
    from .app.window import FrameReviewerWindow
    return FrameReviewerWindow.show_ui()


def restore():
    """Rebuild the window into the workspace control Maya is restoring.

    Invoked by name from the control's ``uiScript`` during Maya layout restore
    (and whenever Maya rebuilds the retained control). Must not raise: a throw
    here leaves the docked panel empty.
    """
    from .app.window import FrameReviewerWindow
    return FrameReviewerWindow.restore_ui()


def shutdown(delete_workspace=False):
    """Stop the live UI and every worker it owns.

    Invoked by name from the workspace control's close callback, and callable
    by hand to force a clean teardown. Returns ``True`` when every background
    worker stopped cleanly.
    """
    from .app.window import shutdown_ui
    return shutdown_ui(delete_workspace=delete_workspace)


def reload():
    """Fully reload every submodule, then show a fresh window.

    ``importlib.reload(framereviewer)`` only re-runs this file; the submodules
    (window, surface, player, ...) stay cached, so UI edits never appear. This
    tears the live UI down, purges the submodules and shows a fresh window::

        import framereviewer
        framereviewer.reload()

    Purging a module frees the code object its still-running QThreads and
    QProcess callbacks are executing. If a worker cannot be stopped, replacing
    its module out from under it crashes Maya, so this refuses to purge until
    teardown reports clean and raises instead.
    """
    clean = shutdown(delete_workspace=True)
    if not clean:
        raise RuntimeError(
            'FrameReviewer reload aborted: a background worker (export, '
            'tracking, or playback cache) did not stop in time. Its callbacks '
            'still point at code this reload would purge, which would crash '
            'Maya. Let the job finish, then reload again.')
    _purge_submodules()
    return show()


def __getattr__(name):  # lazy, so importing the package never forces Qt early
    if name == 'FrameReviewerWindow':
        from .app.window import FrameReviewerWindow
        return FrameReviewerWindow
    raise AttributeError(name)
