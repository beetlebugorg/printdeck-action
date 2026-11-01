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
import resinslice  # noqa: E402

API = os.environ.get("PRINTDECK_API", "https://printdeck.beetlebug.org").rstrip("/")
# The bed of each printer of printdeck.yml in mm, and its process. A part can name another printer
# with "process" and "bed" in printdeck.yml.
PRINTERS = {"taz": ("fdm", (280.0, 280.0)), "phenom": ("resin", (276.0, 155.0))}


def log(msg):
    print(msg, flush=True)


class group:
    """A section of the log of the run, which GitHub folds."""

    def __init__(self, title):
        self.title = title

    def __enter__(self):
        print(f"::group::{self.title}", flush=True)

    def __exit__(self, *exc):
        print("::endgroup::", flush=True)


def clock(sec):
    sec = int(sec)
    h, m = sec // 3600, (sec % 3600) // 60
    return f"{h} h {m} min" if h else f"{m} min {sec % 60} s"


def table(header, rows):
    """A table of Markdown for the summary of the run."""
    esc = lambda v: str(v).replace("|", "\\|").replace("\n", " ")
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    out += ["| " + " | ".join(esc(v) for v in r) + " |" for r in rows]
    return "\n".join(out) + "\n"


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

# The fields that a variant of a part can change. settings adds to the settings of the part.
VARIANT_FIELDS = ("files", "profile", "prebuilt", "material", "count", "alone", "printer", "process", "bed")


def expand(parts):
    """The entries of the parts: one for a part with no variants, and one for each variant of a
    part with variants. An entry has the fields of its part, with the fields of its variant in
    their place, and "variant" (the name of the variant, or "")."""
    out = []
    for p in parts or []:
        base = {k: v for k, v in p.items() if k != "variants"}
        variants = p.get("variants")
        if not variants:
            out.append(dict(base, variant=""))
            continue
        for v in variants:
            e = dict(base)
            for k in VARIANT_FIELDS:
                if k in v:
                    e[k] = v[k]
            e["settings"] = dict(p.get("settings") or {}, **(v.get("settings") or {}))
            e["variant"] = str(v.get("name") or "")
            out.append(e)
    return out


def label(e):
    """The name of an entry with its variant: "Pod door (fine)"."""
    return f"{e['name']} ({e['variant']})" if e.get("variant") else e["name"]


def load_config(path):
    if not os.path.exists(path):
        fail(f"no {path} in the repository")
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    if not cfg.get("parts"):
        fail(f"{path} has no parts")
    cfg["entries"] = expand(cfg["parts"])
    names = set()
    for p in cfg["entries"]:
        if not p.get("name") or not p.get("printer") or not p.get("files"):
            fail(f"each part of {path} needs a name, a printer and files (in the part or in each variant)")
        if p.get("variants") is not None and not p.get("variant"):
            fail(f"{p['name']}: each variant needs a name")
        key = (p["name"].lower(), p["variant"].lower())
        if key in names:
            fail(f"two parts have the name {label(p)}")
        names.add(key)
        if not p.get("prebuilt") and not p.get("profile"):
            fail(f"{label(p)}: a part needs a profile, or a prebuilt file")
        if not isinstance(p["files"], list):
            fail(f"{label(p)}: files is a list")
        for f in p["files"]:
            inside(f, f"a file of {label(p)}")
        if p.get("prebuilt"):
            inside(p["prebuilt"], f"the prebuilt file of {label(p)}")
    docs = cfg.get("docs") or []
    if not isinstance(docs, list) or len(docs) > MAX_DOCS:
        fail(f"docs of {path} is a list of at most {MAX_DOCS} PDFs")
    for d in docs:
        if not isinstance(d, dict) or not d.get("name") or not d.get("file"):
            fail(f"each doc of {path} needs a name and a file")
        inside(d["file"], f"the doc {d['name']}")
        if not str(d["file"]).lower().endswith(".pdf"):
            fail(f"the doc {d['name']} is not a PDF: {d['file']}")
    return cfg


# The docs: the PDFs of the project (a guide, a spec), for the project page.
MAX_DOCS = 10
MAX_DOC_SIZE = 50 << 20


