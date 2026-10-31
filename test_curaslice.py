"""Tests of curaslice.py. Run: python3 -m unittest test_curaslice (in this folder).

The values come from a slice of the Cura app 5.13 with the same profile (the pod door of
bittlex, PLA 0.10 Fine, roll 4). A slice with CuraEngine runs only when CURAENGINE is set.
"""

import os
import struct
import tempfile
import unittest
import zipfile

import curaslice as c

HERE = os.path.dirname(os.path.abspath(__file__))
PROFILE = os.path.join(HERE, "testdata", "taz-workhorse-se")
USER = {"adhesion_type": "brim", "support_enable": "True", "brim_line_count": "12"}


def cube(path, size=10.0):
    """A binary STL of a cube of size mm."""
    s = size
    v = [(0, 0, 0), (s, 0, 0), (s, s, 0), (0, s, 0), (0, 0, s), (s, 0, s), (s, s, s), (0, s, s)]
    faces = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6),
             (3, 0, 4), (3, 4, 7)]
    with open(path, "wb") as f:
        f.write(b"\0" * 80 + struct.pack("<I", len(faces)))
        for a, b, d in faces:
            f.write(struct.pack("<12fH", 0, 0, 0, *v[a], *v[b], *v[d], 0))


class Resolve(unittest.TestCase):
    def setUp(self):
        self.g, self.e, self.mat, _ = c.build(PROFILE, "pla-0.10-fine", "roll-4", USER)

    def test_profile_values(self):
        # From the quality changes, the user layer and the definition changes.
        self.assertEqual(self.e.get("layer_height"), 0.1)
        self.assertEqual(self.e.get("layer_height_0"), 0.25)
        self.assertEqual(self.e.get("speed_print"), 45.0)
        self.assertEqual(self.e.get("brim_line_count"), 12)
        self.assertEqual(self.g.get("adhesion_type"), "brim")
        self.assertEqual(self.g.get("machine_width"), 280.0)

    def test_material_reaches_the_global_stack(self):
        # extruderValues reads the extruder layers (the material), as Cura's getRawProperty.
        self.assertEqual(self.g.get("material_shrinkage_percentage_xy"), 99.74)
        # limit_to_extruder: the global value comes from the extruder of the outer wall.
        self.assertEqual(self.g.get("xy_offset"), -0.013)
        self.assertEqual(self.e.get("hole_xy_offset"), 0.249)

    def test_start_gcode(self):
        gs, _ = c.engine_settings(self.g, self.e, "LTAZWSE05MM_pod_door", self.mat)
        self.assertIn("PRINT_START BED=60 EXTRUDER=210 WIPE=160 LAYER=0.25 SPEED=18", gs["machine_start_gcode"])
        self.assertIn("M117 LTAZWSE05MM_pod_door", gs["machine_start_gcode"])

    def test_no_empty_values_to_the_engine(self):
        gs, es = c.engine_settings(self.g, self.e, "x", self.mat)
        self.assertIsNone(gs.get("extruder_nr"))  # slice_files leaves it out
        self.assertEqual(es.get("extruder_nr"), 0)


class Formulas(unittest.TestCase):
    def test_all_cura_formulas_pass(self):
        # Each formula of the definitions and of the profiles passes the check.
        for name in ("lulzbot_taz_workhorse_se", "lulzbot_taz_workhorse_se_extruder"):
            defs = c.load_definition(os.path.join(PROFILE, "definitions"), name)
            for k, p in defs.items():
                for prop in ("value", "resolve", "limit_to_extruder"):
                    if isinstance(p.get(prop), str):
                        c.Formula(p[prop])
        g, e, _, _ = c.build(PROFILE, "pla-0.10-fine", "roll-4", USER)
        gs, es = c.engine_settings(g, e, "x", {})
        self.assertGreater(len(gs), 600)

    def test_code_does_not_run(self):
        for bad in ("__import__('os').system('true')", "().__class__.__bases__", "open('/etc/passwd').read()",
                    "(lambda: 1)()", "getattr(math, '__dict__')", "math.__dict__", "[x for x in ().__class__.__mro__]",
                    "eval('1')", "str.join('', [])"):
            with self.assertRaises(ValueError, msg=bad):
                c.Formula(bad)
        self.assertEqual(eval(c.Formula("max(1, math.ceil(2.5))").code, {"math": __import__("math"), "max": max}), 3)


class Layout(unittest.TestCase):
    def test_grid_and_limit(self):
        tmp = tempfile.mkdtemp()
        p = os.path.join(tmp, "cube.stl")
        cube(p, 20)
        gs = {"machine_width": 280, "machine_depth": 280, "adhesion_type": "brim", "brim_line_count": 12, "line_width": 0.5}
        places = c.layout([(p, None)] * 4, gs)
        xs = sorted(x for _, (x, _) in places)
        self.assertEqual(len(set(xs)), 4)
        self.assertAlmostEqual(sum(xs), 0, places=6)  # the grid is at the middle
        with self.assertRaises(SystemExit) as e:
            c.layout([(p, None)] * 200, gs)
        self.assertIn("fit", str(e.exception))


class ThreeMF(unittest.TestCase):
    def test_group_with_a_blocker(self):
        tmp = tempfile.mkdtemp()
        p = os.path.join(tmp, "part.3mf")
        ns = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
        tri = '<vertices><vertex x="0" y="0" z="5"/><vertex x="10" y="0" z="5"/><vertex x="0" y="10" z="5"/></vertices>' \
              '<triangles><triangle v1="0" v2="1" v3="2"/></triangles>'
        model = (f'<model xmlns="{ns}"><resources><object id="1" type="model"><mesh>{tri}</mesh></object>'
                 '<object id="2" type="model"><metadatagroup><metadata name="cura:anti_overhang_mesh">True</metadata>'
                 f'</metadatagroup><mesh>{tri}</mesh></object><object id="3" type="model"><components>'
                 '<component objectid="1"/><component objectid="2" transform="1 0 0 0 1 0 0 0 1 20 0 0"/></components>'
                 '</object></resources><build><item objectid="3"/></build></model>')
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("3D/3dmodel.model", model)
        (group, size), = c.objects_of([p], tmp)
        self.assertEqual(size, (30.0, 10.0))
        self.assertEqual([s for _, s in group], [{}, {"anti_overhang_mesh": "True"}])


@unittest.skipUnless(os.environ.get("CURAENGINE"), "set CURAENGINE (and CURAENGINE_LIB) to slice")
class Slice(unittest.TestCase):
    def test_cube(self):
        tmp = tempfile.mkdtemp()
        p, out = os.path.join(tmp, "cube.stl"), os.path.join(tmp, "cube.gcode")
        cube(p, 20)
        g, e, mat, _ = c.build(PROFILE, "pla-0.20-standard", "roll-4", {})
        gs, es = c.engine_settings(g, e, "cube", mat)
        c.slice_files([p], gs, es, out, os.path.join(PROFILE, "definitions"), 1, [";Printdeck title: Cube"])
        f = c.facts(out)
        self.assertGreater(f["seconds"], 60)
        self.assertGreater(f["metres"], 0.1)
        self.assertAlmostEqual((f["minx"] + f["maxx"]) / 2, 140, delta=1)  # the middle of the bed
        self.assertIn(";Printdeck title: Cube", open(out).read(2000))


if __name__ == "__main__":
    unittest.main()
