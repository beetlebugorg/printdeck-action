#!/usr/bin/env python3
"""Slice parts with CuraEngine and a Cura profile of profiles/, as the Cura app does. Python
standard library only.

    curaslice.py --profile profiles/taz-workhorse-se --quality pla-0.20-standard \\
        [--material roll-4] [--set key=value ...] [--count N] [--name JOB] \\
        --out part.gcode PART.stl [PART2.stl ...]

The Cura app works out the value of each setting before it calls CuraEngine. CuraEngine reads
only the values. This script does the same work, with the rules of Cura 5:

  - The stack of the printer, top first: the settings of the part (--set), the quality changes
    (the profile), the quality, the material (extruder only), the definition changes (the
    machine settings), and the definition with its parents.
  - The extruder stack falls through to the global stack. A setting that is not settable per
    extruder comes from the global stack.
  - A formula ("=..." in a profile, "value" in a definition) gets the values of the names in it
    from the stack that asked. The functions are those of Cura: extruderValue, extruderValues,
    resolveOrValue, defaultExtruderPosition, anyExtruderWithMaterial.
  - The global stack uses the "resolve" formula of a setting when the profile does not set it.
  - The start and end G-code get their {setting} tokens from the first extruder.

It prints JSON: the facts of the slice (time, filament, layers, size). The environment variable
CURAENGINE is the path of CuraEngine 5.13, with CURAENGINE_LIB for its libraries.
"""

import argparse
import ast
import configparser
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

# platepack is next to this file. python3 -I does not put the folder of the script on the path.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import platepack  # noqa: E402


# The parts of Python that the formulas of Cura use. A formula with any other part (an attribute
# other than math.<name>, a name with "__", a lambda, a call of another function) does not run:
# a profile of a pull request must not run code in the slice run.
SAFE_NODES = (ast.Expression, ast.BoolOp, ast.BinOp, ast.UnaryOp, ast.Compare, ast.IfExp, ast.Call, ast.Name, ast.Load,
              ast.Constant, ast.List, ast.Tuple, ast.Subscript, ast.Slice, ast.Attribute, ast.GeneratorExp, ast.ListComp,
              ast.comprehension, ast.Store, ast.operator, ast.boolop, ast.cmpop, ast.unaryop, ast.keyword)
SAFE_CALLS = {"min", "max", "abs", "round", "sum", "len", "int", "float", "str", "bool", "any", "all", "map", "list", "sorted",
              "extruderValue", "extruderValues", "resolveOrValue", "defaultExtruderPosition", "anyExtruderWithMaterial",
              "anyExtruderNrWithOrDefault", "valueFromContainer", "extruderValueFromContainer"}


def safe_attribute(n):
    """math.<name>, or index or count of a tuple or list of constants, as in the formula of
    adhesion_type: ('raft', 'brim', 'skirt', 'none').index."""
    if isinstance(n.value, ast.Name) and n.value.id == "math" and "__" not in n.attr:
        return True
    return (isinstance(n.value, (ast.Tuple, ast.List)) and n.attr in ("index", "count")
            and all(isinstance(e, ast.Constant) for e in n.value.elts))


# The functions of math that can run for a long time with a large integer.
SLOW_MATH = {"factorial", "comb", "perm", "prod", "isqrt", "lcm", "gcd"}


class FormulaError(ValueError):
    pass


def check_formula(tree):
    """Refuses a formula with a part of Python that Cura's formulas do not use."""
    for n in ast.walk(tree):
        if not isinstance(n, SAFE_NODES):
            raise FormulaError(f"a formula may not use {type(n).__name__}")
        if isinstance(n, ast.Name) and "__" in n.id:
            raise FormulaError(f"a formula may not use the name {n.id!r}")
        if isinstance(n, ast.Attribute) and not safe_attribute(n):
            raise FormulaError("a formula may use only the attributes of math, and index or count of a list of constants")
        if isinstance(n, ast.Attribute) and n.attr in SLOW_MATH:
            raise FormulaError(f"a formula may not use math.{n.attr}")
        if isinstance(n, ast.Call):
            f = n.func
            ok = (isinstance(f, ast.Name) and f.id in SAFE_CALLS) or (isinstance(f, ast.Attribute) and safe_attribute(f))
            if not ok:
                raise FormulaError("a formula may call only the functions of Cura and math")
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and "__" in n.value:
            raise FormulaError("a formula may not use a string with __")
        if isinstance(n, (ast.ListComp, ast.GeneratorExp)):
            # One loop for each comprehension, and no comprehension in another one: a formula
            # with nested loops can run for hours.
            if len(n.generators) > 1 or any(isinstance(m, (ast.ListComp, ast.GeneratorExp)) for m in ast.walk(n) if m is not n):
                raise FormulaError("a formula may have only one loop in a comprehension, and no comprehension in another one")


# The limits of the values of a formula. A profile of a pull request must not make the slice run
# use all the memory or all the time of the runner, for example with 10**10**9 or [0]*10**10.
MAX_BITS = 4096
MAX_ITEMS = 10000
SEQUENCES = (str, list, tuple)


def is_int(v):
    return isinstance(v, int)


def check_int(v):
    if is_int(v) and v.bit_length() > MAX_BITS:
        raise FormulaError(f"a formula may not make an integer of more than {MAX_BITS} bits")
    return v


def check_items(v):
    if isinstance(v, SEQUENCES) and len(v) > MAX_ITEMS:
        raise FormulaError(f"a formula may not make a list or a string of more than {MAX_ITEMS} items")
    return v


def op_pow(a, b):
    if is_int(a) and is_int(b) and b > 0 and abs(a) > 1 and (b > MAX_BITS or a.bit_length() * b > MAX_BITS * 2):
        raise FormulaError(f"a formula may not make an integer of more than {MAX_BITS} bits")
    return check_int(a ** b)


def op_mul(a, b):
    for seq, n in ((a, b), (b, a)):
        if isinstance(seq, SEQUENCES) and is_int(n) and len(seq) * max(n, 0) > MAX_ITEMS:
            raise FormulaError(f"a formula may not make a list or a string of more than {MAX_ITEMS} items")
    return check_int(a * b)


