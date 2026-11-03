"""The 3D model of a part from its G-code, for a part whose mesh is not the print. Python standard
library only.

A 3MF file can have meshes that the slicer fills in its own way: an infill mesh, a cutting mesh
or a support mesh. The mesh of such a part shows a solid block, but the print has the infill
pattern. needs_model() finds these parts. gcode_stl() writes each printed line of the G-code as a
box of the line width by the layer height, in the coordinates of the part file.
"""

import math
import re
import struct

import curaslice

# The per-mesh settings that make the print differ from the mesh.
SLICER_MESHES = ("infill_mesh", "cutting_mesh", "support_mesh")
# The line types of CuraEngine that are no part of the model: the skirt and the brim, and the
# prime tower.
SKIP_TYPES = ("SKIRT", "PRIME-TOWER")
# The limit of the boxes of one model: 12 triangles each, 50 bytes a triangle, about 120 MB.
MAX_BOXES = 200_000

MOVE = re.compile(r"([XYZEF])(-?\d+(?:\.\d*)?|-?\.\d+)")


def truthy(v):
    return str(v).strip().lstrip("=").lower() == "true"


def needs_model(path):
    """True for a 3MF file with a mesh that the slicer fills in its own way."""
    if not path.lower().endswith(".3mf"):
        return False
    for item in curaslice.read_3mf(path):
        for m in item:
            if any(truthy(m["settings"].get(k, "")) for k in SLICER_MESHES):
                return True
    return False


def lines_of(gcode):
    """The printed lines of a G-code file: (x0, y0, x1, y1, z, h) in the coordinates of the bed,
    with h the height of the layer. It reads absolute and relative E (M82, M83) and G92."""
    out = []
    x = y = z = e = 0.0
    relative_e = False
    typ = ""
    layer = None
    z_prev = 0.0
    layer_z = None
    h = 0.0
    for raw in open(gcode, encoding="utf-8", errors="replace"):
        line = raw.strip()
        if line.startswith(";"):
            if line.startswith(";LAYER:"):
                layer = int(line[7:])
                if layer_z is not None:
                    z_prev = layer_z
                layer_z = None
            elif line.startswith(";TYPE:"):
                typ = line[6:].strip()
            continue
        code = line.split(";", 1)[0].strip()
        if not code:
            continue
        word = code.split(None, 1)[0].upper()
        if word == "M82":
            relative_e = False
        elif word == "M83":
            relative_e = True
        elif word == "G92":
            for k, v in MOVE.findall(code):
                if k == "E":
                    e = float(v)
        elif word in ("G0", "G1"):
            d = {k: float(v) for k, v in MOVE.findall(code)}
            nx, ny, nz = d.get("X", x), d.get("Y", y), d.get("Z", z)
            extruded = False
            if "E" in d:
                extruded = d["E"] > 0 if relative_e else d["E"] > e + 1e-6
                e = e + d["E"] if relative_e else d["E"]
            if nz != z and layer is not None and layer_z is None:
                layer_z = nz
            if extruded and word == "G1" and layer is not None and typ not in SKIP_TYPES and (nx != x or ny != y):
                if layer_z is None:
                    layer_z = nz
                h = max(layer_z - z_prev, 0.05)
                out.append((x, y, nx, ny, layer_z, h))
            x, y, z = nx, ny, nz
    return out


def joined(lines, tol=1e-3):
    """The lines with each run of collinear lines on one layer, end to start, as one line."""
    out = []
    for ln in lines:
        if out:
            x0, y0, x1, y1, z, h = out[-1]
            a0, b0, a1, b1, zz, hh = ln
            if zz == z and hh == h and abs(a0 - x1) < tol and abs(b0 - y1) < tol:
                ux, uy = x1 - x0, y1 - y0
                vx, vy = a1 - a0, b1 - b0
                if abs(ux * vy - uy * vx) < tol * math.hypot(ux, uy) * math.hypot(vx, vy) and ux * vx + uy * vy > 0:
                    out[-1] = (x0, y0, a1, b1, z, h)
                    continue
        out.append(ln)
    return out


def to_file(p, place):
    """A point of the bed in the coordinates of the part file. place has centre, position,
    shift and scale: CuraEngine puts the shifted meshes at centre + position, and scales them by
    the shrinkage about the middle of the group."""
    cx, cy = place["centre"]
    px, py = place["position"]
    dx, dy, dz = place["shift"]
    sxy, sz = place["scale"]
    x, y, z = p
    return ((x - cx - px) / sxy - dx, (y - cy - py) / sxy - dy, z / sz - dz)


def box(x0, y0, x1, y1, z, h, w):
    """The 12 triangles of a line of width w, from z - h to z, with half a width at each end."""
    dx, dy = x1 - x0, y1 - y0
    n = math.hypot(dx, dy)
    if n == 0:
        return []
    ux, uy = dx / n * w / 2, dy / n * w / 2
    nx, ny = -uy, ux
    a, b = (x0 - ux, y0 - uy), (x1 + ux, y1 + uy)
    q = [(a[0] + nx, a[1] + ny), (b[0] + nx, b[1] + ny), (b[0] - nx, b[1] - ny), (a[0] - nx, a[1] - ny)]
    lo = [(qx, qy, z - h) for qx, qy in q]
    hi = [(qx, qy, z) for qx, qy in q]
    tris = [(lo[0], lo[2], lo[1]), (lo[0], lo[3], lo[2]), (hi[0], hi[1], hi[2]), (hi[0], hi[2], hi[3])]
    for i in range(4):
        j = (i + 1) % 4
        tris += [(lo[i], lo[j], hi[j]), (lo[i], hi[j], hi[i])]
    return tris


def gcode_stl(gcode, out, place, width):
    """Writes the printed lines of gcode as a binary STL in the coordinates of the part file. It
    returns the size (X, Y, Z) in mm."""
    lines = joined(lines_of(gcode))
    if not lines:
        raise SystemExit(f"{gcode}: no printed lines")
    if len(lines) > MAX_BOXES:
        raise SystemExit(f"{gcode}: more than {MAX_BOXES} lines for the 3D model")
    tris = []
    for x0, y0, x1, y1, z, h in lines:
        for t in box(x0, y0, x1, y1, z, h, width):
            tris.append(tuple(to_file(p, place) for p in t))
    curaslice.write_stl(out, tris)
    x0, x1, y0, y1, z0, z1 = curaslice.bounds(tris)
    return x1 - x0, y1 - y0, z1 - z0


def transformed(src, out, m):
    """Writes the STL src again with the 3MF transform m (12 numbers) applied."""
    tris = read_stl(src)
    curaslice.write_stl(out, [tuple(apply(m, p) for p in t) for t in tris])


def apply(m, p):
    x, y, z = p
    return (x * m[0] + y * m[3] + z * m[6] + m[9], x * m[1] + y * m[4] + z * m[7] + m[10], x * m[2] + y * m[5] + z * m[8] + m[11])


def read_stl(path):
    data = open(path, "rb").read()
    n = struct.unpack_from("<I", data, 80)[0]
    out = []
    for i in range(n):
        v = struct.unpack_from("<12f", data, 84 + 50 * i)
        out.append(((v[3], v[4], v[5]), (v[6], v[7], v[8]), (v[9], v[10], v[11])))
    return out