def poppler():
    """True when pdftoppm and pdfinfo are on the runner. On GitHub it installs poppler-utils
    when they are not there."""
    if shutil.which("pdftoppm") and shutil.which("pdfinfo"):
        return True
    if os.environ.get("GITHUB_ACTIONS") == "true" and shutil.which("apt-get"):
        log("Install poppler-utils for the pictures of the PDFs")
        r = subprocess.run(["sudo", "apt-get", "install", "-y", "-qq", "poppler-utils"], capture_output=True, text=True)
        if r.returncode != 0:
            log(f"  no poppler-utils: {r.stderr.strip()[-300:]}")
    return bool(shutil.which("pdftoppm") and shutil.which("pdfinfo"))


def pdf_pages(path):
    """The count of the pages of the PDF, or 0 when pdfinfo cannot read it."""
    r = subprocess.run(["pdfinfo", path], capture_output=True, text=True, timeout=60)
    m = re.search(r"^Pages:\s+(\d+)", r.stdout, re.M)
    return int(m.group(1)) if r.returncode == 0 and m else 0


def pdf_thumb(path, out):
    """Writes the first page of the PDF as a PNG 600 pixels wide. Returns False when pdftoppm
    fails."""
    stem = out[:-len(".png")]
    r = subprocess.run(["pdftoppm", "-png", "-f", "1", "-l", "1", "-scale-to-x", "600", "-scale-to-y", "-1",
                        "-singlefile", path, stem], capture_output=True, text=True, timeout=120)
    return r.returncode == 0 and os.path.exists(out)


def plan_docs(cfg, tmp):
    """The entries of the docs for the manifest, and the files to upload by name."""
    docs, files = [], {}
    entries = cfg.get("docs") or []
    if not entries:
        return docs, files
    tools = poppler()
    for i, d in enumerate(entries):
        path = d["file"]
        if not os.path.isfile(path):
            fail(f"the doc {d['name']}: no file {path}")
        size = os.path.getsize(path)
        if size > MAX_DOC_SIZE:
            fail(f"the doc {d['name']}: {size >> 20} MB, more than {MAX_DOC_SIZE >> 20} MB")
        data = open(path, "rb").read()
        if not data.startswith(b"%PDF"):
            fail(f"the doc {d['name']}: {path} is not a PDF")
        entry = {"name": str(d["name"]), "file": path, "pdf": f"doc{i + 1}.pdf", "size": size,
                 "sha256": hashlib.sha256(data).hexdigest()}
        files[entry["pdf"]] = (path, "application/pdf")
        if tools:
            entry["pages"] = pdf_pages(path)
            thumb = os.path.join(tmp, f"doc{i + 1}.png")
            if pdf_thumb(path, thumb):
                entry["thumb"] = f"doc{i + 1}.png"
                files[entry["thumb"]] = (thumb, "image/png")
        docs.append(entry)
        pages = f", {entry['pages']} pages" if entry.get("pages") else ""
        log(f"  {d['name']}: {path}, {size // 1024} KB{pages}" + ("" if entry.get("thumb") else ", no picture"))
    return docs, files


def process_of(part):
    return part.get("process") or PRINTERS.get(str(part.get("printer", "")).lower(), ("fdm",))[0]


def inside(path, what="a file"):
    """The path, after a check that it stays in the repository: a relative path with no "..",
    which also stays in the repository after its symbolic links. A pull request must not make the
    run read a file of the runner, for example /etc/passwd or a link to it."""
    p = str(path)
    if not p or os.path.isabs(p) or "\\" in p or "\0" in p or any(x == ".." for x in p.split("/")):
        fail(f"{what} must be a path in the repository: {p}")
    root = os.path.realpath(os.getcwd())
    real = os.path.realpath(os.path.join(root, p))
    if real != root and not real.startswith(root + os.sep):
        fail(f"{what} leaves the repository: {p}")
    return p


