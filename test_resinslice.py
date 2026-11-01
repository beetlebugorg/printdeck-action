"""Tests of resinslice: a small .sl1 file with known layers goes to a .cbddlp, and the test reads
it back as the deck reads it (deck/internal/chitu/sliced.go)."""

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
