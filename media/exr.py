# -*- coding: utf-8 -*-
"""Precision EXR decode plus a separately labelled display-only fallback.

``read_frame_payload`` retains native HALF/FLOAT/UINT samples and windows in an
owned source-stage FramePayload; ``payload_to_pixmap`` performs the explicit
source -> working -> display conversion.  The default, absent OCIO, is a clearly
identified unmanaged linear -> sRGB preview, never an implicit source transform.
OpenEXR is optional; both its legacy InputFile and modern File APIs are handled.
"""
from __future__ import absolute_import

import os
import re
import json
import subprocess
import struct
import stat
import zlib
from functools import lru_cache

from ..core.qtcompat import QtCore, QtGui

try:
    import numpy as np
except ImportError:
    np = None
try:
    import OpenEXR
except ImportError:
    OpenEXR = None
try:
    import Imath
except ImportError:
    Imath = None


UNMANAGED_LABEL = 'Unmanaged preview: assumed scene-linear -> sRGB (no OCIO config)'
MAX_EXR_PIXELS_BYTES = 256 * 1024 * 1024
MAX_EXR_FILE_BYTES = 512 * 1024 * 1024
MAX_EXR_HEADER_BYTES = 1024 * 1024


def _sane_window(window):
    x, y, w, h = [int(v) for v in window]
    if not (0 < w <= 16384 and 0 < h <= 16384 and
            -2147483648 <= x <= 2147483647 and
            -2147483648 <= y <= 2147483647 and
            x + w - 1 <= 2147483647 and y + h - 1 <= 2147483647):
        raise ValueError('Invalid EXR window dimensions')
    return x, y, w, h


def _check_budget(window, channels=4, bytes_per_sample=4):
    _, _, width, height = _sane_window(window)
    if width * height * channels * bytes_per_sample > MAX_EXR_PIXELS_BYTES:
        raise ValueError('EXR raster exceeds bounded native decode size')


def _channel_selection(channels, layer=None):
    """Return (ordered keys, selected layer), or None if absent/ambiguous.

    Root RGB and named beauty/diffuse layers are independent. A caller must
    select a layer when more than one complete RGB group exists.
    """
    groups = {}
    for name in channels:
        string = str(name)
        prefix, dot, component = string.rpartition('.')
        group = prefix if dot else ''
        component = (component if dot else string).lower()
        if component not in ('r', 'red', 'g', 'green', 'b', 'blue',
                             'a', 'alpha', 'rgb', 'rgba'):
            continue
        values = groups.setdefault(group, {})
        if component in values:
            return None  # duplicate channel names differing only in case
        values[component] = name
    choices = {}
    for group, components in groups.items():
        if any(sum(alias in components for alias in aliases) > 1
               for aliases in (('r', 'red'), ('g', 'green'), ('b', 'blue'),
                               ('a', 'alpha'))):
            continue  # never discard one of two conflicting RGB/A aliases
        def choose(*aliases):
            found = [components[a] for a in aliases if a in components]
            return found[0] if len(found) == 1 else None
        rgb = [choose('r', 'red'), choose('g', 'green'), choose('b', 'blue')]
        packed = [components[k] for k in ('rgb', 'rgba') if k in components]
        if all(value is not None for value in rgb) and not packed:
            alpha = choose('a', 'alpha')
            choices[group] = rgb + ([alpha] if alpha is not None else [])
        elif len(packed) == 1 and not any(value is not None for value in rgb):
            choices[group] = packed
    if layer is not None:
        matches = [group for group in choices if group.lower() == str(layer).lower()]
        if len(matches) != 1:
            return None
        selected = matches[0]
    elif len(choices) == 1:
        selected = next(iter(choices))
    else:
        return None
    return choices[selected], selected


def _header_fields(header, names, layer='', part=0, parts=1):
    window = _sane_window(_window_tuple(header['dataWindow']))
    display = _sane_window(_window_tuple(header.get('displayWindow', header['dataWindow'])))
    _check_budget(window)
    aspect = float(header.get('pixelAspectRatio', 1.0))
    if not np.isfinite(aspect) or aspect <= 0:
        raise ValueError('Invalid EXR pixel aspect')
    return {'width': window[2], 'height': window[3],
            'pixel_aspect_ratio': aspect,
            'data_window': window, 'display_window': display,
            'channels': tuple(str(k) for k in names),
            'layer': layer, 'part': int(part), 'part_count': int(parts),
            'alpha_mode': ('premultiplied' if len(names) == 4 or
                           (len(names) == 1 and str(names[0]).lower().endswith('rgba'))
                           else 'opaque'),
            'geometry_accuracy': 'native EXR header'}


