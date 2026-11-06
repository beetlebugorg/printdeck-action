#!/usr/bin/env python3
"""The Printdeck Action: send the plan of the parts of printdeck.yml, or slice the plates of a build.

    printdeck_action.py plan [--config printdeck.yml]
    printdeck_action.py slice --build <id> [--config printdeck.yml]

plan (at a push, a pull request or a tag): reads printdeck.yml, runs its generate step and checks
the files, finds the parts that changed, slices each part alone, and sends the plan with the
models and the G-code of the parts. The owner then chooses the parts and the plates on the plan
page.

slice (workflow_dispatch with the build): gets the plates of the build, slices each plate with
curaslice.py and the profiles of the repository, and sends the files.

Each request has an OIDC token of GitHub (the permission id-token: write of the workflow). The
service takes a request only from the repository of the project. See
https://github.com/beetlebugorg/printdeck/blob/main/docs/specs/github-builds.md.
"""

import argparse
import hashlib
import json
import math
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

try:
    import yaml
except ImportError:  # the mode processes reads no YAML, and runs before the install of PyYAML
    yaml = None

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import curaslice  # noqa: E402
import platepack  # noqa: E402
import gcodemodel  # noqa: E402
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


def command_data(text):
    """The text for the data of a workflow command, with %, CR and LF escaped as GitHub asks. A
    line break in a message must not start a workflow command of its own."""
    return str(text).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def fail(msg):
    print(f"::error::{command_data(msg)}", flush=True)
    raise SystemExit(1)


# The service.

def token():
    """The OIDC token of GitHub for the service. PRINTDECK_TOKEN takes its place for a test. The
    service takes each token one time only, so each call gets a new token."""
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
VARIANT_FIELDS = ("files", "profile", "prebuilt", "material", "count", "alone", "printer", "process", "bed", "description")

# The fields of a part for the Parts tab of the project page only: a change of them changes no
# plate. description is a few sentences about the part, and group the heading of the part on the
# tab, for example "The case".
TEXT_FIELDS = ("description", "group")
MAX_DESCRIPTION = 1000
MAX_GROUP = 80


def plate_fields(p):
    """The entry of a part with no text fields: what makes its plate."""
    return {k: v for k, v in p.items() if k not in TEXT_FIELDS}


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


# A control character or a line break. A name goes into the G-code and into the log, so a line
# break in it adds a line of G-code or a workflow command. description can have line breaks: it
# goes only to the service.
CONTROL = curaslice.CONTROL


def one_line(v, what):
    """Refuses v when it has a control character or a line break."""
    if v is not None and CONTROL.search(str(v)):
        fail(f"{what} has a control character or a line break: {str(v)!r}")


# The files that a part can have as its prebuilt file, with the extension of the upload. The deck
# prints a .cbddlp or a .ctb on the Phenom, and G-code on an FDM printer.
PREBUILT = {".cbddlp": ".cbddlp", ".ctb": ".ctb", ".gcode": ".gcode", ".gco": ".gco", ".g": ".g"}
MAX_PREBUILT = 200 << 20


def prebuilt_ext(path, what):
    """The extension of the upload of a prebuilt file, from PREBUILT."""
    ext = PREBUILT.get(os.path.splitext(str(path))[1].lower())
    if ext is None:
        fail(f"{what} is not a {', '.join(sorted(PREBUILT))} file: {str(path)!r}")
    return ext


def check_prebuilt(path, what):
    """Checks a prebuilt file before the run reads it: in the repository, with an extension of
    PREBUILT, and at most 200 MB. It returns the extension of the upload."""
    inside(path, what)
    ext = prebuilt_ext(path, what)
    if not os.path.isfile(path):
        fail(f"{what}: no file {path}")
    if os.path.getsize(path) > MAX_PREBUILT:
        fail(f"{what}: {path} is over {MAX_PREBUILT >> 20} MB")
    return ext


