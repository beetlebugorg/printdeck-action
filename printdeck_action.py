#!/usr/bin/env python3
"""The Printdeck Action: send the plan of the parts of printdeck.yml, or slice the plates of a build.

    printdeck_action.py plan [--config printdeck.yml]
    printdeck_action.py slice --build <id> [--config printdeck.yml]

plan (at a push, a pull request or a tag): reads printdeck.yml, runs its generate step and checks
the files, finds the parts that changed, and sends the plan with the models of the parts. The
owner then chooses the parts and the plates on the plan page.

slice (workflow_dispatch with the build): gets the plates of the build, slices each plate with
curaslice.py and the profiles of the repository, and sends the files.

Each request has an OIDC token of GitHub (the permission id-token: write of the workflow). The
service takes a request only from the repository of the project. See
https://github.com/beetlebugorg/printdeck/blob/main/docs/specs/github-builds.md.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import curaslice  # noqa: E402

API = os.environ.get("PRINTDECK_API", "https://printdeck.beetlebug.org").rstrip("/")
# The bed of each printer of printdeck.yml in mm, and its process. A part can name another printer
# with "process" and "bed" in printdeck.yml.
PRINTERS = {"taz": ("fdm", (280.0, 280.0)), "phenom": ("resin", (276.0, 155.0))}


def log(msg):
    print(msg, flush=True)


def fail(msg):
    print(f"::error::{msg}", flush=True)
    raise SystemExit(1)


# The service.

def token():
    """The OIDC token of GitHub for the service. PRINTDECK_TOKEN takes its place for a test."""
    if os.environ.get("PRINTDECK_TOKEN"):
        return os.environ["PRINTDECK_TOKEN"]
    url, req_token = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL"), os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
    if not url or not req_token:
        fail("No OIDC token: the workflow needs the permission id-token: write.")
    audience = urllib.parse.urlparse(API).hostname
    req = urllib.request.Request(url + "&audience=" + urllib.parse.quote(audience),
                                 headers={"Authorization": "Bearer " + req_token})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)["value"]


def call(method, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(API + "/api/v1" + path, data=data, method=method,
                                 headers={"Authorization": "Bearer " + token(), "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        msg = e.read().decode(errors="replace")
        try:
            msg = json.loads(msg).get("error", msg)
        except ValueError:
            pass
        fail(f"{method} {path}: {e.code}: {msg}")


def upload(grant, name, data, ctype="application/octet-stream"):
    """Uploads a file with a grant of the service (an S3 POST policy)."""
    boundary = uuid.uuid4().hex
    parts = []
    fields = dict(grant.get("fields") or {})
    fields.update({"key": grant["prefix"] + name, "Content-Type": ctype, "Cache-Control": "private, max-age=31536000, immutable"})
    for k, v in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\n'
                 f"Content-Type: {ctype}\r\n\r\n".encode() + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(grant["url"], data=b"".join(parts), method="POST",
                                 headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
    try:
        urllib.request.urlopen(req, timeout=600).read()
    except urllib.error.HTTPError as e:
        fail(f"the upload of {name}: {e.code}: {e.read()[:500].decode(errors='replace')}")


# printdeck.yml.

def load_config(path):
    if not os.path.exists(path):
        fail(f"no {path} in the repository")
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    parts = cfg.get("parts") or []
    if not parts:
        fail(f"{path} has no parts")
    names = set()
    for p in parts:
        if not p.get("name") or not p.get("printer") or not p.get("files"):
            fail(f"each part of {path} needs a name, a printer and files")
        if p["name"].lower() in names:
            fail(f"two parts have the name {p['name']}")
        names.add(p["name"].lower())
        if not p.get("prebuilt") and not p.get("profile"):
            fail(f"{p['name']}: a part needs a profile, or a prebuilt file")
    return cfg


def process_of(part):
    return part.get("process") or PRINTERS.get(str(part["printer"]).lower(), ("fdm",))[0]


def profile_of(part):
    """The folder of the profile of the part, and the profile in it."""
    folder, _, quality = str(part["profile"]).partition("/")
    path = os.path.join("profiles", folder)
    if not quality or not os.path.exists(os.path.join(path, "cura.json")):
        fail(f"{part['name']}: no profile {part['profile']} in profiles/")
    return path, quality


# The generate step.

def comparable(path, data):
    """The content of a model file that matters: a 3MF file without its dates, and other files as
    they are. A STEP file has a date in its header, so it does not count."""
    if path.lower().endswith(".3mf"):
        try:
            z = zipfile.ZipFile(__import__("io").BytesIO(data))
            model = next(n for n in z.namelist() if n.lower().endswith(".model"))
            text = z.read(model).decode("utf-8", "replace")
            return re.sub(r'<metadata name="[^"]*Date[^"]*"[^>]*>[^<]*</metadata>', "", text).encode()
        except (zipfile.BadZipFile, StopIteration):
            return data
    return data


def generate(cfg, files):
    """Runs the generate step of printdeck.yml. With check, the files of the parts must be the
    same as the committed files."""
    gen = cfg.get("generate")
    if not gen or os.environ.get("PRINTDECK_SKIP_GENERATE"):  # the switch is for a test on a computer
        return
    before = {f: open(f, "rb").read() for f in files if os.path.exists(f)}
    if gen.get("install"):
        log(f"generate: {gen['install']}")
        subprocess.run(gen["install"], shell=True, check=True)
    log(f"generate: {gen['run']}")
    subprocess.run(gen["run"], shell=True, check=True)
    if not gen.get("check"):
        return
    changed = [f for f in files if f in before and os.path.exists(f) and comparable(f, open(f, "rb").read()) != comparable(f, before[f])]
    missing = [f for f in files if not os.path.exists(f)]
    if changed or missing:
        fail("The generate step made other files than the committed files. Run it, and commit the new files: " +
             ", ".join(changed + missing))


# The parts that changed.

def git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True, check=False).stdout


def event():
    p = os.environ.get("GITHUB_EVENT_PATH")
    if p and os.path.exists(p):
        return json.load(open(p))
    return {}


def base_commit(ev):
    """The commit to compare with: the base of a pull request, or the commit before a push. A tag
    has none."""
    ref = os.environ.get("GITHUB_REF", "")
    if ref.startswith("refs/tags/"):
        return None
    if "pull_request" in ev:
        return ev["pull_request"]["base"]["sha"]
    before = ev.get("before", "")
    if before and set(before) != {"0"}:
        return before
    return None


def changed_parts(cfg, config_path, base, head):
    """The names of the parts that changed between base and head: their files, their prebuilt
    file, the folder of their profile, or their entry in printdeck.yml. With no base, none."""
    if not base:
        return set()
    diff = set(git("diff", "--name-only", base, head).split())
    old = {}
    try:
        old_cfg = yaml.safe_load(git("show", f"{base}:{config_path}")) or {}
        old = {p.get("name", "").lower(): p for p in old_cfg.get("parts") or []}
    except yaml.YAMLError:
        pass
    out = set()
    for p in cfg["parts"]:
        paths = list(p["files"]) + ([p["prebuilt"]] if p.get("prebuilt") else [])
        prof = "profiles/" + str(p.get("profile", "")).partition("/")[0] + "/"
        if (any(f in diff for f in paths) or (p.get("profile") and any(d.startswith(prof) for d in diff))
                or old.get(p["name"].lower()) != p):
            out.add(p["name"])
    return out


# The models of the plan.

def model_stl(path, out):
    """Writes the model of a part file as an STL for the 3D view: an STL as it is, and the build
    of a 3MF without its support blockers. It returns the size (X, Y, Z) in mm."""
    if path.lower().endswith(".3mf"):
        tris = []
        for item in curaslice.read_3mf(path):
            for m in item:
                if str(m["settings"].get("anti_overhang_mesh", "")).lower() != "true":
                    tris += m["triangles"]
    else:
        tris = read_stl(path)
    if not tris:
        fail(f"{path}: no triangles")
    x0, x1, y0, y1, z0, z1 = curaslice.bounds(tris)
    curaslice.write_stl(out, tris, -(x0 + x1) / 2, -(y0 + y1) / 2, -z0)
    return x1 - x0, y1 - y0, z1 - z0


def read_stl(path):
    import struct
    data = open(path, "rb").read()
    n = struct.unpack_from("<I", data, 80)[0] if len(data) >= 84 else 0
    tris = []
    if len(data) == 84 + 50 * n:
        for i in range(n):
            v = struct.unpack_from("<12f", data, 84 + 50 * i)
            tris.append((v[3:6], v[6:9], v[9:12]))
    else:
        vs = [tuple(map(float, m)) for m in re.findall(rb"vertex\s+(\S+)\s+(\S+)\s+(\S+)", data)]
        tris = [tuple(vs[i:i + 3]) for i in range(0, len(vs) - 2, 3)]
    return tris


def bed_and_margin(part):
    """The bed of the printer of the part (X, Y) and the room around each object, in mm."""
    if "bed" in part:
        bed = tuple(float(x) for x in part["bed"])
    else:
        bed = PRINTERS.get(str(part["printer"]).lower(), ("fdm", (0.0, 0.0)))[1]
    if part.get("prebuilt") or process_of(part) != "fdm":
        return bed, 0.0
    path, quality = profile_of(part)
    glob, _, _, _ = curaslice.build(path, quality, part.get("material"), {k: str(v) for k, v in (part.get("settings") or {}).items()})
    gs = {k: glob.get(k) for k in ("machine_width", "machine_depth", "adhesion_type", "brim_line_count", "skirt_brim_line_width",
                                    "line_width", "skirt_gap", "skirt_line_count")}
    if "bed" not in part:
        bed = (float(gs["machine_width"]), float(gs["machine_depth"]))
    return bed, curaslice.plate_margin(gs)


def plan(config_path):
    cfg = load_config(config_path)
    files = [f for p in cfg["parts"] for f in p["files"]]
    generate(cfg, files)
    ev = event()
    head = os.environ.get("GITHUB_SHA") or git("rev-parse", "HEAD").strip()
    pr = 0
    if "pull_request" in ev:
        head, pr = ev["pull_request"]["head"]["sha"], ev["pull_request"]["number"]
    base = base_commit(ev)
    changed = changed_parts(cfg, config_path, base, head)
    log(f"compared with {base or 'nothing'}: {len(changed)} of {len(cfg['parts'])} parts changed")
    tmp = tempfile.mkdtemp(prefix="printdeck-plan-")
    manifest = {"commit": head, "pr": pr, "parts": []}
    models = {}
    for i, p in enumerate(cfg["parts"]):
        entry = {"name": p["name"], "files": list(p["files"]), "count": int(p.get("count", 1)), "printer": str(p["printer"]),
                 "process": process_of(p), "material": str(p.get("material", "")), "alone": bool(p.get("alone")),
                 "changed": p["name"] in changed, "models": []}
        if p.get("prebuilt"):
            entry["prebuilt"] = p["prebuilt"]
        else:
            path, quality = profile_of(p)
            entry["profile"] = f"{os.path.basename(path)}/{quality}@{curaslice.profile_version(path)}"
        sx = sy = sz = 0.0
        for j, f in enumerate(p["files"]):
            if not os.path.exists(f):
                fail(f"{p['name']}: no file {f}")
            name = f"part{i + 1}-{j + 1}.stl"
            x, y, z = model_stl(f, os.path.join(tmp, name))
            sx, sy, sz = max(sx, x), max(sy, y), max(sz, z)
            entry["models"].append(name)
            models[name] = os.path.join(tmp, name)
        (bx, by), margin = bed_and_margin(p)
        entry.update({"size_x": round(sx, 2), "size_y": round(sy, 2), "size_z": round(sz, 2), "bed_x": bx, "bed_y": by,
                      "margin": round(margin, 2)})
        manifest["parts"].append(entry)
        log(f"  {p['name']}: {sx:.0f} x {sy:.0f} x {sz:.0f} mm{' (changed)' if entry['changed'] else ''}")
    reply = call("POST", "/plans", manifest)
    for name, path in models.items():
        upload(reply["grant"], name, open(path, "rb").read(), "model/stl")
    call("POST", f"/plans/{reply['id']}/done")
    log(f"The plan: {reply['url']}")
    summary(f"### Printdeck plan\n\n{len(changed)} of {len(cfg['parts'])} parts changed. "
            f"Choose the parts and the plates: {reply['url']}\n")


def summary(text):
    p = os.environ.get("GITHUB_STEP_SUMMARY")
    if p:
        with open(p, "a") as f:
            f.write(text)


# The slice.

def slice_build(config_path, build):
    job = call("GET", f"/builds/{build}")
    head = git("rev-parse", "HEAD").strip()
    if head != job["commit"]:
        log(f"checking out the commit of the plan, {job['commit'][:7]}")
        subprocess.run(["git", "fetch", "--quiet", "origin", job["commit"]], check=False)
        subprocess.run(["git", "checkout", "--quiet", "--detach", job["commit"]], check=True)
    try:
        files = do_slice(config_path, job)
    except SystemExit:
        call("POST", f"/builds/{build}/failed", {"error": "The slice run failed: see the run on GitHub."})
        raise
    except Exception as e:  # noqa: BLE001: any failure goes to the build page
        call("POST", f"/builds/{build}/failed", {"error": f"The slice run failed: {e}"})
        raise
    reply = call("POST", f"/builds/{build}/files", {"plates": [f for f, _ in files]})
    for plate, paths in files:
        for name, path in paths.items():
            upload(reply["grant"], name, open(path, "rb").read())
    call("POST", f"/builds/{build}/done")
    log(f"The build: {reply['url']}")
    summary(f"### Printdeck build\n\n{len(files)} plates sliced. Check and approve them: {reply['url']}\n")


def do_slice(config_path, job):
    cfg = load_config(config_path)
    by_name = {p["name"].lower(): p for p in cfg["parts"]}
    tmp = tempfile.mkdtemp(prefix="printdeck-slice-")
    out = []
    for n, plate in enumerate(job["plates"], 1):
        parts = []
        for pp in plate["parts"]:
            p = by_name.get(pp["name"].lower())
            if p is None:
                raise SystemExit(f"plate {n}: printdeck.yml of this commit has no part {pp['name']}")
            parts.append((p, int(pp["count"])))
        first = parts[0][0]
        entry = {"printer": plate["printer"], "process": plate["process"], "parts": plate["parts"]}
        paths = {}
        if first.get("prebuilt"):
            src = first["prebuilt"]
            if not os.path.exists(src):
                raise SystemExit(f"{first['name']}: no prebuilt file {src}")
            name = f"plate-{n}{os.path.splitext(src)[1].lower()}"
            shutil.copyfile(src, os.path.join(tmp, name))
            paths[name] = os.path.join(tmp, name)
            entry["file"] = name
            entry["slicer"] = "prebuilt"
        else:
            path, quality = profile_of(first)
            name = f"plate-{n}.gcode"
            title = " + ".join(f"{p['name']}" + (f" x{c}" if c > 1 else "") for p, c in parts)
            meta = [f";Printdeck title: {title}", f";Printdeck build: {job['id']}", f";Printdeck commit: {job['commit']}"]
            log(f"plate {n}: {title} ({quality})")
            facts = curaslice.slice_plate(path, quality, first.get("material"),
                                          [(p["name"], p["files"], c, p.get("settings") or {}) for p, c in parts],
                                          os.path.join(tmp, name), name=f"{cfg.get('project', 'printdeck')}-{n}", meta=meta)
            paths[name] = os.path.join(tmp, name)
            entry.update({"file": name, "seconds": int(facts.get("seconds", 0)), "grams": facts.get("grams", 0),
                          "layers": int(facts.get("layers", 0)), "layer_height": facts.get("layer_height", 0),
                          "profile": facts["profile"], "slicer": "CuraEngine 5.13.0"})
            log(f"  {entry['seconds'] // 60} min, {entry['grams']} g, {entry['layers']} layers")
        data = open(paths[entry["file"]], "rb").read()
        entry["size"] = len(data)
        entry["sha256"] = hashlib.sha256(data).hexdigest()
        out.append((entry, paths))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["plan", "slice"])
    ap.add_argument("--config", default="printdeck.yml")
    ap.add_argument("--build", default="")
    a = ap.parse_args()
    if a.mode == "plan":
        plan(a.config)
    else:
        if not re.fullmatch(r"[a-z0-9]{1,40}", a.build):
            fail("slice needs the ID of a build (the input build of the workflow)")
        slice_build(a.config, a.build)


if __name__ == "__main__":
    main()
