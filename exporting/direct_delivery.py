# -*- coding: utf-8 -*-
"""Atomic publication for simple, non-overwriting exports.

The UI owns job sequencing. This module owns only the same-filesystem private
stage and the transition from a verified stage to a public movie or sequence.
It is intentionally Qt-free.
"""
from __future__ import absolute_import

from dataclasses import dataclass
import json
import os
import shutil
import tempfile


def output_target(spec):
    """Return the public filesystem object owned by an export."""
    return os.path.dirname(spec.path) if spec.sequence else spec.path


def validate_new_outputs(intents):
    """Reject direct delivery when any selected output already exists."""
    for intent in intents:
        spec = intent.spec
        target = output_target(spec)
        if os.path.lexists(target):
            raise FileExistsError('Export output already exists: {}'.format(target))
        manifest = spec.path + '.frmanifest.json'
        if not spec.sequence and os.path.lexists(manifest):
            raise FileExistsError('Export manifest already exists: {}'.format(manifest))


@dataclass
class DirectStage(object):
    """One private render stage and its immutable public export intent."""

    public_spec: object
    root: str
    staged_path: str
    result: bool = False
    error: str = None

    @classmethod
    def create(cls, context, spec):
        target = output_target(spec)
        if os.path.lexists(target):
            raise FileExistsError(
                'Export output appeared before encoding: {}'.format(target))
        parent = os.path.dirname(target) if spec.sequence else os.path.dirname(spec.path)
        os.makedirs(parent, exist_ok=True)
        root = tempfile.mkdtemp(prefix='.frdirect-', dir=parent)
        staged_path = os.path.join(root, os.path.basename(spec.path))
        staged_context = dict(context, path=staged_path, overwrite=False)
        return cls(spec, root, staged_path), staged_context

    def cleanup(self):
        if self.root and os.path.isdir(self.root):
            shutil.rmtree(self.root, ignore_errors=True)

    def _public_manifest(self, verification):
        staged_manifest = self.staged_path + '.frmanifest.json'
        os.unlink(staged_manifest)
        with open(staged_manifest, 'x', encoding='utf-8') as stream:
            json.dump(self.public_spec.manifest(verification), stream, indent=2,
                      sort_keys=True, ensure_ascii=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        return staged_manifest

    def promote(self):
        """Publish verified media without replacing a public output."""
        staged_manifest = self.staged_path + '.frmanifest.json'
        with open(staged_manifest, encoding='utf-8') as stream:
            verification = json.load(stream)['verification']
        if (not isinstance(verification, dict) or
                verification.get('status') != 'verified'):
            raise RuntimeError(
                'Private export stage has no valid verification receipt.')
        staged_manifest = self._public_manifest(verification)
        target = output_target(self.public_spec)
        if os.path.lexists(target):
            raise FileExistsError(
                'Export output appeared before publication: {}'.format(target))
        if self.public_spec.sequence:
            os.rename(self.root, target)
            return
        public_manifest = self.public_spec.path + '.frmanifest.json'
        if os.path.lexists(public_manifest):
            raise FileExistsError(
                'Export manifest appeared before publication: {}'.format(
                    public_manifest))
        os.link(self.staged_path, self.public_spec.path)
        try:
            os.link(staged_manifest, public_manifest)
        except Exception:
            if os.path.isfile(self.public_spec.path):
                os.unlink(self.public_spec.path)
            raise
        os.unlink(self.staged_path)
        self.cleanup()


__all__ = ['DirectStage', 'output_target', 'validate_new_outputs']