def _raw_header(path, layer=None):
    """Read *only* standard single-part EXR header attributes, with byte caps.

    This also works without OpenEXR Python; it cannot decode pixels. Refuse
    deep/multipart/tiled files rather than reporting first-part metadata as
    whole-image truth. Header input is never trusted to allocate frame buffers.
    """
    if not is_exr(path) or os.path.getsize(path) > MAX_EXR_FILE_BYTES:
        return None
    with open(path, 'rb') as stream:
        raw = stream.read(MAX_EXR_HEADER_BYTES + 1)
    if len(raw) < 12 or raw[:4] != b'v/1\x01':
        return None
    version = struct.unpack_from('<I', raw, 4)[0]
    if version & (0x200 | 0x800 | 0x1000):
        return None  # tiled, deep/nonimage or multipart
    position = 8
    attributes = {}

    def cstring():
        nonlocal position
        stop = raw.find(b'\0', position, min(len(raw), position + 256))
        if stop < 0:
            raise ValueError('EXR header string exceeds limit')
        result = raw[position:stop].decode('ascii')
        position = stop + 1
        return result

    while position < len(raw):
        name = cstring()
        if not name:
            break
        typ = cstring()
        if position + 4 > len(raw):
            raise ValueError('Truncated EXR header')
        length = struct.unpack_from('<I', raw, position)[0]
        position += 4
        if length > MAX_EXR_HEADER_BYTES or position + length > len(raw):
            raise ValueError('EXR attribute exceeds limit')
        attributes[name] = (typ, raw[position:position + length])
        position += length
    else:
        raise ValueError('Unterminated EXR header')
    channels = []
    channel_types = {}
    if 'channels' not in attributes or attributes['channels'][0] != 'chlist':
        return None
    data = attributes['channels'][1]
    cursor = 0
    while cursor < len(data):
        end = data.find(b'\0', cursor)
        if end < 0:
            raise ValueError('Malformed EXR channel list')
        if cursor == end:
            break
        if end - cursor > 255 or end + 17 > len(data):
            raise ValueError('Truncated EXR channel')
        pixel_type = struct.unpack_from('<I', data, end + 1)[0]
        x_sampling, y_sampling = struct.unpack_from('<ii', data, end + 9)
        if pixel_type not in (0, 1, 2) or (x_sampling, y_sampling) != (1, 1):
            return None  # FFmpeg would not preserve native samples/sampling
        name = data[cursor:end].decode('ascii')
        channels.append(name)
        channel_types[name] = pixel_type
        cursor = end + 17  # NUL, uint32 type, 4 reserved, 2 uint32 sampling
        if len(channels) > 512:
            raise ValueError('Too many EXR channels')
    selected = _channel_selection(channels, layer)
    if selected is None:
        return None
    names, layer = selected
    # The FFmpeg fallback cannot choose EXR channels or preserve native UINT32
    # through its float32 output. Require canonical, separate RGB(A) only.
    # Any AOV/depth/layer channels make implicit channel routing unprovable.
    if (layer or len(names) != len(channels) or
            names != ['R', 'G', 'B'] and names != ['R', 'G', 'B', 'A'] or
            any(channel_types[name] == 0 for name in names)):
        return None
    def window(name):
        typ, value = attributes[name]
        if typ != 'box2i' or len(value) != 16:
            raise ValueError('Invalid EXR {}'.format(name))
        x0, y0, x1, y1 = struct.unpack('<4i', value)
        return _sane_window((x0, y0, x1 - x0 + 1, y1 - y0 + 1))
    dw = window('dataWindow')
    disp = window('displayWindow') if 'displayWindow' in attributes else dw
    aspect = 1.0
    if 'pixelAspectRatio' in attributes:
        typ, value = attributes['pixelAspectRatio']
        if typ != 'float' or len(value) != 4:
            raise ValueError('Invalid EXR pixelAspectRatio')
        aspect = struct.unpack('<f', value)[0]
    _check_budget(dw)
    if not (0 < aspect < float('inf')):
        raise ValueError('Invalid EXR PAR')
    return {'width': dw[2], 'height': dw[3],
            'pixel_aspect_ratio': float(aspect),
            'data_window': dw, 'display_window': disp,
            'channels': tuple(str(n) for n in names),
            'layer': layer, 'part': 0, 'part_count': 1,
            'alpha_mode': 'premultiplied' if len(names) == 4 or
            str(names[0]).lower().endswith('rgba') else 'opaque',
            'geometry_accuracy': 'parsed EXR header (native raster unverified)'}


def available():
    return bool(np is not None and OpenEXR is not None and
                ((hasattr(OpenEXR, 'InputFile') and Imath is not None) or
                 hasattr(OpenEXR, 'File')))


@lru_cache(maxsize=1)
def _ffmpeg_binaries():
    """Optional bounded precision EXR decoder when Maya lacks OpenEXR Python.

    FFmpeg reconstructs native FLOAT RGB(A), but cannot report arbitrary EXR
    data/display windows or multilayer channel selection. Never label this as
    equivalent to a native OpenEXR decode.
    """
    from .ffmpeg import find_binary
    try:
        return find_binary('ffmpeg'), find_binary('ffprobe')
    except (OSError, RuntimeError):
        return None