def op_lshift(a, b):
    if is_int(a) and is_int(b) and b > MAX_BITS:
        raise FormulaError(f"a formula may not make an integer of more than {MAX_BITS} bits")
    return check_int(a << b)


def op_add(a, b):
    if isinstance(a, SEQUENCES) and isinstance(b, SEQUENCES) and len(a) + len(b) > MAX_ITEMS:
        raise FormulaError(f"a formula may not make a list or a string of more than {MAX_ITEMS} items")
    return check_int(a + b)


def op_mod(a, b):
    # "%" of a string is a format, and "%0999999999d" makes a string of 1 GB.
    if isinstance(a, (str, bytes)):
        raise FormulaError("a formula may not format a string with %")
    return a % b


def bounded_sum(items, start=0):
    if not isinstance(start, (int, float)):
        raise FormulaError("a formula may add only numbers with sum")
    return check_int(sum(items, start))


OPERATIONS = {ast.Pow: "_pow", ast.Mult: "_mul", ast.LShift: "_lshift", ast.Add: "_add", ast.Mod: "_mod"}


class Bounded(ast.NodeTransformer):
    """Puts a call of a function with limits (op_pow, op_mul, ...) in place of each operator
    that can make a large value."""

    def visit_BinOp(self, n):
        self.generic_visit(n)
        name = OPERATIONS.get(type(n.op))
        if name is None:
            return n
        return ast.copy_location(ast.Call(func=ast.Name(id=name, ctx=ast.Load()), args=[n.left, n.right], keywords=[]), n)


class Formula:
    def __init__(self, expr):
        self.expr = expr.strip()
        tree = ast.parse(self.expr, mode="eval")
        check_formula(tree)
        self.names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        self.code = compile(ast.fix_missing_locations(Bounded().visit(tree)), "<setting>", "eval")

    def __repr__(self):
        return "=" + self.expr


# The paths of a profile.

class ProfileError(ValueError):
    pass


# A control character or a line break. A line break in a name adds a line to the G-code.
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
LINE_BREAK = re.compile(r"[\r\n\x0b\x0c\x1c-\x1e\x85\u2028\u2029]")


def one_line(text):
    """The text with a space in place of each line break."""
    return LINE_BREAK.sub(" ", str(text))


def within(folder, rel, what="a file of the profile"):
    """The real path of rel in folder, after a check that it stays in folder after its symbolic
    links. A profile of a pull request must not make the run read a file of the runner, for
    example /proc/self/environ or a link to it."""
    r = rel if isinstance(rel, str) else ""
    if not r or os.path.isabs(r) or "\\" in r or CONTROL.search(r) or ".." in r.split("/"):
        raise ProfileError(f"{what} must be a relative path in {os.path.basename(os.path.normpath(folder))}: {r!r}")
    root = os.path.realpath(folder)
    real = os.path.realpath(os.path.join(root, r))
    if not real.startswith(root + os.sep):
        raise ProfileError(f"{what} leaves {os.path.basename(os.path.normpath(folder))}: {r!r}")
    return real


def check_tree(folder):
    """Checks that each file and folder in folder stays in folder after its symbolic links, and
    that each file is a regular file. The run reads all the files of a profile folder for its
    version, and CuraEngine reads the definitions folder."""
    root = os.path.realpath(folder)
    for top, dirs, files in os.walk(folder):
        for n in dirs + files:
            p = os.path.join(top, n)
            real = os.path.realpath(p)
            if not real.startswith(root + os.sep):
                raise ProfileError(f"{os.path.relpath(p, folder)!r} leaves the profile folder")
            if n in files and not os.path.isfile(real):
                raise ProfileError(f"{os.path.relpath(p, folder)!r} is not a regular file")


def read_error(what, rel, e):
    """A ProfileError for a file that the run cannot read. It has the class of the error and no
    text of the error: the text of a parse error can have a line of the file."""
    detail = f": {one_line(e)}" if isinstance(e, (FormulaError, ProfileError)) else ""
    return ProfileError(f"{what}: the file {rel!r} could not be read ({type(e).__name__}{detail})")


READ_ERRORS = (OSError, ValueError, SyntaxError, KeyError, TypeError, AttributeError, configparser.Error, ET.ParseError,
               RecursionError)

DEFINITION = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]*")


# The definitions.

def load_definition(folder, name, depth=0):
    """The settings of the definition name and its parents: key -> properties. The definitions
    are in folder."""
    if not isinstance(name, str) or not DEFINITION.fullmatch(name) or ".." in name:
        raise ProfileError(f"a bad name of a definition: {name!r}")
    if depth > 20:
        raise ProfileError(f"the definition {name!r} has more than 20 parents")
    rel = name + ".def.json"
    path = within(folder, rel, "a definition")
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, dict):
            raise ValueError("not an object")
    except READ_ERRORS as e:
        raise read_error("the definitions", rel, e) from None
    out = load_definition(folder, d["inherits"], depth + 1) if d.get("inherits") else {}
    out = {k: dict(v) for k, v in out.items()}

    def walk(node):
        for k, v in node.items():
            if not isinstance(v, dict):
                continue
            props = {p: x for p, x in v.items() if p != "children"}
            out.setdefault(k, {}).update(props)
            if "children" in v:
                walk(v["children"])

    walk(d.get("settings", {}))
    for k, v in d.get("overrides", {}).items():
        out.setdefault(k, {}).update(v)
    return out


def typed(value, kind):
    """A value of a profile file as its type. A string with "=" first is a formula."""
    if isinstance(value, str) and value.startswith("="):
        return Formula(value[1:])
    if not isinstance(value, str):
        return value
    v = value.strip()
    try:
        if kind == "float":
            return float(v)
        if kind == "int":
            return int(float(v))
        if kind == "bool":
            return v.lower() in ("true", "1", "yes")
        if kind in ("[int]", "polygon", "polygons", "vec3"):
            return ast.literal_eval(v)
    except (ValueError, SyntaxError):
        return Formula(v)
    return value


