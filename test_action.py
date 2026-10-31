"""Tests of printdeck_action.py. Run: python3 -m unittest test_action"""

import io
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
            self.assertEqual(got, {("A", ""), ("C", ""), ("D", "fine")})
            fine = next(e for e in pa.load_config("printdeck.yml")["entries"] if e["variant"] == "fine")
            self.assertEqual((fine["files"], fine["settings"]), (["d_fine.stl"], {"adhesion_type": "brim", "brim_line_count": 8}))
            open("profiles/taz/cura.json", "w").write('{"x": 1}')  # the profile: all parts
            run("commit", "-q", "-am", "three")
            got = pa.changed_parts(pa.load_config("printdeck.yml"), "printdeck.yml", head, run("rev-parse", "HEAD"))
            self.assertEqual(got, {("A", ""), ("B", ""), ("C", ""), ("D", "fine"), ("D", "standard")})
            self.assertEqual(pa.changed_parts(pa.load_config("printdeck.yml"), "printdeck.yml", None, head), set())
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
