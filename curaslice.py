#!/usr/bin/env python3
"""Slice parts with CuraEngine and a Cura profile of profiles/, as the Cura app does. Python
standard library only.

    curaslice.py --profile profiles/taz-workhorse-se --quality pla-0.20-standard \\
        [--material roll-4] [--set key=value ...] [--count N] [--name JOB] \\
        --out part.gcode PART.stl [PART2.stl ...]

The Cura app works out the value of each setting before it calls CuraEngine. CuraEngine reads
only the values. This script does the same work, with the rules of Cura 5:

  - The stack of the printer, top first: the settings of the part (--set), the quality changes
    (the profile), the quality, the material (extruder only), the definition changes (the
    machine settings), and the definition with its parents.
  - The extruder stack falls through to the global stack. A setting that is not settable per
    extruder comes from the global stack.
  - A formula ("=..." in a profile, "value" in a definition) gets the values of the names in it
    from the stack that asked. The functions are those of Cura: extruderValue, extruderValues,
    resolveOrValue, defaultExtruderPosition, anyExtruderWithMaterial.
  - The global stack uses the "resolve" formula of a setting when the profile does not set it.
  - The start and end G-code get their {setting} tokens from the first extruder.

It prints JSON: the facts of the slice (time, filament, layers, size). The environment variable
CURAENGINE is the path of CuraEngine 5.13, with CURAENGINE_LIB for its libraries.
"""

import argparse
import ast
import configparser
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET


class Formula:
    def __init__(self, expr):
        self.expr = expr.strip()
        self.code = compile(self.expr, "<setting>", "eval")
        self.names = {n.id for n in ast.walk(ast.parse(self.expr, mode="eval")) if isinstance(n, ast.Name)}

    def __repr__(self):
        return "=" + self.expr


# The definitions.

def load_definition(folder, name):
    """The settings of the definition name and its parents: key -> properties."""
    d = json.load(open(os.path.join(folder, name + ".def.json"), encoding="utf-8"))
    out = load_definition(folder, d["inherits"]) if d.get("inherits") else {}
    out = {k: dict(v) for k, v in out.items()}

    def walk(node):
        for k, v in node.items():
            if not isinstance(v, dict):
                continue
            props = {p: x for p, x in v.items() if p != "children"}
            out.setdefault(k, {}).update(props)
            if "children" in v:
                walk(v["children"])

    walk(d.get("settings", {}))
    for k, v in d.get("overrides", {}).items():
        out.setdefault(k, {}).update(v)
    return out


def typed(value, kind):
    """A value of a profile file as its type. A string with "=" first is a formula."""
    if isinstance(value, str) and value.startswith("="):
        return Formula(value[1:])
    if not isinstance(value, str):
        return value
    v = value.strip()
    try:
        if kind == "float":
            return float(v)
        if kind == "int":
            return int(float(v))
        if kind == "bool":
            return v.lower() in ("true", "1", "yes")
        if kind in ("[int]", "polygon", "polygons", "vec3"):
            return ast.literal_eval(v)
    except (ValueError, SyntaxError):
        return Formula(v)
    return value


def definition_container(defs):
    """The values of a definition: its formula, or its default value."""
    out = {}
    for k, p in defs.items():
        if "value" in p and isinstance(p["value"], str):
            out[k] = Formula(p["value"])
        elif "value" in p:
            out[k] = p["value"]
        elif "default_value" in p:
            out[k] = p["default_value"]
    return out


def read_instance(path, defs):
    """The values of a profile file (.inst.cfg)."""
    cp = configparser.ConfigParser(interpolation=None)
    cp.optionxform = str
    cp.read(path, encoding="utf-8")
    if not cp.has_section("values"):
        return {}
    return {k: typed(v, defs.get(k, {}).get("type")) for k, v in cp.items("values")}