def definition_container(defs):
    """The values of a definition: its formula, or its default value."""
    out = {}
    for k, p in defs.items():
        if "value" in p and isinstance(p["value"], str):
            out[k] = Formula(p["value"])
        elif "value" in p:
            out[k] = p["value"]
        elif "default_value" in p:
            out[k] = p["default_value"]
    return out


def read_instance(path, defs):
    """The values of a profile file (.inst.cfg)."""
    cp = configparser.ConfigParser(interpolation=None)
    cp.optionxform = str
    cp.read(path, encoding="utf-8")
    if not cp.has_section("values"):
        return {}
    return {k: typed(v, defs.get(k, {}).get("type")) for k, v in cp.items("values")}


# The names of the settings of a material file (as Cura's XmlMaterialProfile).
MATERIAL_SETTINGS = {
    "print temperature": "default_material_print_temperature",
    "heated bed temperature": "default_material_bed_temperature",
    "standby temperature": "material_standby_temperature",
    "processing temperature graph": "material_flow_temp_graph",
    "print cooling": "cool_fan_speed",
    "retraction amount": "retraction_amount",
    "retraction speed": "retraction_speed",
    "adhesion tendency": "material_adhesion_tendency",
    "surface energy": "material_surface_energy",
    "build volume temperature": "build_volume_temperature",
    "anti ooze retract position": "material_anti_ooze_retracted_position",
    "anti ooze retract speed": "material_anti_ooze_retraction_speed",
    "break preparation position": "material_break_preparation_retracted_position",
    "break preparation speed": "material_break_preparation_speed",
    "break preparation temperature": "material_break_preparation_temperature",
    "break position": "material_break_retracted_position",
    "flush purge speed": "material_flush_purge_speed",
    "flush purge length": "material_flush_purge_length",
    "end of filament purge speed": "material_end_of_filament_purge_speed",
    "end of filament purge length": "material_end_of_filament_purge_length",
    "maximum park duration": "material_maximum_park_duration",
    "no load move factor": "material_no_load_move_factor",
    "break speed": "material_break_speed",
    "break temperature": "material_break_temperature",
}


def read_material(path, defs):
    """The values of a material file, and its metadata. The settings for other machines (the
    <machine> blocks) stay out: they are for Ultimaker printers."""
    ns = {"m": "http://www.ultimaker.com/material", "cura": "http://www.ultimaker.com/cura"}
    root = ET.parse(path).getroot()
    vals = {}
    diameter = root.find("m:properties/m:diameter", ns)
    if diameter is not None:
        vals["material_diameter"] = float(diameter.text)
    settings = root.find("m:settings", ns)
    if settings is not None:
        for el in settings:
            tag = el.tag.split("}")[1]
            key = el.get("key")
            if tag == "setting" and el.tag.startswith("{" + ns["m"]) and key in MATERIAL_SETTINGS:
                k = MATERIAL_SETTINGS[key]
                vals[k] = typed(el.text, defs.get(k, {}).get("type"))
            elif tag == "setting" and el.tag.startswith("{" + ns["cura"]):
                vals[key] = typed(el.text, defs.get(key, {}).get("type"))
    meta = {}
    for k in ("brand", "material", "color"):
        el = root.find("m:metadata/m:name/m:" + k, ns)
        meta[k] = el.text.strip() if el is not None and el.text else ""
    el = root.find("m:metadata/m:GUID", ns)
    meta["guid"] = el.text.strip() if el is not None else ""
    return vals, meta


# The stacks.

class Stack:
    def __init__(self, name, containers, defs, nxt=None, position=None):
        self.name, self.containers, self.defs, self.next, self.position = name, containers, defs, nxt, position
        self.cache, self.resolving = {}, set()
        self.extruders = []

    def raw(self, key):
        for c in self.containers:
            if key in c:
                return c[key]
        return self.next.raw(key) if self.next else None

    def prop(self, key, p, default=None):
        return self.defs.get(key, {}).get(p, default)

    def get(self, key):
        if key in self.cache:
            return self.cache[key]
        if key not in self.defs:
            raise KeyError(key)
        limit = self.limit_extruder(key)
        if self.next is not None and not self.prop(key, "settable_per_extruder", True):
            v = self.next.get(key)
        elif self.next is None and self.should_resolve(key):
            self.resolving.add(key)
            try:
                v = self.evaluate(Formula(self.prop(key, "resolve")))
            finally:
                self.resolving.discard(key)
        elif limit is not None:
            v = limit.get(key)
        else:
            v = self.raw(key)
            if isinstance(v, Formula):
                v = self.evaluate(v)
        v = cast(v, self.prop(key, "type"))
        self.cache[key] = v
        return v

    def limit_extruder(self, key):
        """The extruder of a setting of the global stack with limit_to_extruder (for example
        xy_offset from the extruder of the outer wall), or None. -1 is the default extruder."""
        expr = self.prop(key, "limit_to_extruder")
        if self.next is not None or not expr or not self.prop(key, "settable_per_extruder", True) or not self.extruders:
            return None
        try:
            nr = int(self.evaluate(Formula(str(expr))))
        except (ValueError, TypeError):
            return None
        if nr < 0:
            nr = 0
        return self.extruders[nr] if nr < len(self.extruders) else None

    def raw_value(self, key):
        """The value of the first layer of this stack that has the key, as Cura's getRawProperty:
        with no move to the global stack. extruderValue and extruderValues read this."""
        v = self.raw(key)
        if isinstance(v, Formula):
            v = self.evaluate(v)
        return cast(v, self.prop(key, "type"))

    def should_resolve(self, key):
        """The global stack resolves a setting with a resolve formula, unless the user or the
        quality changes set it."""
        if not self.prop(key, "resolve") or key in self.resolving:
            return False
        return not any(key in c for c in self.containers[:2])

    def evaluate(self, f):
        g = dict(FUNCTIONS)
        g.update(self.functions())
        for n in f.names:
            if n in self.defs and n not in g:
                g[n] = self.get(n)
        try:
            return eval(f.code, g)
        except Exception as e:  # a formula that Cura would also fail on: no value
            raise ValueError(f"{self.name}: {f.expr}: {e}") from e

    def functions(self):
        glob = self if self.next is None else self.next
        exts = glob.extruders

        def ext(pos):
            pos = int(pos)
            return exts[0] if pos < 0 or pos >= len(exts) else exts[pos]

        return {
            "extruderValue": lambda pos, key: ext(pos).raw_value(key),
            "extruderValues": lambda key: [e.raw_value(key) for e in exts] or [glob.get(key)],
            "resolveOrValue": lambda key: glob.get(key),
            "defaultExtruderPosition": lambda: "0",
            "anyExtruderWithMaterial": lambda key: next((e.position for e in exts if e.get(key)), 0),
            "anyExtruderNrWithOrDefault": lambda key: next((e.position for e in exts if e.get(key)), 0),
            "valueFromContainer": lambda key, i: glob.get(key),
            "extruderValueFromContainer": lambda pos, key, i: ext(pos).get(key),
        }