def _bounded_scanline_header(raw):
    """Parse *only* single-part, unambiguous EXR scanline bytes, no guessing.

    Unlike the deliberately conservative FFmpeg metadata route this accepts
    native HALF/FLOAT/UINT samples; the decoder below verifies chunk offsets,
    absolute signed scanline Y positions and every channel's stored byte size.
    Malformed, tiled, deep, multipart, subsampled, packed and named AOV files
    are not candidates for this CPU path.
    """
    if len(raw) < 16 or raw[:4] != b'v/1\x01':
        return None
    flags = struct.unpack_from('<I', raw, 4)[0]
    if flags & ~0xff or (flags & 0xff) not in (1, 2):
        return None  # any feature flags or unsupported version
    attributes = {}
    pos = 8
    for _ in range(256):
        end = raw.find(b'\x00', pos, min(pos + 256, len(raw)))
        if end < 0:
            return None
        key = raw[pos:end].decode('ascii')
        pos = end + 1
        if not key:
            break
        end = raw.find(b'\x00', pos, min(pos + 256, len(raw)))
        if end < 0 or key in attributes:
            return None
        kind = raw[pos:end].decode('ascii')
        pos = end + 1
        if pos + 4 > len(raw):
            return None
        length = struct.unpack_from('<I', raw, pos)[0]
        pos += 4
        if length > MAX_EXR_HEADER_BYTES or pos + length > len(raw):
            return None
        attributes[key] = (kind, raw[pos:pos + length])
        pos += length
    else:
        return None
    required = ('channels', 'compression', 'dataWindow', 'displayWindow')
    if not all(k in attributes for k in required):
        return None

    def window(key):
        typ, payload = attributes[key]
        if typ != 'box2i' or len(payload) != 16:
            raise ValueError('Invalid {} EXR window'.format(key))
        x0, y0, x1, y1 = struct.unpack('<4i', payload)
        return _sane_window((x0, y0, x1 - x0 + 1, y1 - y0 + 1))

    data = window('dataWindow')
    display = window('displayWindow')
    _check_budget(data)
    compression_type, comp = attributes['compression']
    if compression_type != 'compression' or comp not in (b'\x00', b'\x02', b'\x03'):
        return None  # NONE, ZIPS, ZIP only
    compression = comp[0]
    if 'lineOrder' in attributes:
        typ, value = attributes['lineOrder']
        if typ != 'lineOrder' or value not in (b'\x00', b'\x01'):
            return None
    par = 1.0
    if 'pixelAspectRatio' in attributes:
        typ, value = attributes['pixelAspectRatio']
        if typ != 'float' or len(value) != 4:
            return None
        par = struct.unpack('<f', value)[0]
    if not np.isfinite(par) or not 1.0 / 64 <= par <= 64.0:
        return None
    typ, chlist = attributes['channels']
    if typ != 'chlist':
        return None
    channels = []
    ch_pos = 0
    for _ in range(5):
        end = chlist.find(b'\x00', ch_pos)
        if end < 0:
            return None
        if end == ch_pos:
            ch_pos += 1
            break
        if end + 17 > len(chlist):
            return None
        name = chlist[ch_pos:end].decode('ascii')
        pixel_type = struct.unpack_from('<I', chlist, end + 1)[0]
        reserved = chlist[end + 5:end + 9]
        xs, ys = struct.unpack_from('<ii', chlist, end + 9)
        if (name not in ('R', 'G', 'B', 'A') or pixel_type not in (0, 1, 2)
                or reserved != b'\x00' * 4 or xs != 1 or ys != 1
                or name in [n for n, _ in channels]):
            return None
        channels.append((name, pixel_type))
        ch_pos = end + 17
    if (ch_pos != len(chlist) or
            set(n for n, _ in channels) not in
            ({'R', 'G', 'B'}, {'R', 'G', 'B', 'A'}) or
            channels != sorted(channels)):
        return None
    # Do not synthesize an alpha channel or transform UINT through float32.
    sizes = {0: 4, 1: 2, 2: 4}
    row_bytes = data[2] * sum(sizes[k] for _, k in channels)
    count = (data[3] + (16 if compression == 3 else 1) - 1) // (
        16 if compression == 3 else 1)
    if row_bytes * data[3] > MAX_EXR_PIXELS_BYTES:
        return None
    return {'data_window': data, 'display_window': display, 'channels': channels,
            'compression': compression, 'pixel_aspect_ratio': par,
            'header_end': pos, 'chunk_count': count, 'row_bytes': row_bytes}


def _zip_scanline_bytes(encoded, expected):
    """OpenEXR ZIP: bounded zlib, inverse predictor then reverse byte shuffle."""
    if len(encoded) == expected:
        return encoded  # OpenEXR permits raw chunks when ZIP expands them
    decompressor = zlib.decompressobj()
    data = decompressor.decompress(encoded, expected + 1)
    if (not decompressor.eof or decompressor.unused_data or
            decompressor.unconsumed_tail or len(data) != expected):
        return None
    values = bytearray(data)
    for i in range(1, len(values)):
        values[i] = (values[i - 1] + values[i] - 128) & 255
    half = (len(values) + 1) // 2
    unshuffled = bytearray(expected)
    unshuffled[0::2] = values[:half]
    unshuffled[1::2] = values[half:]
    return unshuffled


