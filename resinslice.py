"""Resin plates for the Peopoly Phenom: the layers of PrusaSlicer (.sl1) as a ChiTu file (.cbddlp).

The slice run places the parts of a resin plate on the area of the screen (the grid of the plate),
and PrusaSlicer slices them as one model with its supports and its pad (--export-sla). The .sl1
file is a zip of a PNG for each layer: 8-bit gray, as the slicer shows the plate from above, with
no mirror. PrusaSlicer mirrors X by default, so the profile must set display_mirror_x = 0. This
module writes these layers as a .cbddlp of ChiTuBox for the ChiTu board of the Phenom, with the
exposures and the lifts of the profile.

The layout of a .cbddlp (version 2), as the deck reads it (deck/internal/chitu/sliced.go):

- The header, 112 bytes: the magic 0x12FD0019, the version, the bed in mm, the height of the
  plate, the layer height, the exposures, the count of the bottom layers, the pixels of the
  screen, the offsets of the previews, of the table of the layers and of the print parameters,
  the print time, the projector (1: the screen shows the mirror of the slicer), the level of the
  anti-aliasing, and the power of the light.
- Two previews: a header of 32 bytes each, and the pixels as runs of 16-bit colors.
- The print parameters, 60 bytes: the lifts and their speeds, the volume, the weight, the cost,
  and the time with no light.
- The table of the layers, 36 bytes for each layer: the height, the exposure, the time with no
  light, the offset and the size of the data, and 4 words of 0.
- The data of each layer: a run-length code of 1 bit. In each byte, bit 7 is the color and the 7
  low bits are the length of the run, at most 125. The runs go on past the end of a row.

The module uses only the Python standard library.
"""

import struct
import zipfile
import zlib

MAGIC_CBDDLP = 0x12FD0019
HEADER_SIZE = 112
PREVIEW_HEADER = 32
PARAMS_SIZE = 60
LAYER_DEF = 36
MAX_RUN = 125


# PNG: 8-bit gray or gray with alpha, with no interlace, as PrusaSlicer writes the layers.