# The names of the settings of a material file (as Cura's XmlMaterialProfile).
MATERIAL_SETTINGS = {
    "print temperature": "default_material_print_temperature",
    "heated bed temperature": "default_material_bed_temperature",
    "standby temperature": "material_standby_temperature",
    "processing temperature graph": "material_flow_temp_graph",
    "print cooling": "cool_fan_speed",
    "retraction amount": "retraction_amount",
    "retraction speed": "retraction_speed",
    "adhesion tendency": "material_adhesion_tendency",
    "surface energy": "material_surface_energy",
    "build volume temperature": "build_volume_temperature",
    "anti ooze retract position": "material_anti_ooze_retracted_position",
    "anti ooze retract speed": "material_anti_ooze_retraction_speed",
    "break preparation position": "material_break_preparation_retracted_position",
    "break preparation speed": "material_break_preparation_speed",
    "break preparation temperature": "material_break_preparation_temperature",
    "break position": "material_break_retracted_position",
    "flush purge speed": "material_flush_purge_speed",
    "flush purge length": "material_flush_purge_length",
    "end of filament purge speed": "material_end_of_filament_purge_speed",
    "end of filament purge length": "material_end_of_filament_purge_length",
    "maximum park duration": "material_maximum_park_duration",
    "no load move factor": "material_no_load_move_factor",
    "break speed": "material_break_speed",
    "break temperature": "material_break_temperature",
}


def read_material(path, defs):
    """The values of a material file, and its metadata. The settings for other machines (the
    <machine> blocks) stay out: they are for Ultimaker printers."""
    ns = {"m": "http://www.ultimaker.com/material", "cura": "http://www.ultimaker.com/cura"}
    root = ET.parse(path).getroot()
    vals = {}
    diameter = root.find("m:properties/m:diameter", ns)
    if diameter is not None:
        vals["material_diameter"] = float(diameter.text)
    settings = root.find("m:settings", ns)
    if settings is not None:
        for el in settings:
            tag = el.tag.split("}")[1]
            key = el.get("key")
            if tag == "setting" and el.tag.startswith("{" + ns["m"]) and key in MATERIAL_SETTINGS:
                k = MATERIAL_SETTINGS[key]
                vals[k] = typed(el.text, defs.get(k, {}).get("type"))
            elif tag == "setting" and el.tag.startswith("{" + ns["cura"]):
                vals[key] = typed(el.text, defs.get(key, {}).get("type"))
    meta = {}
    for k in ("brand", "material", "color"):
        el = root.find("m:metadata/m:name/m:" + k, ns)
        meta[k] = el.text.strip() if el is not None and el.text else ""
    el = root.find("m:metadata/m:GUID", ns)
    meta["guid"] = el.text.strip() if el is not None else ""
    return vals, meta


# The stacks.