def _direct_offset_scanline_payload(path, source_id, source_frame,
                                    generation, tier, alpha_mode, header_only=False):
    """Bounded original EXR scanline reader for *verified offset* windows.

    No FFmpeg positions, color conversion, unknown scanline tile placement or
    display padding are guessed. The signed Y in EACH absolute chunk and every
    channel byte are verified before assigning native pixels to the frozen
    dataWindow. Preserve selected independent OpenEXR native handling.
    """
    if np is None or not os.path.isfile(path):
        return None
    try:
        with open(path, 'rb') as stream:
            st = os.fstat(stream.fileno())
            if (not stat.S_ISREG(st.st_mode) or
                    not 0 < st.st_size <= MAX_EXR_FILE_BYTES):
                return None
            raw = stream.read(min(MAX_EXR_HEADER_BYTES + 1, st.st_size))
            header = _bounded_scanline_header(raw)
            if header is None:
                return None
            dw, disp = header['data_window'], header['display_window']
            if (dw == (0, 0, dw[2], dw[3]) and disp == dw):
                return None  # preserve existing origin-zero FFmpeg contract
            if header_only:
                return dict(width=dw[2], height=dw[3],
                            pixel_aspect_ratio=header['pixel_aspect_ratio'],
                            data_window=dw, display_window=disp,
                            channels=tuple(n for n, _ in header['channels']),
                            layer='', part=0, part_count=1,
                            alpha_mode=('premultiplied' if len(header['channels']) == 4
                                        else 'opaque'),
                            geometry_accuracy='bounded EXR offset scanline header; pixels not decoded')
            from ..core.frame_payload import FrameGeometry, FramePayload
            table_start = header['header_end']
            table_length = header['chunk_count'] * 8
            if table_start + table_length > st.st_size:
                return None
            stream.seek(table_start)
            table = stream.read(table_length)
            if len(table) != table_length:
                return None
            offsets = struct.unpack('<{}Q'.format(header['chunk_count']), table)
            block_height = 16 if header['compression'] == 3 else 1
            channels = header['channels']
            names = [n for n, _ in channels]
            dtype = {0: '<u4', 1: '<f2', 2: '<f4'}
            arrays = {name: np.empty((dw[3], dw[2]), dtype=np.dtype(dtype[k]))
                      for name, k in channels}
            seen = set()
            for offset in offsets:
                if not table_start + table_length <= offset <= st.st_size - 8:
                    return None
                stream.seek(offset)
                prefix = stream.read(8)
                if len(prefix) != 8:
                    return None
                absolute_y, packed_length = struct.unpack('<iI', prefix)
                relative_y = absolute_y - dw[1]
                if (relative_y < 0 or relative_y >= dw[3] or
                        relative_y % block_height or relative_y in seen):
                    return None
                line_count = min(block_height, dw[3] - relative_y)
                expected = header['row_bytes'] * line_count
                if (packed_length <= 0 or packed_length > expected + 65536 or
                        offset + 8 + packed_length > st.st_size):
                    return None
                packed = stream.read(packed_length)
                if len(packed) != packed_length:
                    return None
                if header['compression'] == 0:
                    if packed_length != expected:
                        return None
                    content = packed
                else:
                    content = _zip_scanline_bytes(packed, expected)
                    if content is None:
                        return None
                read_at = 0
                for row in range(line_count):
                    for name, kind in channels:
                        size = dw[2] * np.dtype(dtype[kind]).itemsize
                        plane = np.frombuffer(content, dtype=dtype[kind],
                                              count=dw[2], offset=read_at)
                        arrays[name][relative_y + row] = plane
                        read_at += size
                if read_at != expected:
                    return None
                seen.add(relative_y)
            if len(seen) != header['chunk_count']:
                return None
            after = os.fstat(stream.fileno())
            if (after.st_ino, after.st_dev, after.st_size,
                    getattr(after, 'st_mtime_ns', after.st_mtime),
                    getattr(after, 'st_ctime_ns', after.st_ctime)) != (
                    st.st_ino, st.st_dev, st.st_size,
                    getattr(st, 'st_mtime_ns', st.st_mtime),
                    getattr(st, 'st_ctime_ns', st.st_ctime)):
                return None
            pixels = np.stack([arrays[name] for name in
                               ('R', 'G', 'B', 'A') if name in arrays], axis=-1)
            if source_frame is None:
                source_frame = filename_source_frame(path)
            payload = FramePayload(
                pixels, source_id=source_id or os.fspath(path),
                source_frame=0 if source_frame is None else source_frame,
                generation=generation, tier=tier,
                geometry=FrameGeometry(dw[2], dw[3],
                                       header['pixel_aspect_ratio'], dw, disp),
                color_metadata={
                    'format': 'OpenEXR', 'input_space': 'linear',
                    'channels': tuple(names), 'dtype': str(pixels.dtype),
                    'decoder': 'EXR bounded CPU scanline (native samples)',
                    'geometry_accuracy': 'verified original EXR absolute signed scanline Y and header windows',
                    'exr_native_windows_verified': True,
                    'exr_header_data_window': dw,
                    'exr_header_display_window': disp,
                    'exr_layer': '', 'exr_part': 0,
                    'color_management': 'unmanaged',
                    'display_label': UNMANAGED_LABEL},
                alpha_mode=alpha_mode or ('premultiplied' if len(names) == 4
                                           else 'opaque'), stage='source')
            return payload
    except (OSError, ValueError, TypeError, KeyError, IndexError,
            UnicodeDecodeError, OverflowError, MemoryError, struct.error,
            zlib.error):
        return None