def profile_of(part):
    """The folder of the profile of the part, and the profile in it. An FDM profile is a Cura
    stack (cura.json), and a resin profile is in resin.json."""
    folder, _, quality = str(part["profile"]).partition("/")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", folder) or folder in (".", "..") or not re.fullmatch(r"[A-Za-z0-9._-]+", quality or "x"):
        fail(f"{part['name']}: a profile is <folder>/<profile>: {part['profile']}")
    path = inside(os.path.join("profiles", folder), "the folder of a profile")
    stack = "resin.json" if process_of(part) == "resin" else "cura.json"
    if not quality or not os.path.exists(os.path.join(path, stack)):
        fail(f"{part['name']}: no profile {part['profile']} in profiles/ ({stack})")
    return path, quality


def resin_profile(part):
    """The settings of the resin profile of the part (resinslice.load_profile), after a check of
    the settings of the part."""
    path, quality = profile_of(part)
    try:
        resin = resinslice.load_profile(path, quality)
        resinslice.prusaslicer_settings(part.get("settings"), f"the settings of {label(part)}")
    except resinslice.ProfileError as e:
        fail(f"{label(part)}: {e}")
    return resin


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
    # The commands of the repository (of a pull request too) get no way to ask GitHub for a
    # token: the variables of the OIDC requests and of the token of the run stay out.
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("ACTIONS_ID_TOKEN_") and k not in ("GITHUB_TOKEN", "ACTIONS_RUNTIME_TOKEN", "PRINTDECK_TOKEN")}
    if gen.get("install"):
        log(f"generate: {gen['install']}")
        subprocess.run(gen["install"], shell=True, check=True, env=env)
    log(f"generate: {gen['run']}")
    subprocess.run(gen["run"], shell=True, check=True, env=env)
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
    """The commit to compare with. A tag has none. A pull request: its base. A push to the default
    branch: the commit before the push. A push to another branch (also a branch of a fork, for a
    pull request): the merge base with the default branch, so the plan marks all the changes of
    the branch, and not only the changes of the last push."""
    ref = os.environ.get("GITHUB_REF", "")
    if ref.startswith("refs/tags/"):
        return None
    if "pull_request" in ev:
        return ev["pull_request"]["base"]["sha"]
    default = (ev.get("repository") or {}).get("default_branch", "")
    branch = ref.removeprefix("refs/heads/")
    if default and branch and branch != default:
        mb = git("merge-base", "HEAD", "origin/" + default).strip()
        if re.fullmatch(r"[0-9a-f]{40}", mb):
            return mb
    before = ev.get("before", "")
    if before and set(before) != {"0"}:
        return before
    return None


def changed_parts(cfg, config_path, base, head):
    """The (name, variant) of the entries that changed between base and head: their files, their
    prebuilt file, the folder of their profile, or their entry in printdeck.yml. With no base,
    none."""
    if not base:
        return {}
    diff = set(git("diff", "--name-only", base, head).split())
    old = {}
    try:
        old_cfg = yaml.safe_load(git("show", f"{base}:{config_path}")) or {}
        old = {(p.get("name", "").lower(), p.get("variant", "").lower()): p for p in expand(old_cfg.get("parts"))}
    except yaml.YAMLError:
        pass
    out = {}
    for p in cfg["entries"]:
        paths = list(p["files"]) + ([p["prebuilt"]] if p.get("prebuilt") else [])
        prof = "profiles/" + str(p.get("profile", "")).partition("/")[0] + "/"
        was = old.get((p["name"].lower(), p["variant"].lower()))
        why = []
        if was is None:
            why.append("a new part")
        else:
            if any(f in diff for f in paths):
                why.append("its files")
            if p.get("profile") and any(d.startswith(prof) for d in diff):
                why.append("its profile")
            if was != p:
                why.append("its entry in " + config_path)
        if why:
            out[(p["name"], p["variant"])] = why
    return out


def part_digest(p):
    """The SHA-256 of what makes the plate of a part: its entry in printdeck.yml (with no count),
    the content of its files (comparable), its prebuilt file, and the files of its profile. The
    service keeps a waiting plate when a newer plan has its parts with the same digests."""
    h = hashlib.sha256()
    h.update(json.dumps({k: v for k, v in p.items() if k != "count"}, sort_keys=True, default=str).encode())
    for f in list(p["files"]) + ([p["prebuilt"]] if p.get("prebuilt") else []):
        data = open(f, "rb").read()
        h.update(f"\0{f}\0{len(data)}\0".encode())
        h.update(comparable(f, data))
    if p.get("profile"):
        folder = os.path.join("profiles", str(p["profile"]).partition("/")[0])
        for root, dirs, names in os.walk(folder):
            dirs.sort()
            for n in sorted(names):
                path = os.path.join(root, n)
                h.update(f"\0{path}\0".encode())
                h.update(open(path, "rb").read())
    return h.hexdigest()