def load_config(path):
    inside(path, "the config file")
    if not os.path.exists(path):
        fail(f"no {path} in the repository")
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    if not isinstance(cfg, dict) or not cfg.get("parts"):
        fail(f"{path} has no parts")
    one_line(cfg.get("project"), "project")
    cfg["entries"] = expand(cfg["parts"])
    names = set()
    for p in cfg["entries"]:
        if not p.get("name") or not p.get("printer") or not p.get("files"):
            fail(f"each part of {path} needs a name, a printer and files (in the part or in each variant)")
        if p.get("variants") is not None and not p.get("variant"):
            fail(f"{p['name']}: each variant needs a name")
        for k in ("name", "variant", "printer", "process", "material", "profile", "group"):
            one_line(p.get(k), f"{k} of a part")
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
            prebuilt_ext(p["prebuilt"], f"the prebuilt file of {label(p)}")
        for k, most in (("description", MAX_DESCRIPTION), ("group", MAX_GROUP)):
            if p.get(k) is not None and (not isinstance(p[k], str) or len(p[k].strip()) > most):
                fail(f"{label(p)}: {k} is a text of at most {most} characters")
    docs = cfg.get("docs") or []
    if not isinstance(docs, list) or len(docs) > MAX_DOCS:
        fail(f"docs of {path} is a list of at most {MAX_DOCS} PDFs")
    for d in docs:
        if not isinstance(d, dict) or not d.get("name") or not d.get("file"):
            fail(f"each doc of {path} needs a name and a file")
        one_line(d["name"], "the name of a doc")
        inside(d["file"], f"the doc {d['name']}")
        if not str(d["file"]).lower().endswith(".pdf"):
            fail(f"the doc {d['name']} is not a PDF: {d['file']}")
    cover = cfg.get("cover")
    if cover is not None:
        if not isinstance(cover, str) or not cover.lower().endswith((".png", ".jpg", ".jpeg")):
            fail(f"the cover of {path} is a PNG or JPEG file")
        inside(cover, "the cover")
    check_assemblies(cfg, path)
    check_materials(cfg, path)
    return cfg


# The assemblies: models of the parts in their places, for the 3D view and the exploded view on
# the Parts tab of the project page. They change no plate and no digest.
MAX_ASSEMBLIES = 10
MAX_ITEMS = 60
MAX_NAME = 120
COLOR = re.compile(r"#[0-9a-fA-F]{6}")


def is_text(v, most):
    return isinstance(v, str) and 0 < len(v.strip()) <= most


# The materials: a shop link for each material of the parts, so that people can buy the same
# filament or resin. The project pages show a Reorder link at the material.
MAX_URL = 500


def check_materials(cfg, path):
    mats = cfg.get("materials")
    if mats is None:
        return
    if not isinstance(mats, dict) or len(mats) > 50:
        fail(f"materials of {path} is a map of at most 50 materials, by the name in material of the parts")
    for name, m in mats.items():
        one_line(name, f"the material {name!r}")
        if not isinstance(m, dict) or set(m) - {"buy"}:
            fail(f"the material {name!r} of {path} has only buy: the address of a shop page")
        buy = m.get("buy")
        if isinstance(buy, dict):
            # Labels for the links, for example the diameters: the first is the material of the
            # project.
            if not 1 <= len(buy) <= 4:
                fail(f"buy of the material {name!r} has 1 to 4 links")
            for label, u in buy.items():
                one_line(label, f"a label of the shop links of {name!r}")
                if not str(label).strip() or len(str(label)) > 20:
                    fail(f"a label of the shop links of {name!r} has 1 to 20 characters: {label!r}")
                check_shop_url(u, f"the shop link {label!r} of {name!r}")
        elif buy is not None:
            check_shop_url(buy, f"the shop link of {name!r}")


def check_shop_url(u, what):
    """Refuses u when it is not an https address with a host."""
    u = str(u)
    m = re.match(r"^https://([A-Za-z0-9.-]+)(?::[0-9]+)?(?:[/?#][^\s]*)?$", u)
    if len(u) > MAX_URL or not m or "." not in m.group(1) or CONTROL.search(u):
        fail(f"{what} is not an https address: {u!r}")


def material_links(cfg, part):
    """The shop links of the material of the part: [{"label", "url"}], the material of the project
    first. A single link has no label."""
    m = (cfg.get("materials") or {}).get(str(part.get("material", ""))) or {}
    buy = m.get("buy")
    if isinstance(buy, dict):
        return [{"label": str(k).strip(), "url": str(v)} for k, v in buy.items()]
    return [{"label": "", "url": str(buy)}] if buy else []


def material_url(cfg, part):
    """The shop link of the material of the project for the part, or ""."""
    links = material_links(cfg, part)
    return links[0]["url"] if links else ""