def _ffmpeg_float_payload(path, source_id, source_frame, generation, tier,
                          alpha_mode):
    if np is None or not os.path.isfile(path):
        return None
    try:
        header = _raw_header(path)
    except (OSError, ValueError, KeyError, UnicodeDecodeError, struct.error):
        return None
    if header is None:
        return None
    window = header['data_window']
    # EXR cropped/nonzero origins require window-aware reconstruction, which
    # FFmpeg's image decoder cannot prove. Never attach pixels to a guessed
    # source coordinate frame even when FFprobe reports matching dimensions.
    if window != (0, 0, header['width'], header['height']) or \
            header['display_window'] != window:
        return None
    binaries = _ffmpeg_binaries()
    if binaries is None:
        return None
    ffmpeg, ffprobe = binaries
    from .ffmpeg import media_environment, _no_window_kwargs
    options = dict(env=media_environment(), stdout=subprocess.PIPE,
                   stderr=subprocess.PIPE, timeout=12)
    options.update(_no_window_kwargs())
    try:
        meta = subprocess.run([
            ffprobe, '-v', 'error', '-select_streams', 'v:0',
            '-show_entries', 'stream=width,height,pix_fmt,sample_aspect_ratio',
            '-of', 'json', path], **options)
        if meta.returncode:
            return None
        stream = json.loads(meta.stdout.decode('utf-8'))['streams'][0]
        width, height = int(stream['width']), int(stream['height'])
        pixfmt = stream.get('pix_fmt', '')
        # Refuse unusual/deep/ambiguous EXR representations rather than
        # discarding unknown channels under a false precision guarantee.
        if not 0 < width <= 16384 or not 0 < height <= 16384:
            return None
        if not str(pixfmt).startswith(('gbrp', 'gbrap')):
            return None  # decoder negotiated an unexpected layout
        channels = 4 if str(pixfmt).startswith('gbrap') else 3
        if (channels == 4) != (header['alpha_mode'] == 'premultiplied'):
            return None  # do not silently synthesize or discard source alpha
        if channels * width * height * 4 > 256 * 1024 * 1024:
            return None
        pixel_format = 'gbrapf32le' if channels == 4 else 'gbrpf32le'
        result = subprocess.run([
            ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin',
            '-i', path, '-frames:v', '1', '-pix_fmt', pixel_format,
            '-f', 'rawvideo', 'pipe:1'], **options)
        if result.returncode or len(result.stdout) != width * height * channels * 4:
            return None
        planes = np.frombuffer(result.stdout, dtype='<f4').reshape(
            channels, height, width)
        order = (2, 0, 1, 3) if channels == 4 else (2, 0, 1)
        pixels = np.stack([planes[index] for index in order], axis=-1)
        pixel_aspect = header['pixel_aspect_ratio']
        matches_data = (width, height) == (header['width'], header['height'])
        if not matches_data:
            return None
        from ..core.frame_payload import FramePayload, FrameGeometry
        geometry = FrameGeometry(width, height, pixel_aspect,
                                 header['data_window'], header['display_window'])
        return FramePayload(
            pixels, source_id=source_id or os.fspath(path),
            source_frame=(filename_source_frame(path) if source_frame is None
                          else source_frame) or 0,
            generation=generation, tier=tier,
            geometry=geometry,
            color_metadata={
                'format': 'OpenEXR', 'input_space': 'linear',
                'channels': ('R', 'G', 'B', 'A') if channels == 4 else ('R', 'G', 'B'),
                'dtype': 'float32', 'decoder': 'FFmpeg (float32)',
                'geometry_accuracy': ('FFmpeg raster dimensions verified against '
                                      'origin-zero EXR windows; native pixel '
                                      'placement unverified'),
                'exr_header_data_window': header['data_window'],
                'exr_header_display_window': header['display_window'],
                'exr_layer': header['layer'],
                'color_management': 'unmanaged', 'display_label': UNMANAGED_LABEL},
            alpha_mode=alpha_mode or ('premultiplied' if channels == 4 else 'opaque'),
            stage='source')
    except (OSError, ValueError, TypeError, KeyError, IndexError,
            subprocess.TimeoutExpired, OverflowError):
        return None


def is_exr(path):
    return bool(path) and os.fspath(path).lower().endswith('.exr')


def filename_source_frame(path):
    """Read the final signed frame token, if present (no arbitrary version IDs)."""
    stem = os.path.basename(os.fspath(path))
    match = re.search(r'(?:^|[._])(-?\d+)\.exr$', stem, flags=re.I)
    return int(match.group(1)) if match else None


def _window_tuple(window):
    if hasattr(window, 'min'):
        x0, y0 = window.min.x, window.min.y
        x1, y1 = window.max.x, window.max.y
    else:
        (x0, y0), (x1, y1) = window
    return int(x0), int(y0), int(x1) - int(x0) + 1, int(y1) - int(y0) + 1


def _native_pixel_type(channel):
    """EXR HALF, FLOAT, UINT (not UINT16) preserve their native sample types."""
    ptype = getattr(channel, 'type', None)
    value = getattr(ptype, 'v', ptype)
    if value == getattr(Imath.PixelType, 'HALF', -100):
        return np.float16, Imath.PixelType(Imath.PixelType.HALF)
    if value == getattr(Imath.PixelType, 'UINT', -101):
        return np.uint32, Imath.PixelType(Imath.PixelType.UINT)
    if value == getattr(Imath.PixelType, 'FLOAT', -102):
        return np.float32, Imath.PixelType(Imath.PixelType.FLOAT)
    raise ValueError('Unsupported native EXR channel pixel type')


def _legacy_pixels(path, layer=None):
    exr = OpenEXR.InputFile(os.fspath(path))
    try:
        header = exr.header()
        rectangle = _sane_window(_window_tuple(header['dataWindow']))
        x, y, width, height = rectangle
        selected = _channel_selection(header['channels'], layer)
        if selected is None:
            return None
        names, selected_layer = selected
        if len(names) not in (3, 4):
            return None  # legacy InputFile has no interleaved RGB channels
        _check_budget(rectangle, len(names))
        arrays = []
        for name in names:
            dtype, pixel_type = _native_pixel_type(header['channels'][name])
            data = exr.channel(name, pixel_type)
            if len(data) != width * height * np.dtype(dtype).itemsize:
                return None
            array = np.frombuffer(data, dtype=dtype)
            arrays.append(array.reshape(height, width))
        # Promote mixed EXR types without discarding signed float/HDR data.
        pixels = np.stack(arrays, axis=-1)
        return pixels, header, rectangle, names, selected_layer, 0, 1
    finally:
        exr.close()