# The models of the plan.

MAX_MODEL = 200 << 20


def check_model_size(path):
    """Refuses a model file over 200 MB, also when a 3MF file unpacks to more."""
    if os.path.getsize(path) > MAX_MODEL:
        fail(f"{path} is over 200 MB")
    if path.lower().endswith(".3mf"):
        try:
            total = sum(i.file_size for i in zipfile.ZipFile(path).infolist())
        except zipfile.BadZipFile:
            fail(f"{path} is no 3MF file")
        if total > MAX_MODEL * 2:
            fail(f"{path} unpacks to over 400 MB")


def model_stl(path, out):
    """Writes the model of a part file as an STL for the 3D view: an STL as it is, and the build
    of a 3MF without its support blockers. It returns the size (X, Y, Z) in mm."""
    check_model_size(path)
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


def material_info(part):
    """The name and the color of the material of a part for the pages: "MatterHackers PLA Black"
    and "#1a1a1a" for a roll of the profile folder, or the material of printdeck.yml."""
    mat = str(part.get("material", ""))
    if part.get("prebuilt") or not part.get("profile"):
        return mat, ""
    path, _ = profile_of(part)
    try:
        info = json.load(open(os.path.join(path, "cura.json"), encoding="utf-8")).get("materials", {}).get(mat) or {}
    except (OSError, ValueError):
        info = {}
    name = " ".join(x for x in (info.get("brand"), info.get("material"), info.get("color")) if x)
    return name or mat, info.get("color_code") or ""


def bed_and_margin(part):
    """The bed of the printer of the part (X, Y) and the room around each object, in mm."""
    if "bed" in part:
        bed = tuple(float(x) for x in part["bed"])
    else:
        bed = PRINTERS.get(str(part["printer"]).lower(), ("fdm", (0.0, 0.0)))[1]
    if part.get("prebuilt") or not part.get("profile"):
        return bed, 0.0
    if process_of(part) == "resin":
        resin = resin_profile(part)
        return ("bed" in part and bed) or tuple(resin["bed"][:2]), resin["margin"]
    path, quality = profile_of(part)
    glob, _, _, _ = curaslice.build(path, quality, part.get("material"), {k: str(v) for k, v in (part.get("settings") or {}).items()})
    gs = {k: glob.get(k) for k in ("machine_width", "machine_depth", "adhesion_type", "brim_line_count", "skirt_brim_line_width",
                                    "line_width", "skirt_gap", "skirt_line_count")}
    if "bed" not in part:
        bed = (float(gs["machine_width"]), float(gs["machine_depth"]))
    return bed, curaslice.plate_margin(gs)