def check_assemblies(cfg, path):
    """Checks the assemblies of printdeck.yml. It sets "entry" of each item with a part to the
    entry of that part."""
    asms = cfg.get("assemblies")
    if asms is None:
        return
    if not isinstance(asms, list) or len(asms) > MAX_ASSEMBLIES:
        fail(f"assemblies of {path} is a list of at most {MAX_ASSEMBLIES} assemblies")
    by_label = {label(e).lower(): e for e in cfg["entries"]}
    names = set()
    for a in asms:
        if not isinstance(a, dict) or not is_text(a.get("name"), MAX_NAME):
            fail(f"each assembly of {path} needs a name of at most {MAX_NAME} characters")
        one_line(a["name"], "the name of an assembly")
        name = a["name"].strip()
        if name.lower() in names:
            fail(f"two assemblies have the name {name}")
        names.add(name.lower())
        if a.get("description") is not None and not (isinstance(a["description"], str) and len(a["description"].strip()) <= MAX_DESCRIPTION):
            fail(f"the assembly {name}: description is a text of at most {MAX_DESCRIPTION} characters")
        items = a.get("items")
        if not isinstance(items, list) or not 1 <= len(items) <= MAX_ITEMS:
            fail(f"the assembly {name}: items is a list of 1 to {MAX_ITEMS} items")
        for j, it in enumerate(items):
            where = f"the assembly {name}, item {j + 1}"
            if not isinstance(it, dict):
                fail(f"{where}: an item has a file, and a part or a name")
            one_line(it.get("name"), f"{where}: name")
            one_line(it.get("part"), f"{where}: part")
            if it.get("part") is not None:
                e = by_label.get(str(it["part"]).strip().lower())
                if e is None:
                    fail(f"{where}: no part {it['part']} in parts (use \"Name\" or \"Name (variant)\")")
                it["entry"] = e
            elif not is_text(it.get("name"), MAX_NAME):
                fail(f"{where}: an item with no part needs a name of at most {MAX_NAME} characters")
            if it.get("name") is not None and not is_text(it["name"], MAX_NAME):
                fail(f"{where}: name is a text of at most {MAX_NAME} characters")
            if not it.get("file"):
                fail(f"{where}: an item needs a file")
            inside(it["file"], f"the file of {where}")
            if it.get("color") is not None and not (isinstance(it["color"], str) and COLOR.fullmatch(it["color"])):
                fail(f"{where}: color is \"#rrggbb\": {it['color']}")
            if it.get("transform") is not None:
                v = it["transform"]
                if (not isinstance(v, list) or len(v) != 12 or any(isinstance(x, bool) or not isinstance(x, (int, float)) for x in v)
                        or not all(math.isfinite(x) and abs(x) < 1e6 for x in v)):
                    fail(f"{where}: transform is 12 numbers, as in a 3MF file: {v}")
                if it.get("part") is None:
                    fail(f"{where}: transform is for an item of a part")
            if it.get("explode") is not None:
                v = it["explode"]
                if (not isinstance(v, list) or len(v) != 3 or any(isinstance(x, bool) or not isinstance(x, (int, float)) for x in v)
                        or not any(v)):
                    fail(f"{where}: explode is 3 numbers, not all 0: {v}")


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


# The cover: a picture of the project (a render), for the project lists of the service.
MAX_COVER_SIZE = 5 << 20


def plan_cover(cfg):
    """The upload name of the cover, and the file and its content type. Returns ("", None)
    when printdeck.yml has no cover."""
    path = cfg.get("cover")
    if not path:
        return "", None
    if not os.path.isfile(path):
        fail(f"the cover: no file {path}")
    size = os.path.getsize(path)
    if size > MAX_COVER_SIZE:
        fail(f"the cover: {size >> 20} MB, more than {MAX_COVER_SIZE >> 20} MB")
    head = open(path, "rb").read(8)
    if head.startswith(b"\x89PNG"):
        name, ctype = "cover.png", "image/png"
    elif head.startswith(b"\xff\xd8\xff"):
        name, ctype = "cover.jpg", "image/jpeg"
    else:
        fail(f"the cover: {path} is not a PNG or JPEG file")
    log(f"  the cover: {path}, {size // 1024} KB")
    return name, (path, ctype)


def process_of(part):
    return part.get("process") or PRINTERS.get(str(part.get("printer", "")).lower(), ("fdm",))[0]


def inside(path, what="a file"):
    """The path, after a check that it stays in the repository: a relative path with no "..",
    which also stays in the repository after its symbolic links. A pull request must not make the
    run read a file of the runner, for example /etc/passwd or a link to it. A path in .git is
    also refused: .git has the credentials of the checkout."""
    p = str(path)
    if not p or os.path.isabs(p) or "\\" in p or CONTROL.search(p) or any(x == ".." for x in p.split("/")):
        fail(f"{what} must be a path in the repository: {p!r}")
    if any(x.lower() == ".git" for x in p.split("/")):
        fail(f"{what} is in .git: {p!r}")
    root = os.path.realpath(os.getcwd())
    real = os.path.realpath(os.path.join(root, p))
    if real != root and not real.startswith(root + os.sep):
        fail(f"{what} leaves the repository: {p!r}")
    if any(x.lower() == ".git" for x in os.path.relpath(real, root).split(os.sep)):
        fail(f"{what} is a link into .git: {p!r}")
    return p


