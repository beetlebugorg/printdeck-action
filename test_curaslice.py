"""Tests of curaslice.py. Run: python3 -m unittest test_curaslice (in this folder).

The values come from a slice of the Cura app 5.13 with the same profile (the pod door of
bittlex, PLA 0.10 Fine, roll 4). A slice with CuraEngine runs only when CURAENGINE is set.
"""

import json
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


def disc(path, d, n=96, h=5.0):
    """A binary STL of a disc of diameter d mm, height h, with its middle at 100, 50."""
    import math
    r = d / 2
    ring = [(100 + r * math.cos(2 * math.pi * i / n), 50 + r * math.sin(2 * math.pi * i / n)) for i in range(n)]
    tris = []
    for i in range(n):
        a, b = ring[i], ring[(i + 1) % n]
        tris += [((100, 50, 0), (b[0], b[1], 0), (a[0], a[1], 0)), ((100, 50, h), (a[0], a[1], h), (b[0], b[1], h)),
                 ((a[0], a[1], 0), (b[0], b[1], 0), (b[0], b[1], h)), ((a[0], a[1], 0), (b[0], b[1], h), (a[0], a[1], h))]
    c.write_stl(path, tris)


class Layout(unittest.TestCase):
    GS = {"machine_width": 280, "machine_depth": 280, "adhesion_type": "brim", "brim_line_count": 12, "line_width": 0.5}

    def test_cubes_and_limit(self):
        tmp = tempfile.mkdtemp()
        p = os.path.join(tmp, "cube.stl")
        cube(p, 20)
        places = c.layout(c.objects_of([p] * 4, tmp), self.GS)
        pts = [(x, y) for _, (x, y, _) in places]
        self.assertEqual(len(set(pts)), 4)
        # The objects keep 2 mm between them, and their box is at the middle.
        for i in range(4):
            for j in range(i + 1, 4):
                self.assertGreaterEqual(max(abs(pts[i][0] - pts[j][0]), abs(pts[i][1] - pts[j][1])), 22 - 1e-6)
        self.assertAlmostEqual(min(x for x, _ in pts) + max(x for x, _ in pts), 0, places=2)
        self.assertAlmostEqual(min(y for _, y in pts) + max(y for _, y in pts), 0, places=2)
        with self.assertRaises(SystemExit) as e:
            c.layout(c.objects_of([p] * 200, tmp), self.GS)
        self.assertIn("fit", str(e.exception))

    def test_two_discs_on_the_diagonal(self):
        # Two discs of 157 and 151 mm with a 10-line brim: they fit only in opposite corners.
        tmp = tempfile.mkdtemp()
        a, b = os.path.join(tmp, "a.stl"), os.path.join(tmp, "b.stl")
        disc(a, 157)
        disc(b, 151)
        gs = dict(self.GS, brim_line_count=10)
        (_, (x1, y1, _)), (_, (x2, y2, _)) = c.layout(c.objects_of([a, b], tmp), gs)
        self.assertGreaterEqual(((x1 - x2) ** 2 + (y1 - y2) ** 2) ** 0.5, (157 + 151) / 2 + 2 - 0.2)
        m = c.plate_margin(gs)
        for x, y, r in ((x1, y1, 157 / 2), (x2, y2, 151 / 2)):
            self.assertLessEqual(max(abs(x), abs(y)) + r + m, 140 + 0.2)

    def test_turned_copy(self):
        tmp = tempfile.mkdtemp()
        p = os.path.join(tmp, "cube.stl")
        c.write_stl(p, [((10, 0, 0), (0, 5, 0), (0, 0, 1))])
        t = c.turned_stl(p, 16, 0)  # 90 degrees
        (a, b, d), = c.read_stl(t)
        for got, want in zip((a, b, d), ((0, 10, 0), (-5, 0, 0), (0, 0, 1))):
            for g, w in zip(got, want):
                self.assertAlmostEqual(g, w, places=4)


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


class MeshSettings(unittest.TestCase):
    def test_derived_values(self):
        g, e, mat, _ = c.build(PROFILE, "pla-0.20-standard", "roll-4", {})
        gs, es = c.engine_settings(g, e, "x", mat)
        resolve = c.mesh_resolver(PROFILE, "pla-0.20-standard", "roll-4", {}, gs, es)
        m = resolve({"infill_sparse_density": "40", "infill_pattern": "lines"})
        # The line distance follows the density, as in the Cura app: 0.5 mm * 100 / 40.
        self.assertAlmostEqual(m["infill_line_distance"], float(es["line_width"]) * 100 / 40)
        self.assertEqual(m["infill_pattern"], "lines")
        # A value of a 3MF file of the Cura app is a formula.
        m = resolve({"infill_mesh": "=True", "wall_line_count": "=0", "top_layers": "0"})
        self.assertEqual((m["infill_mesh"], m["wall_line_count"], m["top_layers"]), (True, 0, 0))
        self.assertEqual(resolve({}), {})


def profile_copy():
    """A copy of the test profile in a new folder, and the content of its cura.json."""
    import json
    import shutil
    d = os.path.join(tempfile.mkdtemp(), "taz")
    shutil.copytree(PROFILE, d)
    return d, json.load(open(os.path.join(d, "cura.json")))


def write_meta(d, meta):
    import json
    with open(os.path.join(d, "cura.json"), "w") as f:
        json.dump(meta, f)


