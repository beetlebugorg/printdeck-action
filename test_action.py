"""Tests of printdeck_action.py. Run: python3 -m unittest test_action"""

import io
import json
import os
import subprocess
import tempfile
import unittest
import unittest.mock
import zipfile

import printdeck_action as pa
from test_curaslice import cube

NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"


def threemf(path, date):
    tri = ('<vertices><vertex x="0" y="0" z="0"/><vertex x="10" y="0" z="0"/><vertex x="0" y="10" z="5"/></vertices>'
           '<triangles><triangle v1="0" v2="1" v3="2"/></triangles>')
    big = ('<vertices><vertex x="-50" y="-50" z="0"/><vertex x="50" y="-50" z="0"/><vertex x="0" y="50" z="40"/></vertices>'
           '<triangles><triangle v1="0" v2="1" v3="2"/></triangles>')
    model = (f'<model xmlns="{NS}"><metadata name="CreationDate">{date}</metadata><resources>'
             f'<object id="1" type="model"><mesh>{tri}</mesh></object>'
             '<object id="2" type="model"><metadatagroup><metadata name="cura:anti_overhang_mesh">True</metadata>'
             f'</metadatagroup><mesh>{big}</mesh></object><object id="3" type="model"><components>'
             '<component objectid="1"/><component objectid="2"/></components></object></resources>'
             '<build><item objectid="3"/></build></model>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("3D/3dmodel.model", model)


class Files(unittest.TestCase):
    def test_3mf_dates_do_not_count(self):
        d = tempfile.mkdtemp()
        a, b = os.path.join(d, "a.3mf"), os.path.join(d, "b.3mf")
        threemf(a, "2026-10-30")
        threemf(b, "2026-10-31")
        self.assertEqual(pa.comparable(a, open(a, "rb").read()), pa.comparable(b, open(b, "rb").read()))

    def test_model_leaves_out_the_blockers(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "part.3mf")
        threemf(p, "x")
        size = pa.model_stl(p, os.path.join(d, "out.stl"))
        self.assertEqual(size, (10.0, 10.0, 5.0))


class Changed(unittest.TestCase):
    def test_parts_that_changed(self):
        d = tempfile.mkdtemp()
        run = lambda *a: subprocess.run(["git", *a], cwd=d, check=True, capture_output=True, text=True).stdout.strip()
        run("init", "-q", "-b", "main")
        run("config", "user.email", "t@example.org")
        run("config", "user.name", "T")
        os.makedirs(os.path.join(d, "profiles", "taz"))
        open(os.path.join(d, "profiles", "taz", "cura.json"), "w").write("{}")
        cube(os.path.join(d, "a.stl"), 10)
        cube(os.path.join(d, "b.stl"), 10)
        cube(os.path.join(d, "d_fine.stl"), 10)
        cube(os.path.join(d, "d.stl"), 10)
        cfg = ("parts:\n"
               "  - {name: A, files: [a.stl], printer: taz, profile: taz/fine}\n"
               "  - {name: B, files: [b.stl], printer: taz, profile: taz/fine}\n"
               "  - {name: C, files: [b.stl], printer: taz, profile: taz/fine, count: 2}\n"
               "  - name: D\n"
               "    printer: taz\n"
               "    settings: {adhesion_type: brim}\n"
               "    variants:\n"
               "      - {name: fine, files: [d_fine.stl], profile: taz/fine, settings: {brim_line_count: 8}}\n"
               "      - {name: standard, files: [d.stl], profile: taz/standard}\n")
        open(os.path.join(d, "printdeck.yml"), "w").write(cfg)
        run("add", ".")
        run("commit", "-q", "-m", "one")
        base = run("rev-parse", "HEAD")
        cube(os.path.join(d, "a.stl"), 12)  # A: its file
        cube(os.path.join(d, "d_fine.stl"), 12)  # D: only its fine variant
        open(os.path.join(d, "printdeck.yml"), "w").write(cfg.replace("count: 2", "count: 4"))  # C: its entry
        run("commit", "-q", "-am", "two")
        head = run("rev-parse", "HEAD")
        cwd = os.getcwd()
        os.chdir(d)
        try:
            got = pa.changed_parts(pa.load_config("printdeck.yml"), "printdeck.yml", base, head)
            self.assertEqual(set(got), {("A", ""), ("C", ""), ("D", "fine")})
            self.assertEqual((got[("A", "")], got[("C", "")]), (["its files"], ["its entry in printdeck.yml"]))
            fine = next(e for e in pa.load_config("printdeck.yml")["entries"] if e["variant"] == "fine")
            self.assertEqual((fine["files"], fine["settings"]), (["d_fine.stl"], {"adhesion_type": "brim", "brim_line_count": 8}))
            open("profiles/taz/cura.json", "w").write('{"x": 1}')  # the profile: all parts
            run("commit", "-q", "-am", "three")
            got = pa.changed_parts(pa.load_config("printdeck.yml"), "printdeck.yml", head, run("rev-parse", "HEAD"))
            self.assertEqual(set(got), {("A", ""), ("B", ""), ("C", ""), ("D", "fine"), ("D", "standard")})
            self.assertEqual(pa.changed_parts(pa.load_config("printdeck.yml"), "printdeck.yml", None, head), {})
        finally:
            os.chdir(cwd)


class Material(unittest.TestCase):
    def test_roll_name_and_color(self):
        cwd = os.getcwd()
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "profiles"))
        import shutil
        shutil.copytree(os.path.join(cwd, "testdata", "taz-workhorse-se"), os.path.join(d, "profiles", "taz-workhorse-se"))
        os.chdir(d)
        try:
            self.assertEqual(pa.material_info({"name": "A", "profile": "taz-workhorse-se/pla-0.10-fine", "material": "roll-4"}),
                             ("MatterHackers PLA Black", "#1a1a1a"))
            self.assertEqual(pa.material_info({"name": "R", "prebuilt": "r.cbddlp", "material": "Grey resin"}), ("Grey resin", ""))
        finally:
            os.chdir(cwd)