def check_paths(cfg, config_path):
    """Checks each path of printdeck.yml again with inside. The generate step can put a symbolic
    link in place of a file after load_config checked it."""
    inside(config_path, "the config file")
    for p in cfg["entries"]:
        for f in p["files"]:
            inside(f, f"a file of {label(p)}")
        if p.get("prebuilt"):
            inside(p["prebuilt"], f"the prebuilt file of {label(p)}")
        if p.get("profile"):
            profile_of(p)
    for d in cfg.get("docs") or []:
        inside(d["file"], f"the doc {d['name']}")
    if cfg.get("cover"):
        inside(cfg["cover"], "the cover")
    for a in cfg.get("assemblies") or []:
        for it in a["items"]:
            inside(it["file"], f"a file of the assembly {a['name']}")


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
    try:
        curaslice.check_tree(path)
    except curaslice.ProfileError as e:
        fail(f"{part['name']}: the profile {part['profile']}: {e}")
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
            if plate_fields(was) != plate_fields(p):
                why.append("its entry in " + config_path)
        if why:
            out[(p["name"], p["variant"])] = why
    return out


def part_digest(p):
    """The SHA-256 of what makes the plate of a part: its entry in printdeck.yml (with no count),
    the content of its files (comparable), its prebuilt file, and the files of its profile. The
    service keeps a waiting plate when a newer plan has its parts with the same digests."""
    h = hashlib.sha256()
    h.update(json.dumps({k: v for k, v in plate_fields(p).items() if k != "count"}, sort_keys=True, default=str).encode())
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


def model_stl(path, out, place=False):
    """Writes the model of a part file as an STL for the 3D view: an STL as it is, and the build
    of a 3MF without its support blockers. It returns the size (X, Y, Z) in mm. The model goes to
    the center of the bed, on Z 0. With place, the model keeps its coordinates (an item of an
    assembly)."""
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
    if place:
        curaslice.write_stl(out, tris)
    else:
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
    try:
        glob, _, _, _ = curaslice.build(path, quality, part.get("material"), {k: str(v) for k, v in (part.get("settings") or {}).items()})
    except curaslice.ProfileError as e:
        fail(f"{label(part)}: {e}")
    gs = {k: glob.get(k) for k in ("machine_width", "machine_depth", "adhesion_type", "brim_line_count", "skirt_brim_line_width",
                                    "line_width", "skirt_gap", "skirt_line_count")}
    if "bed" not in part:
        bed = (float(gs["machine_width"]), float(gs["machine_depth"]))
    return bed, curaslice.plate_margin(gs)


def plan_assemblies(cfg, tmp, models):
    """The entries of the assemblies for the manifest. It writes the model of each item to tmp
    as asm{i}-{j}.stl, and adds it to models."""
    out = []
    for i, a in enumerate(cfg.get("assemblies") or []):
        entry = {"name": a["name"].strip(), "items": []}
        if (a.get("description") or "").strip():
            entry["description"] = a["description"].strip()
        for j, it in enumerate(a["items"]):
            f = it["file"]
            if not os.path.isfile(f):
                fail(f"the assembly {entry['name']}, item {j + 1}: no file {f}")
            name = f"asm{i + 1}-{j + 1}.stl"
            e = it.get("entry")
            if e is not None and e.get("gcode_model") and it.get("transform"):
                # The G-code model is in the coordinates of the part file. The transform puts it
                # in its place in the assembly.
                gcodemodel.transformed(e["gcode_model"], os.path.join(tmp, name), [float(x) for x in it["transform"]])
            else:
                model_stl(f, os.path.join(tmp, name), place=True)
            models[name] = os.path.join(tmp, name)
            item = {"name": (it.get("name") or "").strip() or label(e)}
            if e is not None:
                item["part"] = label(e)
            item["model"] = name
            color = it.get("color") or (material_info(e)[1] if e is not None else "")
            if color:
                item["color"] = color.lower()
            if it.get("explode") is not None:
                item["explode"] = [float(x) for x in it["explode"]]
            entry["items"].append(item)
        out.append(entry)
        log(f"  {entry['name']}: {len(entry['items'])} items")
    return out


# The slice of each part in the plan run.

# The folder of the slices of the parts, named by the digest of the part. action.yml keeps it in
# the cache of GitHub Actions, so a part that did not change is not sliced again.
PART_CACHE = os.environ.get("PRINTDECK_PART_CACHE") or os.path.expanduser("~/.cache/printdeck-parts")
MAX_SLICE_ERROR = 1000
FACT_KEYS = ("seconds", "grams", "ml", "layers", "layer_height", "slicer", "profile", "places", "centre", "scale", "line_width")


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "part"


