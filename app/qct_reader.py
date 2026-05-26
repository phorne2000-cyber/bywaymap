
import datetime
import itertools
import math
import struct
from pathlib import Path
from typing import Any, BinaryIO

from PIL import Image

TILE_SIZE = 64
_structs = {}

def unpack(stream: BinaryIO, fmt: str):
    s = _structs.get(fmt)
    if s is None:
        s = struct.Struct(fmt)
        _structs[fmt] = s
    data = stream.read(s.size)
    if not data or len(data) != s.size:
        raise EOFError("unexpected eof")
    return s.unpack(data)[0]

def integer(f): return unpack(f, "<I")
def float64(f): return unpack(f, "<d")
def byte(f): return unpack(f, "B")
def short(f): return unpack(f, "<H")

def string(f):
    out = bytearray()
    while True:
        c = f.read(1)
        if not c or c == b"\0":
            break
        out.extend(c)
    try:
        return out.decode("utf-8", errors="replace")
    except Exception:
        return bytes(out).decode("latin1", errors="replace")

def pointer(f, value):
    ptr = integer(f)
    offset = f.tell()
    if ptr == 0:
        return None
    f.seek(ptr)
    try:
        return value(f)
    finally:
        f.seek(offset)

def date_time(f):
    try:
        return datetime.datetime.fromtimestamp(integer(f)).isoformat()
    except Exception:
        return None

def array(f, size, value):
    if callable(size):
        size = size(f)
    return [value(f) for _ in range(size)]

def datum_shift(f):
    return {"north": float64(f), "east": float64(f)}

def licence_information(f):
    data = {"identifier": integer(f)}
    f.seek(8, 1)
    data.update({"license_description": pointer(f, string), "serial_number": array(f, 32, byte)})
    f.seek(84, 1)
    return data

def digital_map_shop(f):
    return {"size": integer(f), "url": pointer(f, string)}

def extended_data_structure(f):
    data = {"map_type": pointer(f, string), "datum_shift": pointer(f, datum_shift), "disk_name": pointer(f, string)}
    f.seek(8, 1)
    data.update({
        "license_information": pointer(f, licence_information),
        "associated_data": pointer(f, string),
        "digital_map_shop": pointer(f, digital_map_shop),
    })
    return data

def map_outline_point(f):
    return {"latitude": float64(f), "longitude": float64(f)}

def meta_data(f):
    data = {
        "magic": integer(f),
        "version": integer(f),
        "width": integer(f),
        "height": integer(f),
        "long_title": pointer(f, string),
        "name": pointer(f, string),
        "identifier": pointer(f, string),
        "edition": pointer(f, string),
        "revision": pointer(f, string),
        "keywords": pointer(f, string),
        "copyright": pointer(f, string),
        "scale": pointer(f, string),
        "datum": pointer(f, string),
        "depths": pointer(f, string),
        "heights": pointer(f, string),
        "projection": pointer(f, string),
        "bit_field": integer(f),
        "original_file_name": pointer(f, string),
        "original_file_size": integer(f),
        "original_file_creation_time": date_time(f),
    }
    f.seek(4, 1)
    data.update({"extended_data_struct": pointer(f, extended_data_structure), "map_outline_points": integer(f)})
    count = data.get("map_outline_points") or 0
    data["map_outline"] = pointer(f, lambda f_: array(f_, count, map_outline_point)) or []
    return data

def deinterlace(y):
    return int("".join(reversed(f"{y:06b}")), 2)

def iter_bits(f):
    while True:
        b = byte(f)
        for _ in range(8):
            if b:
                yield b & 1
                b >>= 1
            else:
                yield 0

def decode_code_book(f):
    code_book = []
    branches = 0
    colors = 0
    while colors <= branches:
        b = byte(f)
        code_book.append(b)
        if b == 128:
            code_book.append(65539 - short(f))
            code_book.append(None)
            branches += 1
        elif b < 128:
            colors += 1
        else:
            branches += 1
    return code_book

