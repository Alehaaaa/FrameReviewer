# -*- coding: utf-8 -*-
"""Shared bounded subprocess ownership for export helpers."""
from __future__ import absolute_import

import subprocess
import time

from ..media.ffmpeg import _no_window_kwargs, media_environment


def run_bounded(args, *, timeout, output_limit, input_data=None,
                interrupted=None, set_process=None, poll_interval=.3,
                cancelled='Export process cancelled.',
                timed_out='Export process exceeded bounded runtime.',
                failed='Export process failed: {}'):
    """Run one non-shell process with cancellation, deadline and output cap."""
    if interrupted and interrupted():
        raise RuntimeError(cancelled)
    kwargs = {
        'stdin': subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
        'stdout': subprocess.PIPE,
        'stderr': subprocess.PIPE,
        'env': media_environment(),
    }
    kwargs.update(_no_window_kwargs())
    process = subprocess.Popen(args, **kwargs)
    if set_process is not None:
        set_process(process)
    deadline = time.monotonic() + timeout
    pending_input = input_data
    try:
        while True:
            if interrupted and interrupted():
                raise RuntimeError(cancelled)
            if time.monotonic() >= deadline:
                raise RuntimeError(timed_out)
            try:
                output, error = process.communicate(
                    input=pending_input, timeout=poll_interval)
                break
            except subprocess.TimeoutExpired:
                pending_input = None
        if process.returncode or len(output) > output_limit:
            raise RuntimeError(failed.format(
                error.decode('utf-8', 'replace')[-400:]))
        if interrupted and interrupted():
            raise RuntimeError(cancelled)
        return output
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        if set_process is not None:
            set_process(None)


__all__ = ['run_bounded']