def read_png_gray(data):
    """Returns (width, height, rows): the rows of the 8-bit gray PNG data as bytes."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG file")
    pos, idat, width = 8, [], 0
    while pos < len(data):
        length, kind = struct.unpack_from(">I4s", data, pos)
        chunk = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            width, height, depth, ctype, _, _, interlace = struct.unpack(">IIBBBBB", chunk)
            if depth != 8 or ctype not in (0, 4) or interlace != 0:
                raise ValueError("a layer PNG must be 8-bit gray with no interlace")
            bpp = 1 if ctype == 0 else 2
        elif kind == b"IDAT":
            idat.append(chunk)
        elif kind == b"IEND":
            break
    raw = zlib.decompress(b"".join(idat))
    stride = width * bpp
    rows, prev = [], bytearray(stride)
    for y in range(height):
        f = raw[y * (stride + 1)]
        line = bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        if f == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif f == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif f == 3:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif f == 4:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b, c = prev[i], prev[i - bpp] if i >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 0xFF
        prev = line
        rows.append(bytes(line[::bpp]) if bpp == 2 else bytes(line))
    return width, height, rows


def encode_layer(rows, mirror):
    """The run-length code of 1 bit of a layer: a pixel of 128 or more is lit. mirror stores each
    row from right to left, for a printer that shows the mirror of the slicer."""
    out = bytearray()
    color, run = 0, 0

    def flush():
        nonlocal run
        while run > 0:
            n = min(run, MAX_RUN)
            out.append((color << 7) | n)
            run -= n

    for row in rows:
        if mirror:
            row = row[::-1]
        # Runs of the same color: the row as bits with a fast scan for the changes.
        bits = row.translate(LIT)
        i, n = 0, len(bits)
        while i < n:
            c = bits[i]
            j = bits.find(b"\x00" if c else b"\x01", i)
            if j < 0:
                j = n
            if c != color:
                flush()
                color = c
            run += j - i
            i = j
    flush()
    return bytes(out)


# A pixel of 128 or more is lit (1), else 0.
LIT = bytes(1 if v >= 128 else 0 for v in range(256))


def encode_preview(width, height, pixel):
    """The runs of 16-bit colors of a preview: pixel(x, y) returns (r, g, b). A run of more than
    one pixel has the flag 0x0020 and a second word 0x3000 with the count minus one (at most
    0xFFF + 1)."""
    out = bytearray()
    prev, count = None, 0

    def flush():
        if prev is None:
            return
        word = ((prev[0] >> 3) << 11) | ((prev[1] >> 3) << 6) | (prev[2] >> 3)
        if count > 1:
            out.extend(struct.pack("<HH", word | 0x0020, 0x3000 | (count - 1)))
        else:
            out.extend(struct.pack("<H", word))

    for y in range(height):
        for x in range(width):
            c = pixel(x, y)
            if c == prev and count < 0x1000:
                count += 1
                continue
            flush()
            prev, count = c, 1
    flush()
    return bytes(out)


def preview_of(rows, width, height, size):
    """A small preview of the plate from the first layers: lit pixels in white on black, size x
    size/aspect pixels."""
    pw, ph = size, max(1, round(size * height / width))
    sx, sy = width / pw, height / ph

    def pixel(x, y):
        r = rows[min(height - 1, int((y + 0.5) * sy))]
        return (0xF0, 0xF0, 0xF0) if r[min(width - 1, int((x + 0.5) * sx))] >= 128 else (0x10, 0x10, 0x10)

    return pw, ph, encode_preview(pw, ph, pixel)


def sl1_layers(path):
    """The config.ini of the .sl1 file as a dict, and the names of its layer PNGs in order."""
    z = zipfile.ZipFile(path)
    cfg = {}
    for line in z.read("config.ini").decode().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip()
    names = sorted(n for n in z.namelist() if n.endswith(".png") and not n.startswith("thumbnail"))
    return z, cfg, names


def write_cbddlp(sl1, out, resin):
    """Writes the layers of the .sl1 file as the .cbddlp out. resin has the settings of the
    profile: bed (x, y, z mm), resolution (x, y), mirror, layer_height, exposure,
    bottom_exposure, bottom_layers, light_off, bottom_light_off, lift, lift_speed, bottom_lift,
    bottom_lift_speed, retract_speed, pwm, bottom_pwm, and optionally density (g/ml) and
    cost_per_ml. It returns the facts of the plate: layers, height, seconds, ml, grams."""
    z, cfg, names = sl1_layers(sl1)
    resx, resy = resin["resolution"]
    lh = float(resin["layer_height"])
    n = len(names)
    if n == 0:
        raise ValueError("the .sl1 file has no layers")
    if abs(float(cfg.get("layerHeight", lh)) - lh) > 1e-6:
        raise ValueError("the layer height of the .sl1 file is not the layer height of the profile")
    bottom = int(resin["bottom_layers"])
    layers, first = [], None
    for i, name in enumerate(names):
        w, h, rows = read_png_gray(z.read(name))
        if (w, h) != (resx, resy):
            raise ValueError(f"layer {i}: {w} x {h} pixels, not {resx} x {resy}")
        if first is None:
            first = rows
        layers.append(encode_layer(rows, resin["mirror"]))
    z.close()
    # The time of the print: the light and the time with no light of each layer, and the moves
    # of the plate up by the lift and down again.
    def layer_time(i):
        b = i < bottom
        light = float(resin["bottom_exposure"] if b else resin["exposure"])
        off = float(resin["bottom_light_off"] if b else resin["light_off"])
        lift = float(resin["bottom_lift"] if b else resin["lift"])
        up = float(resin["bottom_lift_speed"] if b else resin["lift_speed"])
        down = float(resin["retract_speed"])
        return light, off, light + off + lift / up * 60 + lift / down * 60
    seconds = sum(layer_time(i)[2] for i in range(n))
    ml = float(cfg.get("usedMaterial", 0))
    grams = ml * float(resin.get("density", 1.1))
    cost = ml * float(resin.get("cost_per_ml", 0))

    large = preview_of(first, resx, resy, 400)
    small = preview_of(first, resx, resy, 200)
    # The layout: the header, the two previews, the print parameters, the table, the layers.
    off = HEADER_SIZE
    large_at = off
    off += PREVIEW_HEADER + len(large[2])
    small_at = off
    off += PREVIEW_HEADER + len(small[2])
    params_at = off
    off += PARAMS_SIZE
    table_at = off
    off += LAYER_DEF * n
    data_at = []
    for d in layers:
        data_at.append(off)
        off += len(d)

    bx, by, bz = resin["bed"]
    head = struct.pack("<IIfffIIfffffIIIIIIIIIIIIHHIII",
                       MAGIC_CBDDLP, 2, bx, by, bz, 0, 0, n * lh, lh,
                       float(resin["exposure"]), float(resin["bottom_exposure"]), float(resin["light_off"]),
                       bottom, resx, resy, large_at, table_at, n, small_at, round(seconds),
                       1 if resin["mirror"] else 0, params_at, PARAMS_SIZE, 1,
                       int(resin.get("bottom_pwm", 255)), int(resin.get("pwm", 255)), 0, 0, 0)
    assert len(head) == HEADER_SIZE, len(head)

    def preview_block(at, p):
        w, h, pix = p
        return struct.pack("<IIII16x", w, h, at + PREVIEW_HEADER, len(pix)) + pix

    params = struct.pack("<ffffffffffI4I",
                         float(resin["bottom_lift"]), float(resin["bottom_lift_speed"]),
                         float(resin["lift"]), float(resin["lift_speed"]), float(resin["retract_speed"]),
                         ml, grams, cost, float(resin["bottom_light_off"]), float(resin["light_off"]),
                         bottom, 0, 0, 0, 0)
    assert len(params) == PARAMS_SIZE, len(params)
    table = bytearray()
    for i, d in enumerate(layers):
        light, off_s, _ = layer_time(i)
        table += struct.pack("<fffII4I", (i + 1) * lh, light, off_s, data_at[i], len(d), 0, 0, 0, 0)
    with open(out, "wb") as f:
        f.write(head)
        f.write(preview_block(large_at, large))
        f.write(preview_block(small_at, small))
        f.write(params)
        f.write(table)
        for d in layers:
            f.write(d)
    return {"layers": n, "height": round(n * lh, 3), "seconds": round(seconds), "ml": round(ml, 2), "grams": round(grams, 1)}
