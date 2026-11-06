"""Tests of platepack. testdata/pack_cases.json has cases with their places. The Go copy of
platepack in Printdeck (deck/internal/cloud/platepack) checks the same file, so the plan page
shows the plates of the slice. PACK_CASES_WRITE=1 writes the file again from this code."""

import json
import math
import os
import unittest

import platepack as pp

HERE = os.path.dirname(os.path.abspath(__file__))
CASES = os.path.join(HERE, "testdata", "pack_cases.json")


def disc(d, n=120):
    return pp.outline([(d / 2 * math.cos(2 * math.pi * i / n), d / 2 * math.sin(2 * math.pi * i / n)) for i in range(n)])


def ell(w, d):
    """An L shape w x d with arms 10 mm wide."""
    return pp.outline([(0, 0), (w, 0), (w, 10), (10, 10), (10, d), (0, d)])


def cases():
    out = []

    def add(name, outlines, bed=(280, 280), margin=7.0):
        out.append({"name": name, "outlines": outlines, "bed": list(bed), "margin": margin})

    add("one part", [pp.rect_outline(50, 30)])
    add("one part too big", [pp.rect_outline(300, 30)])
    add("two discs on the diagonal", [disc(156.8), disc(151.2)])
    add("two discs too big", [disc(160), disc(158)])
    add("four cubes", [pp.rect_outline(20, 20)] * 4, margin=6.0)
    add("mixed boxes", [pp.rect_outline(120, 40), pp.rect_outline(60, 60), pp.rect_outline(30, 90), pp.rect_outline(15, 15)])
    add("long parts turn", [pp.rect_outline(250, 30), pp.rect_outline(250, 30), pp.rect_outline(30, 200)])
    add("ells", [ell(80, 60), ell(80, 60), ell(50, 50)], margin=4.0)
    add("small bed", [disc(40), pp.rect_outline(50, 20), disc(30)], bed=(120, 120), margin=3.0)
    add("twelve discs", [disc(60)] * 12, margin=3.0)
    add("thirty cubes in the grid", [pp.rect_outline(25, 25)] * 30)
    add("too many in the grid", [pp.rect_outline(25, 25)] * 200)
    for c in out:
        places, fit = pp.plate(c["outlines"], tuple(c["bed"]), c["margin"])
        c["places"], c["fit"] = (None if places is None else [list(p) for p in places]), fit
    return out


class Cases(unittest.TestCase):
    def test_cases(self):
        got = cases()
        if os.environ.get("PACK_CASES_WRITE"):
            with open(CASES, "w") as f:
                json.dump(got, f, indent=1)
        with open(CASES) as f:
            want = json.load(f)
        self.assertEqual([c["name"] for c in got], [c["name"] for c in want])
        for g, w in zip(got, want):
            self.assertEqual(g["fit"], w["fit"], g["name"])
            self.assertEqual(g["places"], w["places"], g["name"])

    def test_places_are_free(self):
        # Each place keeps the gap between the polygons, and the margin on the bed.
        for c in cases():
            if c["places"] is None:
                continue
            w, d = c["bed"]
            polys = []
            for h, (x, y, k) in zip(c["outlines"], c["places"]):
                t = pp.turned(h, k)
                bx = pp.box_of(t, (x, y), c["margin"])
                if len(c["outlines"]) <= pp.MAX_PACK:
                    self.assertGreaterEqual(bx[0], -w / 2 - 1e-3, c["name"])
                    self.assertLessEqual(bx[2], w / 2 + 1e-3, c["name"])
                    self.assertGreaterEqual(bx[1], -d / 2 - 1e-3, c["name"])
                    self.assertLessEqual(bx[3], d / 2 + 1e-3, c["name"])
                polys.append((t, (x, y)))
            for i in range(len(polys)):
                for j in range(i + 1, len(polys)):
                    (ha, pa), (hb, pb) = polys[i], polys[j]
                    hn = [ha[m] + hb[(m + pp.N // 2) % pp.N] + pp.GAP for m in range(pp.N)]
                    self.assertTrue(pp.outside((pb[0] - pa[0], pb[1] - pa[1]), [v - 1e-3 for v in hn]), (c["name"], i, j))

    def test_basket_pair(self):
        c = next(c for c in cases() if c["name"] == "two discs on the diagonal")
        self.assertEqual(c["fit"], 2)
        self.assertIsNone(next(c for c in cases() if c["name"] == "two discs too big")["places"])


if __name__ == "__main__":
    unittest.main()