def decode_huffman(f):
    code_book = decode_code_book(f)
    if len(code_book) == 1:
        color = code_book[0]
        while True:
            yield color
    bits = iter_bits(f)
    while True:
        p = 0
        while True:
            code = code_book[p]
            if code < 128:
                break
            bit = next(bits)
            if bit:
                if code == 128:
                    p += code_book[p + 1]
                else:
                    p += 257 - code
            else:
                if code == 128:
                    p += 3
                else:
                    p += 1
        yield code

def decode_rle(f):
    f.seek(-1, 1)
    sub_palette_len = byte(f)
    sub_palette = array(f, sub_palette_len, byte)
    if not sub_palette_len:
        return
    sub_palette_len -= 1
    repeat_shift = 0
    while sub_palette_len:
        sub_palette_len >>= 1
        repeat_shift += 1
    sub_palette_mask = (1 << repeat_shift) - 1
    while True:
        b = byte(f)
        color = sub_palette[b & sub_palette_mask]
        repeat = b >> repeat_shift
        for _ in range(repeat):
            yield color

def decode_packed(f):
    f.seek(-1, 1)
    sub_palette_len = byte(f)
    sub_palette = array(f, sub_palette_len, byte)
    if not sub_palette_len:
        return
    sub_palette_len -= 1
    next_shift = 0
    while sub_palette_len:
        sub_palette_len >>= 1
        next_shift += 1
    sub_palette_mask = (1 << next_shift) - 1
    total_per_int = 32 // max(next_shift, 1)
    while True:
        i = integer(f)
        for _ in range(total_per_int):
            yield sub_palette[i & sub_palette_mask]
            i >>= next_shift

def draw_image(f, drawing, x, y):
    b0 = byte(f)
    if b0 in (0, 0xFF):
        pixels = decode_huffman(f)
    elif b0 > 127:
        pixels = decode_packed(f)
    else:
        pixels = decode_rle(f)

    for row0 in range(TILE_SIZE):
        row = deinterlace(row0) + y
        for col in range(x, x + TILE_SIZE):
            drawing[col, row] = next(pixels)

def convert_qct_to_png(qct_path: str | Path, png_path: str | Path) -> dict[str, Any]:
    qct_path = Path(qct_path)
    png_path = Path(png_path)
    with qct_path.open("rb") as f:
        md = meta_data(f)
        geo_refs = array(f, 40, float64)
        raw_palette = array(f, 1024, byte)[:512]
        palette = [(r, g, b) for b, g, r, _ in itertools.zip_longest(*[iter(raw_palette)] * 4, fillvalue=0)]
        interp_mat = array(f, 16384, byte)

        width_tiles = int(md["width"])
        height_tiles = int(md["height"])
        image_index = array(f, width_tiles * height_tiles, integer)

        image = Image.new("P", (width_tiles * TILE_SIZE, height_tiles * TILE_SIZE), 0)
        flat_palette = []
        for r, g, b in palette:
            flat_palette.extend([r, g, b])
        flat_palette.extend([0] * (768 - len(flat_palette)))
        image.putpalette(flat_palette[:768])
        drawing = image.load()

        for ty in range(height_tiles):
            for tx in range(width_tiles):
                ptr = image_index[(width_tiles * ty) + tx]
                if not ptr:
                    continue
                f.seek(ptr)
                draw_image(f, drawing, tx * TILE_SIZE, ty * TILE_SIZE)

        png_path.parent.mkdir(parents=True, exist_ok=True)
        image.convert("RGBA").save(png_path)

    outline = md.get("map_outline") or []
    lons = [float(p["longitude"]) for p in outline if p and p.get("longitude") is not None]
    lats = [float(p["latitude"]) for p in outline if p and p.get("latitude") is not None]
    bounds = [min(lons), min(lats), max(lons), max(lats)] if lons and lats else None

    safe_meta = {
        "name": md.get("name"),
        "long_title": md.get("long_title"),
        "identifier": md.get("identifier"),
        "scale": md.get("scale"),
        "datum": md.get("datum"),
        "projection": md.get("projection"),
        "width_tiles": md.get("width"),
        "height_tiles": md.get("height"),
        "outline_points": len(outline),
    }

    return {
        "state": "ready",
        "png_path": str(png_path),
        "image_width": width_tiles * TILE_SIZE,
        "image_height": height_tiles * TILE_SIZE,
        "bounds": bounds,
        "metadata": safe_meta,
    }
