"""Slice FDM plates with zslice (https://github.com/beetlebugorg/zslice) in place of CuraEngine,
for a part with "slicer: zslice" in printdeck.yml.

The plate is the same as the plate of CuraEngine: curaslice checks the Cura settings of the parts,
and places the objects with the Cura stack of the profile folder. zslice then slices the plate as
a 3MF file. The profiles of zslice are next to the Cura stack, in the zslice folder of the profile
folder: machine/<one>.toml, filament/<material>.toml and project/<quality>.toml. Make them with
"zslice import cura <profile folder> -o <profile folder>/zslice".

The settings of printdeck.yml are Cura keys. zslice maps them ("-c key=value"), and stops on a key
that it cannot use. The settings that all the parts of the plate have go to the whole plate. The
other settings go to their object in the 3MF file ("cura:<key>").

zslice comes from its npm package on GitHub Packages, at the version and the SHA-256 of ZSLICE.
GitHub Packages needs a token for each download: PRINTDECK_PACKAGES_TOKEN. take_token() removes
it from the environment, so that no program that the Action starts gets it.
"""

import json
import math
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import types
import zipfile
from xml.sax.saxutils import escape, quoteattr

import curaslice
import platepack

# The npm package of zslice: the version, and the SHA-256 of its tarball.
ZSLICE = ("0.5.0-beta", "11e4f4bf5c40b7faa4a652204a24065361bf92ed383e2b76455dcc76e1132215")
REGISTRY = "https://npm.pkg.github.com/"
NAME = f"zslice {ZSLICE[0]}"

# LAST_WARNINGS are the notes of zslice at the last slice, for the log of the Action.
LAST_WARNINGS = []

_token = None


def take_token():
    """Removes the token for GitHub Packages from the environment, and keeps it for fetch."""
    global _token
    _token = os.environ.pop("PRINTDECK_PACKAGES_TOKEN", "") or None


def command():
    """The command that runs zslice: ZSLICE (the path of bin/zslice.mjs of the package), or the
    package in the cache of the runner, which fetch gets first."""
    js = os.environ.get("ZSLICE") or fetch(os.path.expanduser(f"~/.cache/printdeck-zslice-{ZSLICE[0]}"))
    node = shutil.which("node")
    if not node:
        raise SystemExit("zslice needs Node.js 20 or later: no node on the runner")
    v = subprocess.run([node, "--version"], capture_output=True, text=True).stdout.strip()
    m = re.match(r"v(\d+)\.", v)
    if not m or int(m.group(1)) < 20:
        raise SystemExit(f"zslice needs Node.js 20 or later: node is {v or 'unknown'}")
    return [node, js]


def _curl(url, out=None):
    """GET url with the token, through curl. The token goes to curl on its standard input, and never on
    its command line. curl sends it to npm.pkg.github.com only: it does not send the header to
    the host of a redirect (the storage of the tarball)."""
    if not url.startswith(REGISTRY):
        raise SystemExit(f"zslice: the package is not on {REGISTRY}: {url}")
    if not _token:
        raise SystemExit("zslice comes from GitHub Packages, which needs a token: give the job the permission "
                         "packages: read (the Action uses the job token)")
    args = ["curl", "-fsSL", "--proto", "=https", "--max-redirs", "3", "--max-time", "300", "-K", "-", url]
    if out:
        args += ["-o", out]
    cfg = 'header = "Authorization: Bearer ' + _token + '"\n'
    p = subprocess.run(args, input=cfg.encode(), capture_output=True, env={"PATH": os.environ.get("PATH", "")})
    if p.returncode != 0:
        raise SystemExit(f"zslice: the download of {url} failed: {p.stderr.decode(errors='replace').strip()[-300:]}")
    return p.stdout


