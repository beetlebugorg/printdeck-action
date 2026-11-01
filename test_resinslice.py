"""Tests of resinslice: a small .sl1 file with known layers goes to a .cbddlp, and the test reads
it back as the deck reads it (deck/internal/chitu/sliced.go)."""

import json
import os
import struct
import tempfile
import unittest
import zipfile
import zlib

import resinslice

W, H = 300, 200


def png_gray(rows):
    """An 8-bit gray PNG of rows, with filter 0 on each row."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    raw = b"".join(b"\x00" + bytes(r) for r in rows)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", len(rows[0]), len(rows), 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def layer(i):
    """A lit rectangle that moves to the right with each layer, and a lit pixel at (0, 0)."""
    rows = [bytearray(W) for _ in range(H)]
    for y in range(40, 160):
        for x in range(20 + 10 * i, 250 + 10 * i):
            if x < W:
                rows[y][x] = 255
    rows[0][0] = 200
    return rows


RESIN = dict(bed=(30, 20, 100), resolution=(W, H), mirror=True, layer_height=0.05,
             exposure=8, bottom_exposure=60, bottom_layers=1, transition_layers=1, light_off=1, bottom_light_off=2,
             lift=5, lift_speed=60, bottom_lift=6, bottom_lift_speed=30, retract_speed=120, bottom_retract_speed=90,
             rest_after_retract=3)


def decode(data, resx, resy):
    """The pixels of a 1-bit layer, as a list of rows of 0 and 1."""
    px = []
    for b in data:
        px.extend([b >> 7] * (b & 0x7F))
    assert len(px) == resx * resy, len(px)
    return [px[y * resx:(y + 1) * resx] for y in range(resy)]


class TestCbddlp(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.sl1 = os.path.join(self.dir, "plate.sl1")
        with zipfile.ZipFile(self.sl1, "w") as z:
            z.writestr("config.ini", "layerHeight = 0.05\nusedMaterial = 2.5\n")
            z.writestr("thumbnail/thumbnail400x400.png", png_gray(layer(0)))
            for i in range(4):
                z.writestr("plate%05d.png" % i, png_gray(layer(i)))
        self.out = os.path.join(self.dir, "plate.cbddlp")

    def test_round_trip(self):
        facts = resinslice.write_cbddlp(self.sl1, self.out, RESIN)
        self.assertEqual(facts["layers"], 4)
        self.assertEqual(facts["ml"], 2.5)
        data = open(self.out, "rb").read()
        u = lambda off: struct.unpack_from("<I", data, off)[0]
        f = lambda off: struct.unpack_from("<f", data, off)[0]
        self.assertEqual((u(0), u(4)), (resinslice.MAGIC_CBDDLP, 4))
        self.assertEqual((u(52), u(56), u(68), u(48)), (W, H, 4, 1))
        self.assertEqual(zlib.crc32(data[:-4]), u(len(data) - 4))
        # The slicer block: the name of the printer, the transition layers, the parameters of version 4.
        self.assertEqual((u(104), u(108)), (172, 76))
        name_at, name_len = u(200), u(204)
        self.assertEqual(data[name_at:name_at + name_len], b"Peopoly Phenom")
        self.assertEqual((u(232), u(236)), (1, 248))
        self.assertEqual(f(224), 3)
        notice_at, notice_len = u(248 + 72), u(248 + 76)
        self.assertTrue(data[notice_at:notice_at + notice_len].startswith(b"Layout and record format"))
        self.assertEqual(u(80), 1)
        self.assertAlmostEqual(f(28), 0.2, places=5)
        # The print parameters: the lifts and the volume.
        p = u(84)
        self.assertEqual(u(88), resinslice.PARAMS_SIZE)
        self.assertEqual(struct.unpack_from("<5f", data, p), (6, 30, 5, 60, 120))
        self.assertAlmostEqual(f(p + 20), 2.5, places=5)
        table = u(64)
        for i in range(4):
            z, light, off, addr, size = struct.unpack_from("<fffII", data, table + i * resinslice.LAYER_DEF)
            self.assertAlmostEqual(z, 0.05 * (i + 1), places=5)
            # Layer 0 is a bottom layer, and layer 1 the transition layer half way to the exposure.
            self.assertEqual((light, off), [(60, 2), (34, 1), (8, 1), (8, 1)][i])
            # The record before the data: the entry of the table, the sizes, the lift and the rest.
            rec = data[addr - resinslice.LAYER_RECORD:addr]
            self.assertEqual(rec[:36], data[table + i * 36:table + i * 36 + 36])
            total, lift, speed, _, _, retract = struct.unpack_from("<I5f", rec, 36)
            self.assertEqual((total, lift, speed, retract), (size + 84, 6, 30, 90) if i == 0 else (size + 84, 5, 60, 120))
            self.assertEqual(struct.unpack_from("<2f", rec, 76), (3, 255))
            got = decode(data[addr:addr + size], W, H)
            want = [[1 if v >= 128 else 0 for v in reversed(r)] for r in layer(i)]
            self.assertEqual(got, want, "layer %d" % i)
            self.assertTrue(all(b & 0x7F <= resinslice.MAX_RUN for b in data[addr:addr + size]))

    def test_no_mirror(self):
        resinslice.write_cbddlp(self.sl1, self.out, dict(RESIN, mirror=False))
        data = open(self.out, "rb").read()
        addr, size = struct.unpack_from("<II", data, struct.unpack_from("<I", data, 64)[0] + 12)
        self.assertEqual(struct.unpack_from("<I", data, 80)[0], 0)
        self.assertEqual(decode(data[addr:addr + size], W, H)[0][0], 1)

    def test_wrong_size(self):
        with self.assertRaises(ValueError):
            resinslice.write_cbddlp(self.sl1, self.out, dict(RESIN, resolution=(W + 1, H)))


if __name__ == "__main__":
    unittest.main()


PROFILE = {"printer": {"name": "Test", "bed": [60, 40, 100], "resolution": [W, H], "mirror": True, "margin": 2,
                       "prusaslicer": {"supports_enable": 1, "pad_enable": 0}},
           "profiles": {"p": dict({k: v for k, v in RESIN.items() if k not in ("bed", "resolution", "mirror")},
                                  name="Test resin", prusaslicer={"support_object_elevation": 3})}}

# A stand-in for PrusaSlicer: it writes an .sl1 with two layers (W x H), and the arguments.
FAKE_SLICER = """#!/usr/bin/env python3
import json, struct, sys, zipfile, zlib
args = sys.argv[1:]
out = args[args.index("--output") + 1]
def chunk(k, d): return struct.pack(">I", len(d)) + k + d + struct.pack(">I", zlib.crc32(k + d))
rows = b"".join(b"\\0" + bytes([255 if 100 < x < 200 else 0 for x in range(%d)]) for _ in range(%d))
png = (b"\\x89PNG\\r\\n\\x1a\\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", %d, %d, 8, 0, 0, 0, 0))
       + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))