FUNCTIONS = {"math": math, "min": min, "max": max, "abs": abs, "round": round, "sum": bounded_sum, "len": len, "int": int,
             "float": float, "str": str, "bool": bool, "any": any, "all": all, "map": map, "list": list, "sorted": sorted,
             "True": True, "False": False, "None": None, "__builtins__": {},
             "_pow": op_pow, "_mul": op_mul, "_lshift": op_lshift, "_add": op_add, "_mod": op_mod}


def cast(v, kind):
    if v is None:
        return v
    try:
        if kind == "float":
            return float(v)
        if kind == "int":
            return int(round(float(v)))
        if kind == "bool":
            return bool(v)
    except (TypeError, ValueError):
        pass
    return v


def engine_text(v):
    """A value as the Cura app sends it to CuraEngine: str() of the Python value."""
    return str(v)


TOKEN = re.compile(r"\{([^{}]+)\}")


def token_text(v):
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def expand(text, stack, extra):
    """The {key} and {key, extruder} tokens of the start and end G-code."""
    def one(m):
        body = m.group(1).strip()
        key, _, nr = body.partition(",")
        key = key.strip()
        st = stack
        if nr.strip():
            st = stack.next.extruders[int(nr.strip())] if stack.next else stack.extruders[int(nr.strip())]
        if key in extra:
            return str(extra[key])
        if key in st.defs:
            return token_text(st.get(key))
        try:
            return token_text(st.evaluate(Formula(body)))
        except Exception:
            return m.group(0)
    return TOKEN.sub(one, text)


def read_meta(profile_dir):
    """The content of cura.json of profile_dir."""
    check_tree(profile_dir)
    try:
        with open(within(profile_dir, "cura.json"), encoding="utf-8") as f:
            meta = json.load(f)
        if not isinstance(meta, dict):
            raise ValueError("not an object")
    except ProfileError:
        raise
    except READ_ERRORS as e:
        raise read_error(f"the profile {os.path.basename(os.path.normpath(profile_dir))}", "cura.json", e) from None
    return meta


def definitions_dir(profile_dir):
    return within(profile_dir, "definitions", "the definitions folder")


def build(profile_dir, quality, material=None, overrides=None):
    """The global stack and the extruder stack of a profile of profile_dir. Each file of the
    profile must stay in profile_dir, and each definition in profile_dir/definitions."""
    meta = read_meta(profile_dir)
    what = f"the profile {os.path.basename(os.path.normpath(profile_dir))}/{quality}"
    ddir = definitions_dir(profile_dir)
    gdefs = load_definition(ddir, meta.get("machine"))
    edefs_only = load_definition(ddir, meta.get("extruder"))
    edefs = {k: dict(v) for k, v in gdefs.items()}
    for k, v in edefs_only.items():
        edefs.setdefault(k, {}).update(v)
    q = (meta.get("profiles") or {}).get(quality)
    if not isinstance(q, dict):
        raise ProfileError(f"{what}: cura.json has no profile {quality!r}")

    def read(reader, rel):
        path = within(profile_dir, rel, f"a file of {what}")
        try:
            return reader(path, gdefs)
        except READ_ERRORS as e:
            raise read_error(what, rel, e) from None

    user = {k: typed(v, gdefs.get(k, {}).get("type")) for k, v in (overrides or {}).items()}
    glob = Stack("global", [
        {k: v for k, v in user.items() if not gdefs.get(k, {}).get("settable_per_extruder", True)},
        read(read_instance, q.get("quality_changes")),
        {},  # intent
        read(read_instance, q.get("quality")),
        {},  # material
        {},  # variant
        read(read_instance, meta.get("definition_changes")),
        definition_container(gdefs),
    ], gdefs)
    mvals, mmeta = {}, {}
    if material:
        m = (meta.get("materials") or {}).get(material)
        if not isinstance(m, dict):
            raise ProfileError(f"{what}: cura.json has no material {material!r}")
        mvals, mmeta = read(read_material, m.get("file"))
    ext = Stack("extruder 0", [
        {k: v for k, v in user.items() if gdefs.get(k, {}).get("settable_per_extruder", True)},
        read(read_instance, q.get("quality_changes_extruder")),
        {},
        {},
        mvals,
        {},
        read(read_instance, meta.get("definition_changes_extruder")),
        definition_container(edefs_only),
    ], edefs, nxt=glob, position=0)
    glob.extruders = [ext]
    return glob, ext, mmeta, meta


def engine_settings(glob, ext, jobname, mmeta):
    """The settings for CuraEngine: all the keys of each stack, and the expanded G-code."""
    extra = {"jobname": jobname, "material_name": mmeta.get("material", ""), "material_brand": mmeta.get("brand", ""),
             "material_type": mmeta.get("material", ""), "initial_extruder_nr": 0}
    # A name goes into the start G-code: a line break in it adds a line of G-code.
    extra = {k: one_line(v) for k, v in extra.items()}
    gs, es = {}, {}
    for k in glob.defs:
        if glob.defs[k].get("type") == "category":
            continue
        try:
            gs[k] = glob.get(k)
        except (ValueError, KeyError):
            continue
    for k in ext.defs:
        if ext.defs[k].get("type") == "category":
            continue
        try:
            es[k] = ext.get(k)
        except (ValueError, KeyError):
            continue
    for k in ("machine_start_gcode", "machine_end_gcode"):
        gs[k] = expand(str(gs.get(k, "")), ext, extra)
    for k in ("machine_extruder_start_code", "machine_extruder_end_code"):
        if k in es:
            es[k] = expand(str(es[k]), ext, extra)
    return gs, es