def fetch(cache):
    """Gets the package into cache, after a check of its SHA-256, and returns the path of its
    command. A package in the cache is checked again."""
    import hashlib
    version, want = ZSLICE
    tgz = os.path.join(cache, f"zslice-{version}.tgz")
    js = os.path.join(cache, "package", "bin", "zslice.mjs")
    if not os.path.isfile(tgz):
        os.makedirs(cache, exist_ok=True)
        meta = json.loads(_curl(REGISTRY + "@beetlebugorg%2fzslice"))
        url = meta["versions"][version]["dist"]["tarball"]
        _curl(url, tgz + ".part")
        os.replace(tgz + ".part", tgz)
    got = hashlib.sha256(open(tgz, "rb").read()).hexdigest()
    if got != want:
        os.remove(tgz)
        raise SystemExit(f"zslice {version}: the package has the SHA-256 {got}. The Action expects {want}")
    if not os.path.isfile(js):
        shutil.rmtree(os.path.join(cache, "package"), ignore_errors=True)
        with tarfile.open(tgz) as t:
            t.extractall(cache, filter="data")
    return js


def profiles(profile_dir, quality, material):
    """The machine, filament and project profiles of zslice for the profile folder."""
    folder = curaslice.within(profile_dir, "zslice", "the zslice folder")
    machines = sorted(f for f in os.listdir(curaslice.within(folder, "machine", "the zslice machine folder")) if f.endswith(".toml"))
    if len(machines) != 1:
        raise curaslice.ProfileError(f"the zslice folder needs one machine profile in machine/. It has {len(machines)}")
    out = [("-m", curaslice.within(folder, "machine/" + machines[0], "the zslice machine profile"))]
    if material:
        p = curaslice.within(folder, f"filament/{material}.toml", "a zslice filament profile")
        if not os.path.isfile(p):
            raise curaslice.ProfileError(f"no zslice filament profile filament/{material}.toml")
        out.append(("-f", p))
    p = curaslice.within(folder, f"project/{quality}.toml", "a zslice project profile")
    if not os.path.isfile(p):
        raise curaslice.ProfileError(f"no zslice project profile project/{quality}.toml")
    out.append(("-p", p))
    return out


def value_text(v):
    """A value of printdeck.yml as a Cura key gets it: true and false in lower case."""
    if isinstance(v, bool):
        return "true" if v else "false"
    return curaslice.one_line(str(v))


def write_3mf(path, objects):
    """Writes a plate: objects is a list of (triangles, settings, (x, y)), with the triangles about
    their reference point and turned, and their place on the bed."""
    res, items = [], []
    for i, (tris, settings, (x, y)) in enumerate(objects, 1):
        verts, index, faces = [], {}, []
        for tri in tris:
            ids = []
            for v in tri:
                k = (round(v[0], 5), round(v[1], 5), round(v[2], 5))
                if k not in index:
                    index[k] = len(verts)
                    verts.append(k)
                ids.append(index[k])
            if len(set(ids)) == 3:
                faces.append(ids)
        meta = "".join(f"<metadata name={quoteattr('cura:' + k)}>{escape(value_text(v))}</metadata>" for k, v in sorted(settings.items()))
        res.append(f'<object id="{i}" type="model">' + (f"<metadatagroup>{meta}</metadatagroup>" if meta else "") +
                   "<mesh><vertices>" + "".join(f'<vertex x="{a}" y="{b}" z="{c}"/>' for a, b, c in verts) +
                   "</vertices><triangles>" + "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in faces) +
                   "</triangles></mesh></object>")
        items.append(f'<item objectid="{i}" transform="1 0 0 0 1 0 0 0 1 {x:.4f} {y:.4f} 0"/>')
    model = ('<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" '
             'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"><resources>' + "".join(res) +
             "</resources><build>" + "".join(items) + "</build></model>\n")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8"?>\n<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/></Types>')
        z.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>')
        z.writestr("3D/3dmodel.model", model)