class Config(unittest.TestCase):
    def test_checks(self):
        d = tempfile.mkdtemp()
        cwd = os.getcwd()
        os.chdir(d)
        try:
            for text in ("parts: []\n", "parts:\n  - {name: A, printer: taz}\n",
                         "parts:\n  - {name: A, files: [a], printer: taz, profile: p/q}\n  - {name: a, files: [b], printer: taz, profile: p/q}\n",
                         "parts:\n  - {name: A, files: [a], printer: taz}\n"):
                open("printdeck.yml", "w").write(text)
                with self.assertRaises(SystemExit, msg=text):
                    pa.load_config("printdeck.yml")
        finally:
            os.chdir(cwd)


if __name__ == "__main__":
    unittest.main()


class Paths(unittest.TestCase):
    def test_paths_stay_in_the_repository(self):
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "stl"))
        open(os.path.join(d, "stl", "a.stl"), "w").write("x")
        os.symlink("/etc/passwd", os.path.join(d, "stl", "passwd.stl"))
        os.symlink(os.path.join(d, "stl"), os.path.join(d, "inside"))
        cwd = os.getcwd()
        os.chdir(d)
        try:
            self.assertEqual(pa.inside("stl/a.stl"), "stl/a.stl")
            self.assertEqual(pa.inside("inside/a.stl"), "inside/a.stl")  # a link in the repository
            for bad in ("/etc/passwd", "../x.stl", "stl/../../x", "stl/passwd.stl", "", "a\\b"):
                with self.assertRaises(SystemExit, msg=bad):
                    pa.inside(bad)
            for prof in ("../x/y", "a/../../b", "a/b/c"):
                with self.assertRaises(SystemExit, msg=prof):
                    pa.profile_of({"name": "A", "profile": prof})
        finally:
            os.chdir(cwd)


class Base(unittest.TestCase):
    def test_a_branch_compares_with_the_merge_base(self):
        d = tempfile.mkdtemp()
        run = lambda *a: subprocess.run(["git", *a], cwd=d, check=True, capture_output=True, text=True).stdout.strip()
        run("init", "-q", "-b", "main")
        run("config", "user.email", "t@example.org")
        run("config", "user.name", "T")
        open(os.path.join(d, "a"), "w").write("1")
        run("add", ".")
        run("commit", "-q", "-m", "one")
        base = run("rev-parse", "HEAD")
        run("checkout", "-q", "-b", "door")
        for n in ("2", "3"):
            open(os.path.join(d, "a"), "w").write(n)
            run("commit", "-qam", n)
        run("update-ref", "refs/remotes/origin/main", base)
        cwd = os.getcwd()
        os.chdir(d)
        old = dict(os.environ)
        try:
            os.environ["GITHUB_REF"] = "refs/heads/door"
            ev = {"before": "x" * 40, "repository": {"default_branch": "main"}}
            self.assertEqual(pa.base_commit(ev), base)  # all the commits of the branch
            os.environ["GITHUB_REF"] = "refs/heads/main"
            self.assertEqual(pa.base_commit(dict(ev, before="ab" * 20)), "ab" * 20)  # the last push
            os.environ["GITHUB_REF"] = "refs/tags/v1"
            self.assertIsNone(pa.base_commit(ev))
        finally:
            os.chdir(cwd)
            os.environ.clear()
            os.environ.update(old)


