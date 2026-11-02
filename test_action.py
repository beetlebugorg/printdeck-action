"""Tests of printdeck_action.py. Run: python3 -m unittest test_action"""

import io
import json
import os
import subprocess
import tempfile
import unittest
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
        p = os.path.join(d, "printdeck.yml")
        for text in ("parts: []\n", "parts:\n  - {name: A, printer: taz}\n",
                     "parts:\n  - {name: A, files: [a], printer: taz, profile: p/q}\n  - {name: a, files: [b], printer: taz, profile: p/q}\n",
                     "parts:\n  - {name: A, files: [a], printer: taz}\n"):
            open(p, "w").write(text)
            with self.assertRaises(SystemExit, msg=text):
                pa.load_config(p)


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

    def test_missing_file(self):
        cfg = self.config("assemblies:\n  - {name: S, items: [{name: D, file: asm/none.stl}]}\n")
        with self.assertRaises(SystemExit):
            pa.plan_assemblies(cfg, self.dir, {})

    def test_no_digest_change(self):
        a = self.config("")["entries"]
        b = self.config(self.GOOD)["entries"]
        self.assertEqual([pa.part_digest(p) for p in a], [pa.part_digest(p) for p in b])
