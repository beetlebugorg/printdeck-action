"""Tests of zslicer.py. Run: python3 -m unittest test_zslicer (in this folder).

testdata/taz-workhorse-se/zslice comes from "zslice import cura testdata/taz-workhorse-se -o
testdata/taz-workhorse-se/zslice". A slice with zslice runs only when ZSLICE is set: the path of
bin/zslice.mjs of the npm package.
"""

import hashlib
import io
import json
import os
import tarfile
import tempfile
import unittest
import zipfile
from unittest import mock

import curaslice
import zslicer as z
from test_curaslice import cube

HERE = os.path.dirname(os.path.abspath(__file__))
PROFILE = os.path.join(HERE, "testdata", "taz-workhorse-se")


class Profiles(unittest.TestCase):
    def test_files(self):
        got = z.profiles(PROFILE, "pla-0.20-standard", "roll-4")
        self.assertEqual([f for f, _ in got], ["-m", "-f", "-p"])
        self.assertTrue(got[1][1].endswith("filament/roll-4.toml"))

    def test_missing(self):
        with self.assertRaises(curaslice.ProfileError):
            z.profiles(PROFILE, "pla-0.20-standard", "roll-99")
        with self.assertRaises(curaslice.ProfileError):
            z.profiles(PROFILE, "no-such-quality", "roll-4")
        with self.assertRaises(curaslice.ProfileError):
            z.profiles(PROFILE, "../../etc/passwd", None)


class Token(unittest.TestCase):
    def test_take_token(self):
        with mock.patch.dict(os.environ, {"PRINTDECK_PACKAGES_TOKEN": "secret"}):
            z.take_token()
            self.assertNotIn("PRINTDECK_PACKAGES_TOKEN", os.environ)
        self.assertEqual(z._token, "secret")
        z._token = None

    def test_registry_only(self):
        z._token = "secret"
        try:
            with self.assertRaises(SystemExit) as e:
                z._curl("https://example.com/zslice.tgz")
            self.assertNotIn("secret", str(e.exception))
        finally:
            z._token = None


class Fetch(unittest.TestCase):
    def package(self, cache):
        """A package in the cache, as a download leaves it."""
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as t:
            data = b"// zslice\n"
            info = tarfile.TarInfo("package/bin/zslice.mjs")
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
        os.makedirs(cache)
        path = os.path.join(cache, f"zslice-{z.ZSLICE[0]}.tgz")
        open(path, "wb").write(buf.getvalue())
        return path, hashlib.sha256(buf.getvalue()).hexdigest()

    def test_checked(self):
        cache = os.path.join(tempfile.mkdtemp(), "c")
        path, sha = self.package(cache)
        with mock.patch.object(z, "ZSLICE", (z.ZSLICE[0], sha)):
            js = z.fetch(cache)
        self.assertTrue(os.path.isfile(js))

    def test_bad_hash(self):
        cache = os.path.join(tempfile.mkdtemp(), "c")
        path, _ = self.package(cache)
        with mock.patch.object(z, "ZSLICE", (z.ZSLICE[0], "0" * 64)), self.assertRaises(SystemExit):
            z.fetch(cache)
        self.assertFalse(os.path.exists(path))


class Plate(unittest.TestCase):
    def test_3mf(self):
        out = os.path.join(tempfile.mkdtemp(), "p.3mf")
        tris = [((0, 0, 0), (1, 0, 0), (0, 1, 0)), ((0, 0, 0), (1, 0, 0), (1, 0, 0))]
        z.write_3mf(out, [(tris, {"infill_sparse_density": 30, "a<b": 'x"y'}, (140, 140))])
        model = zipfile.ZipFile(out).read("3D/3dmodel.model").decode()
        self.assertIn('<metadata name="cura:infill_sparse_density">30</metadata>', model)
        self.assertIn('name="cura:a&lt;b"', model)
        self.assertIn('transform="1 0 0 0 1 0 0 0 1 140.0000 140.0000 0"', model)
        self.assertEqual(model.count("<triangle "), 1)  # the flat triangle goes

    def test_values(self):
        self.assertEqual(z.value_text(False), "false")
        self.assertEqual(z.value_text("a\nb"), "a b")


@unittest.skipUnless(os.environ.get("ZSLICE"), "ZSLICE is not set")
class Slice(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.a, self.b = os.path.join(self.tmp, "a.stl"), os.path.join(self.tmp, "b.stl")
        cube(self.a, 20)
        cube(self.b, 10)

    def test_one_part(self):
        out = os.path.join(self.tmp, "a.gcode")
        f = z.slice_plate(PROFILE, "pla-0.20-standard", "roll-4", [("A", [self.a], 1, {"support_enable": False})], out,
                          meta=[";Printdeck title: A"])
        self.assertGreater(f["seconds"], 60)
        self.assertGreater(f["grams"], 1)
        self.assertEqual(f["layer_height"], 0.2)
        self.assertGreater(f["layers"], 90)
        # One part goes to the middle of the bed (280 x 280).
        self.assertAlmostEqual((f["minx"] + f["maxx"]) / 2, 140, delta=1)
        self.assertAlmostEqual((f["miny"] + f["maxy"]) / 2, 140, delta=1)
        text = open(out).read()
        for line in (";TIME:", ";Filament used:", ";PRINTDECK_MATERIAL:", ";Printdeck title: A", ";Printdeck slicer: zslice"):
            self.assertIn(line, text)
        self.assertEqual(len(f["places"]), 1)

    def test_plate(self):
        # Two parts with copies; the parts share the brim, and have another infill.
        out = os.path.join(self.tmp, "p.gcode")
        f = z.slice_plate(PROFILE, "pla-0.20-standard", "roll-4",
                          [("A", [self.a], 2, {"adhesion_type": "brim", "infill_sparse_density": 40}),
                           ("B", [self.b], 1, {"adhesion_type": "brim", "infill_sparse_density": 10})], out)
        self.assertEqual(len(f["places"]), 3)
        self.assertGreater(f["maxx"] - f["minx"], 40)

    def test_bad_key(self):
        with self.assertRaises(SystemExit) as e:
            z.slice_plate(PROFILE, "pla-0.20-standard", "roll-4", [("A", [self.a], 1, {"adhesion_type": "raft"})],
                          os.path.join(self.tmp, "x.gcode"))
        self.assertIn("raft", str(e.exception))


if __name__ == "__main__":
    unittest.main()