def engine_command():
    exe = os.environ.get("CURAENGINE") or "/opt/ultimaker-cura/CuraEngine"
    lib = os.environ.get("CURAENGINE_LIB")
    if lib:
        return ["/lib64/ld-linux-x86-64.so.2", "--library-path", lib, exe]
    return [exe]


NS3MF = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
# The limits of the build of a 3MF file. A component can be in the build many times, so a small
# file can make a large build.
MAX_3MF_DEPTH = 16
MAX_3MF_MESHES = 10000
MAX_3MF_TRIANGLES = 5_000_000


def read_3mf(path):
    """The objects of the build of a 3MF file: for each build item, a list of meshes, each with
    its triangles (in mm, with the transforms) and its Cura settings (metadata "cura:<key>",
    for example cura:anti_overhang_mesh for a support blocker)."""
    import zipfile
    path_name = os.path.basename(path)
    z = zipfile.ZipFile(path)
    name = next(n for n in z.namelist() if n.lower().endswith(".model"))
    root = ET.fromstring(z.read(name))
    objects = {}
    for ob in root.iter(NS3MF + "object"):
        settings = {}
        for md in ob.iter(NS3MF + "metadata"):
            key = md.get("name", "")
            if key.startswith("cura:"):
                settings[key[5:]] = (md.text or "").strip()
        mesh = ob.find(NS3MF + "mesh")
        tris = []
        if mesh is not None:
            vs = [(float(v.get("x")), float(v.get("y")), float(v.get("z"))) for v in mesh.iter(NS3MF + "vertex")]
            tris = [(vs[int(t.get("v1"))], vs[int(t.get("v2"))], vs[int(t.get("v3"))]) for t in mesh.iter(NS3MF + "triangle")]
        comps = [(c.get("objectid"), c.get("transform")) for c in ob.iter(NS3MF + "component")]
        objects[ob.get("id")] = (ob.get("name", ""), tris, settings, comps)

    def matrix(t):
        if not t:
            return None
        m = [float(x) for x in t.split()]
        return m

    def apply(m, p):
        if m is None:
            return p
        x, y, z = p
        return (x * m[0] + y * m[3] + z * m[6] + m[9], x * m[1] + y * m[4] + z * m[7] + m[10], x * m[2] + y * m[5] + z * m[8] + m[11])

    count = [0, 0]  # the meshes and the triangles of the build

    def flat(oid, mats, out, path=()):
        if oid not in objects:
            raise SystemExit(f"{path_name}: a component of no object {oid!r}")
        if len(path) >= MAX_3MF_DEPTH or oid in path:
            raise SystemExit(f"{path_name}: the components are more than {MAX_3MF_DEPTH} deep, or in a loop")
        name, tris, settings, comps = objects[oid]
        count[0] += 1
        count[1] += len(tris)
        if count[0] > MAX_3MF_MESHES or count[1] > MAX_3MF_TRIANGLES:
            raise SystemExit(f"{path_name}: the build has more than {MAX_3MF_MESHES} meshes or {MAX_3MF_TRIANGLES} triangles")
        if tris:
            pts = []
            for tri in tris:
                q = tri
                for m in mats:
                    q = tuple(apply(m, p) for p in q)
                pts.append(q)
            out.append({"name": name, "triangles": pts, "settings": settings})
        for cid, t in comps:
            flat(cid, [matrix(t)] + mats, out, path + (oid,))

    items = []
    for it in root.iter(NS3MF + "item"):
        out = []
        flat(it.get("objectid"), [matrix(it.get("transform"))], out)
        items.append(out)
    return items


def write_stl(path, triangles, dx=0.0, dy=0.0, dz=0.0):
    import struct
    with open(path, "wb") as f:
        f.write(b"\0" * 80 + struct.pack("<I", len(triangles)))
        for a, b, c in triangles:
            f.write(struct.pack("<12fH", 0, 0, 0, a[0] + dx, a[1] + dy, a[2] + dz, b[0] + dx, b[1] + dy, b[2] + dz,
                                c[0] + dx, c[1] + dy, c[2] + dz, 0))


def bounds(triangles):
    xs = [p[0] for t in triangles for p in t]
    ys = [p[1] for t in triangles for p in t]
    zs = [p[2] for t in triangles for p in t]
    return min(xs), max(xs), min(ys), max(ys), min(zs), max(zs)


def read_stl(path):
    """The triangles of an STL file (binary or text): ((x, y, z), (x, y, z), (x, y, z))."""
    import struct
    data = open(path, "rb").read()
    n = struct.unpack_from("<I", data, 80)[0] if len(data) >= 84 else 0
    out = []
    if len(data) == 84 + 50 * n:
        for i in range(n):
            v = struct.unpack_from("<12f", data, 84 + 50 * i)
            out.append((v[3:6], v[6:9], v[9:12]))
    else:
        vs = [tuple(float(x) for x in m.groups()) for m in re.finditer(rb"vertex\s+(\S+)\s+(\S+)\s+(\S+)", data)]
        out = [tuple(vs[i:i + 3]) for i in range(0, len(vs) - 2, 3)]
    if not out:
        raise SystemExit(f"{path}: no triangles")
    return out