def slice_part(cfg, p, i, commit, tmp):
    """Slices the part p alone, with a count of 1, or gets its slice from PART_CACHE. It returns
    (the name of the file for the upload, its path, the facts). A part with a prebuilt file has no
    slice: it returns None."""
    if p.get("prebuilt"):
        return None
    resin = process_of(p) == "resin"
    ext = ".cbddlp" if resin else ".gcode"
    name = f"part{i + 1}{ext}"
    digest = part_digest(p)
    cached, meta = os.path.join(PART_CACHE, digest + ext), os.path.join(PART_CACHE, digest + ".json")
    if os.path.isfile(cached) and os.path.isfile(meta):
        try:
            facts = json.load(open(meta))
            out = os.path.join(tmp, name)
            shutil.copyfile(cached, out)
            log(f"  {label(p)}: the slice of the cache ({clock(int(facts.get('seconds', 0)))})")
            return name, out, facts
        except (OSError, ValueError):
            pass
    path, quality = profile_of(p)
    out = os.path.join(tmp, name)
    t0 = __import__("time").time()
    for f in p["files"]:
        check_model_size(f)
    if resin:
        resin_profile(p)
        work = tempfile.mkdtemp(prefix=f"part-{i + 1}-", dir=tmp)
        facts = resinslice.slice_plate(path, quality, [(label(p), p["files"], 1, p.get("settings") or {})], out, work, log=log)
        facts = {"seconds": int(facts["seconds"]), "ml": facts["ml"], "grams": facts["grams"], "layers": int(facts["layers"]),
                 "layer_height": facts["layer_height"], "slicer": os.environ.get("PRUSASLICER_NAME", "PrusaSlicer"),
                 "profile": f"{os.path.basename(path)}/{quality}@{curaslice.profile_version(path)}"}
    else:
        meta_lines = [f";Printdeck title: {label(p)}", f";Printdeck commit: {commit}"]
        facts = curaslice.slice_plate(path, quality, p.get("material"), [(label(p), p["files"], 1, p.get("settings") or {})], out,
                                      name=f"{cfg.get('project', 'printdeck')}-{slug(label(p))}", meta=meta_lines)
        for w in curaslice.LAST_WARNINGS:
            print(f"::warning title=CuraEngine, {command_data(label(p))}::{command_data(w)}", flush=True)
        facts = {k: facts[k] for k in FACT_KEYS if k in facts}
        facts.update({"seconds": int(facts.get("seconds", 0)), "layers": int(facts.get("layers", 0)), "slicer": "CuraEngine 5.13.0"})
    log(f"  {label(p)}: sliced in {__import__('time').time() - t0:.1f} s ({clock(int(facts.get('seconds', 0)))})")
    try:
        os.makedirs(PART_CACHE, exist_ok=True)
        shutil.copyfile(out, cached)
        json.dump(facts, open(meta, "w"))
    except OSError as e:
        log(f"  the cache of the slices: {e}")
    return name, out, facts


def prune_cache(digests):
    """Removes the slices of PART_CACHE that no part of this plan has."""
    if not os.path.isdir(PART_CACHE):
        return
    for n in os.listdir(PART_CACHE):
        if n.split(".", 1)[0] not in digests:
            try:
                os.remove(os.path.join(PART_CACHE, n))
            except OSError:
                pass


def gcode_model(p, gcode, facts, out):
    """Writes the G-code model of p to out, in the coordinates of its file. It returns False for a
    part that needs no G-code model: one that is no single 3MF file with a slicer-made mesh."""
    if len(p["files"]) != 1 or not gcodemodel.needs_model(p["files"][0]) or len(facts.get("places") or []) != 1:
        return False
    place = dict(facts["places"][0], centre=facts["centre"], scale=facts["scale"])
    gcodemodel.gcode_stl(gcode, out, place, float(facts.get("line_width") or 0.4))
    check_model_size(out)
    return True