def _open_modern(path, header_only=False):
    filename = os.fspath(path)
    if header_only:
        try:
            return OpenEXR.File(filename, separate_channels=True, header_only=True)
        except TypeError:
            try:
                return OpenEXR.File(filename, header_only=True)
            except TypeError:
                if os.path.isfile(filename):
                    raise RuntimeError('OpenEXR File API lacks safe header-only reads')
    try:
        return OpenEXR.File(filename, separate_channels=True)
    except TypeError:
        return OpenEXR.File(filename)


def _modern_parts(file, layer=None, part=None):
    parts = file.parts
    if not parts or (len(parts) != 1 and part is None):
        return None
    index = 0 if part is None else int(part)
    if index < 0 or index >= len(parts):
        return None
    chosen = parts[index]
    header = chosen.header
    channel_map = chosen.channels
    selected = _channel_selection(channel_map or header.get('channels', {}), layer)
    if selected is None:
        return None
    names, selected_layer = selected
    rectangle = (_sane_window(_window_tuple(header['dataWindow']))
                 if 'dataWindow' in header else None)
    if rectangle is not None:
        _check_budget(rectangle, 4)
    return chosen, header, channel_map, names, selected_layer, rectangle, index, len(parts)


def _modern_preflight(path, part=None):
    """Open just headers before allocating modern API's decompressed pixels."""
    if not os.path.isfile(path):
        return True  # tests with injected in-memory File implementations
    try:
        file = _open_modern(path, header_only=True)
    except (TypeError, RuntimeError):
        # Existing older File APIs may lack a header-only read. Their InputFile
        # implementation is safer for bounded reads, if available.
        return False
    try:
        if not (1 <= len(file.parts) <= 32):
            return False
        if len(file.parts) != 1 and part is None:
            return False
        index = 0 if part is None else int(part)
        if index < 0 or index >= len(file.parts):
            return False
        # Modern File decodes every part, not just the requested part. Apply
        # a cumulative cap to all part/channel storage before actual decode.
        total_bytes = 0
        for entry in file.parts:
            header = entry.header
            data_window = _sane_window(_window_tuple(header['dataWindow']))
            channel_count = len(header.get('channels', {}))
            if channel_count <= 0:
                channel_count = len(entry.channels)
            if channel_count <= 0:
                return False  # unbounded unknown channel count
            count = max(4, channel_count)
            total_bytes += data_window[2] * data_window[3] * count * 4
            if total_bytes > MAX_EXR_PIXELS_BYTES:
                return False
        return True
    finally:
        closer = getattr(file, 'close', None)
        if closer is not None:
            closer()


def _modern_pixels(path, layer=None, part=None):
    if not _modern_preflight(path, part):
        return None
    file = _open_modern(path)
    try:
        selected = _modern_parts(file, layer, part)
        if selected is None:
            return None
        (_, header, channel_map, names, selected_layer, rectangle,
         index, part_count) = selected
        if len(names) == 1:
            pixels = np.asarray(channel_map[names[0]].pixels)
            expected = 4 if str(names[0]).lower().endswith('rgba') else 3
            if pixels.ndim != 3 or pixels.shape[2] != expected:
                return None
            channels = tuple('RGBA'[:expected])
        else:
            arrays = [np.asarray(channel_map[name].pixels) for name in names]
            if any(item.ndim != 2 for item in arrays):
                return None
            pixels = np.stack(arrays, axis=-1)
            channels = names
        if pixels.dtype.kind not in 'uf' or pixels.dtype.itemsize not in (2, 4, 8):
            return None
        if rectangle is None:
            rectangle = (0, 0, pixels.shape[1], pixels.shape[0])
            header = dict(header, dataWindow=((0, 0),
                                              (pixels.shape[1] - 1, pixels.shape[0] - 1)),
                          __framereviewer_assumed_window__=True)
        _check_budget(rectangle, pixels.shape[2], pixels.dtype.itemsize)
        if pixels.shape[:2] != (rectangle[3], rectangle[2]):
            return None
        return pixels, header, rectangle, channels, selected_layer, index, part_count
    finally:
        closer = getattr(file, 'close', None)
        if closer is not None:
            closer()


def _native_file_guard(path, allow_multipart=False):
    """Reject oversized on-disk inputs and legacy multipart/deep decoders."""
    if not os.path.isfile(path):
        return True  # injected decoder fixtures use synthetic paths
    if os.path.getsize(path) > MAX_EXR_FILE_BYTES:
        return False
    with open(path, 'rb') as stream:
        prefix = stream.read(8)
    if len(prefix) != 8 or prefix[:4] != b'v/1\x01':
        return False
    flags = struct.unpack_from('<I', prefix, 4)[0]
    return not bool(flags & 0x800 or (flags & 0x1000 and not allow_multipart))