def plan(config_path):
    ev = event()
    cfg = load_config(config_path)
    files = sorted({f for p in cfg["entries"] for f in p["files"]})
    with group("Generate the model files"):
        generate(cfg, files)
    head = os.environ.get("GITHUB_SHA") or git("rev-parse", "HEAD").strip()
    pr = 0
    if "pull_request" in ev:
        head, pr = ev["pull_request"]["head"]["sha"], ev["pull_request"]["number"]
    base = base_commit(ev)
    changed = changed_parts(cfg, config_path, base, head)
    log(f"Compared with {base[:7] if base else 'nothing'}: {len(changed)} of {len(cfg['entries'])} parts changed")
    tmp = tempfile.mkdtemp(prefix="printdeck-plan-")
    manifest = {"commit": head, "pr": pr, "parts": []}
    models = {}
    for i, p in enumerate(cfg["entries"]):
        entry = {"name": p["name"], "variant": p["variant"], "files": list(p["files"]), "count": int(p.get("count", 1)), "printer": str(p["printer"]),
                 "process": process_of(p), "material": str(p.get("material", "")), "alone": bool(p.get("alone")),
                 "changed": (p["name"], p["variant"]) in changed, "models": []}
        if p.get("prebuilt"):
            entry["prebuilt"] = p["prebuilt"]
        else:
            path, quality = profile_of(p)
            entry["profile"] = f"{os.path.basename(path)}/{quality}@{curaslice.profile_version(path)}"
        sx = sy = sz = 0.0
        for j, f in enumerate(p["files"]):
            if not os.path.exists(f):
                fail(f"{label(p)}: no file {f}")
            name = f"part{i + 1}-{j + 1}.stl"
            x, y, z = model_stl(f, os.path.join(tmp, name))
            sx, sy, sz = max(sx, x), max(sy, y), max(sz, z)
            entry["models"].append(name)
            models[name] = os.path.join(tmp, name)
        entry["material_name"], entry["material_color"] = material_info(p)
        entry["digest"] = part_digest(p)
        (bx, by), margin = bed_and_margin(p)
        entry.update({"size_x": round(sx, 2), "size_y": round(sy, 2), "size_z": round(sz, 2), "bed_x": bx, "bed_y": by,
                      "margin": round(margin, 2)})
        manifest["parts"].append(entry)
        why = changed.get((p["name"], p["variant"]))
        log(f"  {label(p)}: {sx:.0f} x {sy:.0f} x {sz:.0f} mm" + (f", changed: {', '.join(why)}" if why else ""))
    doc_files = {}
    if cfg.get("docs"):
        with group("Read the docs"):
            manifest["docs"], doc_files = plan_docs(cfg, tmp)
    reply = call("POST", "/plans", manifest)
    with group(f"Upload {len(models)} models"):
        for name, path in models.items():
            upload(reply["grant"], name, open(path, "rb").read(), "model/stl")
            log(f"  {name}: {os.path.getsize(path) // 1024} KB")
    if doc_files:
        with group(f"Upload {len(manifest['docs'])} PDFs"):
            for name, (path, ctype) in doc_files.items():
                upload(reply["grant"], name, open(path, "rb").read(), ctype)
                log(f"  {name}: {os.path.getsize(path) // 1024} KB")
    call("POST", f"/plans/{reply['id']}/done")
    log(f"The plan: {reply['url']}")
    print(f"::notice title=Printdeck plan::{len(changed)} of {len(cfg['entries'])} parts changed. Choose the parts: {reply['url']}",
          flush=True)
    rows = []
    for e in manifest["parts"]:
        why = changed.get((e["name"], e["variant"]))
        rows.append([e["name"], e["variant"] or "-", e.get("printer", ""),
                     e.get("profile", "").partition("@")[0].rpartition("/")[2] or ("ready file" if e.get("prebuilt") else "-"),
                     e.get("material_name") or e.get("material") or "-", f"{e['size_x']:.0f} x {e['size_y']:.0f} x {e['size_z']:.0f} mm",
                     ("yes: " + ", ".join(why)) if why else "no"])
    against = f"`{base[:7]}`" if base else "nothing (a tag, or the first push)"
    summary(f"## Printdeck plan\n\n**[Choose the parts and the plates]({reply['url']})**\n\n"
            f"{len(changed)} of {len(cfg['entries'])} parts changed, compared with {against}.\n\n" +
            table(["Part", "Variant", "Printer", "Profile", "Material", "Size", "Changed"], rows))


def summary(text):
    p = os.environ.get("GITHUB_STEP_SUMMARY")
    if p:
        with open(p, "a") as f:
            f.write(text)


# The slice.