def slice_one(cfg, p, i, head, tmp, entry, models):
    """Slices the part p for the plan, and adds the facts, the file and the G-code model to entry
    and models. A part that does not slice gets slice_error."""
    try:
        got = slice_part(cfg, p, i, head, tmp)
    except (SystemExit, curaslice.ProfileError, resinslice.ProfileError, Exception) as e:  # noqa: BLE001
        # The text of an error of this Action, or of the slicer. Any other error has only its
        # class, as failure_text gives it.
        ours = isinstance(e, (SystemExit, curaslice.ProfileError, resinslice.ProfileError))
        text = (" ".join(str(e).split()) if ours else f"the slice failed: {type(e).__name__}")[:MAX_SLICE_ERROR] or "the slicer failed"
        entry["slice_error"] = text
        print(f"::error title=Printdeck slice, {command_data(label(p))}::{command_data(text)}", flush=True)
        return
    if got is None:
        return
    name, path, facts = got
    data = open(path, "rb").read()
    entry.update({k: facts[k] for k in ("seconds", "grams", "ml", "layers", "layer_height", "slicer") if k in facts})
    entry.update({"slice_file": name, "slice_size": len(data), "slice_sha256": hashlib.sha256(data).hexdigest()})
    models[name] = path
    if name.endswith(".gcode"):
        local = os.path.join(tmp, f"part{i + 1}-gcode.stl")
        try:
            made = gcode_model(p, path, facts, local)
        except SystemExit as e:
            log(f"  {label(p)}: no G-code model: {e}")
            made = False
        if made:
            p["gcode_model"] = local
            # The part view: the model goes to the middle of the bed on Z 0, as model_stl puts it.
            tris = [t for item in curaslice.read_3mf(p["files"][0]) for m in item for t in m["triangles"]]
            x0, x1, y0, y1, z0, _ = curaslice.bounds(tris)
            view = os.path.join(tmp, entry["models"][0])
            gcodemodel.transformed(local, view, [1, 0, 0, 0, 1, 0, 0, 0, 1, -(x0 + x1) / 2, -(y0 + y1) / 2, -z0])
            log(f"  {label(p)}: the 3D model is from the G-code")


def plan(config_path):
    ev = event()
    cfg = load_config(config_path)
    files = sorted({f for p in cfg["entries"] for f in p["files"]} |
                   {it["file"] for a in cfg.get("assemblies") or [] for it in a["items"]})
    with group("Generate the model files"):
        generate(cfg, files)
    check_paths(cfg, config_path)
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
        for k in TEXT_FIELDS:
            if p.get(k):
                entry[k] = " ".join(p[k].split()) if k == "group" else p[k].strip()
        if p.get("prebuilt"):
            check_prebuilt(p["prebuilt"], f"the prebuilt file of {label(p)}")
            entry["prebuilt"] = p["prebuilt"]
        else:
            path, quality = profile_of(p)
            entry["profile"] = f"{os.path.basename(path)}/{quality}@{curaslice.profile_version(path)}"
        sx = sy = sz = 0.0
        outline = None
        for j, f in enumerate(p["files"]):
            if not os.path.exists(f):
                fail(f"{label(p)}: no file {f}")
            name = f"part{i + 1}-{j + 1}.stl"
            x, y, z = model_stl(f, os.path.join(tmp, name))
            sx, sy, sz = max(sx, x), max(sy, y), max(sz, z)
            # The outline for the layout of the plan page (platepack): with more files, the
            # largest value of each direction.
            o = platepack.outline([(q[0], q[1]) for t in curaslice.read_stl(os.path.join(tmp, name)) for q in t])
            outline = o if outline is None else [max(u, v) for u, v in zip(outline, o)]
            entry["models"].append(name)
            models[name] = os.path.join(tmp, name)
        entry["material_name"], entry["material_color"] = material_info(p)
        links = material_links(cfg, p)
        if links:
            entry["material_url"] = links[0]["url"]
            entry["material_links"] = links
        entry["digest"] = part_digest(p)
        if not os.environ.get("PRINTDECK_SKIP_SLICE"):  # the switch is for a test on a computer
            slice_one(cfg, p, i, head, tmp, entry, models)
        (bx, by), margin = bed_and_margin(p)
        entry.update({"size_x": round(sx, 2), "size_y": round(sy, 2), "size_z": round(sz, 2), "bed_x": bx, "bed_y": by,
                      "margin": round(margin, 2)})
        if outline is not None:
            entry["outline"] = outline
        manifest["parts"].append(entry)
        why = changed.get((p["name"], p["variant"]))
        log(f"  {label(p)}: {sx:.0f} x {sy:.0f} x {sz:.0f} mm" + (f", changed: {', '.join(why)}" if why else ""))
    doc_files = {}
    if cfg.get("docs"):
        with group("Read the docs"):
            manifest["docs"], doc_files = plan_docs(cfg, tmp)
    if cfg.get("assemblies"):
        with group("Read the assemblies"):
            manifest["assemblies"] = plan_assemblies(cfg, tmp, models)
    cover, cover_file = plan_cover(cfg)
    if cover:
        manifest["cover"] = cover
    reply = call("POST", "/plans", manifest)
    with group(f"Upload {len(models)} models"):
        for name, path in models.items():
            ctype = {".gcode": "text/x-gcode", ".cbddlp": "application/octet-stream"}.get(os.path.splitext(name)[1], "model/stl")
            upload(reply["grant"], name, open(path, "rb").read(), ctype)
            log(f"  {name}: {os.path.getsize(path) // 1024} KB")
    if doc_files:
        with group(f"Upload {len(manifest['docs'])} PDFs"):
            for name, (path, ctype) in doc_files.items():
                upload(reply["grant"], name, open(path, "rb").read(), ctype)
                log(f"  {name}: {os.path.getsize(path) // 1024} KB")
    if cover_file:
        upload(reply["grant"], cover, open(cover_file[0], "rb").read(), cover_file[1])
    call("POST", f"/plans/{reply['id']}/done")
    log(f"The plan: {reply['url']}")
    if not os.environ.get("PRINTDECK_SKIP_SLICE"):
        prune_cache({e["digest"] for e in manifest["parts"]})
    print(f"::notice title=Printdeck plan::{len(changed)} of {len(cfg['entries'])} parts changed. Choose the parts: {reply['url']}",
          flush=True)
    rows = []
    for e in manifest["parts"]:
        why = changed.get((e["name"], e["variant"]))
        rows.append([e["name"], e["variant"] or "-", e.get("printer", ""),
                     e.get("profile", "").partition("@")[0].rpartition("/")[2] or ("ready file" if e.get("prebuilt") else "-"),
                     e.get("material_name") or e.get("material") or "-", f"{e['size_x']:.0f} x {e['size_y']:.0f} x {e['size_z']:.0f} mm",
                     clock(e["seconds"]) if e.get("seconds") else ("does not slice" if e.get("slice_error") else "-"),
                     (f"{e['ml']} ml" if e.get("ml") else f"{e['grams']} g") if e.get("grams") or e.get("ml") else "-",
                     ("yes: " + ", ".join(why)) if why else "no"])
    against = f"`{base[:7]}`" if base else "nothing (a tag, or the first push)"
    summary(f"## Printdeck plan\n\n**[Choose the parts and the plates]({reply['url']})**\n\n"
            f"{len(changed)} of {len(cfg['entries'])} parts changed, compared with {against}.\n\n" +
            table(["Part", "Variant", "Printer", "Profile", "Material", "Size", "Print time", "Filament", "Changed"], rows))
    bad = [e["name"] for e in manifest["parts"] if e.get("slice_error")]
    if bad:
        fail(f"{len(bad)} parts do not slice: {', '.join(bad)}. See the errors above.")


