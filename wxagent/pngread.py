"""
A minimal PNG reader, because Pillow is not installed and one dependency for
one job is not worth it.

Handles what RainViewer actually serves: non-interlaced PNG at 1, 2, 4 or 8
bits per sample, in colour types 0 (grey), 2 (RGB), 3 (palette), 4 (grey+alpha)
and 6 (RGBA). RainViewer's radar tiles are 4-bit palette images, which is why
the sub-byte unpacking below exists. Anything else raises rather than guessing -
a silently mis-decoded radar tile would produce confident nonsense, which is
the one outcome this agent is built to avoid.
"""

from __future__ import annotations

import struct
import zlib

__all__ = ["PngImage", "decode"]


class PngError(ValueError):
    pass


class PngImage:
    __slots__ = ("width", "height", "channels", "pixels", "indices", "palette")

    def __init__(self, width: int, height: int, channels: int,
                 pixels: bytes | bytearray,
                 indices: bytes | None = None,
                 palette: bytes | None = None):
        self.width = width
        self.height = height
        self.channels = channels
        self.pixels = pixels
        # For palette images the raw index is kept alongside the expanded RGBA.
        # On a radar tile whose palette is an ordered intensity ramp, the index
        # IS the intensity level - far more robust than matching colours back
        # to a table by nearest-neighbour.
        self.indices = indices
        self.palette = palette

    def index(self, x: int, y: int) -> int | None:
        if self.indices is None:
            return None
        if not (0 <= x < self.width and 0 <= y < self.height):
            raise IndexError(f"({x}, {y}) outside {self.width}x{self.height}")
        return self.indices[y * self.width + x]

    def pixel(self, x: int, y: int) -> tuple[int, ...]:
        """(r, g, b, a) style tuple, `channels` long, at integer coordinates."""
        if not (0 <= x < self.width and 0 <= y < self.height):
            raise IndexError(f"({x}, {y}) outside {self.width}x{self.height}")
        i = (y * self.width + x) * self.channels
        return tuple(self.pixels[i:i + self.channels])


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def decode(data: bytes) -> PngImage:
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise PngError("not a PNG")

    pos = 8
    width = height = depth = colour = 0
    interlace = 0
    palette: bytes = b""
    trns: bytes = b""
    idat = bytearray()

    while pos + 8 <= len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        ctype = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        pos += 12 + length          # 4 len + 4 type + body + 4 crc

        if ctype == b"IHDR":
            width, height, depth, colour, _comp, _filt, interlace = \
                struct.unpack(">IIBBBBB", body[:13])
        elif ctype == b"PLTE":
            palette = body
        elif ctype == b"tRNS":
            trns = body
        elif ctype == b"IDAT":
            idat += body
        elif ctype == b"IEND":
            break

    if not width or not height:
        raise PngError("missing IHDR")
    if depth not in (1, 2, 4, 8):
        raise PngError(f"unsupported bit depth {depth}")
    if interlace:
        raise PngError("interlaced PNG not supported")

    src_channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(colour)
    if src_channels is None:
        raise PngError(f"unsupported colour type {colour}")

    raw = zlib.decompress(bytes(idat))
    # At depths below 8 a scanline is packed several samples to the byte, and
    # the filters operate on BYTES with an offset of one whole pixel rounded
    # up - so the filter step and the sample step are different numbers.
    bits_per_pixel = src_channels * depth
    stride = (width * bits_per_pixel + 7) // 8
    fstep = max(1, bits_per_pixel // 8)
    if len(raw) < (stride + 1) * height:
        raise PngError("truncated image data")

    # Undo the per-scanline filters (PNG spec 9.2).
    out = bytearray(stride * height)
    prev = bytearray(stride)
    p = 0
    for y in range(height):
        ftype = raw[p]
        p += 1
        line = bytearray(raw[p:p + stride])
        p += stride
        if ftype == 1:                      # Sub
            for i in range(fstep, stride):
                line[i] = (line[i] + line[i - fstep]) & 0xFF
        elif ftype == 2:                    # Up
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ftype == 3:                    # Average
            for i in range(stride):
                a = line[i - fstep] if i >= fstep else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:                    # Paeth
            for i in range(stride):
                a = line[i - fstep] if i >= fstep else 0
                c = prev[i - fstep] if i >= fstep else 0
                line[i] = (line[i] + _paeth(a, prev[i], c)) & 0xFF
        elif ftype != 0:
            raise PngError(f"bad filter type {ftype}")
        out[y * stride:(y + 1) * stride] = line
        prev = line

    # Unpack sub-byte samples into one byte each.
    if depth < 8:
        mask = (1 << depth) - 1
        per_byte = 8 // depth
        unpacked = bytearray(width * src_channels * height)
        for y in range(height):
            row = out[y * stride:(y + 1) * stride]
            base = y * width * src_channels
            for i in range(width * src_channels):
                b = row[i // per_byte]
                shift = 8 - depth * ((i % per_byte) + 1)
                unpacked[base + i] = (b >> shift) & mask
        out = unpacked

    # Expand a palette image to RGBA so callers see one shape, keeping the
    # original indices for callers that want the intensity level directly.
    if colour == 3:
        if not palette:
            raise PngError("palette image without PLTE")
        idx_plane = bytes(out[:width * height])
        rgba = bytearray(width * height * 4)
        for i in range(width * height):
            idx = idx_plane[i]
            j = idx * 3
            if j + 2 < len(palette):
                rgba[i * 4] = palette[j]
                rgba[i * 4 + 1] = palette[j + 1]
                rgba[i * 4 + 2] = palette[j + 2]
            rgba[i * 4 + 3] = trns[idx] if idx < len(trns) else 255
        return PngImage(width, height, 4, bytes(rgba),
                        indices=idx_plane, palette=palette)

    return PngImage(width, height, src_channels, bytes(out))