class Stack:
    def __init__(self, name, containers, defs, nxt=None, position=None):
        self.name, self.containers, self.defs, self.next, self.position = name, containers, defs, nxt, position
        self.cache, self.resolving = {}, set()
        self.extruders = []

    def raw(self, key):
        for c in self.containers:
            if key in c:
                return c[key]
        return self.next.raw(key) if self.next else None

    def prop(self, key, p, default=None):
        return self.defs.get(key, {}).get(p, default)

    def get(self, key):
        if key in self.cache:
            return self.cache[key]
        if key not in self.defs:
            raise KeyError(key)
        limit = self.limit_extruder(key)
        if self.next is not None and not self.prop(key, "settable_per_extruder", True):
            v = self.next.get(key)
        elif self.next is None and self.should_resolve(key):
            self.resolving.add(key)
            try:
                v = self.evaluate(Formula(self.prop(key, "resolve")))
            finally:
                self.resolving.discard(key)
        elif limit is not None:
            v = limit.get(key)
        else:
            v = self.raw(key)
            if isinstance(v, Formula):
                v = self.evaluate(v)
        v = cast(v, self.prop(key, "type"))
        self.cache[key] = v
        return v

    def limit_extruder(self, key):
        """The extruder of a setting of the global stack with limit_to_extruder (for example
        xy_offset from the extruder of the outer wall), or None. -1 is the default extruder."""
        expr = self.prop(key, "limit_to_extruder")
        if self.next is not None or not expr or not self.prop(key, "settable_per_extruder", True) or not self.extruders:
            return None
        try:
            nr = int(self.evaluate(Formula(str(expr))))
        except (ValueError, TypeError):
            return None
        if nr < 0:
            nr = 0
        return self.extruders[nr] if nr < len(self.extruders) else None

    def raw_value(self, key):
        """The value of the first layer of this stack that has the key, as Cura's getRawProperty:
        with no move to the global stack. extruderValue and extruderValues read this."""
        v = self.raw(key)
        if isinstance(v, Formula):
            v = self.evaluate(v)
        return cast(v, self.prop(key, "type"))

    def should_resolve(self, key):
        """The global stack resolves a setting with a resolve formula, unless the user or the
        quality changes set it."""
        if not self.prop(key, "resolve") or key in self.resolving:
            return False
        return not any(key in c for c in self.containers[:2])

    def evaluate(self, f):
        g = dict(FUNCTIONS)
        g.update(self.functions())
        for n in f.names:
            if n in self.defs and n not in g:
                g[n] = self.get(n)
        try:
            return eval(f.code, g)
        except Exception as e:  # a formula that Cura would also fail on: no value
            raise ValueError(f"{self.name}: {f.expr}: {e}") from e

    def functions(self):
        glob = self if self.next is None else self.next
        exts = glob.extruders

        def ext(pos):
            pos = int(pos)
            return exts[0] if pos < 0 or pos >= len(exts) else exts[pos]

        return {
            "extruderValue": lambda pos, key: ext(pos).raw_value(key),
            "extruderValues": lambda key: [e.raw_value(key) for e in exts] or [glob.get(key)],
            "resolveOrValue": lambda key: glob.get(key),
            "defaultExtruderPosition": lambda: "0",
            "anyExtruderWithMaterial": lambda key: next((e.position for e in exts if e.get(key)), 0),
            "anyExtruderNrWithOrDefault": lambda key: next((e.position for e in exts if e.get(key)), 0),
            "valueFromContainer": lambda key, i: glob.get(key),
            "extruderValueFromContainer": lambda pos, key, i: ext(pos).get(key),
        }


FUNCTIONS = {"math": math, "min": min, "max": max, "abs": abs, "round": round, "sum": sum, "len": len, "int": int,
             "float": float, "str": str, "bool": bool, "any": any, "all": all, "map": map, "list": list, "sorted": sorted,
             "True": True, "False": False, "None": None, "__builtins__": {}}


def cast(v, kind):
    if v is None:
        return v
    try:
        if kind == "float":
            return float(v)
        if kind == "int":
            return int(round(float(v)))
        if kind == "bool":
            return bool(v)
    except (TypeError, ValueError):
        pass
    return v


def engine_text(v):
    """A value as the Cura app sends it to CuraEngine: str() of the Python value."""
    return str(v)


TOKEN = re.compile(r"\{([^{}]+)\}")


def token_text(v):
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def expand(text, stack, extra):
    """The {key} and {key, extruder} tokens of the start and end G-code."""
    def one(m):
        body = m.group(1).strip()
        key, _, nr = body.partition(",")
        key = key.strip()
        st = stack
        if nr.strip():
            st = stack.next.extruders[int(nr.strip())] if stack.next else stack.extruders[int(nr.strip())]
        if key in extra:
            return str(extra[key])
        if key in st.defs:
            return token_text(st.get(key))
        try:
            return token_text(st.evaluate(Formula(body)))
        except Exception:
            return m.group(0)
    return TOKEN.sub(one, text)


