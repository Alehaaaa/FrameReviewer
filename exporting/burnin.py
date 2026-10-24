# -*- coding: utf-8 -*-
"""Burn-in templates: tokenized text placed in the frame corners.

Pure Python and Qt-free -- this resolves *what text goes where*; the renderer
(export / snapshot) draws it. Templates reference ``{token}`` fields that are
substituted from a context built per frame, so a burn-in can show the shot,
version, artist, timecode, and frame numbers that the time model already knows.

Tokens:

====================  ===================================================
``{shot}``            shot / clip name
``{version}``         version label
``{artist}``          artist / author
``{filename}``        source file name
``{date}``            session or export date
``{user}``            reviewing user
``{fps}``             frames per second
``{display_frame}``   zero-based display frame
``{source_frame}``    real source frame (may be negative/non-zero)
``{timecode}``        HH:MM:SS:FF (drop-frame when requested)
``{watermark}``       watermark text
====================  ===================================================

Unknown tokens are left intact so a typo is visible rather than silently blank.
"""
from __future__ import absolute_import

import re


# Corner/edge anchors a field can occupy.
POSITIONS = ('tl', 'tc', 'tr', 'bl', 'bc', 'br')

TOKENS = ('shot', 'version', 'artist', 'filename', 'date', 'user', 'fps',
          'display_frame', 'source_frame', 'timecode', 'watermark')

_TOKEN_RE = re.compile(r'\{(\w+)\}')


def available_tokens():
    return tuple(TOKENS)


def resolve_text(template, context):
    """Substitute ``{token}`` fields from ``context``; keep unknown tokens."""
    def repl(match):
        key = match.group(1)
        value = context.get(key)
        return str(value) if value is not None else match.group(0)
    return _TOKEN_RE.sub(repl, template or '')


def template_tokens(template):
    """The set of token names referenced in a template string."""
    return set(_TOKEN_RE.findall(template or ''))


def frame_context(base, display_frame, mapping=None, drop_frame=False):
    """Extend a base context with per-frame tokens.

    ``mapping`` is a :class:`~framereviewer.media.sequence.FrameMapping`; when
    given, ``source_frame``, ``timecode`` and ``fps`` are derived from it.
    """
    ctx = dict(base or {})
    ctx['display_frame'] = int(display_frame)
    if mapping is not None:
        ctx['source_frame'] = mapping.display_to_source(int(display_frame))
        ctx['timecode'] = mapping.display_to_timecode(
            int(display_frame), drop_frame=drop_frame)
        ctx.setdefault('fps', round(mapping.fps, 3))
    else:
        # In the legacy ExportJob the output index is also the source-frame
        # coordinate. Do not expose unresolved {source_frame} in burned media.
        ctx.setdefault('source_frame', int(display_frame))
        if 'timecode' not in ctx and ctx.get('fps'):
            if drop_frame:
                raise ValueError('Drop-frame timecode requires FrameMapping.')
            nominal = max(1, int(round(float(ctx['fps']))))
            count = abs(int(display_frame))
            seconds, frame = divmod(count, nominal)
            hours, seconds = divmod(seconds, 3600)
            minutes, seconds = divmod(seconds, 60)
            ctx['timecode'] = '{}{:02d}:{:02d}:{:02d}:{:02d}'.format(
                '-' if display_frame < 0 else '', hours, minutes, seconds, frame)
    return ctx