with zipfile.ZipFile(out, "w") as z:
    z.writestr("config.ini", "layerHeight = 0.05\\nusedMaterial = 1.5\\n")
    z.writestr("p00000.png", png)
    z.writestr("p00001.png", png)
open(out + ".args", "w").write(json.dumps(args))
""" % (W, H, W, H)


class TestPlates(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.prof = os.path.join(self.dir, "prof")
        os.makedirs(self.prof)
        self.write(PROFILE)

    def write(self, doc):
        with open(os.path.join(self.prof, "resin.json"), "w") as f:
            json.dump(doc, f)

    def test_profile(self):
        r = resinslice.load_profile(self.prof, "p")
        self.assertEqual((r["bed"], r["resolution"], r["margin"]), ([60, 40, 100], [W, H], 2))
        self.assertEqual(r["prusaslicer"], {"supports_enable": "1", "pad_enable": "0", "support_object_elevation": "3"})
        ini = resinslice.prusaslicer_ini(r, {"pad_enable": True})
        self.assertIn("display_mirror_x = 0\n", ini)
        self.assertIn("pad_enable = 1\n", ini)
        self.assertIn(f"display_pixels_x = {W}\n", ini)
        for bad in ({"output_filename_format": "x"}, {"post_process": "rm"}, {"pad_enable": "1; rm"}):
            with self.assertRaises(resinslice.ProfileError, msg=bad):
                resinslice.prusaslicer_ini(r, bad)
        with self.assertRaises(resinslice.ProfileError):
            resinslice.load_profile(self.prof, "other")
        for k, v in (("exposure", 0), ("lift", 99), ("layer_height", None)):
            doc = json.loads(json.dumps(PROFILE))
            if v is None:
                del doc["profiles"]["p"][k]
            else:
                doc["profiles"]["p"][k] = v
            self.write(doc)
            with self.assertRaises(resinslice.ProfileError, msg=k):
                resinslice.load_profile(self.prof, "p")

    def test_layout(self):
        self.assertEqual(resinslice.plate_layout([(10, 10)], (276, 155), 4), [(138, 77.5)])
        three = resinslice.plate_layout([(40, 30)] * 3, (276, 155), 4)
        self.assertEqual([round(x) for x, _ in three], [90, 138, 186])
        self.assertTrue(all(y == 77.5 for _, y in three))
        with self.assertRaises(resinslice.ProfileError):
            resinslice.plate_layout([(300, 10)], (276, 155), 4)
        with self.assertRaises(resinslice.ProfileError):
            resinslice.plate_layout([(100, 100)] * 4, (276, 155), 4)

    def test_slice_plate(self):
        fake = os.path.join(self.dir, "slicer")
        open(fake, "w").write(FAKE_SLICER)
        os.chmod(fake, 0o755)
        stl = os.path.join(self.dir, "a.stl")
        tri = ((0, 0, 2), (4, 0, 2), (0, 3, 6))
        with open(stl, "wb") as f:
            f.write(b"\0" * 80 + struct.pack("<I", 1) + struct.pack("<12fH", 0, 0, 0, *tri[0], *tri[1], *tri[2], 0))
        work = os.path.join(self.dir, "work")
        os.makedirs(work)
        out = os.path.join(self.dir, "plate.cbddlp")
        old = os.environ.get("PRUSASLICER")
        os.environ["PRUSASLICER"] = fake
        try:
            facts = resinslice.slice_plate(self.prof, "p", [("A", [stl], 2, {"pad_enable": 1})], out, work, log=lambda m: None)
        finally:
            if old is None:
                del os.environ["PRUSASLICER"]
            else:
                os.environ["PRUSASLICER"] = old
        self.assertEqual((facts["layers"], facts["ml"], facts["profile_name"]), (2, 1.5, "Test resin"))
        args = json.load(open(os.path.join(work, "plate.sl1.args")))
        self.assertEqual(args[:4], ["--export-sla", "--load", os.path.join(work, "printer.ini"), "--merge"])
        self.assertIn("pad_enable = 1\n", open(os.path.join(work, "printer.ini")).read())
        # Two copies, each moved to its place: the middle of the copy at its place, at Z 0.
        xs = []
        for i in (1, 2):
            data = open(os.path.join(work, f"object-{i}.stl"), "rb").read()
            v = struct.unpack_from("<12f", data, 84)
            self.assertEqual((v[5], v[8], v[11]), (0, 0, 4))
            xs.append((v[3] + v[6]) / 2)
        self.assertEqual([round(x, 1) for x in xs], [26.0, 34.0])
        self.assertEqual(open(out, "rb").read()[:4], struct.pack("<I", resinslice.MAGIC_CBDDLP))


@unittest.skipUnless(os.environ.get("PRUSASLICER"), "PRUSASLICER is not set")
class TestRealSlice(unittest.TestCase):
    """A real slice with PrusaSlicer and the Phenom profile of testdata: two 10 mm cubes and a
    20 mm cube on one plate. PRINTDECK_ARTIFACTS keeps the .cbddlp for a check with the deck."""

    def test_phenom_plate(self):
        from test_curaslice import cube
        d = tempfile.mkdtemp()
        small, big = os.path.join(d, "small.stl"), os.path.join(d, "big.stl")
        cube(small, 10)
        cube(big, 20)
        work = os.path.join(d, "work")
        os.makedirs(work)
        out = os.path.join(os.environ.get("PRINTDECK_ARTIFACTS") or d, "cubes.cbddlp")
        prof = os.path.join(os.path.dirname(os.path.abspath(__file__)), "testdata", "peopoly-phenom")
        facts = resinslice.slice_plate(prof, "abs-like-0.05", [("Small", [small], 2, {}), ("Big", [big], 1, {})], out, work)
        print(facts)
        self.assertGreater(facts["layers"], 400)  # 20 mm, with the supports and the pad under it
        data = open(out, "rb").read()
        u = lambda off: struct.unpack_from("<I", data, off)[0]
        rx, ry, table, n = u(52), u(56), u(64), u(68)
        self.assertEqual((rx, ry, u(80)), (3840, 2160, 1))
        # A layer 15 mm under the top of the big cube, in all three cubes: the big cube is on the
        # right of the plate as the slicer shows it, and the screen has the mirror (projector 1).
        i = n - 1 - int(15 / 0.05)
        addr, size = struct.unpack_from("<II", data, table + 36 * i + 12)
        rows = decode(data[addr:addr + size], rx, ry)
        lit = [x for x in range(rx) if rows[ry // 2][x]]
        px = 276.48 / rx
        groups, start = [], lit[0]
        for a, b in zip(lit, lit[1:] + [None]):
            if b is None or b - a > 1:
                groups.append(((rx - 1 - a) * px, (rx - 1 - start) * px))  # back to the view of the slicer, in mm
                start = b
        groups.sort()
        widths = [round(hi - lo) for lo, hi in groups]
        self.assertEqual(widths, [10, 10, 20], groups)