def build(profile_dir, quality, material=None, overrides=None):
    """The global stack and the extruder stack of a profile of profile_dir."""
    meta = json.load(open(os.path.join(profile_dir, "cura.json"), encoding="utf-8"))
    ddir = os.path.join(profile_dir, "definitions")
    gdefs = load_definition(ddir, meta["machine"])
    edefs_only = load_definition(ddir, meta["extruder"])
    edefs = {k: dict(v) for k, v in gdefs.items()}
    for k, v in edefs_only.items():
        edefs.setdefault(k, {}).update(v)
    q = meta["profiles"][quality]
    path = lambda rel: os.path.join(profile_dir, rel)
    user = {k: typed(v, gdefs.get(k, {}).get("type")) for k, v in (overrides or {}).items()}
    glob = Stack("global", [
        {k: v for k, v in user.items() if not gdefs.get(k, {}).get("settable_per_extruder", True)},
        read_instance(path(q["quality_changes"]), gdefs),
        {},  # intent
        read_instance(path(q["quality"]), gdefs),
        {},  # material
        {},  # variant
        read_instance(path(meta["definition_changes"]), gdefs),
        definition_container(gdefs),
    ], gdefs)
    mvals, mmeta = {}, {}
    if material:
        m = meta["materials"][material]
        mvals, mmeta = read_material(path(m["file"]), gdefs)
    ext = Stack("extruder 0", [
        {k: v for k, v in user.items() if gdefs.get(k, {}).get("settable_per_extruder", True)},
        read_instance(path(q["quality_changes_extruder"]), gdefs),
        {},
        {},
        mvals,
        {},
        read_instance(path(meta["definition_changes_extruder"]), gdefs),
        definition_container(edefs_only),
    ], edefs, nxt=glob, position=0)
    glob.extruders = [ext]
    return glob, ext, mmeta, meta


def engine_settings(glob, ext, jobname, mmeta):
    """The settings for CuraEngine: all the keys of each stack, and the expanded G-code."""
    extra = {"jobname": jobname, "material_name": mmeta.get("material", ""), "material_brand": mmeta.get("brand", ""),
             "material_type": mmeta.get("material", ""), "initial_extruder_nr": 0}
    gs, es = {}, {}
    for k in glob.defs:
        if glob.defs[k].get("type") == "category":
            continue
        try:
            gs[k] = glob.get(k)
        except (ValueError, KeyError):
            continue
    for k in ext.defs:
        if ext.defs[k].get("type") == "category":
            continue
        try:
            es[k] = ext.get(k)
        except (ValueError, KeyError):
            continue
    for k in ("machine_start_gcode", "machine_end_gcode"):
        gs[k] = expand(str(gs.get(k, "")), ext, extra)
    for k in ("machine_extruder_start_code", "machine_extruder_end_code"):
        if k in es:
            es[k] = expand(str(es[k]), ext, extra)
    return gs, es


def engine_command():
    exe = os.environ.get("CURAENGINE") or "/opt/ultimaker-cura/CuraEngine"
    lib = os.environ.get("CURAENGINE_LIB")
    if lib:
        return ["/lib64/ld-linux-x86-64.so.2", "--library-path", lib, exe]
    return [exe]


NS3MF = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"