class ProfilePaths(unittest.TestCase):
    def test_paths_stay_in_the_profile(self):
        d, meta = profile_copy()
        c.build(d, "pla-0.10-fine", "roll-4", {})
        for bad in ("/proc/self/environ", "../taz/quality/draft.inst.cfg", "quality/../../x", "", "a\nb"):
            m = json.loads(json.dumps(meta))
            m["profiles"]["pla-0.10-fine"]["quality_changes"] = bad
            write_meta(d, m)
            with self.assertRaises(c.ProfileError, msg=bad):
                c.build(d, "pla-0.10-fine", "roll-4", {})
        for bad in ("../../x", "/etc/x", "a/b"):
            m = json.loads(json.dumps(meta))
            m["machine"] = bad
            write_meta(d, m)
            with self.assertRaises(c.ProfileError, msg=bad):
                c.build(d, "pla-0.10-fine", "roll-4", {})

    def test_links_out_of_the_profile(self):
        outside = os.path.join(tempfile.mkdtemp(), "secret.inst.cfg")
        open(outside, "w").write("SECRET=1\n")
        for link in ("quality/draft.inst.cfg", "cura.json", "definitions"):
            d, _ = profile_copy()
            target = os.path.join(d, link)
            import shutil
            shutil.rmtree(target) if os.path.isdir(target) else os.remove(target)
            os.symlink(outside if link != "definitions" else os.path.dirname(outside), target)
            with self.assertRaises(c.ProfileError, msg=link):
                c.build(d, "pla-0.10-fine", "roll-4", {})

    def test_no_file_text_in_the_error(self):
        d, meta = profile_copy()
        open(os.path.join(d, "quality", "bad.inst.cfg"), "w").write("SECRET=hunter2\n")
        meta["profiles"]["pla-0.10-fine"]["quality"] = "quality/bad.inst.cfg"
        write_meta(d, meta)
        with self.assertRaises(c.ProfileError) as e:
            c.build(d, "pla-0.10-fine", "roll-4", {})
        self.assertNotIn("hunter2", str(e.exception))
        self.assertIn("MissingSectionHeaderError", str(e.exception))
        self.assertIsNone(e.exception.__cause__)
        self.assertTrue(e.exception.__suppress_context__)


class OneLine(unittest.TestCase):
    def test_names_add_no_lines_to_the_gcode(self):
        g, e, mat, _ = c.build(PROFILE, "pla-0.10-fine", "roll-4", USER)
        gs, _ = c.engine_settings(g, e, "job\nG28\r\nM104 S300 x", mat)
        self.assertIn("M117 job G28  M104 S300 x", gs["machine_start_gcode"])
        out = os.path.join(tempfile.mkdtemp(), "a.gcode")
        open(out, "w").write(";FLAVOR:Marlin\nG1 X1\n")
        c.write_header(out, "Gcode header after slicing: ;FLAVOR:Marlin\n", [";Printdeck title: A\nG28\rM104 S300"])
        self.assertEqual(open(out).read().split("\n"), [";FLAVOR:Marlin", ";Printdeck title: A G28 M104 S300", "G1 X1", ""])


class FormulaLimits(unittest.TestCase):
    def value(self, expr):
        return eval(c.Formula(expr).code, dict(c.FUNCTIONS))

    def test_large_values(self):
        for bad in ("10**10**9", "2**100000", "[0]*10**10", "'x'*10**9", "10**9*'x'", "1 << 10**9",
                    "'%099999999d' % 1", "sum([[0]*10000]*10000, [])", "(10**100)**100"):
            with self.assertRaises(ValueError, msg=bad):
                self.value(bad)

    def test_refused_parts(self):
        for bad in ("math.factorial(10**8)", "math.comb(10**8, 5000)", "math.prod([10**9] * 9999)",
                    "[x for x in [1] for y in [2]]", "[[y for y in [1]] for x in [2]]", "sum(sum(y for y in [1]) for x in [2])"):
            with self.assertRaises(ValueError, msg=bad):
                c.Formula(bad)

    def test_normal_values(self):
        self.assertEqual(self.value("2 ** 10 + 3 * 4 - 7 % 3"), 1035)
        self.assertEqual(self.value("[1, 2] * 2 + [3]"), [1, 2, 1, 2, 3])
        self.assertEqual(self.value("'a' + 'b' * 2"), "abb")
        self.assertEqual(self.value("sum(x * 2 for x in [1, 2, 3])"), 12)
        self.assertAlmostEqual(self.value("0.5 ** 2 * 1.5"), 0.375)


class ThreeMFLimits(unittest.TestCase):
    def write(self, objects, build):
        p = os.path.join(tempfile.mkdtemp(), "part.3mf")
        ns = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("3D/3dmodel.model", f'<model xmlns="{ns}"><resources>{objects}</resources><build>{build}</build></model>')
        return p

    TRI = ('<object id="1" type="model"><mesh><vertices><vertex x="0" y="0" z="0"/><vertex x="1" y="0" z="0"/>'
           '<vertex x="0" y="1" z="0"/></vertices><triangles><triangle v1="0" v2="1" v3="2"/></triangles></mesh></object>')

    def chain(self, n, fan=1):
        obs = [self.TRI]
        for i in range(2, n + 2):
            comps = "".join(f'<component objectid="{i - 1}"/>' for _ in range(fan))
            obs.append(f'<object id="{i}" type="model"><components>{comps}</components></object>')
        return self.write("".join(obs), f'<item objectid="{n + 1}"/>')

    def test_limits(self):
        self.assertEqual(len(c.read_3mf(self.chain(10))[0]), 1)
        for p in (self.chain(30),  # too deep
                  self.chain(15, fan=4),  # 4**15 copies of the mesh
                  self.write('<object id="1" type="model"><components><component objectid="1"/></components></object>',
                             '<item objectid="1"/>')):  # a loop
            with self.assertRaises(SystemExit):
                c.read_3mf(p)


if __name__ == "__main__":
    unittest.main()
