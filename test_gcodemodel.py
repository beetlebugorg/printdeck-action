"""Tests of gcodemodel.py. Run: python3 -m unittest test_gcodemodel"""

import os
import tempfile
import unittest
import zipfile

import curaslice as c
import gcodemodel as g

PROFILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "testdata", "taz-workhorse-se")
NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"


def write(text):
    p = os.path.join(tempfile.mkdtemp(), "t.gcode")
    open(p, "w").write(text)
    return p


def box_mesh(x0, y0, z0, x1, y1, z1):
    """The vertices and the triangles of a box, as 3MF XML."""
    vs = [(x, y, z) for z in (z0, z1) for y in (y0, y1) for x in (x0, x1)]
    faces = [(0, 2, 1), (1, 2, 3), (4, 5, 6), (5, 7, 6), (0, 1, 4), (1, 5, 4),
             (2, 6, 3), (3, 6, 7), (0, 4, 2), (2, 4, 6), (1, 3, 5), (3, 7, 5)]
    vx = "".join(f'<vertex x="{x}" y="{y}" z="{z}"/>' for x, y, z in vs)
    tx = "".join(f'<triangle v1="{a}" v2="{b}" v3="{d}"/>' for a, b, d in faces)
    return f"<mesh><vertices>{vx}</vertices><triangles>{tx}</triangles></mesh>"


WINDOW = {"infill_mesh": "True", "infill_mesh_order": "1", "wall_line_count": "0", "wall_thickness": "0",
          "top_bottom_thickness": "0", "top_layers": "0", "bottom_layers": "0", "initial_bottom_layers": "0",
          "infill_pattern": "honeycomb", "infill_sparse_density": "20", "infill_line_distance": "2.53"}

PLATE = {"top_bottom_thickness": "0", "top_layers": "0", "bottom_layers": "0", "initial_bottom_layers": "0",
         "infill_sparse_density": "100", "infill_line_distance": "0.4", "infill_pattern": "lines"}


