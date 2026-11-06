"""The layout of the objects of a plate. The plan page of Printdeck has the same layout in Go
(deck/internal/cloud/platepack), so it shows the same plate as the slice. The two must give the
same places: testdata/pack_cases.json has cases for both.

The outline of an object is its support function on N fixed directions: for each direction, the
largest distance of the object from its reference point (the middle of its box) in that
direction. These N lines make a convex polygon around the object, a little larger than the
object. A turn of the object by a multiple of 360/N degrees moves the values along the list, so
both languages turn an object in the same way.

The layout puts the largest object first. For each object and each turn, the free places are
the places where the object is on the bed and keeps gap mm from each object on the bed. The
candidates are the corners of the polygons of the free area. The layout picks the candidate
that gives the smallest box around all the objects, then the lowest Y, then the lowest X. At the
end, the box of the objects goes to the middle of the bed.

The brim or the skirt of each object (margin mm) stays on the bed. The brims of two objects can
merge: the objects keep only gap mm between them."""

import math

N = 64  # the directions of an outline
TURN = 4  # the turns are multiples of TURN directions: 22.5 degrees
EPS = 1e-6
GAP = 2.0  # the room between two objects, in mm
EDGE = 0.0  # the room at the edges of the bed, in mm

DIRS = [(round(math.cos(2 * math.pi * i / N), 12), round(math.sin(2 * math.pi * i / N), 12)) for i in range(N)]


def up(v):
    """v rounded up to 0.01 mm: the outlines go to the plan page with this precision."""
    return math.ceil(round(v * 100, 6)) / 100


def outline(points):
    """The outline of the points (x, y) about the middle of their box."""
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    return [up(max((x - cx) * c + (y - cy) * s for x, y in points)) for c, s in DIRS]


def rect_outline(w, d):
    """The outline of a box w x d about its middle."""
    return [up(abs(c) * w / 2 + abs(s) * d / 2) for c, s in DIRS]


def turned(h, k):
    """The outline h turned by k directions, counterclockwise."""
    return [h[(i - k) % N] for i in range(N)]


def turn_degrees(k):
    return 360.0 * k / N


def corners(h):
    """The corners of the polygon of the outline h: corner i is on the lines i and i + 1."""
    out = []
    for i in range(N):
        (a, b), (c, d) = DIRS[i], DIRS[(i + 1) % N]
        e, f = h[i], h[(i + 1) % N]
        det = a * d - b * c
        out.append(((e * d - b * f) / det, (a * f - e * c) / det))
    return out


def poly(h, at=(0.0, 0.0)):
    """The corners of the polygon of h at the point at, with no two corners at the same place."""
    out = []
    for x, y in corners(h):
        q = (at[0] + x, at[1] + y)
        if not out or abs(q[0] - out[-1][0]) > EPS or abs(q[1] - out[-1][1]) > EPS:
            out.append(q)
    while len(out) > 1 and abs(out[0][0] - out[-1][0]) <= EPS and abs(out[0][1] - out[-1][1]) <= EPS:
        out.pop()
    return out


def area(h):
    p = corners(h)
    return abs(sum(p[i][0] * p[(i + 1) % N][1] - p[(i + 1) % N][0] * p[i][1] for i in range(N))) / 2


def iround(v):
    return math.floor(v * 1000 + 0.5)


def outside(q, h):
    """True when the point q is not inside the polygon of the outline h (on its edge counts as
    outside)."""
    x, y = q
    return max(c * x + s * y - v for (c, s), v in zip(DIRS, h)) >= -EPS


def crossing(p1, p2, p3, p4):
    """The point where the segments p1 p2 and p3 p4 cross, or None."""
    d1x, d1y = p2[0] - p1[0], p2[1] - p1[1]
    d2x, d2y = p4[0] - p3[0], p4[1] - p3[1]
    den = d1x * d2y - d1y * d2x
    if abs(den) < 1e-12:
        return None
    t = ((p3[0] - p1[0]) * d2y - (p3[1] - p1[1]) * d2x) / den
    u = ((p3[0] - p1[0]) * d1y - (p3[1] - p1[1]) * d1x) / den
    if -EPS <= t <= 1 + EPS and -EPS <= u <= 1 + EPS:
        return (p1[0] + t * d1x, p1[1] + t * d1y)
    return None