def slice_build(config_path, build):
    job = call("GET", f"/builds/{build}")
    # The build page links to this run on GitHub.
    call("POST", f"/builds/{build}/started", {"run_id": os.environ.get("GITHUB_RUN_ID", "")})
    head = git("rev-parse", "HEAD").strip()
    if head != job["commit"]:
        log(f"checking out the commit of the plan, {job['commit'][:7]}")
        subprocess.run(["git", "fetch", "--quiet", "origin", job["commit"]], check=False)
        if subprocess.run(["git", "cat-file", "-e", job["commit"] + "^{commit}"], check=False).returncode != 0:
            # The commit of a pull request of a fork is in the refs of the pull requests.
            subprocess.run(["git", "fetch", "--quiet", "origin", "+refs/pull/*/head:refs/remotes/origin/pull/*"], check=False)
        subprocess.run(["git", "checkout", "--quiet", "--detach", job["commit"]], check=True)
    log(f"Build {build}: {len(job['plates'])} plates of the commit {job['commit'][:7]}")
    try:
        files = do_slice(config_path, job)
    except SystemExit:
        call("POST", f"/builds/{build}/failed", {"error": "The slice run failed: see the run on GitHub."})
        raise
    except Exception as e:  # noqa: BLE001: any failure goes to the build page
        call("POST", f"/builds/{build}/failed", {"error": f"The slice run failed: {e}"})
        raise
    call("POST", f"/builds/{build}/progress", {"text": "Sending the files of the plates"})
    reply = call("POST", f"/builds/{build}/files", {"plates": [f for f, _ in files]})
    with group("Upload the files"):
        for plate, paths in files:
            for name, path in paths.items():
                upload(reply["grant"], name, open(path, "rb").read())
                log(f"  {name}: {os.path.getsize(path) / 1e6:.1f} MB")
    call("POST", f"/builds/{build}/done")
    log(f"The build: {reply['url']}")
    print(f"::notice title=Printdeck build::{len(files)} plates sliced. Check and approve them: {reply['url']}", flush=True)
    rows = []
    for n, (e, _) in enumerate(files, 1):
        parts = ", ".join(label(dict(pp, variant=pp.get("variant") or "")) + (f" x{pp['count']}" if pp["count"] > 1 else "")
                          for pp in e["parts"])
        rows.append([n, parts, e.get("profile", "").partition("@")[0].rpartition("/")[2] or e.get("slicer", ""),
                     e.get("material_name") or e.get("material") or "-",
                     clock(e["seconds"]) if e.get("seconds") else "-", f"{e['grams']} g" if e.get("grams") else "-",
                     f"{e['layers']} x {e['layer_height']} mm" if e.get("layers") else "-", f"{e['size'] / 1e6:.1f} MB"])
    summary(f"## Printdeck build\n\n**[Check and approve the plates]({reply['url']})**\n\n"
            f"{len(files)} plates of the commit `{job['commit'][:7]}`.\n\n" +
            table(["Plate", "Parts", "Profile", "Material", "Print time", "Filament", "Layers", "File"], rows))