def summary(text):
    p = os.environ.get("GITHUB_STEP_SUMMARY")
    if p:
        with open(p, "a") as f:
            f.write(text)


# The slice.

def progress(build, text, phase, plate=0):
    """Tells the build page what the slice run does now."""
    body = {"text": text, "phase": phase}
    if plate:
        body["plate"] = plate
    call("POST", f"/builds/{build}/progress", body)


def started(build):
    """Tells the service that the run started, with the run ID for the link on the build page."""
    call("POST", f"/builds/{build}/started", {"run_id": os.environ.get("GITHUB_RUN_ID", "")})

def slice_build(config_path, build):
    job = call("GET", f"/builds/{build}")
    # The build page links to this run on GitHub. The step processes told the service already;
    # this call is for a run with no such step.
    if not os.environ.get("PRINTDECK_STARTED"):
        started(build)
    commit = job.get("commit")
    if not isinstance(commit, str) or not COMMIT.fullmatch(commit):
        call("POST", f"/builds/{build}/failed", {"error": "The slice run failed: the build has no valid commit."})
        fail(f"the build has no valid commit: {str(commit)[:80]!r}")
    if git("rev-parse", "HEAD").strip() != commit:
        log(f"checking out the commit of the plan, {commit[:7]}")
        progress(build, "Getting the commit of the plan", PHASE_SETUP)
        fetch_commit(commit)
        subprocess.run(["git", "checkout", "--quiet", "--detach", commit, "--"], check=True)
    if git("rev-parse", "HEAD").strip() != commit:
        call("POST", f"/builds/{build}/failed", {"error": "The slice run failed: the checkout is not the commit of the plan."})
        fail(f"the checkout is not the commit of the plan, {commit}")
    log(f"Build {build}: {len(job['plates'])} plates of the commit {commit[:7]}")
    try:
        files = do_slice(config_path, job)
    except SystemExit:
        call("POST", f"/builds/{build}/failed", {"error": "The slice run failed: see the run on GitHub."})
        raise
    except Exception as e:  # noqa: BLE001: any failure goes to the build page
        call("POST", f"/builds/{build}/failed", {"error": failure_text(e)})
        raise
    progress(build, "Sending the files of the plates", PHASE_UPLOAD)
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


PHASE_SETUP, PHASE_SLICE, PHASE_UPLOAD = "setup", "slice", "upload"
# The commit of a build: the SHA-1 of a commit, 40 hex digits.
COMMIT = re.compile(r"[0-9a-f]{40}")


