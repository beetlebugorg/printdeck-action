"""Resin plates for the Peopoly Phenom: the layers of PrusaSlicer (.sl1) as a ChiTu file (.cbddlp).

The slice run places the parts of a resin plate on the area of the screen, and PrusaSlicer slices
them as one model with its supports and its pad (--export-sla). The .sl1 file is a zip of a PNG
for each layer: 8-bit gray, as the slicer shows the plate from above, with no mirror. PrusaSlicer
mirrors X by default, so the printer settings set display_mirror_x = 0. This module writes these
layers as a .cbddlp of version 4 for the ChiTu board of the Phenom, with the exposures and the
lifts of the profile.

The layout is the layout of a file of ChiTuBox 1.4.0 for the Phenom (2026-10-31):

- The header, 112 bytes: the magic 0x12FD0019, the version 4, the bed in mm, the height of the
  plate, the layer height, the exposures, the count of the bottom layers, the pixels of the
  screen, the offsets of the previews, of the table of the layers and of the print parameters,
  the print time, the projector (1: the screen shows the mirror of the slicer), the level of the
  anti-aliasing, the power of the light, and the offset and the size of the slicer block.
- The print parameters, 60 bytes: the lifts and their speeds, the volume, the weight, the cost,
  the time with no light, and the count of the bottom layers.
- The slicer block, 76 bytes: the name of the printer, the time of the file, the rest after a
  retract, the count of the transition layers, and the offset of the parameters of version 4.
- The parameters of version 4, 464 bytes: the retract speed of the bottom layers, the rests, the
  count of the layers, and the offset of the notice of ChiTuBox.
- Two previews: a header of 32 bytes each, and the pixels as runs of 16-bit colors.
- The name of the printer and the notice of ChiTuBox.
- The table of the layers, 36 bytes for each layer: the height, the exposure, the time with no
  light, the offset and the size of the data, and the size of the record before the data (84).
- For each layer: a record of 84 bytes (its 36 bytes of the table, the size of the record and
  the data, the lift, the lift speed, the retract speed, the rest after the retract and the power
  of the light), then the data: a run-length code of 1 bit. In each byte, bit 7 is the color and
  the 7 low bits are the length of the run, at most 125. The runs go on past the end of a row.
- The CRC-32 of all the bytes before it.

The deck reads these files with deck/internal/chitu/sliced.go. The module uses only the Python
standard library.
"""

import struct
import time
import zipfile
import zlib

MAGIC_CBDDLP = 0x12FD0019
VERSION = 4
HEADER_SIZE = 112
PARAMS_SIZE = 60
SLICER_SIZE = 76
PARAMS4_SIZE = 464
PREVIEW_HEADER = 32
PREVIEW_SIZE = (500, 447)  # the size of both previews of ChiTuBox for the Phenom
LAYER_DEF = 36
LAYER_RECORD = 84  # the record before the data of each layer: LAYER_DEF and 48 more bytes
MAX_RUN = 125
MACHINE = b"Peopoly Phenom"
# The notice that ChiTuBox writes in each file. The ChiTu board can check it, so the file has it.
NOTICE = (b"Layout and record format for the ctb and cbddlp file types are the copyrighted programs or codes "
          b"of CBD Technology (China) Inc..The Customer or User shall not in any manner reproduce, distribute, "
          b"modify, decompile, disassemble, decrypt, extract, reverse engineer, lease, assign, or sublicense "
          b"the said programs or codes.")
# Two words of the parameters of version 4 with no known meaning: the values of the file of ChiTuBox.
PARAMS4_UNKNOWN = (45073.0, 99172)


# PNG: 8-bit gray or gray with alpha, with no interlace, as PrusaSlicer writes the layers.