def box_of(h, p, margin):
    """The box of the outline h at p, with the margin: (x0, y0, x1, y1)."""
    return (p[0] - h[N // 2] - margin, p[1] - h[3 * N // 4] - margin, p[0] + h[0] + margin, p[1] + h[N // 4] + margin)


def pack(outlines, bed, margin, gap=GAP, edge=EDGE):
    """The places of the objects with the outlines on a bed (W, D): for each object (x, y, k),
    the offset of its reference point from the middle of the bed and its turn in directions, or
    None for an object that does not fit. One object goes to the middle with no turn."""
    w, d = bed
    if len(outlines) == 1:
        h = outlines[0]
        fits = h[0] + h[N // 2] + 2 * margin <= w + EPS and h[N // 4] + h[3 * N // 4] + 2 * margin <= d + EPS
        return [(0.0, 0.0, 0) if fits else None]
    order = sorted(range(len(outlines)), key=lambda i: (-iround(area(outlines[i])), i))
    placed = []  # (index, turned outline, point)
    out = [None] * len(outlines)
    for i in order:
        best = None
        seen = set()
        for k in range(0, N, TURN):
            h = turned(outlines[i], k)
            # A turn that gives the same outline as a smaller turn gives the same places.
            if tuple(h) in seen:
                continue
            seen.add(tuple(h))
            x0, x1 = -w / 2 + edge + margin + h[N // 2], w / 2 - edge - margin - h[0]
            y0, y1 = -d / 2 + edge + margin + h[3 * N // 4], d / 2 - edge - margin - h[N // 4]
            if x0 > x1 + EPS or y0 > y1 + EPS:
                continue
            # The polygons where the reference point of the object cannot go: one for each
            # object on the bed.
            nfps = []
            for _, ha, pa in placed:
                hn = [ha[j] + h[(j + N // 2) % N] + gap for j in range(N)]
                cs = poly(hn, pa)
                bb = (min(x for x, _ in cs), min(y for _, y in cs), max(x for x, _ in cs), max(y for _, y in cs))
                nfps.append((pa, hn, cs, bb))
            rect = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
            cands = list(rect)
            for _, _, cs, bb in nfps:
                if bb[2] < x0 - EPS or bb[0] > x1 + EPS or bb[3] < y0 - EPS or bb[1] > y1 + EPS:
                    continue
                cands += cs
                for j in range(len(cs)):
                    a, b = cs[j], cs[(j + 1) % len(cs)]
                    for r in range(4):
                        q = crossing(a, b, rect[r], rect[(r + 1) % 4])
                        if q:
                            cands.append(q)
            for m in range(len(nfps)):
                for n in range(m + 1, len(nfps)):
                    bm, bn = nfps[m][3], nfps[n][3]
                    if bm[2] < bn[0] or bn[2] < bm[0] or bm[3] < bn[1] or bn[3] < bm[1]:
                        continue
                    cm, cn = nfps[m][2], nfps[n][2]
                    for j in range(len(cm)):
                        a, b = cm[j], cm[(j + 1) % len(cm)]
                        ex0, ex1 = min(a[0], b[0]), max(a[0], b[0])
                        ey0, ey1 = min(a[1], b[1]), max(a[1], b[1])
                        for l in range(len(cn)):
                            c, e = cn[l], cn[(l + 1) % len(cn)]
                            if max(c[0], e[0]) < ex0 or min(c[0], e[0]) > ex1 or max(c[1], e[1]) < ey0 or min(c[1], e[1]) > ey1:
                                continue
                            q = crossing(a, b, c, e)
                            if q:
                                cands.append(q)
            # The box of the objects on the bed, and the score of each candidate: the size of the
            # box with the object at the candidate. The first free candidate in the order of the
            # scores is the place of this turn.
            boxes = [box_of(ha, pa, margin) for _, ha, pa in placed]
            ux0 = min((b[0] for b in boxes), default=math.inf)
            uy0 = min((b[1] for b in boxes), default=math.inf)
            ux1 = max((b[2] for b in boxes), default=-math.inf)
            uy1 = max((b[3] for b in boxes), default=-math.inf)
            scored = []
            for q in cands:
                if q[0] < x0 - EPS or q[0] > x1 + EPS or q[1] < y0 - EPS or q[1] > y1 + EPS:
                    continue
                q = (min(max(q[0], x0), x1), min(max(q[1], y0), y1))
                b = box_of(h, q, margin)
                size = max(ux1, b[2]) - min(ux0, b[0]) + max(uy1, b[3]) - min(uy0, b[1])
                scored.append(((iround(size), iround(q[1]), iround(q[0]), k), q))
            scored.sort(key=lambda c: c[0])  # stable: the candidates of one score stay in their order
            for key, q in scored:
                if best is not None and key >= best[0]:
                    break
                if all(outside((q[0] - pa[0], q[1] - pa[1]), hn) for pa, hn, _, _ in nfps):
                    best = (key, h, q, k)
                    break
        if best is not None:
            _, h, q, k = best
            placed.append((i, h, q))
            out[i] = (q, k)
    if not placed:
        return [None] * len(outlines)
    bx = [box_of(h, q, margin) for _, h, q in placed]
    sx = -(min(b[0] for b in bx) + max(b[2] for b in bx)) / 2
    sy = -(min(b[1] for b in bx) + max(b[3] for b in bx)) / 2
    return [None if o is None else (iround(o[0][0] + sx) / 1000, iround(o[0][1] + sy) / 1000, o[1]) for o in out]


# MAX_PACK is the most objects for pack. A plate with more objects (many copies of a part) goes
# in the grid: pack needs too much time for it.
MAX_PACK = 12
GRID_EDGE = 10.0


def size_of(h):
    """The size (w, d) of the box of the outline h."""
    return h[0] + h[N // 2], h[N // 4] + h[3 * N // 4]


def grid(outlines, bed, margin):
    """The places of the earlier layout: the objects in rows, each in a cell of the size of the
    largest object with its margin, GRID_EDGE mm from the edges, and the grid at the middle. It
    returns the places (x, y, 0), or None, and the number of objects that fit."""
    w, d = bed
    sizes = [size_of(h) for h in outlines]
    cw = max(s[0] for s in sizes) + 2 * margin
    cd = max(s[1] for s in sizes) + 2 * margin
    cols = max(1, math.floor((w - 2 * GRID_EDGE) / cw + EPS))
    fit = cols * max(1, math.floor((d - 2 * GRID_EDGE) / cd + EPS))
    n = len(outlines)
    rows = math.ceil(n / cols)
    if rows * cd > d - 2 * GRID_EDGE + EPS:
        return None, min(fit, n)
    cols = min(cols, n)
    out = []
    for i in range(n):
        r, c = divmod(i, cols)
        out.append((iround((c - (cols - 1) / 2) * cw) / 1000, iround(((rows - 1) / 2 - r) * cd) / 1000, 0))
    return out, n


def plate(outlines, bed, margin):
    """The places (x, y, k) of the objects of a plate, or None, and the number of objects that
    fit. One object goes to the middle. Up to MAX_PACK objects go in pack, else in the grid; when
    pack does not place all the objects, the grid can."""
    n = len(outlines)
    if n == 0:
        return [], 0
    if n == 1:
        p = pack(outlines, bed, margin)
        return (p, 1) if p[0] is not None else (None, 0)
    if n <= MAX_PACK:
        p = pack(outlines, bed, margin)
        placed = sum(x is not None for x in p)
        if placed == n:
            return p, n
        g, fit = grid(outlines, bed, margin)
        if g is not None:
            return g, n
        return None, max(placed, fit)
    return grid(outlines, bed, margin)