def plate_3mf(path, window=WINDOW):
    """A 40 x 30 x 1.6 mm plate, with a 30 x 20 mm window in its middle, in one group at 100, 100."""
    meta = "".join(f'<metadata name="cura:{k}">{v}</metadata>' for k, v in window.items())
    # An infill mesh changes only the infill of the plate, so the plate has no top or bottom skin.
    plate = "".join(f'<metadata name="cura:{k}">{v}</metadata>' for k, v in PLATE.items())
    model = (f'<model unit="millimeter" xmlns="{NS}"><resources>'
             f'<object id="1" type="model"><metadatagroup>{plate}</metadatagroup>{box_mesh(0, 0, 0, 40, 30, 1.6)}</object>'
             f'<object id="2" type="model"><metadatagroup>{meta}</metadatagroup>{box_mesh(5, 5, 0, 35, 25, 1.6)}</object>'
             '<object id="3" type="model"><components><component objectid="1"/><component objectid="2"/></components></object>'
             '</resources><build><item objectid="3" transform="1 0 0 0 1 0 0 0 1 100 100 0"/></build></model>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("3D/3dmodel.model", model)


class Lines(unittest.TestCase):
    def test_extrusions(self):
        p = write(";LAYER:0\nG0 Z0.3\n;TYPE:SKIRT\nG1 X10 Y0 E1\n;TYPE:WALL-OUTER\nG0 X0 Y0\n"
                  "G1 X5 Y0 E2\nG1 X10 Y0 E3\nG1 X10 Y5 E4\nG0 Z0.8\nG0 X20\nG0 Z0.3\nG1 E3.5\n"  # a hop and a retraction
                  ";LAYER:1\nG0 Z0.5\nG92 E0\nG1 X10 Y10 E1\nM83\nG1 X0 Y10 E0.5\nG1 X0 Y20 E-0.5\n")
        lines = g.lines_of(p)
        self.assertEqual(lines, [(0, 0, 5, 0, 0.3, 0.3), (5, 0, 10, 0, 0.3, 0.3), (10, 0, 10, 5, 0.3, 0.3),
                                 (20, 5, 10, 10, 0.5, 0.2), (10, 10, 0, 10, 0.5, 0.2)])
        # The two lines along X are one line.
        self.assertEqual(g.joined(lines)[0], (0, 0, 10, 0, 0.3, 0.3))
        self.assertEqual(len(g.joined(lines)), 4)

    def test_no_join_back(self):
        lines = [(0, 0, 10, 0, 0.3, 0.3), (10, 0, 5, 0, 0.3, 0.3)]
        self.assertEqual(g.joined(lines), lines)

    def test_box(self):
        tris = g.box(0, 0, 10, 0, 1.0, 0.25, 0.5)
        self.assertEqual(len(tris), 12)
        x0, x1, y0, y1, z0, z1 = c.bounds(tris)
        self.assertEqual((x0, x1, y0, y1, z0, z1), (-0.25, 10.25, -0.25, 0.25, 0.75, 1.0))

    def test_to_file(self):
        place = {"centre": (140, 140), "position": (10, -5), "shift": (-20, -15, 0), "scale": (1.01, 1.0)}
        x, y, z = g.to_file((150 + 1.01 * 4, 135 + 1.01 * -2, 1.5), place)
        self.assertAlmostEqual(x, 24)
        self.assertAlmostEqual(y, 13)
        self.assertAlmostEqual(z, 1.5)
        # A turn of 90 degrees: the point (4, -2) of the shifted mesh is at (2, 4) after the turn.
        place["turn"] = 90.0
        x, y, _ = g.to_file((150 + 1.01 * 2, 135 + 1.01 * 4, 1.5), place)
        self.assertAlmostEqual(x, 24)
        self.assertAlmostEqual(y, 13)

    def test_needs_model(self):
        d = tempfile.mkdtemp()
        plate_3mf(os.path.join(d, "a.3mf"))
        plate_3mf(os.path.join(d, "b.3mf"), {"infill_pattern": "grid"})
        self.assertTrue(g.needs_model(os.path.join(d, "a.3mf")))
        self.assertFalse(g.needs_model(os.path.join(d, "b.3mf")))
        self.assertFalse(g.needs_model(os.path.join(d, "c.stl")))
        self.assertTrue(g.truthy("=True"))


@unittest.skipUnless(os.environ.get("CURAENGINE"), "set CURAENGINE (and CURAENGINE_LIB) to slice")
class Slice(unittest.TestCase):
    def test_plate_with_window(self):
        d = tempfile.mkdtemp()
        src, out, stl = (os.path.join(d, n) for n in ("plate.3mf", "plate.gcode", "plate.stl"))
        plate_3mf(src)
        f = c.slice_plate(PROFILE, "pla-0.20-standard", "roll-4", [("Plate", [src], 1, {})], out)
        place = dict(f["places"][0], centre=f["centre"], scale=f["scale"])
        g.gcode_stl(out, stl, place, f["line_width"])
        tris = g.read_stl(stl)
        model = c.bounds([t for item in c.read_3mf(src) for m in item for t in m["triangles"]])
        got = c.bounds(tris)
        for a, b in zip(got, model):
            self.assertAlmostEqual(a, b, delta=0.3)
        # The window prints as infill with open cells: the lines cover less than half of it.
        lines = [ln for ln in g.joined(g.lines_of(out)) if ln[4] == min(x[4] for x in g.lines_of(out))]
        to = lambda x, y: g.to_file((x, y, 0), place)[:2]  # noqa: E731
        inside = 0.0
        for x0, y0, x1, y1, _, _ in lines:
            (a, b), (e, h) = to(x0, y0), to(x1, y1)
            if 108 < (a + e) / 2 < 132 and 108 < (b + h) / 2 < 122:  # the window, at 100 + 5 to 100 + 35
                inside += ((e - a) ** 2 + (h - b) ** 2) ** 0.5 * f["line_width"]
        self.assertGreater(inside, 10)
        self.assertLess(inside, 0.5 * 24 * 14)


if __name__ == "__main__":
    unittest.main()