# A PDF of one page with a black square, with no table of the objects (poppler finds them).
TINY_PDF = (b"%PDF-1.4\n1 0 obj <</Type/Catalog/Pages 2 0 R>> endobj\n"
            b"2 0 obj <</Type/Pages/Kids[3 0 R]/Count 1>> endobj\n"
            b"3 0 obj <</Type/Page/Parent 2 0 R/MediaBox[0 0 200 283]/Contents 4 0 R>> endobj\n"
            b"4 0 obj <</Length 22>> stream\n50 100 100 100 re f\nendstream endobj\n"
            b"trailer <</Root 1 0 R>>\n%%EOF\n")


class Docs(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.dir)
        os.makedirs("docs")
        open("docs/guide.pdf", "wb").write(TINY_PDF)
        open("a.stl", "w").write("x")

    def tearDown(self):
        os.chdir(self.cwd)

    def config(self, docs):
        open("printdeck.yml", "w").write("parts:\n  - {name: A, files: [a.stl], printer: taz, profile: p/q}\n" + docs)
        return pa.load_config("printdeck.yml")

    def test_checks(self):
        for docs in ("docs: x\n", "docs:\n  - {file: docs/guide.pdf}\n", "docs:\n  - {name: G, file: ../g.pdf}\n",
                     "docs:\n  - {name: G, file: docs/guide.txt}\n"):
            with self.assertRaises(SystemExit, msg=docs):
                self.config(docs)
        self.assertEqual(len(self.config("docs:\n  - {name: Guide, file: docs/guide.pdf}\n")["docs"]), 1)

    def test_plan_docs(self):
        cfg = self.config("docs:\n  - {name: Guide, file: docs/guide.pdf}\n")
        docs, files = pa.plan_docs(cfg, self.dir)
        self.assertEqual(len(docs), 1)
        d = docs[0]
        self.assertEqual((d["name"], d["file"], d["pdf"], d["size"]), ("Guide", "docs/guide.pdf", "doc1.pdf", len(TINY_PDF)))
        self.assertEqual(files["doc1.pdf"], ("docs/guide.pdf", "application/pdf"))
        if pa.poppler():
            self.assertEqual(d["pages"], 1)
            self.assertEqual(d["thumb"], "doc1.png")
            png = open(files["doc1.png"][0], "rb").read()
            self.assertTrue(png.startswith(b"\x89PNG"))
            self.assertEqual(int.from_bytes(png[16:20], "big"), 600)

    def test_not_a_pdf(self):
        open("docs/guide.pdf", "wb").write(b"hello")
        with self.assertRaises(SystemExit):
            pa.plan_docs(self.config("docs:\n  - {name: Guide, file: docs/guide.pdf}\n"), self.dir)


class Materials(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.dir)
        open("a.stl", "w").write("x")

    def tearDown(self):
        os.chdir(self.cwd)

    def config(self, mats):
        open("printdeck.yml", "w").write("parts:\n  - {name: A, files: [a.stl], printer: taz, profile: p/q, material: roll-3}\n" + mats)
        return pa.load_config("printdeck.yml")

    def test_checks(self):
        for mats in ("materials: [a]\n", "materials:\n  roll-3: {buy: http://shop.example/x}\n",
                     "materials:\n  roll-3: {buy: 'javascript:alert(1)'}\n", "materials:\n  roll-3: {buy: https://localhost/x}\n",
                     "materials:\n  roll-3: {price: 3}\n", "materials:\n  roll-3: {buy: \"https://shop.example/a\\nb\"}\n"):
            with self.assertRaises(SystemExit, msg=mats):
                self.config(mats)

    def test_url(self):
        cfg = self.config("materials:\n  roll-3: {buy: 'https://www.matterhackers.com/store/l/navy-pla?x=1'}\n")
        part = cfg["parts"][0]
        self.assertEqual(pa.material_url(cfg, part), "https://www.matterhackers.com/store/l/navy-pla?x=1")
        self.assertEqual(pa.material_url(self.config(""), part), "")
        cfg = self.config("materials:\n  roll-3:\n    buy:\n      2.85 mm: https://shop.example/a\n      1.75 mm: https://shop.example/b\n")
        self.assertEqual(pa.material_links(cfg, cfg["parts"][0]),
                         [{"label": "2.85 mm", "url": "https://shop.example/a"}, {"label": "1.75 mm", "url": "https://shop.example/b"}])
        self.assertEqual(pa.material_url(cfg, cfg["parts"][0]), "https://shop.example/a")
        for bad in ("materials:\n  roll-3:\n    buy: {a: http://shop.example/a}\n",
                    "materials:\n  roll-3:\n    buy: {}\n",
                    "materials:\n  roll-3:\n    buy: {'" + "x" * 21 + "': https://shop.example/a}\n"):
            with self.assertRaises(SystemExit, msg=bad):
                self.config(bad)