def do_slice(config_path, job):
    cfg = load_config(config_path)
    by_name = {(p["name"].lower(), p["variant"].lower()): p for p in cfg["entries"]}
    tmp = tempfile.mkdtemp(prefix="printdeck-slice-")
    out = []
    total = len(job["plates"])
    for n, plate in enumerate(job["plates"], 1):
        parts = []
        for pp in plate["parts"]:
            p = by_name.get((pp["name"].lower(), str(pp.get("variant") or "").lower()))
            if p is None:
                raise SystemExit(f"plate {n}: printdeck.yml of this commit has no part {label(dict(pp, variant=pp.get('variant') or ''))}")
            parts.append((p, int(pp["count"])))
        first = parts[0][0]
        entry = {"printer": plate["printer"], "process": plate["process"], "parts": plate["parts"],
                 "material_name": plate.get("material_name", ""), "material": plate.get("material", "")}
        title = " + ".join(label(p) + (f" x{c}" if c > 1 else "") for p, c in parts)
        call("POST", f"/builds/{job['id']}/progress", {"text": f"Slicing plate {n} of {total}: {title}"})
        paths = {}
        if first.get("prebuilt"):
            src = first["prebuilt"]
            if not os.path.exists(src):
                raise SystemExit(f"{label(first)}: no prebuilt file {src}")
            name = f"plate-{n}{os.path.splitext(src)[1].lower()}"
            shutil.copyfile(src, os.path.join(tmp, name))
            paths[name] = os.path.join(tmp, name)
            entry["file"] = name
            entry["slicer"] = "prebuilt"
        elif process_of(first) == "resin":
            path, quality = profile_of(first)
            resin_profile(first)
            name = f"plate-{n}.cbddlp"
            work = tempfile.mkdtemp(prefix=f"plate-{n}-", dir=tmp)
            with group(f"Plate {n} of {total}: {title}"):
                log(f"Printer {first['printer']}, profile {first['profile']}, material {entry['material_name'] or first.get('material')}")
                t0 = __import__("time").time()
                try:
                    facts = resinslice.slice_plate(path, quality, [(label(p), p["files"], c, p.get("settings") or {}) for p, c in parts],
                                                   os.path.join(tmp, name), work, log=log)
                except resinslice.ProfileError as e:
                    raise SystemExit(f"plate {n}: {e}")
                took = __import__("time").time() - t0
                paths[name] = os.path.join(tmp, name)
                entry.update({"file": name, "seconds": int(facts["seconds"]), "ml": facts["ml"], "grams": facts["grams"],
                              "layers": int(facts["layers"]), "layer_height": facts["layer_height"],
                              "profile": f"{os.path.basename(path)}/{quality}@{curaslice.profile_version(path)}",
                              "slicer": os.environ.get("PRUSASLICER_NAME", "PrusaSlicer")})
                log(f"Sliced in {took:.1f} s: {clock(entry['seconds'])}, {entry['ml']} ml, {entry['layers']} layers of "
                    f"{entry['layer_height']} mm")
        else:
            path, quality = profile_of(first)
            name = f"plate-{n}.gcode"
            meta = [f";Printdeck title: {title}", f";Printdeck build: {job['id']}", f";Printdeck commit: {job['commit']}"]
            with group(f"Plate {n} of {total}: {title}"):
                log(f"Printer {first['printer']}, profile {first['profile']}, material {entry['material_name'] or first.get('material')}")
                for p, c in parts:
                    log(f"  {label(p)} x{c}: {', '.join(p['files'])}" + (f", settings {p['settings']}" if p.get("settings") else ""))
                t0 = __import__("time").time()
                facts = curaslice.slice_plate(path, quality, first.get("material"),
                                              [(label(p), p["files"], c, p.get("settings") or {}) for p, c in parts],
                                              os.path.join(tmp, name), name=f"{cfg.get('project', 'printdeck')}-{n}", meta=meta)
                took = __import__("time").time() - t0
                for w in curaslice.LAST_WARNINGS:
                    print(f"::warning title=CuraEngine, plate {n}::{w}", flush=True)
                paths[name] = os.path.join(tmp, name)
                entry.update({"file": name, "seconds": int(facts.get("seconds", 0)), "grams": facts.get("grams", 0),
                              "layers": int(facts.get("layers", 0)), "layer_height": facts.get("layer_height", 0),
                              "profile": facts["profile"], "slicer": "CuraEngine 5.13.0"})
                log(f"Sliced in {took:.1f} s: {clock(entry['seconds'])}, {entry['grams']} g, {entry['layers']} layers of "
                    f"{entry['layer_height']} mm, profile {facts['profile']}")
        data = open(paths[entry["file"]], "rb").read()
        entry["size"] = len(data)
        entry["sha256"] = hashlib.sha256(data).hexdigest()
        out.append((entry, paths))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["plan", "slice", "processes"])
    ap.add_argument("--config", default="printdeck.yml")
    ap.add_argument("--build", default="")
    a = ap.parse_args()
    if a.mode == "plan":
        plan(a.config)
    elif a.mode == "processes":
        # For the steps of action.yml: the slicers that the plates of the build need.
        if not re.fullmatch(r"[a-z0-9]{1,40}", a.build):
            fail("processes needs the ID of a build")
        procs = {pl.get("process") or "fdm" for pl in call("GET", f"/builds/{a.build}")["plates"]}
        with open(os.environ.get("GITHUB_OUTPUT", os.devnull), "a") as f:
            for k in ("fdm", "resin"):
                f.write(f"{k}={'true' if k in procs else 'false'}\n")
        log(f"The plates need: {', '.join(sorted(procs)) or 'no slicer'}")
    else:
        if not re.fullmatch(r"[a-z0-9]{1,40}", a.build):
            fail("slice needs the ID of a build (the input build of the workflow)")
        slice_build(a.config, a.build)


if __name__ == "__main__":
    main()
