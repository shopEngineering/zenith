#!/usr/bin/env python3
"""Write a 256x256 PNG placeholder icon (ZENITH cyan) using only the stdlib.

Avoids pulling in rsvg/imagemagick just to raster the favicon. A flat cyan square
is a perfectly adequate AppImage icon placeholder; swap in a real rendered icon
later if desired. Usage: make-icon.py <out.png>
"""
import struct
import sys
import zlib

W = H = 256
R, G, B = 0x3F, 0xE3, 0xFF  # ZENITH cyan (#3fe3ff)


def chunk(tag, data):
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def main(out):
    raw = bytearray()
    row = bytes((R, G, B)) * W
    for _ in range(H):
        raw.append(0)          # filter type 0 (None) per scanline
        raw.extend(row)
    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0))  # 8-bit RGB
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    png += chunk(b"IEND", b"")
    with open(out, "wb") as f:
        f.write(png)


if __name__ == "__main__":
    main(sys.argv[1])