def render_frame(image, template, base=None, display_frame=0, mapping=None,
                 drop_frame=False, margin=16, font_size=18):
    """Bake a template into a PIL-compatible RGB/RGBA frame.

    Supports NumPy input (returns a new NumPy image of the same shape/dtype)
    and PIL Image input (returns a fresh Image). Per-frame tokens use the shared
    FrameMapping when supplied. Pillow is optional unless rasterizing a burn-in.
    """
    template = (template if isinstance(template, BurnInTemplate)
                else BurnInTemplate.from_dict(template))
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        Image = ImageDraw = ImageFont = None
    if Image is None:
        # Maya ships OpenCV more commonly than Pillow; use the same per-frame
        # text placement and outline without any font files or GUI thread.
        try:
            import cv2
            import numpy as np
        except ImportError:
            raise RuntimeError('Pillow or OpenCV is required for pixel burn-ins.')
        if not isinstance(image, np.ndarray) or image.dtype != np.uint8:
            raise TypeError('OpenCV burn-ins require a uint8 NumPy frame.')
        result = np.ascontiguousarray(image.copy())
        height, width = result.shape[:2]
        font = cv2.FONT_HERSHEY_SIMPLEX
        scale = max(0.3, float(font_size) / 32.0)
        for position, message in template.resolve_frame(
                base, display_frame, mapping, drop_frame).items():
            (tw, th), baseline = cv2.getTextSize(message, font, scale, 1)
            x = (margin if position.endswith('l') else
                 width - margin - tw if position.endswith('r') else (width - tw) // 2)
            y = (margin + th if position.startswith('t') else
                 height - margin - baseline)
            point = (int(x), int(y))
            cv2.putText(result, message, point, font, scale,
                        (0, 0, 0), thickness=3, lineType=cv2.LINE_AA)
            cv2.putText(result, message, point, font, scale,
                        (255, 255, 255), thickness=1, lineType=cv2.LINE_AA)
        return result
    import numpy as np
    is_array = isinstance(image, np.ndarray)
    original = image
    pil = Image.fromarray(image.copy()) if is_array else image.copy()
    if pil.mode not in ('RGB', 'RGBA'):
        pil = pil.convert('RGBA')
    draw = ImageDraw.Draw(pil)
    try:
        font = ImageFont.truetype('DejaVuSans.ttf', int(font_size))
    except (OSError, IOError):
        font = ImageFont.load_default()
    width, height = pil.size
    for position, message in template.resolve_frame(
            base, display_frame, mapping, drop_frame).items():
        if hasattr(draw, 'textbbox'):
            box = draw.textbbox((0, 0), message, font=font)
            tw, th = box[2] - box[0], box[3] - box[1]
        else:
            tw, th = draw.textsize(message, font=font)
        x = (margin if position.endswith('l') else
             width - margin - tw if position.endswith('r') else (width - tw) / 2)
        y = margin if position.startswith('t') else height - margin - th
        draw.text((int(x), int(y)), message, font=font,
                  fill=(255, 255, 255), stroke_width=2, stroke_fill=(0, 0, 0))
    if is_array:
        result = np.asarray(pil)
        return result.astype(original.dtype, copy=True)
    return pil


def ffmpeg_filters(template, base=None, source_start=0, margin=16, font_size=18):
    """Generate drawtext filters for untracked exports with per-frame tokens.

    FFmpeg ``n`` is zero-based output frame; source frame is derived from the
    exported in point. A precise source timecode should instead be rasterized
    from FrameMapping with :func:`render_frame`.
    """
    tmpl = (template if isinstance(template, BurnInTemplate)
            else BurnInTemplate.from_dict(template))
    base = dict(base or {})
    x_pos = {'l': str(margin), 'c': '(w-text_w)/2',
             'r': 'w-text_w-{}'.format(margin)}
    filters = []
    for pos, text in tmpl.fields.items():
        replacements = {
            'display_frame': '__FR_DISPLAY__',
            'source_frame': '__FR_SOURCE__',
            'timecode': '__FR_TIMECODE__',
        }
        substituted = resolve_text(text, dict(base, **replacements))
        escaped = (substituted.replace('\\', '\\\\').replace(':', '\\:')
                   .replace("'", "\\'").replace(',', '\\,').replace('%', '\\%'))
        escaped = escaped.replace('__FR_DISPLAY__', '%{n}')
        escaped = escaped.replace('__FR_SOURCE__',
                                  '%{eif\\:n+' + str(int(source_start)) + '\\:d}')
        escaped = escaped.replace('__FR_TIMECODE__', '%{pts\\:hms}')
        y = str(margin) if pos.startswith('t') else 'h-text_h-{}'.format(margin)
        filters.append("drawtext=text='{}':x={}:y={}:fontsize={}:"
                       "fontcolor=white:borderw=2:bordercolor=black".format(
                           escaped, x_pos[pos[1]], y, int(font_size)))
    return filters


class BurnInTemplate(object):
    """Named template mapping corner/edge positions to token strings."""

    def __init__(self, fields=None, name=''):
        self.name = name
        self.fields = {pos: str(text) for pos, text in (fields or {}).items()
                       if pos in POSITIONS and text}

    def set_field(self, position, text):
        if position not in POSITIONS:
            raise ValueError('Unknown burn-in position {!r}.'.format(position))
        if text:
            self.fields[position] = str(text)
        else:
            self.fields.pop(position, None)
        return self

    def tokens(self):
        """All token names referenced across the template's fields."""
        found = set()
        for text in self.fields.values():
            found |= template_tokens(text)
        return found

    def resolve(self, context):
        """Return ``{position: resolved_text}`` for the non-empty fields."""
        out = {}
        for position, text in self.fields.items():
            resolved = resolve_text(text, context)
            if resolved:
                out[position] = resolved
        return out

    def resolve_frame(self, base, display_frame, mapping=None, drop_frame=False):
        """Convenience: build the per-frame context then resolve."""
        return self.resolve(
            frame_context(base, display_frame, mapping, drop_frame))

    def to_dict(self):
        return {'name': self.name, 'fields': dict(self.fields)}

    @classmethod
    def from_dict(cls, data):
        return cls(fields=data.get('fields'), name=data.get('name', ''))


# -- built-in presets -------------------------------------------------------

def _presets():
    return {
        'review': BurnInTemplate(name='review', fields={
            'tl': '{shot}  v{version}',
            'tr': '{timecode}',
            'bl': '{artist}',
            'br': '{display_frame}  @ {fps}',
        }),
        'client': BurnInTemplate(name='client', fields={
            'tc': '{watermark}',
            'bl': '{shot} v{version}',
            'br': '{timecode}',
        }),
        'minimal': BurnInTemplate(name='minimal', fields={
            'bl': '{filename}',
            'br': '{display_frame}',
        }),
        'source': BurnInTemplate(name='source', fields={
            'tl': '{shot}',
            'tr': '{source_frame}',
            'br': '{timecode}',
        }),
    }


PRESET_NAMES = tuple(sorted(_presets().keys()))


def preset(name):
    """Return a fresh copy of a built-in preset by name."""
    presets = _presets()
    if name not in presets:
        raise ValueError('Unknown burn-in preset {!r}.'.format(name))
    return presets[name]