def read_header(path, layer=None, part=None):
    """Bounded EXR geometry/selection for the media probe; no frame payload.

    Ambiguous layers or multipart images require an explicit selection. Without
    Python OpenEXR, the restricted EXR header parser supports single-part
    scanline RGB(A) only and exposes the metadata as header-only evidence.
    """
    if not is_exr(path):
        return None
    try:
        if not available():
            if part not in (None, 0):
                return None
            header = _raw_header(path, layer)
            if header is not None:
                return header
            if layer not in (None, ''):
                return None
            return _direct_offset_scanline_payload(
                path, '', None, 0, 'source', None, header_only=True)
        if not _native_file_guard(path, allow_multipart=hasattr(OpenEXR, 'File')):
            return None
        if hasattr(OpenEXR, 'File'):
            if not _modern_preflight(path, part):
                return None
            # Header-only metadata must never trigger eager frame decoding for
            # a real file. Synthetic in-memory API shims can omit this mode.
            modes = (True,) if os.path.isfile(path) else (True, False)
            for header_only in modes:
                handle = _open_modern(path, header_only=header_only)
                try:
                    selected = _modern_parts(handle, layer, part)
                    if selected is not None:
                        (_, header, _, names, selected_layer, rectangle,
                         index, count) = selected
                        if rectangle is not None:
                            return _header_fields(header, names, selected_layer,
                                                  index, count)
                finally:
                    closer = getattr(handle, 'close', None)
                    if closer is not None:
                        closer()
            return None
        if part not in (None, 0):
            return None
        handle = OpenEXR.InputFile(os.fspath(path))
        try:
            header = handle.header()
            selected = _channel_selection(header['channels'], layer)
            if selected is None:
                return None
            names, selected_layer = selected
            if len(names) not in (3, 4):
                return None
            return _header_fields(header, names, selected_layer)
        finally:
            handle.close()
    except (OSError, RuntimeError, ValueError, TypeError, KeyError,
            AttributeError, IndexError, OverflowError, UnicodeDecodeError,
            struct.error):
        return None


def available_views(path):
    """Enumerate unambiguous, native OpenEXR RGB(A) parts/layers for artists.

    A selected view is always explicit ``(part, layer)``. This scans bounded
    native headers; the FFmpeg-only fallback cannot select a named layer or
    multipart view, so returning guesses would misrepresent its pixels.
    """
    if not available() or not is_exr(path):
        return ()
    choices = []
    try:
        if not _native_file_guard(path, allow_multipart=hasattr(OpenEXR, 'File')):
            return ()
        if hasattr(OpenEXR, 'File'):
            if not _modern_preflight(path, part=0):
                return ()
            handle = _open_modern(path, header_only=True)
            try:
                parts = handle.parts
                if not 1 <= len(parts) <= 32:
                    return ()
                source = [(index, part.header,
                           part.channels or part.header.get('channels', {}))
                          for index, part in enumerate(parts)]
            finally:
                closer = getattr(handle, 'close', None)
                if closer is not None:
                    closer()
        else:
            handle = OpenEXR.InputFile(os.fspath(path))
            try:
                header = handle.header()
                source = [(0, header, header['channels'])]
            finally:
                handle.close()
        for part_index, header, channels in source:
            groups = sorted({(str(name).rsplit('.', 1)[0]
                              if '.' in str(name) else '')
                             for name in channels})
            for layer in groups:
                selected = _channel_selection(channels, layer=layer)
                if selected is None:
                    continue
                names, matched = selected
                if len(names) not in (1, 3, 4):
                    continue
                try:
                    info = _header_fields(header, names, matched, part_index,
                                          len(source))
                except (ValueError, TypeError, KeyError, OverflowError):
                    continue
                choices.append(info)
                if len(choices) > 64:
                    return ()
    except (OSError, RuntimeError, ValueError, TypeError, KeyError,
            AttributeError, IndexError, OverflowError):
        return ()
    return tuple(choices)


def read_frame_payload(path, source_id='', source_frame=None, generation=0,
                       tier='source', alpha_mode=None, layer=None, part=None,
                       display_canvas=False):
    """Read a source-stage FramePayload, or None for missing/unsupported EXR.

    ``source_frame`` may be negative; when absent it is inferred only from the
    filename's final frame token and otherwise defaults to zero. All pixel
    values, including negatives, over-range and non-finite samples, survive.
    """
    if not is_exr(path):
        return None
    if not available():
        if layer not in (None, '') or part not in (None, 0):
            return None  # FFmpeg cannot select an EXR layer/part by identity
        # Safe native offset support is a strict, separate scanline reader:
        # FFmpeg cannot independently prove nonzero signed pixel placement.
        # Do not broaden its established origin-zero precision contract.
        fallback = _direct_offset_scanline_payload(
            path, source_id, source_frame, generation, tier, alpha_mode)
        if fallback is None:
            fallback = _ffmpeg_float_payload(path, source_id, source_frame,
                                             generation, tier, alpha_mode)
        if fallback is None or not display_canvas:
            return fallback
        try:
            return fallback.to_display_canvas()
        except (ValueError, TypeError, MemoryError):
            return None
    try:
        if not _native_file_guard(path, allow_multipart=hasattr(OpenEXR, 'File')):
            return None
        if hasattr(OpenEXR, 'File'):
            decoded = _modern_pixels(path, layer, part)
        else:
            if part not in (None, 0):
                return None
            decoded = _legacy_pixels(path, layer)
        if decoded is None:
            return None
        pixels, header, data_window, names, selected_layer, index, part_count = decoded
        assumed_window = bool(header.get('__framereviewer_assumed_window__', False))
        if display_canvas and assumed_window:
            return None  # no reliable EXR coordinate origin for canvas placement
        from ..core.frame_payload import FrameGeometry, FramePayload
        width, height = pixels.shape[1], pixels.shape[0]
        display_window = (_window_tuple(header['displayWindow'])
                          if 'displayWindow' in header else data_window)
        par = float(header.get('pixelAspectRatio', 1.0))
        if not np.isfinite(par) or par <= 0:
            return None
        has_alpha = len(names) == 4
        association = alpha_mode or ('premultiplied' if has_alpha else 'opaque')
        source_identity = source_id or os.fspath(path)
        if layer is not None or part is not None:
            source_identity = '{}#exr-part={}:layer={}'.format(
                source_identity, index, selected_layer)
        metadata = {'format': 'OpenEXR', 'input_space': 'linear',
                    'transfer': 'scene-linear (assumed)',
                    'channels': tuple(str(n) for n in names),
                    'exr_layer': selected_layer, 'exr_part': index,
                    'exr_part_count': part_count,
                    'exr_native_windows_verified': not assumed_window,
                    'dtype': str(pixels.dtype), 'color_management': 'unmanaged',
                    'geometry_accuracy': ('raster origin assumed (no EXR dataWindow)'
                                          if assumed_window else
                                          'native EXR data/display windows'),
                    'display_label': UNMANAGED_LABEL}
        if source_frame is None:
            source_frame = filename_source_frame(path)
        payload = FramePayload(
            pixels, source_id=source_identity,
            source_frame=0 if source_frame is None else source_frame,
            generation=generation, tier=tier,
            geometry=FrameGeometry(width, height, par, data_window,
                                   display_window),
            color_metadata=metadata, alpha_mode=association, stage='source')
        return payload.to_display_canvas() if display_canvas else payload
    except (OSError, RuntimeError, ValueError, TypeError, KeyError,
            AttributeError, IndexError, OverflowError, MemoryError,
            struct.error):
        return None