def header(stats, lines):
    """The lines that go before the G-code of zslice: the Cura header that the deck and
    curaslice.facts read, and the lines of Printdeck."""
    b = stats.get("bounds") or {}
    lo, hi = b.get("min") or [0, 0, 0], b.get("max") or [0, 0, 0]
    out = [f";TIME:{int(round(stats.get('printTimeS', 0)))}",
           f";Filament used: {stats.get('filamentM', 0):.5f}m", f";Layer height: {stats.get('layerHeight', 0)}"]
    out += [f";{k}:{v}" for k, v in zip(("MINX", "MINY", "MINZ"), lo)] + [f";{k}:{v}" for k, v in zip(("MAXX", "MAXY", "MAXZ"), hi)]
    out.append(f";LAYER_COUNT:{int(stats.get('layerCount', 0))}")
    return out + [curaslice.one_line(l) for l in lines]


def slice_plate(profile_dir, quality, material, parts, out, name="plate", meta=()):
    """Slices one plate with zslice, as curaslice.slice_plate does with CuraEngine, and returns
    the same facts."""
    defs = curaslice.load_definition(curaslice.definitions_dir(profile_dir), curaslice.read_meta(profile_dir).get("machine"))
    plate, per = curaslice.split_settings(defs, [(p[0], p[3]) for p in parts])
    glob, ext, mmeta, meta_json = curaslice.build(profile_dir, quality, material, plate)
    gs, es = curaslice.engine_settings(glob, ext, name, mmeta)
    zprofiles = profiles(profile_dir, quality, material)
    version = curaslice.profile_version(profile_dir)
    profile = f"{os.path.basename(os.path.normpath(profile_dir))}/{quality}@{version}"
    lines = []
    if mmeta:
        lines += [f";PRINTDECK_MATERIAL: {mmeta.get('material', '')}", f";PRINTDECK_MATERIAL_NAME: {mmeta.get('color', '')}"]
        code = meta_json["materials"][material].get("color_code")
        if code:
            lines.append(f";PRINTDECK_COLOR: {code}")
    lines += list(meta) + [f";Printdeck profile: {profile}", f";Printdeck slicer: {NAME}"]

    # The per-mesh settings that every part has with the same value go to the whole plate too.
    common = dict(per[0]) if per else {}
    for mine in per[1:]:
        common = {k: v for k, v in common.items() if k in mine and str(mine[k]).lower() == str(v).lower()}
    plate_keys = {**plate, **common}

    tmp = tempfile.mkdtemp(prefix="zslice-")
    objs = []
    for (pname, files, count, _), mine in zip(parts, per):
        own = {k: v for k, v in mine.items() if k not in common}
        for f in files:
            for obj in curaslice.objects_of([f], tmp):
                objs += [(obj, own)] * max(1, count)
    places = curaslice.layout([o for o, _ in objs], gs)
    cx, cy = (0.0, 0.0) if str(gs.get("machine_center_is_zero")).lower() == "true" else \
        (float(gs.get("machine_width")) / 2, float(gs.get("machine_depth")) / 2)
    plate_objs, places_out = [], []
    for ((obj, size), (x, y, turn)), (_, own) in zip(places, objs):
        places_out.append({"shift": curaslice.GROUP_SHIFT.get(id(obj), (0.0, 0.0, 0.0)), "position": (x, y),
                           "turn": platepack.turn_degrees(turn)})
        t = math.radians(platepack.turn_degrees(turn))
        c, s = math.cos(t), math.sin(t)
        for path, settings in obj:
            tris = [tuple((px * c - py * s, px * s + py * c, pz) for px, py, pz in tri) for tri in curaslice.read_stl(path)]
            plate_objs.append((tris, {**own, **settings}, (cx + x, cy + y)))
    plate_3mf = os.path.join(tmp, "plate.3mf")
    write_3mf(plate_3mf, plate_objs)

    raw, stats_path = os.path.join(tmp, "out.gcode"), os.path.join(tmp, "stats.json")
    args = command() + ["slice", plate_3mf]
    for flag, p in zprofiles:
        args += [flag, p]
    for k, v in sorted(plate_keys.items()):
        args += ["-c", f"{k}={value_text(v)}"]
    args += ["-o", raw, "--stats", stats_path]
    env = {"PATH": os.environ.get("PATH", ""), "HOME": tmp}
    p = subprocess.run(args, env=env, capture_output=True, text=True, timeout=int(os.environ.get("SLICE_TIMEOUT", 1800)))
    if p.returncode != 0 or not os.path.exists(raw):
        raise SystemExit("zslice failed:\n" + (p.stderr or p.stdout)[-3000:])
    global LAST_WARNINGS
    LAST_WARNINGS = [l.strip() for l in p.stderr.splitlines() if l.strip()][:20]
    with open(stats_path) as r:
        stats = json.load(r)
    with open(out, "w", encoding="utf-8") as w:
        w.write("\n".join(header(stats, lines)) + "\n")
        with open(raw, encoding="utf-8", errors="replace") as r:
            shutil.copyfileobj(r, w)
    shutil.rmtree(tmp, ignore_errors=True)

    f = curaslice.facts(out)
    f["grams"] = round(float(stats.get("filamentG", 0)), 1)
    f["profile"] = profile
    f["places"] = places_out
    f["centre"] = (cx, cy)
    f["scale"] = (1.0, 1.0)
    f["line_width"] = float(es.get("line_width") or gs.get("line_width") or 0.4)
    f["settings"] = curaslice.plate_settings(gs, es)
    return f