def objects_of(files, tmp):
    """The objects to slice. An object is a group: (list of (STL path, Cura settings), its size).
    A plain STL file is a group of one mesh, and a 3MF build item a group of its meshes. The
    group goes to the middle of the bed and onto the bed, with the same offset for each mesh, as
    the Cura app places it. GROUP_OUTLINE has the outline of each group for the layout."""
    out = []
    for f in files:
        if not f.lower().endswith(".3mf"):
            tris = read_stl(f)
            x0, x1, y0, y1, z0, _ = bounds(tris)
            dx, dy, dz = -(x0 + x1) / 2, -(y0 + y1) / 2, -z0
            p = os.path.join(tmp, f"{os.path.splitext(os.path.basename(f))[0]}-{len(out)}.stl")
            write_stl(p, tris, dx, dy, dz)
            meshes = [(p, {})]
            out.append((meshes, (x1 - x0, y1 - y0)))
            GROUP_SHIFT[id(meshes)] = (dx, dy, dz)
            GROUP_OUTLINE[id(meshes)] = platepack.outline([(q[0], q[1]) for t in tris for q in t])
            continue
        for i, item in enumerate(read_3mf(f)):
            x0, x1, y0, y1, z0, _ = bounds([t for m in item for t in m["triangles"]])
            dx, dy, dz = -(x0 + x1) / 2, -(y0 + y1) / 2, -z0
            meshes = []
            for j, m in enumerate(item):
                p = os.path.join(tmp, f"{os.path.splitext(os.path.basename(f))[0]}-{i}-{j}.stl")
                write_stl(p, m["triangles"], dx, dy, dz)
                meshes.append((p, m["settings"]))
            out.append((meshes, (x1 - x0, y1 - y0)))
            GROUP_SHIFT[id(meshes)] = (dx, dy, dz)
            # The outline is of the meshes that print, about the middle of their box, as the plan
            # run makes it (model_stl): a support blocker can be out of the part. GROUP_OFFSET is
            # that middle in the coordinates of the group.
            pts = [(q[0] + dx, q[1] + dy) for m in item if str(m["settings"].get("anti_overhang_mesh", "")).lower() != "true"
                   for t in m["triangles"] for q in t]
            if pts:
                GROUP_OUTLINE[id(meshes)] = platepack.outline(pts)
                xs, ys = [q[0] for q in pts], [q[1] for q in pts]
                GROUP_OFFSET[id(meshes)] = ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)
    return out


# The shift that objects_of gives to the meshes of each group, the outline of the group
# (platepack), and the middle of the outline in the group, by id() of its list of meshes.
GROUP_SHIFT = {}
GROUP_OUTLINE = {}
GROUP_OFFSET = {}


def stl_size(path):
    """The X and Y size of an STL file (binary or text), in mm."""
    import struct
    data = open(path, "rb").read()
    xs, ys = [], []
    n = struct.unpack_from("<I", data, 80)[0] if len(data) >= 84 else 0
    if len(data) == 84 + 50 * n:
        for i in range(n):
            v = struct.unpack_from("<12f", data, 84 + 50 * i)
            xs += v[3::3][:3]
            ys += v[4::3][:3]
    else:
        for m in re.finditer(rb"vertex\s+(\S+)\s+(\S+)\s+(\S+)", data):
            xs.append(float(m.group(1)))
            ys.append(float(m.group(2)))
    if not xs:
        raise SystemExit(f"{path}: no triangles")
    return max(xs) - min(xs), max(ys) - min(ys)


def layout(meshes, gs):
    """The places of the objects (from objects_of): (x, y, k) for each, the offset from the
    middle of the bed and the turn in directions of platepack. platepack.plate places them, with
    room for the brim or the skirt of each on the bed. One object goes to the middle."""
    if len(meshes) == 1:
        return [(meshes[0], (0.0, 0.0, 0))]
    outlines = [GROUP_OUTLINE.get(id(obj)) or platepack.rect_outline(*size) for obj, size in meshes]
    bed = (float(gs.get("machine_width")), float(gs.get("machine_depth")))
    places, fit = platepack.plate(outlines, bed, plate_margin(gs))
    if places is None:
        raise SystemExit(f"{len(meshes)} objects do not fit on the bed: {fit} fit")
    out = []
    for (obj, size), (x, y, k) in zip(meshes, places):
        # platepack places the middle of the outline. The group goes by its own reference point.
        ox, oy = GROUP_OFFSET.get(id(obj), (0.0, 0.0))
        t = math.radians(platepack.turn_degrees(k))
        out.append(((obj, size), (x - (ox * math.cos(t) - oy * math.sin(t)), y - (ox * math.sin(t) + oy * math.cos(t)), k)))
    return out


def turned_stl(path, k, n):
    """A copy of the STL file path turned by k directions of platepack about Z, through the
    point 0, 0: the reference point of the group. n makes the name of the copy unique."""
    t = math.radians(platepack.turn_degrees(k))
    c, s = math.cos(t), math.sin(t)
    tris = [tuple((x * c - y * s, x * s + y * c, z) for x, y, z in tri) for tri in read_stl(path)]
    out = f"{os.path.splitext(path)[0]}-turn{n}.stl"
    write_stl(out, tris)
    return out


def profile_version(profile_dir):
    """The version of a profile folder: the first 12 hex digits of the SHA-256 of its files."""
    import hashlib
    h = hashlib.sha256()
    for root, _, files in sorted(os.walk(profile_dir)):
        for f in sorted(files):
            p = os.path.join(root, f)
            h.update(os.path.relpath(p, profile_dir).encode() + b"\0")
            h.update(open(p, "rb").read())
    return h.hexdigest()[:12]


def mesh_resolver(profile_dir, quality, material, plate, gs, es):
    """A function that gives the CuraEngine settings of one mesh from its own settings, as the
    Cura app gives them: the mesh settings on top of the stack, with each formula worked out. It
    returns each setting of the mesh, and each other per-mesh setting whose value changes, for
    example infill_line_distance for another infill_sparse_density. A value can be a formula
    ("=0", as the Cura app writes a 3MF file)."""
    cache = {}

    def resolve(mine):
        if not mine:
            return {}
        key = json.dumps(mine, sort_keys=True, default=str)
        if key in cache:
            return cache[key]
        glob, ext, _, _ = build(profile_dir, quality, material, {**plate, **mine})
        out = {}
        for k, d in ext.defs.items():
            if d.get("type") == "category" or not (k in mine or d.get("settable_per_mesh")):
                continue
            try:
                v = ext.get(k)
            except (ValueError, KeyError):
                if k in mine:
                    raise ProfileError(f"the setting {k} = {mine[k]!r} has no value")
                continue
            base = es.get(k, gs.get(k))
            if k in mine or engine_text(v) != engine_text(base):
                out[k] = v
        cache[key] = out
        return out

    return resolve


