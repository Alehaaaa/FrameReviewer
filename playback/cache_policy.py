# -*- coding: utf-8 -*-
"""Validated playback/cache configuration shared by all frame sources."""
from __future__ import absolute_import

import os


REALTIME = 'realtime'
PLAY_ALL = 'play_all'
POLICIES = (REALTIME, PLAY_ALL)
ONCE = 'once'
LOOP = 'loop'
PING_PONG = 'ping_pong'
LOOP_MODES = (ONCE, LOOP, PING_PONG)


def validate_playback_policy(policy):
    if policy not in POLICIES:
        raise ValueError('Unknown playback policy: {}'.format(policy))
    return policy


def validate_loop_mode(mode):
    if mode not in LOOP_MODES:
        raise ValueError('Unknown transport mode: {}'.format(mode))
    return mode


def candidate_frame(policy, current, anchor, elapsed_ms, fps, speed=1.0):
    """Choose a frame using OpenRV-style realtime or play-all semantics."""
    validate_playback_policy(policy)
    advance = int(max(0.0, float(elapsed_ms)) * max(0.001, float(fps)) *
                  abs(float(speed)) / 1000.0)
    direction = -1 if speed < 0 else 1
    if policy == PLAY_ALL:
        return int(current) + direction * (1 if advance >= 1 else 0)
    return int(anchor) + direction * advance


def transport_frame(candidate, low, high, mode=ONCE):
    """Return ``(frame, reflected)`` for a signed, unbounded candidate.

    ``reflected`` is whether ping-pong traversal ends in the reverse segment
    of its two-way period. The caller keeps the running direction and resets
    its time anchor after a reflection.
    """
    validate_loop_mode(mode)
    low, high = int(low), int(high)
    if high <= low:
        return low, False
    candidate = int(candidate)
    if mode == ONCE:
        return max(low, min(high, candidate)), False
    if mode == LOOP:
        return low + (candidate - low) % (high - low + 1), False
    width = high - low
    phase = (candidate - low) % (width * 2)
    return (low + phase, False) if phase <= width else (high - (phase - width), True)

DEFAULT_DECODE_WIDTH = 1280
DEFAULT_CACHE_MB = 256
DEFAULT_CACHE_FRAMES = 96
DEFAULT_BATCH_FRAMES = 48
DEFAULT_LOOKBACK_PERCENT = 25
CACHE_OFF = 'off'
CACHE_REGION = 'region'
CACHE_LOOKAHEAD = 'lookahead'
CACHE_MODES = (CACHE_OFF, CACHE_REGION, CACHE_LOOKAHEAD)


def _bounded(value, default, minimum, maximum):
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = int(default)
    return max(int(minimum), min(int(maximum), value))


class CachePolicy(object):
    """Immutable-in-practice cache limits passed to decoder backends."""

    def __init__(self, decode_width=DEFAULT_DECODE_WIDTH,
                 cache_mb=DEFAULT_CACHE_MB,
                 max_frames=DEFAULT_CACHE_FRAMES,
                 batch_frames=DEFAULT_BATCH_FRAMES,
                 lookback_percent=DEFAULT_LOOKBACK_PERCENT,
                 cache_mode=CACHE_LOOKAHEAD):
        self.decode_width = _bounded(decode_width, DEFAULT_DECODE_WIDTH, 320, 8192)
        self.cache_mb = _bounded(cache_mb, DEFAULT_CACHE_MB, 32, 4096)
        self.max_frames = _bounded(max_frames, DEFAULT_CACHE_FRAMES, 8, 2048)
        self.batch_frames = _bounded(batch_frames, DEFAULT_BATCH_FRAMES, 4, 256)
        self.lookback_percent = _bounded(
            lookback_percent, DEFAULT_LOOKBACK_PERCENT, 0, 90)
        self.cache_mode = (cache_mode if cache_mode in CACHE_MODES
                           else CACHE_LOOKAHEAD)
        self.cache_bytes = self.cache_mb * 1024 * 1024

    @classmethod
    def from_settings(cls, settings=None, environ=None):
        """Build policy from preferences, with environment overrides."""
        env = os.environ if environ is None else environ

        def preference(key, default):
            if settings is None:
                return default
            return settings.get_int('cache/' + key, default)

        values = {
            'decode_width': preference('decode_width', DEFAULT_DECODE_WIDTH),
            'cache_mb': preference('memory_mb', DEFAULT_CACHE_MB),
            'max_frames': preference('max_frames', DEFAULT_CACHE_FRAMES),
            'batch_frames': preference('batch_frames', DEFAULT_BATCH_FRAMES),
            'lookback_percent': preference(
                'lookback_percent', DEFAULT_LOOKBACK_PERCENT),
            'cache_mode': (settings.get_str('cache/mode', CACHE_LOOKAHEAD)
                           if settings is not None else CACHE_LOOKAHEAD),
        }
        names = {
            'decode_width': 'FRAMEREVIEWER_DECODE_WIDTH',
            'cache_mb': 'FRAMEREVIEWER_CACHE_MB',
            'max_frames': 'FRAMEREVIEWER_CACHE_FRAMES',
            'batch_frames': 'FRAMEREVIEWER_CACHE_BATCH',
            'lookback_percent': 'FRAMEREVIEWER_CACHE_LOOKBACK',
            'cache_mode': 'FRAMEREVIEWER_CACHE_MODE',
        }
        for key, name in names.items():
            raw = env.get(name)
            if raw not in (None, ''):
                values[key] = raw
        return cls(**values)

    def as_dict(self):
        return {
            'decode_width': self.decode_width,
            'cache_mb': self.cache_mb,
            'cache_bytes': self.cache_bytes,
            'max_frames': self.max_frames,
            'batch_frames': self.batch_frames,
            'lookback_percent': self.lookback_percent,
            'cache_mode': self.cache_mode,
        }
