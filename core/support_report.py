# -*- coding: utf-8 -*-
"""Redacted, bounded support diagnostics for studio deployments."""
from __future__ import absolute_import

import copy
import json
import os
import platform
import re
import sys

from .capabilities import probe_capabilities


_SECRET = re.compile(r'(?i)\b(password|passwd|token|secret|api[_-]?key|'
                     r'access[_-]?key|session[_-]?id)\b(\s*[=:]\s*)'
                     r'([^\s,;&]+)')
_URL_CREDENTIALS = re.compile(r'(?i)(://)[^/@\s]+@')
_SENSITIVE_KEY = re.compile(r'(?i)(?:password|passwd|token|secret|api[_-]?key|'
                            r'access[_-]?key|auth|cookie|session[_-]?id)')
_MEDIA_PATH_KEY = re.compile(r'(?i)(?:^|[_-])(?:media|path|filename|file|'
                             r'source_url|source_path|url)(?:$|[_-])')


def redact(value):
    """Recursively scrub user paths, obvious credentials and long text fields."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            key = str(key)
            if _SENSITIVE_KEY.search(key):
                result[key] = '<REDACTED>'
            elif _MEDIA_PATH_KEY.search(key) and isinstance(item, str):
                result[key] = '<PATH REDACTED>'
            else:
                result[key] = redact(item)
        return result
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value[:80]]
    if not isinstance(value, str):
        return value
    home = os.path.expanduser('~')
    output = value.replace(home, '<HOME>') if home and home != '/' else value
    output = _URL_CREDENTIALS.sub(r'\1<REDACTED>@', output)
    output = _SECRET.sub(lambda match: match.group(1) + match.group(2) + '<REDACTED>',
                         output)
    return output[:2000]


def build_support_report(session=None, player=None, probes=None, host=None):
    """No raw session documents, drawing payloads or media paths are included."""
    capabilities = [item.to_dict() for item in probe_capabilities(probes=probes)]
    report = {
        'app': 'FrameReviewer',
        'python': sys.version.split()[0],
        'platform': platform.platform(),
        'capabilities': capabilities,
    }
    if host:
        report['host'] = host
    if session is not None:
        report['session'] = {
            'source_count': len(session.sources),
            'active_source': bool(session.current_source()),
            'representation_count': sum(len(s.representations) for s in session.sources),
            'missing_paths': sum(1 for s in session.sources
                                 for rep in s.representations
                                 if not os.path.exists(rep.path)),
            'comparison_mode': session.view.get('comparison_mode', 'sequence'),
        }
    if player is not None and hasattr(player, 'diagnostics_snapshot'):
        try:
            report['playback'] = copy.deepcopy(player.diagnostics_snapshot())
        except (RuntimeError, ValueError, TypeError) as exc:
            report['playback'] = {'error': str(exc)}
    return redact(report)


def report_json(session=None, player=None, probes=None, host=None):
    return json.dumps(build_support_report(session, player, probes, host),
                      indent=2, ensure_ascii=False, sort_keys=True)