def slice_files(meshes, gs, es, out, defs_dir, count=1, lines=(), resolve=None):
    """Slices the files meshes (paths, or (path, settings) pairs: the per-mesh settings of a part)
    with the settings gs and es into out. resolve (mesh_resolver) gives the CuraEngine settings of
    a mesh from its own settings. With no resolve, the settings go to CuraEngine as they are."""
    args = engine_command() + ["slice", "-v", "-j", os.path.join(defs_dir, "fdmprinter.def.json")]
    # A setting with no value does not go to CuraEngine, as in the Cura app.
    for k, v in gs.items():
        if v is not None:
            args += ["-s", f"{k}={engine_text(v)}"]
    args += ["-e0"]
    for k, v in es.items():
        if v is not None:
            args += ["-s", f"{k}={engine_text(v)}"]
    # Each object goes to extruder 0. One object goes to the middle of the bed, as the Cura app
    # places one part. layout places more objects, and can turn them. Each group of objects_of is
    # centered already, with its meshes in place.
    tmp = tempfile.mkdtemp(prefix="curaslice-meshes-")
    pairs = [m if isinstance(m, tuple) else (m, {}) for m in meshes]
    objs = []
    for path, extra in pairs:
        for obj in objects_of([path], tmp):
            objs += [(obj, extra)] * count
    places = layout([o for o, _ in objs], gs)
    global LAST_PLACES
    LAST_PLACES = []
    for n, (((obj, size), (x, y, turn)), (_, extra)) in enumerate(zip(places, objs)):
        LAST_PLACES.append({"shift": GROUP_SHIFT.get(id(obj), (0.0, 0.0, 0.0)), "position": (x, y),
                            "turn": platepack.turn_degrees(turn)})
        for path, settings in obj:
            if turn:
                path = turned_stl(path, turn, n)
            args += ["-l", path, "-s", "extruder_nr=0", "-s", f"mesh_position_x={x:.3f}", "-s", f"mesh_position_y={y:.3f}"]
            # The settings of the mesh in its 3MF file are on top of the settings of the part.
            if resolve:
                mesh = resolve({**extra, **settings})
            else:
                mesh = {**{k: engine_text(v) for k, v in extra.items()}, **settings}
            for k, v in mesh.items():
                args += ["-s", f"{k}={engine_text(v)}"]
    args += ["-o", out]
    # The arguments can pass the limit of a command line: write them to a file of arguments.
    env = dict(os.environ, CURA_ENGINE_SEARCH_PATH=defs_dir)
    p = subprocess.run(args, env=env, capture_output=True, text=True, timeout=int(os.environ.get("SLICE_TIMEOUT", 1800)))
    if p.returncode != 0 or not os.path.exists(out):
        raise SystemExit("CuraEngine failed:\n" + (p.stderr or p.stdout)[-3000:])
    write_header(out, p.stdout + p.stderr, lines)
    global LAST_WARNINGS
    LAST_WARNINGS = engine_warnings(p.stdout + p.stderr)


# LAST_WARNINGS are the warnings of CuraEngine at the last slice, for the log of the Action.
LAST_WARNINGS = []
# LAST_PLACES has an entry for each object of the last slice: the shift of objects_of, the mesh
# position on the bed, and the turn in degrees about Z. gcodemodel uses them to put G-code back in
# the coordinates of the file.
LAST_PLACES = []


def engine_warnings(log):
    """The warnings and the errors of the log of CuraEngine, with no warnings of the
    definitions: a setting with no default value is a part of the definitions of Cura 5.13."""
    out = []
    for line in log.splitlines():
        if ("[warning]" in line or "[error]" in line) and "has no [default_]value" not in line:
            out.append(line.split("] ", 2)[-1].strip())
    return out[:20]


HEADER_KEYS = (";FLAVOR:", ";TIME:", ";Filament used:", ";Layer height:", ";MINX:", ";MINY:", ";MINZ:", ";MAXX:", ";MAXY:",
               ";MAXZ:", ";TARGET_MACHINE.NAME:")


def split_settings(defs, parts):
    """The settings of the parts of one plate: the settings that Cura allows for each mesh stay
    with their part, and the other settings go to the whole plate. Two parts with another value
    of a plate setting cannot share a plate."""
    plate, per = {}, []
    for name, settings in parts:
        mine = {}
        for k, v in (settings or {}).items():
            if k not in defs:
                raise SystemExit(f"{name}: no Cura setting {k}")
            if defs[k].get("settable_per_mesh"):
                mine[k] = v
            elif k in plate and str(plate[k][1]).lower() != str(v).lower():
                raise SystemExit(f"{name} and {plate[k][0]} need another {k}: {v} and {plate[k][1]}. Put them on separate plates.")
            else:
                plate[k] = (name, v)
        per.append(mine)
    return {k: v for k, (_, v) in plate.items()}, per


def slice_plate(profile_dir, quality, material, parts, out, name="plate", meta=()):
    """Slices one plate. parts: (name, files, count, settings) for each part. It returns the facts
    of the slice and the profile with its version."""
    defs = load_definition(definitions_dir(profile_dir), read_meta(profile_dir).get("machine"))
    plate, per = split_settings(defs, [(p[0], p[3]) for p in parts])
    glob, ext, mmeta, meta_json = build(profile_dir, quality, material, plate)
    gs, es = engine_settings(glob, ext, name, mmeta)
    version = profile_version(profile_dir)
    profile = f"{os.path.basename(os.path.normpath(profile_dir))}/{quality}@{version}"
    lines = []
    if mmeta:
        lines += [f";PRINTDECK_MATERIAL: {mmeta.get('material', '')}", f";PRINTDECK_MATERIAL_NAME: {mmeta.get('color', '')}"]
        code = meta_json["materials"][material].get("color_code")
        if code:
            lines.append(f";PRINTDECK_COLOR: {code}")
    lines += list(meta) + [f";Printdeck profile: {profile}"]
    # Each file of each part, with its copies, and the per-mesh settings of its part.
    meshes = []
    for (pname, files, count, _), mine in zip(parts, per):
        for f in files:
            meshes += [(f, mine)] * max(1, count)
    resolve = mesh_resolver(profile_dir, quality, material, plate, gs, es)
    slice_files(meshes, gs, es, out, definitions_dir(profile_dir), 1, lines, resolve)
    f = facts(out)
    if "metres" in f:
        r = float(es.get("material_diameter") or gs.get("material_diameter") or 2.85) / 2
        f["grams"] = round(math.pi * r * r * f["metres"] * 1000 * 1.24 / 1000, 1)
    f["profile"] = profile
    f["places"] = [dict(p) for p in LAST_PLACES]
    f["centre"] = (0.0, 0.0) if str(gs.get("machine_center_is_zero")).lower() == "true" else \
        (float(gs.get("machine_width")) / 2, float(gs.get("machine_depth")) / 2)
    f["scale"] = (float(es.get("material_shrinkage_percentage_xy") or gs.get("material_shrinkage_percentage_xy") or 100) / 100,
                  float(es.get("material_shrinkage_percentage_z") or gs.get("material_shrinkage_percentage_z") or 100) / 100)
    f["line_width"] = float(es.get("line_width") or gs.get("line_width") or 0.4)
    return f