SETTING = re.compile(r"(machine|filament|project)\.[a-z0-9_]+")


def slice_bambu(profile_dir, quality, material, parts, out, name="plate", meta=()):
    """Slices one part for a Bambu Lab printer, with the zslice profiles of the folder and no Cura
    stack. The part is one file at its place in the file. A 3MF file gives each object its
    filament. The settings of the part are zslice settings (project.wall_count)."""
    if len(parts) != 1 or len(parts[0][1]) != 1 or parts[0][2] != 1:
        raise SystemExit("a plate of a Bambu Lab printer is one part with one file, count 1 (alone: true)")
    _, files, _, settings = parts[0]
    args = command() + ["slice", files[0]]
    for flag, p in profiles(profile_dir, quality, material):
        args += [flag, p]
    for k, v in sorted((settings or {}).items()):
        if not SETTING.fullmatch(str(k)):
            raise curaslice.ProfileError(f"the setting {k!r} of a Bambu Lab part is no zslice setting (project.wall_count)")
        args += ["-s", f"{k}={value_text(v)}"]
    tmp = tempfile.mkdtemp(prefix="zslice-")
    raw, stats_path = os.path.join(tmp, "out.gcode"), os.path.join(tmp, "stats.json")
    args += ["-o", raw, "--stats", stats_path]
    env = {"PATH": os.environ.get("PATH", ""), "HOME": tmp}
    p = subprocess.run(args, env=env, capture_output=True, text=True, timeout=int(os.environ.get("SLICE_TIMEOUT", 1800)))
    if p.returncode != 0 or not os.path.exists(raw):
        raise SystemExit("zslice failed:\n" + (p.stderr or p.stdout)[-3000:])
    LAST_WARNINGS[:] = [l.strip() for l in p.stderr.splitlines() if l.strip()][:20]
    with open(stats_path) as r:
        stats = json.load(r)
    profile = f"{os.path.basename(os.path.normpath(profile_dir))}/{quality}@{curaslice.profile_version(profile_dir)}"
    lines = list(meta) + [f";Printdeck profile: {profile}", f";Printdeck slicer: {NAME}"]
    with open(out, "w", encoding="utf-8") as w:
        w.write("\n".join(header(stats, lines)) + "\n")
        with open(raw, encoding="utf-8", errors="replace") as r:
            shutil.copyfileobj(r, w)
    shutil.rmtree(tmp, ignore_errors=True)
    f = curaslice.facts(out)
    f["grams"] = round(float(stats.get("filamentG", 0)), 1)
    f["profile"] = profile
    f["places"] = []
    f["line_width"] = 0.4
    return f


# The slicer of a printer with no Cura stack, for printdeck_action.fdm_slicer.
BAMBU = types.SimpleNamespace(slice_plate=slice_bambu, LAST_WARNINGS=LAST_WARNINGS)