def read_3mf(path):
    """The objects of the build of a 3MF file: for each build item, a list of meshes, each with
    its triangles (in mm, with the transforms) and its Cura settings (metadata "cura:<key>",
    for example cura:anti_overhang_mesh for a support blocker)."""
    import zipfile
    z = zipfile.ZipFile(path)
    name = next(n for n in z.namelist() if n.lower().endswith(".model"))
    root = ET.fromstring(z.read(name))
    objects = {}
    for ob in root.iter(NS3MF + "object"):
        settings = {}
        for md in ob.iter(NS3MF + "metadata"):
            key = md.get("name", "")
            if key.startswith("cura:"):
                settings[key[5:]] = (md.text or "").strip()
        mesh = ob.find(NS3MF + "mesh")
        tris = []
        if mesh is not None:
            vs = [(float(v.get("x")), float(v.get("y")), float(v.get("z"))) for v in mesh.iter(NS3MF + "vertex")]
            tris = [(vs[int(t.get("v1"))], vs[int(t.get("v2"))], vs[int(t.get("v3"))]) for t in mesh.iter(NS3MF + "triangle")]
        comps = [(c.get("objectid"), c.get("transform")) for c in ob.iter(NS3MF + "component")]
        objects[ob.get("id")] = (ob.get("name", ""), tris, settings, comps)

    def matrix(t):
        if not t:
            return None
        m = [float(x) for x in t.split()]
        return m

    def apply(m, p):
        if m is None:
            return p
        x, y, z = p
        return (x * m[0] + y * m[3] + z * m[6] + m[9], x * m[1] + y * m[4] + z * m[7] + m[10], x * m[2] + y * m[5] + z * m[8] + m[11])

    def flat(oid, mats, out):
        name, tris, settings, comps = objects[oid]
        if tris:
            pts = []
            for tri in tris:
                q = tri
                for m in mats:
                    q = tuple(apply(m, p) for p in q)
                pts.append(q)
            out.append({"name": name, "triangles": pts, "settings": settings})
        for cid, t in comps:
            flat(cid, [matrix(t)] + mats, out)

    items = []
    for it in root.iter(NS3MF + "item"):
        out = []
        flat(it.get("objectid"), [matrix(it.get("transform"))], out)
        items.append(out)
    return items


def write_stl(path, triangles, dx=0.0, dy=0.0, dz=0.0):
    import struct
    with open(path, "wb") as f:
        f.write(b"\0" * 80 + struct.pack("<I", len(triangles)))
        for a, b, c in triangles:
            f.write(struct.pack("<12fH", 0, 0, 0, a[0] + dx, a[1] + dy, a[2] + dz, b[0] + dx, b[1] + dy, b[2] + dz,
                                c[0] + dx, c[1] + dy, c[2] + dz, 0))


def bounds(triangles):
    xs = [p[0] for t in triangles for p in t]
    ys = [p[1] for t in triangles for p in t]
    zs = [p[2] for t in triangles for p in t]
    return min(xs), max(xs), min(ys), max(ys), min(zs), max(zs)


def objects_of(files, tmp):
    """The objects to slice. A plain object is (STL path, None). A 3MF build item is a group:
    (list of (STL path, Cura settings), its size). The group goes to the middle of the bed and
    onto the bed, with the same offset for each mesh, as the Cura app places it."""
    out = []
    for f in files:
        if not f.lower().endswith(".3mf"):
            out.append((f, None))
            continue
        for i, item in enumerate(read_3mf(f)):
            x0, x1, y0, y1, z0, _ = bounds([t for m in item for t in m["triangles"]])
            dx, dy, dz = -(x0 + x1) / 2, -(y0 + y1) / 2, -z0
            meshes = []
            for j, m in enumerate(item):
                p = os.path.join(tmp, f"{os.path.splitext(os.path.basename(f))[0]}-{i}-{j}.stl")
                write_stl(p, m["triangles"], dx, dy, dz)
                meshes.append((p, m["settings"]))
            out.append((meshes, (x1 - x0, y1 - y0)))
    return out


def stl_size(path):
    """The X and Y size of an STL file (binary or text), in mm."""
    import struct
    data = open(path, "rb").read()
    xs, ys = [], []
    n = struct.unpack_from("<I", data, 80)[0] if len(data) >= 84 else 0
    if len(data) == 84 + 50 * n:
        for i in range(n):
            v = struct.unpack_from("<12f", data, 84 + 50 * i)
            xs += v[3::3][:3]
            ys += v[4::3][:3]
    else:
        for m in re.finditer(rb"vertex\s+(\S+)\s+(\S+)\s+(\S+)", data):
            xs.append(float(m.group(1)))
            ys.append(float(m.group(2)))
    if not xs:
        raise SystemExit(f"{path}: no triangles")
    return max(xs) - min(xs), max(ys) - min(ys)