def read_rgb_float(path):
    """Compatibility API: return linear HxWx3 float32 data, or None."""
    payload = read_frame_payload(path)
    if payload is None:
        return None
    rgb = payload.pixels[:, :, :3].astype(np.float32)
    return np.nan_to_num(rgb, copy=False)


def read_size(path, layer=None, part=None):
    """Selected EXR data-window size without decoding native pixel arrays."""
    info = read_header(path, layer=layer, part=part)
    if info is not None:
        return info['width'], info['height']
    if not available() and layer is None and part is None:
        payload = _ffmpeg_float_payload(path, '', None, 0, 'source', None)
        return ((payload.geometry.width, payload.geometry.height)
                if payload is not None else None)
    return None


def payload_to_pixmap(payload, decode_width=None, color_state=None,
                      with_status=False):
    """Render a *display-only* pixmap; source pixels remain immutable.

    With a configured OCIO state, use the existing color pipeline. Otherwise,
    explicitly report unmanaged linear -> sRGB. A broken configuration falls
    back with a diagnostic; callers can show ``status`` in the review UI.
    """
    from ..viewer.color import ColorPipelineState, process_payload
    if payload is None or payload.stage != 'source':
        return (None, 'No source EXR payload') if with_status else None
    fallback = ColorPipelineState(input_space='linear', working_space='linear',
                                  display_space='sRGB')
    invalid_state = ''
    try:
        state = ColorPipelineState.from_dict(color_state)
    except (ValueError, TypeError) as exc:
        state = fallback
        invalid_state = str(exc)
    configured = bool(state.config)
    status = ('OCIO managed: {}'.format(state.config) if configured
              else UNMANAGED_LABEL)
    if invalid_state:
        status = 'Unmanaged preview: invalid color state ({}); linear -> sRGB'.format(
            invalid_state)
    if not configured and color_state is not None:
        # Honor grade/exposure while supplying the inferred EXR input space.
        from dataclasses import replace
        state = replace(state, input_space='linear')
    if color_state is None:
        state = fallback
    try:
        working = process_payload(payload, state, stage='working')
        display = process_payload(working, state, stage='display')
    except (RuntimeError, ValueError, ImportError, OSError) as exc:
        status = 'Unmanaged preview: OCIO transform failed ({}); linear -> sRGB'.format(exc)
        working = process_payload(payload, fallback, stage='working')
        display = process_payload(working, fallback, stage='display')
    rgba = np.asarray(display.pixels, dtype=np.float32)
    if rgba.shape[2] == 3:
        alpha = np.ones(rgba.shape[:2] + (1,), np.float32)
        rgba = np.concatenate((rgba, alpha), axis=-1)
    elif display.alpha_mode == 'premultiplied' and rgba.shape[2] == 4:
        # Qt Format_RGBA8888 stores *straight* alpha; no double premultiplying.
        rgb = np.divide(rgba[:, :, :3], rgba[:, :, 3:4],
                        out=np.zeros_like(rgba[:, :, :3]),
                        where=rgba[:, :, 3:4] > 1e-8)
        rgba = np.concatenate((rgb, rgba[:, :, 3:4]), axis=-1)
    rgba = np.nan_to_num(rgba, nan=0.0, posinf=1.0, neginf=0.0)
    data = np.ascontiguousarray(np.clip(rgba * 255.0 + 0.5, 0, 255).astype(np.uint8))
    height, width = data.shape[:2]
    image = QtGui.QImage(data.data, width, height, width * 4,
                         QtGui.QImage.Format_RGBA8888)
    pixmap = QtGui.QPixmap.fromImage(image.copy())
    if decode_width and pixmap.width() > decode_width:
        pixmap = pixmap.scaledToWidth(int(decode_width), QtCore.Qt.SmoothTransformation)
    return (pixmap, status) if with_status else pixmap


def read_pixmap(path, decode_width=None):
    """Existing pixmap API for thumbnails, without losing the raw decode path."""
    payload = read_frame_payload(path)
    return payload_to_pixmap(payload, decode_width) if payload is not None else None