class Cover(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.dir)
        open("a.stl", "w").write("x")
        open("render.png", "wb").write(b"\x89PNG\r\n\x1a\nrest")
        open("render.jpg", "wb").write(b"\xff\xd8\xff\xe0rest")
        open("fake.png", "wb").write(b"hello")

    def tearDown(self):
        os.chdir(self.cwd)

    def config(self, cover):
        open("printdeck.yml", "w").write("parts:\n  - {name: A, files: [a.stl], printer: taz, profile: p/q}\n" + cover)
        return pa.load_config("printdeck.yml")

    def test_checks(self):
        for cover in ("cover: [a]\n", "cover: a.stl\n", "cover: ../render.png\n"):
            with self.assertRaises(SystemExit, msg=cover):
                self.config(cover)

    def test_plan_cover(self):
        self.assertEqual(pa.plan_cover(self.config("")), ("", None))
        self.assertEqual(pa.plan_cover(self.config("cover: render.png\n")), ("cover.png", ("render.png", "image/png")))
        self.assertEqual(pa.plan_cover(self.config("cover: render.jpg\n")), ("cover.jpg", ("render.jpg", "image/jpeg")))
        with self.assertRaises(SystemExit):
            pa.plan_cover(self.config("cover: fake.png\n"))


class Digest(unittest.TestCase):
    def test_digest_of_a_part(self):
        d = tempfile.mkdtemp()
        cwd = os.getcwd()
        os.chdir(d)
        try:
            os.makedirs("profiles/taz")
            open("profiles/taz/cura.json", "w").write("{}")
            threemf("a.3mf", "2026-10-01")
            p = {"name": "A", "variant": "", "files": ["a.3mf"], "printer": "taz", "profile": "taz/q", "count": 1}
            first = pa.part_digest(p)
            self.assertEqual(len(first), 64)
            threemf("a.3mf", "2026-10-31")  # only the date changed
            self.assertEqual(pa.part_digest(p), first)
            self.assertEqual(pa.part_digest(dict(p, count=4)), first)
            # The text of the Parts tab changes no plate.
            self.assertEqual(pa.part_digest(dict(p, description="The door of the pod.", group="The pod")), first)
            self.assertNotEqual(pa.part_digest(dict(p, settings={"adhesion_type": "brim"})), first)
            open("profiles/taz/cura.json", "w").write('{"x": 1}')
            self.assertNotEqual(pa.part_digest(p), first)
        finally:
            os.chdir(cwd)


class TestTextFields(unittest.TestCase):
    def test_description_and_group(self):
        d = tempfile.mkdtemp()
        cwd = os.getcwd()
        os.chdir(d)
        try:
            open("a.stl", "w").write("solid a\nendsolid a\n")
            def cfg(extra):
                open("printdeck.yml", "w").write(
                    "parts:\n  - name: A\n    files: [a.stl]\n    printer: taz\n    profile: taz/q\n" + extra)
                return pa.load_config("printdeck.yml")
            e = cfg("    group: The case\n    description: |\n      The front of the case.\n")["entries"][0]
            self.assertEqual((e["group"], e["description"].strip()), ("The case", "The front of the case."))
            with self.assertRaises(SystemExit):
                cfg("    description: [not, text]\n")
            with self.assertRaises(SystemExit):
                cfg("    group: " + "x" * 81 + "\n")
            v = pa.expand([{"name": "A", "description": "a", "variants": [{"name": "fine", "description": "b"}, {"name": "draft"}]}])
            self.assertEqual([x["description"] for x in v], ["b", "a"])
        finally:
            os.chdir(cwd)


