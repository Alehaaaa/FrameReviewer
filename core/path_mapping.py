# -*- coding: utf-8 -*-
"""Studio path mapping for cross-platform media relinking.

Pure Python, no Qt. A session authored on one operating system (or mount layout)
often references media under a root that is spelled differently elsewhere -- a
Windows UNC share vs. a Linux NFS mount, or a per-artist local cache. A
:class:`PathMapper` rewrites those roots using ordered rules, so a portable
session can relink its sources on any host.

Rules are applied longest-source-first and only at a path-segment boundary, so
``/show/seqA`` never matches a rule for ``/show/seq``. Matching is
case-insensitive on Windows-style paths by default (configurable for
determinism in tests). Environment variables (``$VAR``, ``${VAR}`` and
``%VAR%``) are expanded first, against an injectable environment.
"""
from __future__ import absolute_import

import os
import re


_ENV_RE = re.compile(r'\$\{([^}]+)\}|\$(\w+)|%([^%]+)%')


def expand_env(path, environ=None):
    """Expand ``$VAR`` / ``${VAR}`` / ``%VAR%`` against ``environ``.

    Unknown variables are left untouched (rather than replaced with an empty
    string), so a missing mapping is visible instead of silently corrupting the
    path.
    """
    if not path:
        return path
    env = os.environ if environ is None else environ

    def replace(match):
        name = match.group(1) or match.group(2) or match.group(3)
        return env.get(name, match.group(0))

    return _ENV_RE.sub(replace, path)


def to_slashes(path):
    """Normalize separators to ``/`` for matching (keeps drive letters)."""
    return (path or '').replace('\\', '/')


class PathMapper(object):
    """Ordered root-rewriting rules for relinking media across hosts."""

    def __init__(self, rules=None, case_sensitive=None, environ=None):
        self._environ = environ
        # Default to case-insensitive matching only when the host uses
        # Windows-style paths; callers/tests may force either behavior.
        if case_sensitive is None:
            case_sensitive = (os.sep != '\\')
        self.case_sensitive = bool(case_sensitive)
        self._rules = []
        for src, dst in (rules or []):
            self.add_rule(src, dst)

    # -- rule management ---------------------------------------------------
    def add_rule(self, src, dst):
        """Add a ``src`` root -> ``dst`` root rewrite rule."""
        src_norm = to_slashes(src).rstrip('/')
        dst_norm = to_slashes(dst).rstrip('/')
        if not src_norm:
            raise ValueError('A path-mapping rule needs a non-empty source.')
        self._rules.append((src_norm, dst_norm))
        # Longest source first so the most specific root wins.
        self._rules.sort(key=lambda rule: len(rule[0]), reverse=True)
        return self

    @property
    def rules(self):
        return list(self._rules)

    # -- mapping -----------------------------------------------------------
    def _matches(self, prefix, path):
        """True if ``path`` starts with ``prefix`` at a segment boundary."""
        plen = len(prefix)
        if len(path) < plen:
            return False
        head = path[:plen]
        if self.case_sensitive:
            if head != prefix:
                return False
        elif head.lower() != prefix.lower():
            return False
        # Boundary: exactly the prefix, or the next character is a separator.
        return len(path) == plen or path[plen] == '/'

    def map(self, path):
        """Return ``path`` with the first matching root rewritten.

        Environment variables are expanded first. If no rule matches, the
        env-expanded path is returned unchanged.
        """
        if not path:
            return path
        expanded = expand_env(path, self._environ)
        slashed = to_slashes(expanded)
        for src, dst in self._rules:
            if self._matches(src, slashed):
                remainder = slashed[len(src):]
                return dst + remainder
        return expanded

    def map_existing(self, path, exists=os.path.exists):
        """Map ``path``, then also try each rule until one yields a real file.

        Returns the first candidate that exists on disk (the direct mapping is
        tried first), or the direct mapping when none exist. ``exists`` is
        injectable for testing.
        """
        primary = self.map(path)
        if exists(primary):
            return primary
        expanded = to_slashes(expand_env(path, self._environ))
        for src, dst in self._rules:
            if self._matches(src, expanded):
                candidate = dst + expanded[len(src):]
                if candidate != primary and exists(candidate):
                    return candidate
        return primary

    def reversed(self):
        """A mapper with each rule's direction swapped."""
        return PathMapper([(dst, src) for src, dst in self._rules],
                          case_sensitive=self.case_sensitive,
                          environ=self._environ)

    # -- construction helpers ---------------------------------------------
    @classmethod
    def from_spec(cls, spec, case_sensitive=None, environ=None):
        """Parse ``"src=dst;src2=dst2"`` (``;`` or newline separated)."""
        rules = []
        for line in re.split(r'[;\n]', spec or ''):
            line = line.strip()
            if not line or '=' not in line:
                continue
            src, dst = line.split('=', 1)
            rules.append((src.strip(), dst.strip()))
        return cls(rules, case_sensitive=case_sensitive, environ=environ)

    @classmethod
    def from_env(cls, var='FRAMEREVIEWER_PATH_MAP', environ=None):
        """Build from an environment variable holding a spec string."""
        env = os.environ if environ is None else environ
        return cls.from_spec(env.get(var, ''), environ=environ)