def plate_margin(gs):
    """The room around each object for the brim or the skirt, in mm, as layout uses it."""
    lw = float(gs.get("skirt_brim_line_width") or gs.get("line_width") or 0.5)
    margin = 2.0
    if gs.get("adhesion_type") == "brim":
        margin += float(gs.get("brim_line_count") or 0) * lw
    elif gs.get("adhesion_type") == "skirt":
        margin += float(gs.get("skirt_gap") or 3) + float(gs.get("skirt_line_count") or 1) * lw
    return margin


def write_header(gcode, log, extra=()):
    """CuraEngine writes the header of the G-code before it slices, with no time and no
    filament. After the slice it logs the real header, which the Cura app writes into the file.
    This puts the logged header in place of the first one."""
    m = re.search(r"Gcode header after slicing: (.*?)(?=\n\[\d{4}-|\Z)", log, re.S)
    if not m:
        return
    header = [l for l in m.group(1).strip().splitlines() if l.startswith(";")]
    lines = open(gcode, encoding="utf-8", errors="replace").read().split("\n")
    n = 0
    while n < len(lines) and lines[n].startswith(HEADER_KEYS):
        n += 1
    while n < len(lines) and not lines[n].strip():
        n += 1
    # A line of extra with a line break adds a line of G-code.
    open(gcode, "w", encoding="utf-8").write("\n".join(header + [one_line(l) for l in extra] + lines[n:]))


def facts(gcode):
    out = {}
    for line in open(gcode, errors="ignore"):
        if not line.startswith(";"):
            if out.get("layers"):
                break
            continue
        for key, name, conv in ((";TIME:", "seconds", int), (";Filament used:", "metres", lambda s: float(s.rstrip("m"))),
                                (";Layer height:", "layer_height", float), (";LAYER_COUNT:", "layers", int)):
            if line.startswith(key):
                out[name] = conv(line[len(key):].strip())
        for ax in ("MINX", "MINY", "MINZ", "MAXX", "MAXY", "MAXZ"):
            if line.startswith(";" + ax + ":"):
                out[ax.lower()] = float(line.split(":", 1)[1])
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("meshes", nargs="*")
    ap.add_argument("--profile", required=True, help="a folder of profiles/")
    ap.add_argument("--quality", required=True, help="a profile of cura.json, for example pla-0.20-standard")
    ap.add_argument("--material", help="a material of cura.json, for example roll-4")
    ap.add_argument("--set", action="append", default=[], help="key=value: a setting of the part")
    ap.add_argument("--count", type=int, default=1, help="the copies of each file on the plate")
    ap.add_argument("--name", default="", help="the job name ({jobname} in the start G-code)")
    ap.add_argument("--out", help="the G-code file")
    ap.add_argument("--dump", help="write the settings for CuraEngine to this JSON file")
    ap.add_argument("--meta", action="append", default=[],
                    help='key=value: a line ";Printdeck key: value" in the header, for example title=Pod door')
    a = ap.parse_args()
    overrides = {}
    for kv in a.set:
        k, v = kv.split("=", 1)
        overrides[k] = v
    glob, ext, mmeta, meta = build(a.profile, a.quality, a.material, overrides)
    name = a.name or (os.path.splitext(os.path.basename(a.meshes[0]))[0] if a.meshes else "part")
    gs, es = engine_settings(glob, ext, name, mmeta)
    if a.dump:
        json.dump({"global": {k: engine_text(v) for k, v in gs.items()}, "extruder": {k: engine_text(v) for k, v in es.items()}},
                  open(a.dump, "w"), indent=1, sort_keys=True)
    if not a.meshes:
        return
    out = a.out or os.path.join(tempfile.mkdtemp(prefix="curaslice-"), name + ".gcode")
    version = profile_version(a.profile)
    # The lines of the Printdeck plugin for Cura, which the deck reads (gcodemeta), and the
    # profile with its version.
    lines = []
    if mmeta:
        lines += [f";PRINTDECK_MATERIAL: {mmeta.get('material', '')}", f";PRINTDECK_MATERIAL_NAME: {mmeta.get('color', '')}"]
        code = meta["materials"][a.material].get("color_code")
        if code:
            lines.append(f";PRINTDECK_COLOR: {code}")
    for kv in a.meta:
        k, _, v = kv.partition("=")
        lines.append(f";Printdeck {k.strip()}: {v.strip()}")
    profile = f"{os.path.basename(os.path.normpath(a.profile))}/{a.quality}@{version}"
    lines.append(f";Printdeck profile: {profile}")
    slice_files(a.meshes, gs, es, out, definitions_dir(a.profile), a.count, lines)
    density = 1.24
    f = facts(out)
    if "metres" in f:
        r = float(gs.get("material_diameter") or es.get("material_diameter") or 2.85) / 2
        f["grams"] = round(math.pi * r * r * f["metres"] * 1000 * density / 1000, 1)
    print(json.dumps({"gcode": out, "profile": profile, "material": a.material, **f}, indent=1))


if __name__ == "__main__":
    main()