class Assemblies(unittest.TestCase):
    PARTS = ("parts:\n"
             "  - {name: Slate frame, files: [frame.stl], printer: taz, profile: taz-workhorse-se/pla-0.10-fine, material: roll-4}\n"
             "  - name: Door\n    printer: taz\n    profile: taz-workhorse-se/pla-0.10-fine\n"
             "    variants:\n      - {name: fine, files: [door.stl]}\n")
    GOOD = ("assemblies:\n"
            "  - name: Slate v4\n"
            "    description: |\n      The case on the base.\n"
            "    items:\n"
            "      - {part: slate FRAME, file: asm/frame.stl, explode: [0, 0, 1]}\n"
            "      - {part: Door (fine), name: Front door, file: asm/door.stl, color: \"#AABBCC\"}\n"
            "      - {name: Touch Display 2, file: asm/display.stl, color: \"#202226\", explode: [0, -1.5, 0]}\n")

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        import shutil
        shutil.copytree(os.path.join(self.cwd, "testdata", "taz-workhorse-se"), os.path.join(self.dir, "profiles", "taz-workhorse-se"))
        os.chdir(self.dir)
        os.makedirs("asm")
        for f in ("frame.stl", "door.stl"):
            cube(f, 10)
        cube("asm/frame.stl", 10)
        cube("asm/door.stl", 4)
        cube("asm/display.stl", 6)

    def tearDown(self):
        os.chdir(self.cwd)

    def config(self, asm):
        open("printdeck.yml", "w").write(self.PARTS + asm)
        return pa.load_config("printdeck.yml")

    def test_checks(self):
        self.assertEqual(len(self.config(self.GOOD)["assemblies"]), 1)
        item = "assemblies:\n  - name: S\n    items:\n      - {%s}\n"
        bad = [item % "part: Nothing, file: asm/frame.stl",  # an unknown part
               item % "part: Door, file: asm/door.stl",  # the part has variants: "Door (fine)"
               item % "file: asm/display.stl",  # no part and no name
               item % "name: D",  # no file
               item % "name: D, file: ../x.stl",
               item % "name: D, file: asm/display.stl, color: red",
               item % "name: D, file: asm/display.stl, color: \"#12345\"",
               item % "name: D, file: asm/display.stl, explode: [0, 0, 0]",
               item % "name: D, file: asm/display.stl, explode: [0, 1]",
               item % "name: D, file: asm/display.stl, explode: [0, x, 1]",
               "assemblies:\n  - name: S\n    items: []\n",
               "assemblies:\n  - {name: S, items: [" + ", ".join(["{name: D, file: asm/display.stl}"] * 61) + "]}\n",
               "assemblies:\n" + "".join(f"  - {{name: S{n}, items: [{{name: D, file: asm/display.stl}}]}}\n" for n in range(11)),
               "assemblies:\n  - {name: S, items: [{name: D, file: asm/display.stl}]}\n"
               "  - {name: s, items: [{name: D, file: asm/display.stl}]}\n",
               "assemblies:\n  - {items: [{name: D, file: asm/display.stl}]}\n"]
        for text in bad:
            with self.assertRaises(SystemExit, msg=text):
                self.config(text)

    def test_plan_assemblies(self):
        cfg = self.config(self.GOOD)
        models = {}
        asms = pa.plan_assemblies(cfg, self.dir, models)
        self.assertEqual(asms, [{"name": "Slate v4", "description": "The case on the base.", "items": [
            {"name": "Slate frame", "part": "Slate frame", "model": "asm1-1.stl", "color": "#1a1a1a", "explode": [0.0, 0.0, 1.0]},
            {"name": "Front door", "part": "Door (fine)", "model": "asm1-2.stl", "color": "#aabbcc"},
            {"name": "Touch Display 2", "model": "asm1-3.stl", "color": "#202226", "explode": [0.0, -1.5, 0.0]}]}])
        self.assertEqual(sorted(models), ["asm1-1.stl", "asm1-2.stl", "asm1-3.stl"])
        # The model of an item keeps its place in the assembly.
        self.assertEqual(pa.read_stl(models["asm1-3.stl"]), pa.read_stl("asm/display.stl"))

    def test_transform_checks(self):
        item = "assemblies:\n  - name: S\n    items:\n      - {%s}\n"
        ok = self.config(item % "part: slate frame, file: asm/frame.stl, transform: [1, 0, 0, 0, 1, 0, 0, 0, 1, 5, 0, 2.5]")
        self.assertEqual(len(ok["assemblies"][0]["items"][0]["transform"]), 12)
        for bad in ("part: slate frame, file: asm/frame.stl, transform: [1, 0, 0]",
                    "part: slate frame, file: asm/frame.stl, transform: [1, 0, 0, 0, 1, 0, 0, 0, 1, 5, 0, x]",
                    "part: slate frame, file: asm/frame.stl, transform: [1, 0, 0, 0, 1, 0, 0, 0, 1, 5, 0, .inf]",
                    "name: D, file: asm/display.stl, transform: [1, 0, 0, 0, 1, 0, 0, 0, 1, 5, 0, 2]"):  # no part
            with self.assertRaises(SystemExit, msg=bad):
                self.config(item % bad)

    def test_gcode_model_in_place(self):
        cfg = self.config("assemblies:\n  - name: S\n    items:\n"
                          "      - {part: slate frame, file: asm/frame.stl, transform: [1, 0, 0, 0, 1, 0, 0, 0, 1, 100, 0, 0]}\n"
                          "      - {part: door (fine), file: asm/door.stl, transform: [1, 0, 0, 0, 1, 0, 0, 0, 1, 100, 0, 0]}\n")
        # The frame has a G-code model; the door has none, so its item keeps its file.
        cfg["entries"][0]["gcode_model"] = "frame.stl"
        models = {}
        pa.plan_assemblies(cfg, self.dir, models)
        moved = [tuple((x + 100, y, z) for x, y, z in t) for t in pa.read_stl("frame.stl")]
        self.assertEqual(pa.read_stl(models["asm1-1.stl"]), moved)
        self.assertEqual(pa.read_stl(models["asm1-2.stl"]), pa.read_stl("asm/door.stl"))

    def test_missing_file(self):
        cfg = self.config("assemblies:\n  - {name: S, items: [{name: D, file: asm/none.stl}]}\n")
        with self.assertRaises(SystemExit):
            pa.plan_assemblies(cfg, self.dir, {})

    def test_no_digest_change(self):
        a = self.config("")["entries"]
        b = self.config(self.GOOD)["entries"]
        self.assertEqual([pa.part_digest(p) for p in a], [pa.part_digest(p) for p in b])