def read_png_gray(data):
    """Returns (width, height, rows): the rows of the 8-bit gray PNG data as bytes."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG file")
    pos, idat, width, height, bpp = 8, [], 0, 0, 1
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


# A pixel of 128 or more is lit (1), else 0.
LIT = bytes(1 if v >= 128 else 0 for v in range(256))


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


def preview_of(lit, width, height):
    """A preview of PREVIEW_SIZE: the plate from above in the middle. lit is a list of rows of
    bytes (width x height), with a byte that is not 0 for each lit pixel."""
    pw, ph = PREVIEW_SIZE
    sh = max(1, round(pw * height / width))
    top = (ph - sh) // 2
    sx, sy = width / pw, height / sh
    on, off, edge = (0xF0, 0xF0, 0xF0), (0x20, 0x20, 0x20), (0x10, 0x10, 0x10)

    def pixel(x, y):
        if y < top or y >= top + sh:
            return edge
        r = lit[min(height - 1, int((y - top + 0.5) * sy))]
        x0 = int(x * sx)
        return on if any(r[x0:max(x0 + 1, int((x + 1) * sx))]) else off

    return pw, ph, encode_preview(pw, ph, pixel)


def sl1_layers(path):
    """The zip of the .sl1 file, its config.ini as a dict, and the names of its layer PNGs in
    order."""
    z = zipfile.ZipFile(path)
    cfg = {}
    for line in z.read("config.ini").decode().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip()
    names = sorted(n for n in z.namelist() if n.endswith(".png") and "/" not in n and not n.startswith("thumbnail"))
    return z, cfg, names


class Layer:
    """The settings of one layer: the exposure, the time with no light, the lift, the speeds and
    the rest after the retract."""

    def __init__(self, resin, i):
        bottom, trans = int(resin["bottom_layers"]), int(resin.get("transition_layers", 0))
        b = i < bottom
        exp, bexp = float(resin["exposure"]), float(resin["bottom_exposure"])
        if b:
            self.exposure = bexp
        elif i < bottom + trans:
            # The transition layers go from the bottom exposure to the exposure in equal steps.
            self.exposure = bexp - (i - bottom + 1) * (bexp - exp) / (trans + 1)
        else:
            self.exposure = exp
        self.light_off = float(resin["bottom_light_off"] if b else resin["light_off"])
        self.lift = float(resin["bottom_lift"] if b else resin["lift"])
        self.lift_speed = float(resin["bottom_lift_speed"] if b else resin["lift_speed"])
        self.retract_speed = float(resin.get("bottom_retract_speed", resin["retract_speed"]) if b else resin["retract_speed"])
        self.rest = float(resin.get("rest_after_retract", 0))
        self.pwm = int(resin.get("bottom_pwm", 255) if b else resin.get("pwm", 255))

    def seconds(self):
        """The time of the layer: the light, the time with no light, the lift up and down, and the
        rest."""
        return (self.exposure + self.light_off + self.lift / self.lift_speed * 60 + self.lift / self.retract_speed * 60
                + self.rest)


def write_cbddlp(sl1, out, resin, now=None):
    """Writes the layers of the .sl1 file as the .cbddlp out. resin has the settings of the
    profile: bed (x, y, z mm), resolution (x, y), mirror, layer_height, exposure,
    bottom_exposure, bottom_layers, transition_layers, light_off, bottom_light_off, lift,
    lift_speed, bottom_lift, bottom_lift_speed, retract_speed, bottom_retract_speed,
    rest_after_retract, pwm, bottom_pwm, density (g/ml) and cost_per_ml. now is the time of the
    file (seconds), for a test. It returns the facts of the plate: layers, height, seconds, ml,
    grams."""
    z, cfg, names = sl1_layers(sl1)
    resx, resy = (int(v) for v in resin["resolution"])
    lh = float(resin["layer_height"])
    n = len(names)
    if n == 0:
        raise ValueError("the .sl1 file has no layers")
    if abs(float(cfg.get("layerHeight", lh)) - lh) > 1e-6:
        raise ValueError("the layer height of the .sl1 file is not the layer height of the profile")
    bottom = int(resin["bottom_layers"])
    mirror = bool(resin["mirror"])
    # lit has a big integer for each row: a byte of 1 for each lit pixel of each fourth layer.
    data, lit = [], [0] * resy
    for i, name in enumerate(names):
        w, h, rows = read_png_gray(z.read(name))
        if (w, h) != (resx, resy):
            raise ValueError(f"layer {i}: {w} x {h} pixels, not {resx} x {resy}")
        # The preview: the lit pixels of each fourth layer, from above.
        if i % 4 == 0:
            for y, r in enumerate(rows):
                if r.strip(b"\x00"):
                    lit[y] |= int.from_bytes(r.translate(LIT), "big")
        data.append(encode_layer(rows, mirror))
    z.close()
    layers = [Layer(resin, i) for i in range(n)]
    seconds = sum(lay.seconds() for lay in layers)
    ml = float(cfg.get("usedMaterial", 0))
    grams = ml * float(resin.get("density", 1.1))
    cost = ml * float(resin.get("cost_per_ml", 0))
    preview = preview_of([v.to_bytes(resx, "big") for v in lit], resx, resy)

    # The offsets of the blocks, in the order of the file of ChiTuBox.
    params_at = HEADER_SIZE
    slicer_at = params_at + PARAMS_SIZE
    params4_at = slicer_at + SLICER_SIZE
    large_at = params4_at + PARAMS4_SIZE
    small_at = large_at + PREVIEW_HEADER + len(preview[2])
    machine_at = small_at + PREVIEW_HEADER + len(preview[2])
    notice_at = machine_at + len(MACHINE)
    table_at = notice_at + len(NOTICE)
    at, data_at = table_at + LAYER_DEF * n, []
    for d in data:
        at += LAYER_RECORD
        data_at.append(at)
        at += len(d)

    bx, by, bz = (float(v) for v in resin["bed"])
    head = struct.pack("<IIfffIIfffffIIIIIIIIIIIIHHIII",
                       MAGIC_CBDDLP, VERSION, bx, by, bz, 0, 0, n * lh, lh,
                       float(resin["exposure"]), float(resin["bottom_exposure"]), float(resin["light_off"]),
                       bottom, resx, resy, large_at, table_at, n, small_at, round(seconds),
                       1 if mirror else 0, params_at, PARAMS_SIZE, 1,
                       int(resin.get("bottom_pwm", 255)), int(resin.get("pwm", 255)), 0, slicer_at, SLICER_SIZE)
    params = struct.pack("<10f5I",
                         float(resin["bottom_lift"]), float(resin["bottom_lift_speed"]),
                         float(resin["lift"]), float(resin["lift_speed"]), float(resin["retract_speed"]),
                         ml, grams, cost, float(resin["bottom_light_off"]), float(resin["light_off"]),
                         bottom, 0, 1, 0, 0)
    minutes = int((time.time() if now is None else now) // 60)
    rest = float(resin.get("rest_after_retract", 0))
    trans = int(resin.get("transition_layers", 0))
    slicer = struct.pack("<7fIIIIIIffIIII", 0, 0, 0, 0, 0, 0, 0, machine_at, len(MACHINE), 0, minutes, 4, 0,
                         rest, 0, trans, params4_at, 0, 0)
    params4 = struct.pack("<fffffffffffIII", float(resin.get("bottom_retract_speed", resin["retract_speed"])), 0, 0, 4.0, 0, 4.0,
                          rest, 0, 0, 0, PARAMS4_UNKNOWN[0], PARAMS4_UNKNOWN[1], trans, n)
    params4 += bytes(72 - len(params4)) + struct.pack("<II", notice_at, len(NOTICE))
    params4 += bytes(PARAMS4_SIZE - len(params4))
    for block, size in ((head, HEADER_SIZE), (params, PARAMS_SIZE), (slicer, SLICER_SIZE), (params4, PARAMS4_SIZE)):
        assert len(block) == size, (len(block), size)

    def preview_block(at):
        w, h, pix = preview
        return struct.pack("<IIII16x", w, h, at + PREVIEW_HEADER, len(pix)) + pix

    def layer_def(i):
        lay = layers[i]
        return struct.pack("<fffII4I", (i + 1) * lh, lay.exposure, lay.light_off, data_at[i], len(data[i]),
                           0, LAYER_RECORD, 0, 0)

    crc = 0
    with open(out, "wb") as f:
        def put(b):
            nonlocal crc
            crc = zlib.crc32(b, crc)
            f.write(b)

        put(head + params + slicer + params4 + preview_block(large_at) + preview_block(small_at) + MACHINE + NOTICE)
        put(b"".join(layer_def(i) for i in range(n)))
        for i, d in enumerate(data):
            lay = layers[i]
            put(layer_def(i) + struct.pack("<I11f", len(d) + LAYER_RECORD, lay.lift, lay.lift_speed, 0, 0,
                                           lay.retract_speed, 0, 0, 0, 0, lay.rest, float(lay.pwm)) + d)
        f.write(struct.pack("<I", crc))
    return {"layers": n, "height": round(n * lh, 3), "seconds": round(seconds), "ml": round(ml, 2), "grams": round(grams, 1)}


# The profiles and the plates.
#
# A resin profile is profiles/<folder>/resin.json in the repository:
#
#   {"printer": {"name": "Peopoly Phenom", "bed": [276.48, 155.52, 400], "resolution": [3840, 2160],
#                "mirror": true, "margin": 4, "prusaslicer": {...}},
#    "profiles": {"<name>": {"name": "...", "layer_height": 0.05, "exposure": 18, ...,
#                            "prusaslicer": {...}}}}
#
# The printer and the profile can set PrusaSlicer settings of the supports, the pad and the
# hollowing (PRUSASLICER_KEYS). The settings of a part in printdeck.yml add to them, for the whole
# plate. The module sets the screen and the bed of PrusaSlicer from the printer.

import json
import math
import os
import re
import shutil
import subprocess

PRUSASLICER_KEYS = {
    "supports_enable", "support_tree_type", "support_head_front_diameter", "support_head_penetration",
    "support_head_width", "support_pillar_diameter", "support_small_pillar_diameter_percent",
    "support_max_bridges_on_pillar", "support_max_bridge_length", "support_max_pillar_link_distance",
    "support_pillar_connection_mode", "support_buildplate_only", "support_enforcers_only",
    "support_points_density_relative", "support_points_minimal_distance", "support_object_elevation",
    "support_critical_angle", "support_base_diameter", "support_base_height",
    "pad_enable", "pad_wall_thickness", "pad_wall_height", "pad_brim_size", "pad_max_merge_distance",
    "pad_wall_slope", "pad_around_object", "pad_around_object_everywhere", "pad_object_gap",
    "pad_object_connector_stride", "pad_object_connector_width", "pad_object_connector_penetration",
    "hollowing_enable", "hollowing_min_thickness", "hollowing_quality", "hollowing_closing_distance",
    "elefant_foot_compensation", "material_correction", "slice_closing_radius",
}
SETTING_VALUE = re.compile(r"^[A-Za-z0-9._%,-]{1,40}$")

# The settings of a profile, with their limits. A value out of its limits stops the run.
RESIN_LIMITS = {
    "layer_height": (0.01, 0.2), "exposure": (0.5, 300), "bottom_exposure": (1, 600), "bottom_layers": (0, 100),
    "transition_layers": (0, 100), "light_off": (0, 60), "bottom_light_off": (0, 60), "lift": (0.5, 30),
    "lift_speed": (1, 600), "bottom_lift": (0.5, 30), "bottom_lift_speed": (1, 600), "retract_speed": (1, 600),
    "bottom_retract_speed": (1, 600), "rest_after_retract": (0, 60), "pwm": (1, 255), "bottom_pwm": (1, 255),
    "density": (0.5, 3), "cost_per_ml": (0, 10),
}
REQUIRED = ("layer_height", "exposure", "bottom_exposure", "bottom_layers", "light_off", "bottom_light_off", "lift",
            "lift_speed", "bottom_lift", "bottom_lift_speed", "retract_speed")


class ProfileError(ValueError):
    pass


def prusaslicer_settings(d, what):
    """The PrusaSlicer settings of d, checked against PRUSASLICER_KEYS."""
    out = {}
    for k, v in (d or {}).items():
        v = str(int(v) if isinstance(v, bool) else v)
        if k not in PRUSASLICER_KEYS:
            raise ProfileError(f"{what}: {k} is not a PrusaSlicer setting of the supports, the pad or the hollowing")
        if not SETTING_VALUE.match(v):
            raise ProfileError(f"{what}: a bad value of {k}: {v!r}")
        out[k] = v
    return out


def load_profile(profile_dir, name):
    """The settings of the profile name of profiles/<folder>/resin.json, for write_cbddlp, with
    "prusaslicer": the PrusaSlicer settings of the printer and of the profile, and "margin": the
    room around each part on the plate in mm."""
    try:
        with open(os.path.join(profile_dir, "resin.json"), encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError) as e:
        raise ProfileError(f"{profile_dir}/resin.json: {e}")
    printer, prof = doc.get("printer") or {}, (doc.get("profiles") or {}).get(name)
    if prof is None:
        raise ProfileError(f"{profile_dir}/resin.json has no profile {name}")
    out = {k: v for k, v in prof.items() if k != "prusaslicer"}
    for k in REQUIRED:
        if k not in out:
            raise ProfileError(f"the profile {name} needs {k}")
    for k, (lo, hi) in RESIN_LIMITS.items():
        if k in out:
            v = float(out[k])
            if not lo <= v <= hi:
                raise ProfileError(f"the profile {name}: {k} is {v}, out of {lo} to {hi}")
    try:
        bed = [float(v) for v in printer["bed"]]
        res = [int(v) for v in printer["resolution"]]
    except (KeyError, TypeError, ValueError):
        raise ProfileError(f"{profile_dir}/resin.json: the printer needs bed [x, y, z] and resolution [x, y]")
    if len(bed) != 3 or len(res) != 2 or min(bed) <= 0 or not (100 <= res[0] <= 20000 and 100 <= res[1] <= 20000):
        raise ProfileError(f"{profile_dir}/resin.json: a bad bed or resolution")
    out.update(bed=bed, resolution=res, mirror=bool(printer.get("mirror", True)), margin=float(printer.get("margin", 4)),
               printer_name=str(printer.get("name", "")))
    ps = prusaslicer_settings(printer.get("prusaslicer"), "the printer")
    ps.update(prusaslicer_settings(prof.get("prusaslicer"), f"the profile {name}"))
    out["prusaslicer"] = ps
    return out


def prusaslicer_ini(resin, settings):
    """The text of the PrusaSlicer config for the profile resin and the settings of the plate."""
    bx, by, bz = resin["bed"]
    rx, ry = resin["resolution"]
    cfg = {
        "printer_technology": "SLA", "display_width": f"{bx:g}", "display_height": f"{by:g}",
        "display_pixels_x": str(rx), "display_pixels_y": str(ry), "display_orientation": "landscape",
        # The layers as the slicer shows the plate: write_cbddlp makes the mirror of the printer.
        "display_mirror_x": "0", "display_mirror_y": "0",
        "bed_shape": f"0x0,{bx:g}x0,{bx:g}x{by:g},0x{by:g}", "max_print_height": f"{bz:g}",
        "layer_height": f"{float(resin['layer_height']):g}", "initial_layer_height": f"{float(resin['layer_height']):g}",
        "exposure_time": f"{float(resin['exposure']):g}", "initial_exposure_time": f"{float(resin['bottom_exposure']):g}",
        "faded_layers": str(int(resin.get("transition_layers", 0)) or 3),
        "material_density": f"{float(resin.get('density', 1.1)):g}",
    }
    cfg.update(resin["prusaslicer"])
    cfg.update(prusaslicer_settings(settings, "the settings of the plate"))
    return "".join(f"{k} = {v}\n" for k, v in cfg.items())


def read_triangles(path):
    """The triangles of an STL file (binary or text), or of all the meshes of a 3MF file."""
    if path.lower().endswith(".3mf"):
        import curaslice
        return [t for item in curaslice.read_3mf(path) for m in item for t in m["triangles"]]
    with open(path, "rb") as f:
        data = f.read()
    n = struct.unpack_from("<I", data, 80)[0] if len(data) >= 84 else 0
    if len(data) == 84 + 50 * n:
        return [(v[3:6], v[6:9], v[9:12]) for v in (struct.unpack_from("<12f", data, 84 + 50 * i) for i in range(n))]
    vs = [tuple(map(float, m)) for m in re.findall(rb"vertex\s+(\S+)\s+(\S+)\s+(\S+)", data)]
    return [tuple(vs[i:i + 3]) for i in range(0, len(vs) - 2, 3)]


def plate_layout(sizes, bed, margin, edge=10.0):
    """The middles of the objects of the sizes [(x, y)] on the bed (x, y) in mm, from the corner of
    the bed: in rows, with the room margin around each, edge from the edges, and the grid in the
    middle. It is the grid of curaslice.layout, which the plan page also uses."""
    bx, by = bed[0], bed[1]
    if len(sizes) == 1:
        w, d = sizes[0]
        if w > bx or d > by:
            raise ProfileError(f"the part ({w:.0f} x {d:.0f} mm) does not fit on the screen ({bx:.0f} x {by:.0f} mm)")
        return [(bx / 2, by / 2)]
    cw = max(w for w, _ in sizes) + 2 * margin
    cd = max(d for _, d in sizes) + 2 * margin
    cols = max(1, int((bx - 2 * edge) // cw))
    rows = math.ceil(len(sizes) / cols)
    if rows * cd > by - 2 * edge:
        fit = cols * max(0, int((by - 2 * edge) // cd))
        raise ProfileError(f"{len(sizes)} objects do not fit on the screen: {fit} fit")
    cols = min(cols, len(sizes))
    return [(bx / 2 + (c - (cols - 1) / 2) * cw, by / 2 + ((rows - 1) / 2 - r) * cd)
            for r, c in (divmod(i, cols) for i in range(len(sizes)))]


def prusaslicer_command(folder):
    """The command of PrusaSlicer: $PRUSASLICER, else the Flathub app with access to folder only."""
    if os.environ.get("PRUSASLICER"):
        return [os.environ["PRUSASLICER"]]
    if not shutil.which("flatpak"):
        raise ProfileError("no PrusaSlicer: set PRUSASLICER, or install com.prusa3d.PrusaSlicer from Flathub")
    return ["flatpak", "run", "--die-with-parent", f"--filesystem={folder}", "--nofilesystem=home",
            "--command=prusa-slicer", "com.prusa3d.PrusaSlicer"]


def slice_plate(profile_dir, name, parts, out, folder, log=print):
    """Slices a resin plate into the .cbddlp out. parts are (label, files, count, settings). The
    files of each part are one object; each copy is an object of its own on the plate. folder is
    an empty folder for the work. Returns the facts of the plate and of the profile."""
    resin = load_profile(profile_dir, name)
    settings = {}
    for _, _, _, s in parts:
        settings.update(s or {})
    objects = []
    for lab, files, count, _ in parts:
        tris = [t for f in files for t in read_triangles(f)]
        if not tris:
            raise ProfileError(f"{lab}: no triangles")
        xs = [p[0] for t in tris for p in t]
        ys = [p[1] for t in tris for p in t]
        zs = [p[2] for t in tris for p in t]
        size = (max(xs) - min(xs), max(ys) - min(ys))
        if max(zs) - min(zs) + float(settings.get("support_object_elevation", resin["prusaslicer"].get("support_object_elevation", 5))) > resin["bed"][2]:
            raise ProfileError(f"{lab} is taller than the printer")
        mid = ((max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2, min(zs))
        objects += [(lab, tris, size, mid)] * int(count)
    places = plate_layout([o[2] for o in objects], resin["bed"], resin["margin"])
    import curaslice
    stls = []
    for i, ((lab, tris, _, mid), (x, y)) in enumerate(zip(objects, places)):
        p = os.path.join(folder, f"object-{i + 1}.stl")
        curaslice.write_stl(p, tris, x - mid[0], y - mid[1], -mid[2])
        stls.append(p)
        log(f"  {lab}: at X {x:.1f}, Y {y:.1f} mm")
    ini = os.path.join(folder, "printer.ini")
    with open(ini, "w") as f:
        f.write(prusaslicer_ini(resin, settings))
    sl1 = os.path.join(folder, "plate.sl1")
    cmd = prusaslicer_command(folder) + ["--export-sla", "--load", ini, "--merge", "--dont-arrange"] + stls + ["--output", sl1]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if r.returncode != 0 or not os.path.exists(sl1):
        raise ProfileError(f"PrusaSlicer failed ({r.returncode}): {(r.stderr or r.stdout).strip()[-1500:]}")
    facts = write_cbddlp(sl1, out, resin)
    facts["layer_height"] = float(resin["layer_height"])
    facts["profile_name"] = str(resin.get("name") or name)
    return facts