def layout(meshes, gs):
    """The places of the objects (from objects_of): (x, y) offsets from the middle of the bed.
    The objects go in rows, with room for the brim or the skirt of each, and the grid is at the
    middle."""
    if len(meshes) == 1:
        return [(meshes[0], (0.0, 0.0))]
    margin = plate_margin(gs)
    sizes = [size or stl_size(obj) for obj, size in meshes]
    bw = float(gs.get("machine_width")) - 2 * 10
    bd = float(gs.get("machine_depth")) - 2 * 10
    cw = max(w for w, _ in sizes) + 2 * margin
    cd = max(d for _, d in sizes) + 2 * margin
    cols = max(1, int(bw // cw))
    rows = math.ceil(len(meshes) / cols)
    if rows * cd > bd:
        fit = cols * max(1, int(bd // cd))
        raise SystemExit(f"{len(meshes)} objects do not fit on the bed: {fit} fit")
    cols = min(cols, len(meshes))
    out = []
    for i, m in enumerate(meshes):
        r, c = divmod(i, cols)
        out.append((m, ((c - (cols - 1) / 2) * cw, ((rows - 1) / 2 - r) * cd)))
    return out


def profile_version(profile_dir):
    """The version of a profile folder: the first 12 hex digits of the SHA-256 of its files."""
    import hashlib
    h = hashlib.sha256()
    for root, _, files in sorted(os.walk(profile_dir)):
        for f in sorted(files):
            p = os.path.join(root, f)
            h.update(os.path.relpath(p, profile_dir).encode() + b"\0")
            h.update(open(p, "rb").read())
    return h.hexdigest()[:12]


def slice_files(meshes, gs, es, out, defs_dir, count=1, lines=()):
    """Slices the files meshes (paths, or (path, settings) pairs: the per-mesh settings of a part)
    with the settings gs and es into out."""
    args = engine_command() + ["slice", "-v", "-j", os.path.join(defs_dir, "fdmprinter.def.json")]
    # A setting with no value does not go to CuraEngine, as in the Cura app.
    for k, v in gs.items():
        if v is not None:
            args += ["-s", f"{k}={engine_text(v)}"]
    args += ["-e0"]
    for k, v in es.items():
        if v is not None:
            args += ["-s", f"{k}={engine_text(v)}"]
    # Each object goes to extruder 0. One object goes to the middle of the bed, as the Cura app
    # places one part. More objects go in a grid around the middle. A plain STL file is centered
    # by CuraEngine; a group of a 3MF file is centered already, with its meshes in place.
    tmp = tempfile.mkdtemp(prefix="curaslice-meshes-")
    pairs = [m if isinstance(m, tuple) else (m, {}) for m in meshes]
    objs = []
    for path, extra in pairs:
        for obj in objects_of([path], tmp):
            objs += [(obj, extra)] * count
    places = layout([o for o, _ in objs], gs)
    for ((obj, size), (x, y)), (_, extra) in zip(places, objs):
        mine = [f"{k}={engine_text(v)}" for k, v in extra.items()]
        if size is None:
            args += ["-l", os.path.abspath(obj), "-s", "extruder_nr=0", "-s", "center_object=True",
                     "-s", f"mesh_position_x={x:.3f}", "-s", f"mesh_position_y={y:.3f}"]
            for kv in mine:
                args += ["-s", kv]
            continue
        for path, settings in obj:
            args += ["-l", path, "-s", "extruder_nr=0", "-s", f"mesh_position_x={x:.3f}", "-s", f"mesh_position_y={y:.3f}"]
            for kv in mine:
                args += ["-s", kv]
            for k, v in settings.items():
                args += ["-s", f"{k}={v}"]
    args += ["-o", out]
    # The arguments can pass the limit of a command line: write them to a file of arguments.
    env = dict(os.environ, CURA_ENGINE_SEARCH_PATH=defs_dir)
    p = subprocess.run(args, env=env, capture_output=True, text=True, timeout=int(os.environ.get("SLICE_TIMEOUT", 1800)))
    if p.returncode != 0 or not os.path.exists(out):
        raise SystemExit("CuraEngine failed:\n" + (p.stderr or p.stdout)[-3000:])
    write_header(out, p.stdout + p.stderr, lines)


HEADER_KEYS = (";FLAVOR:", ";TIME:", ";Filament used:", ";Layer height:", ";MINX:", ";MINY:", ";MINZ:", ";MAXX:", ";MAXY:",
               ";MAXZ:", ";TARGET_MACHINE.NAME:")


def split_settings(defs, parts):
    """The settings of the parts of one plate: the settings that Cura allows for each mesh stay
    with their part, and the other settings go to the whole plate. Two parts with another value
    of a plate setting cannot share a plate."""
    plate, per = {}, []
    for name, settings in parts:
        mine = {}
        for k, v in (settings or {}).items():
            if k not in defs:
                raise SystemExit(f"{name}: no Cura setting {k}")
            if defs[k].get("settable_per_mesh"):
                mine[k] = v
            elif k in plate and str(plate[k][1]).lower() != str(v).lower():
                raise SystemExit(f"{name} and {plate[k][0]} need another {k}: {v} and {plate[k][1]}. Put them on separate plates.")
            else:
                plate[k] = (name, v)
        per.append(mine)
    return {k: v for k, (_, v) in plate.items()}, per


def slice_plate(profile_dir, quality, material, parts, out, name="plate", meta=()):
    """Slices one plate. parts: (name, files, count, settings) for each part. It returns the facts
    of the slice and the profile with its version."""
    defs = load_definition(os.path.join(profile_dir, "definitions"),
                           json.load(open(os.path.join(profile_dir, "cura.json"), encoding="utf-8"))["machine"])
    plate, per = split_settings(defs, [(p[0], p[3]) for p in parts])
    glob, ext, mmeta, meta_json = build(profile_dir, quality, material, plate)
    gs, es = engine_settings(glob, ext, name, mmeta)
    version = profile_version(profile_dir)
    profile = f"{os.path.basename(os.path.normpath(profile_dir))}/{quality}@{version}"
    lines = []
    if mmeta:
        lines += [f";PRINTDECK_MATERIAL: {mmeta.get('material', '')}", f";PRINTDECK_MATERIAL_NAME: {mmeta.get('color', '')}"]
        code = meta_json["materials"][material].get("color_code")
        if code:
            lines.append(f";PRINTDECK_COLOR: {code}")
    lines += list(meta) + [f";Printdeck profile: {profile}"]
    # Each file of each part, with its copies, and the per-mesh settings of its part.
    meshes = []
    for (pname, files, count, _), mine in zip(parts, per):
        for f in files:
            meshes += [(f, mine)] * max(1, count)
    slice_files(meshes, gs, es, out, os.path.join(profile_dir, "definitions"), 1, lines)
    f = facts(out)
    if "metres" in f:
        r = float(es.get("material_diameter") or gs.get("material_diameter") or 2.85) / 2
        f["grams"] = round(math.pi * r * r * f["metres"] * 1000 * 1.24 / 1000, 1)
    f["profile"] = profile
    return f


def plate_margin(gs):
    """The room around each object for the brim or the skirt, in mm, as layout uses it."""
    lw = float(gs.get("skirt_brim_line_width") or gs.get("line_width") or 0.5)
    margin = 2.0
    if gs.get("adhesion_type") == "brim":
        margin += float(gs.get("brim_line_count") or 0) * lw
    elif gs.get("adhesion_type") == "skirt":
        margin += float(gs.get("skirt_gap") or 3) + float(gs.get("skirt_line_count") or 1) * lw
    return margin


def write_header(gcode, log, extra=()):
    """CuraEngine writes the header of the G-code before it slices, with no time and no
    filament. After the slice it logs the real header, which the Cura app writes into the file.
    This puts the logged header in place of the first one."""
    m = re.search(r"Gcode header after slicing: (.*?)(?=\n\[\d{4}-|\Z)", log, re.S)
    if not m:
        return
    header = [l for l in m.group(1).strip().splitlines() if l.startswith(";")]
    lines = open(gcode, encoding="utf-8", errors="replace").read().split("\n")
    n = 0
    while n < len(lines) and lines[n].startswith(HEADER_KEYS):
        n += 1
    while n < len(lines) and not lines[n].strip():
        n += 1
    open(gcode, "w", encoding="utf-8").write("\n".join(header + list(extra) + lines[n:]))


def facts(gcode):
    out = {}
    for line in open(gcode, errors="ignore"):
        if not line.startswith(";"):
            if out.get("layers"):
                break
            continue
        for key, name, conv in ((";TIME:", "seconds", int), (";Filament used:", "metres", lambda s: float(s.rstrip("m"))),
                                (";Layer height:", "layer_height", float), (";LAYER_COUNT:", "layers", int)):
            if line.startswith(key):
                out[name] = conv(line[len(key):].strip())
        for ax in ("MINX", "MINY", "MINZ", "MAXX", "MAXY", "MAXZ"):
            if line.startswith(";" + ax + ":"):
                out[ax.lower()] = float(line.split(":", 1)[1])
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("meshes", nargs="*")
    ap.add_argument("--profile", required=True, help="a folder of profiles/")
    ap.add_argument("--quality", required=True, help="a profile of cura.json, for example pla-0.20-standard")
    ap.add_argument("--material", help="a material of cura.json, for example roll-4")
    ap.add_argument("--set", action="append", default=[], help="key=value: a setting of the part")
    ap.add_argument("--count", type=int, default=1, help="the copies of each file on the plate")
    ap.add_argument("--name", default="", help="the job name ({jobname} in the start G-code)")
    ap.add_argument("--out", help="the G-code file")
    ap.add_argument("--dump", help="write the settings for CuraEngine to this JSON file")
    ap.add_argument("--meta", action="append", default=[],
                    help='key=value: a line ";Printdeck key: value" in the header, for example title=Pod door')
    a = ap.parse_args()
    overrides = {}
    for kv in a.set:
        k, v = kv.split("=", 1)
        overrides[k] = v
    glob, ext, mmeta, meta = build(a.profile, a.quality, a.material, overrides)
    name = a.name or (os.path.splitext(os.path.basename(a.meshes[0]))[0] if a.meshes else "part")
    gs, es = engine_settings(glob, ext, name, mmeta)
    if a.dump:
        json.dump({"global": {k: engine_text(v) for k, v in gs.items()}, "extruder": {k: engine_text(v) for k, v in es.items()}},
                  open(a.dump, "w"), indent=1, sort_keys=True)
    if not a.meshes:
        return
    out = a.out or os.path.join(tempfile.mkdtemp(prefix="curaslice-"), name + ".gcode")
    version = profile_version(a.profile)
    # The lines of the Printdeck plugin for Cura, which the deck reads (gcodemeta), and the
    # profile with its version.
    lines = []
    if mmeta:
        lines += [f";PRINTDECK_MATERIAL: {mmeta.get('material', '')}", f";PRINTDECK_MATERIAL_NAME: {mmeta.get('color', '')}"]
        code = meta["materials"][a.material].get("color_code")
        if code:
            lines.append(f";PRINTDECK_COLOR: {code}")
    for kv in a.meta:
        k, _, v = kv.partition("=")
        lines.append(f";Printdeck {k.strip()}: {v.strip()}")
    profile = f"{os.path.basename(os.path.normpath(a.profile))}/{a.quality}@{version}"
    lines.append(f";Printdeck profile: {profile}")
    slice_files(a.meshes, gs, es, out, os.path.join(a.profile, "definitions"), a.count, lines)
    density = 1.24
    f = facts(out)
    if "metres" in f:
        r = float(gs.get("material_diameter") or es.get("material_diameter") or 2.85) / 2
        f["grams"] = round(math.pi * r * r * f["metres"] * 1000 * density / 1000, 1)
    print(json.dumps({"gcode": out, "profile": profile, "material": a.material, **f}, indent=1))


if __name__ == "__main__":
    main()