class InRepo(unittest.TestCase):
    """A test in a new folder as the repository."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.dir)
        cube("a.stl", 10)

    def tearDown(self):
        os.chdir(self.cwd)

    def config(self, text):
        open("printdeck.yml", "w").write(text)
        return pa.load_config("printdeck.yml")


class Names(InRepo):
    def test_control_characters(self):
        part = "  - {name: A, files: [a.stl], printer: taz, profile: p/q}\n"
        bad = ['project: "x\\nG28"\nparts:\n' + part,
               'parts:\n  - {name: "A\\nG28", files: [a.stl], printer: taz, profile: p/q}\n',
               'parts:\n  - {name: "A\\r::error::x", files: [a.stl], printer: taz, profile: p/q}\n',
               'parts:\n  - {name: "A\\u2028B", files: [a.stl], printer: taz, profile: p/q}\n',
               'parts:\n  - {name: "A\\x1b[0m", files: [a.stl], printer: taz, profile: p/q}\n',
               'parts:\n  - {name: A, files: [a.stl], printer: taz, profile: p/q, group: "G\\nx"}\n',
               'parts:\n  - {name: A, files: [a.stl], printer: taz, profile: p/q, material: "m\\x7f"}\n',
               'parts:\n  - name: A\n    printer: taz\n    profile: p/q\n    variants:\n      - {name: "fine\\nG28", files: [a.stl]}\n',
               'parts:\n' + part + 'assemblies:\n  - {name: "S\\nx", items: [{name: B, file: a.stl}]}\n',
               'parts:\n' + part + 'assemblies:\n  - {name: S, items: [{name: "B\\nx", file: a.stl}]}\n',
               'parts:\n  - {name: A, files: ["a\\n.stl"], printer: taz, profile: p/q}\n']
        for text in bad:
            with self.assertRaises(SystemExit, msg=text):
                self.config(text)
        cfg = self.config('project: bittlex\nparts:\n  - {name: A, files: [a.stl], printer: taz, profile: p/q, '
                          'description: "Line one.\\nLine two."}\n')
        self.assertEqual(cfg["entries"][0]["description"], "Line one.\nLine two.")

    def test_fail_escapes_line_breaks(self):
        out = io.StringIO()
        with unittest.mock.patch("sys.stdout", out), self.assertRaises(SystemExit):
            pa.fail("a\n::add-mask::x\r50%")
        self.assertEqual(out.getvalue(), "::error::a%0A::add-mask::x%0D50%25\n")


class GitPaths(InRepo):
    def test_no_paths_in_git(self):
        os.makedirs(".git")
        open(".git/config", "w").write("[core]\n")
        os.symlink(".git", "dotgit")
        os.symlink(".git/config", "config.stl")
        for bad in (".git/config", "a/.git/x", ".GIT/config", "dotgit/config", "config.stl"):
            with self.assertRaises(SystemExit, msg=bad):
                pa.inside(bad)
        self.assertEqual(pa.inside("a.stl"), "a.stl")
        for text in ('parts:\n  - {name: A, files: [.git/config], printer: taz, profile: p/q}\n',
                     'parts:\n  - {name: A, files: [a.stl], printer: phenom, prebuilt: .git/x.cbddlp}\n',
                     'parts:\n  - {name: A, files: [a.stl], printer: taz, profile: p/q}\ncover: .git/x.png\n',
                     'parts:\n  - {name: A, files: [a.stl], printer: taz, profile: p/q}\ndocs:\n  - {name: D, file: .git/x.pdf}\n'):
            with self.assertRaises(SystemExit, msg=text):
                self.config(text)

    def test_config_is_a_link(self):
        os.symlink("/proc/self/environ", "printdeck.yml")
        with self.assertRaises(SystemExit):
            pa.load_config("printdeck.yml")


class Prebuilt(InRepo):
    def test_extensions_and_size(self):
        for ext in (".cbddlp", ".ctb", ".gcode", ".CBDDLP"):
            open("plate" + ext, "wb").write(b"x" * 10)
            self.config(f"parts:\n  - {{name: A, files: [a.stl], printer: phenom, prebuilt: plate{ext}}}\n")
            self.assertEqual(pa.check_prebuilt("plate" + ext, "p"), ext.lower())
        for bad in ("plate.txt", "plate", "plate.cbddlp.sh"):
            with self.assertRaises(SystemExit, msg=bad):
                self.config(f"parts:\n  - {{name: A, files: [a.stl], printer: phenom, prebuilt: {bad}}}\n")
        with self.assertRaises(SystemExit):
            pa.check_prebuilt("none.cbddlp", "p")
        with unittest.mock.patch.object(pa, "MAX_PREBUILT", 5), self.assertRaises(SystemExit):
            pa.check_prebuilt("plate.cbddlp", "p")

    def test_paths_again_after_the_generate_step(self):
        cfg = self.config("parts:\n  - {name: A, files: [a.stl], printer: taz, profile: p/q}\n")
        os.remove("a.stl")
        os.symlink("/etc/passwd", "a.stl")
        with self.assertRaises(SystemExit):
            pa.check_paths(cfg, "printdeck.yml")


class SliceCommit(InRepo):
    def run_build(self, commit, heads):
        calls, runs = [], []

        def call(method, path, body=None):
            calls.append((method, path, body))
            return {"id": "b1", "commit": commit, "plates": []} if method == "GET" else {"url": "u", "grant": {}}

        def git(*args):
            return heads.pop(0) + "\n" if args[0] == "rev-parse" and args[1] == "HEAD" else ""

        def run(args, **kw):
            runs.append(args)
            return subprocess.CompletedProcess(args, 0)

        with unittest.mock.patch.multiple(pa, call=call, git=git, do_slice=lambda *a: []), \
                unittest.mock.patch.object(pa.subprocess, "run", run), unittest.mock.patch.dict(os.environ, {"PRINTDECK_STARTED": "1"}), \
                unittest.mock.patch("sys.stdout", io.StringIO()):
            try:
                pa.slice_build("printdeck.yml", "b1")
            except SystemExit:
                return calls, runs, True
        return calls, runs, False

    def test_bad_commit(self):
        for bad in ("--upload-pack=touch /tmp/x", "HEAD", "a" * 39, "A" * 40, None, 7):
            calls, runs, failed = self.run_build(bad, ["0" * 40])
            self.assertTrue(failed, bad)
            self.assertEqual(runs, [], bad)
            self.assertTrue(any(p.endswith("/failed") for _, p, _ in calls), bad)

    def test_checkout(self):
        good = "a" * 40
        calls, runs, failed = self.run_build(good, ["b" * 40, good, good])
        self.assertFalse(failed)
        self.assertIn(["git", "checkout", "--quiet", "--detach", good, "--"], runs)
        fetch = next(r for r in runs if r[:2] == ["git", "fetch"])
        self.assertEqual(fetch[-2:], ["--", good])
        # The checkout is not the commit of the build.
        calls, runs, failed = self.run_build(good, ["b" * 40, "", "b" * 40])
        self.assertTrue(failed)
        self.assertTrue(any(p.endswith("/failed") for _, p, _ in calls))

    def test_failure_text(self):
        import configparser
        e = configparser.MissingSectionHeaderError("/proc/self/environ", 1, "ACTIONS_ID_TOKEN_REQUEST_TOKEN=secret\n")
        self.assertEqual(pa.failure_text(e), "The slice run failed: MissingSectionHeaderError. See the run on GitHub.")
        self.assertEqual(pa.failure_text(pa.curaslice.ProfileError("the profile x/y: a\nb")),
                         "The slice run failed: the profile x/y: a b")


class SliceModelSize(InRepo):
    def test_size_before_the_slice(self):
        import shutil
        shutil.copytree(os.path.join(self.cwd, "testdata", "taz-workhorse-se"), os.path.join("profiles", "taz-workhorse-se"))
        open("printdeck.yml", "w").write("parts:\n  - {name: A, files: [a.stl], printer: taz, profile: taz-workhorse-se/pla-0.10-fine}\n")
        job = {"id": "b1", "commit": "a" * 40, "plates": [{"printer": "taz", "process": "fdm", "parts": [{"name": "A", "count": 1}]}]}
        sliced = []
        with unittest.mock.patch.object(pa, "call", lambda *a, **k: {}), \
                unittest.mock.patch.object(pa, "MAX_MODEL", 100), \
                unittest.mock.patch.object(pa.curaslice, "slice_plate", lambda *a, **k: sliced.append(a)), \
                unittest.mock.patch("sys.stdout", io.StringIO()):
            with self.assertRaises(SystemExit):
                pa.do_slice("printdeck.yml", job)
        self.assertEqual(sliced, [])


class PartSlice(InRepo):
    PARTS = "parts:\n  - {name: A, files: [a.stl], printer: taz, profile: taz-workhorse-se/pla-0.10-fine}\n"

    def test_slice_error(self):
        cfg = self.config(self.PARTS)
        entry, models = {}, {}
        with unittest.mock.patch.object(pa, "slice_part", side_effect=SystemExit("CuraEngine failed:\nbad value")):
            pa.slice_one(cfg, cfg["entries"][0], 0, "c" * 40, self.dir, entry, models)
        self.assertEqual(entry, {"slice_error": "CuraEngine failed: bad value"})
        with unittest.mock.patch.object(pa, "slice_part", side_effect=KeyError("/home/secret")):
            pa.slice_one(cfg, cfg["entries"][0], 0, "c" * 40, self.dir, entry, models)
        self.assertEqual(entry, {"slice_error": "the slice failed: KeyError"})
        self.assertEqual(models, {})

    def test_cache(self):
        cfg = self.config(self.PARTS)
        p = cfg["entries"][0]
        cache = os.path.join(self.dir, "cache")
        os.makedirs(cache)
        open(os.path.join(cache, pa.part_digest(p) + ".gcode"), "w").write(";TIME:60\n")
        json.dump({"seconds": 60, "grams": 1.5, "layers": 4, "layer_height": 0.2, "slicer": "CuraEngine 5.13.0"},
                  open(os.path.join(cache, pa.part_digest(p) + ".json"), "w"))
        open(os.path.join(cache, "0" * 64 + ".gcode"), "w").write("old")
        entry, models = {"models": ["part1-1.stl"]}, {}
        with unittest.mock.patch.object(pa, "PART_CACHE", cache):
            pa.slice_one(cfg, p, 0, "c" * 40, self.dir, entry, models)
            pa.prune_cache({pa.part_digest(p)})
        self.assertEqual({k: entry[k] for k in ("seconds", "grams", "layers", "slice_file", "slice_size")},
                         {"seconds": 60, "grams": 1.5, "layers": 4, "slice_file": "part1.gcode", "slice_size": 9})
        self.assertEqual(sorted(models), ["part1.gcode"])
        self.assertEqual(sorted(os.listdir(cache)), sorted(pa.part_digest(p) + e for e in (".gcode", ".json")))