def failure_text(e):
    """The error of a failed slice for the build page. A ProfileError has a text of this
    Action, on one line. Any other error has only its class: the text of a parse error can have
    a line of the file that the run read."""
    if isinstance(e, (curaslice.ProfileError, resinslice.ProfileError)):
        return f"The slice run failed: {curaslice.one_line(e)[:500]}"
    return f"The slice run failed: {type(e).__name__}. See the run on GitHub."


def fetch_commit(commit):
    """Fetches the commit of the plan. A shallow checkout (fetch-depth: 1) gets the commit alone,
    and a partial clone (filter: blob:none) gets its files at the checkout. commit is 40 hex
    digits (COMMIT)."""
    shallow = git("rev-parse", "--is-shallow-repository").strip() == "true"
    depth = ["--depth=1"] if shallow else []
    subprocess.run(["git", "fetch", "--quiet", *depth, "origin", "--", commit], check=False)
    if subprocess.run(["git", "cat-file", "-e", commit + "^{commit}"], check=False).returncode != 0:
        # The commit of a pull request of a fork is in the refs of the pull requests.
        subprocess.run(["git", "fetch", "--quiet", *depth, "origin", "--", "+refs/pull/*/head:refs/remotes/origin/pull/*"],
                       check=False)


def do_slice(config_path, job):
    cfg = load_config(config_path)
    by_name = {(p["name"].lower(), p["variant"].lower()): p for p in cfg["entries"]}
    tmp = tempfile.mkdtemp(prefix="printdeck-slice-")
    out = []
    total = len(job["plates"])
    for i, plate in enumerate(job["plates"], 1):
        # The service leaves out a plate that has the G-code of the plan. "number" is the number
        # of the plate in the build, so the file names stay the same.
        n = int(plate.get("number") or i)
        parts = []
        for pp in plate["parts"]:
            p = by_name.get((pp["name"].lower(), str(pp.get("variant") or "").lower()))
            if p is None:
                raise SystemExit(f"plate {n}: printdeck.yml of this commit has no part {label(dict(pp, variant=pp.get('variant') or ''))}")
            parts.append((p, int(pp["count"])))
        first = parts[0][0]
        # The run checks the size of each model file before a slicer reads it. A 3MF file can unpack to more.
        for p, _ in parts:
            if not first.get("prebuilt"):
                for f in p["files"]:
                    if not os.path.isfile(f):
                        raise SystemExit(f"{label(p)}: no file {f}")
                    check_model_size(f)
        entry = {"printer": plate["printer"], "process": plate["process"], "parts": plate["parts"],
                 "material_name": plate.get("material_name", ""), "material": plate.get("material", "")}
        title = " + ".join(label(p) + (f" x{c}" if c > 1 else "") for p, c in parts)
        progress(job["id"], f"Slicing plate {i} of {total}: {title}", PHASE_SLICE, n)
        paths = {}
        if first.get("prebuilt"):
            src = first["prebuilt"]
            name = f"plate-{n}{check_prebuilt(src, f'the prebuilt file of {label(first)}')}"
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
                try:
                    facts = curaslice.slice_plate(path, quality, first.get("material"),
                                                  [(label(p), p["files"], c, p.get("settings") or {}) for p, c in parts],
                                                  os.path.join(tmp, name), name=f"{cfg.get('project', 'printdeck')}-{n}", meta=meta)
                except curaslice.ProfileError as e:
                    print(f"::error::{command_data(f'plate {n}: {e}')}", flush=True)
                    raise
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
    elif a.mode == "processes" and not a.build:
        # For the steps of action.yml in the plan run: the slicers that the parts need.
        cfg = load_config(a.config)
        procs = {process_of(p) for p in cfg["entries"] if not p.get("prebuilt")}
        with open(os.environ.get("GITHUB_OUTPUT", os.devnull), "a") as f:
            for k in ("fdm", "resin"):
                f.write(f"{k}={'true' if k in procs else 'false'}\n")
        log(f"The parts need: {', '.join(sorted(procs)) or 'no slicer'}")
    elif a.mode == "processes":
        # For the steps of action.yml: the slicers that the plates of the build need.
        if not re.fullmatch(r"[a-z0-9]{1,40}", a.build):
            fail("processes needs the ID of a build")
        # The first step of the run: the build page shows at once that the run started.
        try:
            started(a.build)
            progress(a.build, "Getting the slicer", PHASE_SETUP)
            procs = {pl.get("process") or "fdm" for pl in call("GET", f"/builds/{a.build}")["plates"]}
        except SystemExit:
            call("POST", f"/builds/{a.build}/failed", {"error": "The slice run failed: see the run on GitHub."})
            raise
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
