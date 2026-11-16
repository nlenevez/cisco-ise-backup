"""Convert API objects to stable, diffable files and back.

Apply normalises the target's copy with the same functions to detect changes.
"""

import copy
import hashlib
import hmac
import json
import re

POLICY_VOLATILE = {"id", "link", "hitCounts"}
MASKED = re.compile(r"^\*+$")


def dumps(obj):
    return json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def slug(name):
    s = re.sub(r"[^A-Za-z0-9 ._@+=-]+", "_", str(name)).strip(" ._") or "_"
    if s != name:
        # Different names can slug to the same file; a short hash keeps them
        # apart without making ordinary names unreadable.
        s = f"{s[:80]}~{hashlib.sha1(str(name).encode()).hexdigest()[:6]}"
    return s


def map_path(obj, path, fn):
    """Apply fn to every value at a dotted path; "x[]" iterates a list."""
    parts = path.split(".")

    def rec(o, i):
        if not isinstance(o, dict):
            return
        p = parts[i]
        many = p.endswith("[]")
        k = p[:-2] if many else p
        if o.get(k) is None:
            return
        if i == len(parts) - 1:
            o[k] = [fn(v) for v in o[k]] if many else fn(o[k])
        elif many:
            for v in o[k]:
                rec(v, i + 1)
        else:
            rec(o[k], i + 1)

    rec(obj, 0)


def strip_keys(obj, keys):
    if isinstance(obj, dict):
        return {k: strip_keys(v, keys) for k, v in obj.items() if k not in keys}
    if isinstance(obj, list):
        return [strip_keys(v, keys) for v in obj]
    return obj


# -- ERS objects ------------------------------------------------------------

def unwrap(doc):
    """{"NetworkDevice": {...}} -> ("NetworkDevice", {...})"""
    (root, inner), = doc.items()
    return root, inner


def rewrite_refs(inner, t, lookup, on_missing):
    """Replace every reference in inner using lookup(target_res, value).

    lookup returns the replacement or None; on_missing(ref, value) supplies
    the value to use instead (and records the problem).
    """
    for ref in t.refs:
        def one(v, ref=ref):
            if v in (None, ""):
                return v
            got = lookup(ref.target, v)
            return got if got is not None else on_missing(ref, v)

        if ref.mode == "csv":
            map_path(inner, ref.path, lambda s: ",".join(
                str(one(x.strip())) for x in str(s).split(",") if x.strip()))
        else:
            map_path(inner, ref.path, one)


def object_name(t, inner, fallback=None):
    if t.key:
        return "__".join(str(inner.get(k, "")) for k in t.key)
    return inner.get("name") or fallback


def extract_secrets(inner, t, fp_key=None, inline=False):
    """Move secret values out of inner. Returns {path: value}.

    Each secret becomes {"$secret": "set" | "hmac:<fp>" | "unavailable"}
    ("unavailable" when ISE returned it masked). inline=True leaves readable
    values in place and only replaces masked ones.
    """
    found = {}
    for path in t.secrets:
        def take(v, path=path):
            if v in (None, ""):
                return v
            v = str(v)
            if MASKED.match(v):
                return {"$secret": "unavailable"}
            if inline:
                return v
            found[path] = v
            if fp_key:
                fp = hmac.new(fp_key.encode(), v.encode(), "sha256").hexdigest()
                return {"$secret": "hmac:" + fp[:16]}
            return {"$secret": "set"}
        map_path(inner, path, take)
    return found


def inject_secrets(inner, t, values):
    """Put secret values back; drop placeholders that have no value.

    Returns the paths left without a value.
    """
    missing = []
    for path in t.secrets:
        def put(v, path=path):
            if isinstance(v, dict) and "$secret" in v:
                if path in values:
                    return values[path]
                missing.append(path)
                return None
            return v
        map_path(inner, path, put)
    _drop_nones(inner)
    return missing


def _drop_nones(d):
    if isinstance(d, dict):
        for k in [k for k, v in d.items() if v is None]:
            del d[k]
        for v in d.values():
            _drop_nones(v)
    elif isinstance(d, list):
        for v in d:
            _drop_nones(v)


def comparable(obj):
    """Copy with every secret placeholder made equal, for change detection."""
    if isinstance(obj, dict):
        if set(obj) == {"$secret"}:
            return "$secret"
        return {k: comparable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [comparable(v) for v in obj]
    return obj


def normalize_ers(doc, t, id_to_name, errors, fp_key=None, where="", inline=False):
    """Live ERS document -> (name, file document, secrets)."""
    doc = copy.deepcopy(doc)
    root, inner = unwrap(doc)
    oid = inner.get("id")

    def lookup(target, v):
        return id_to_name.get(target, {}).get(v)

    def missing(ref, v):
        errors.append({"where": where or f"ers/{t.res}/{oid}",
                       "error": f"{ref.path} references unknown {ref.target} {v}"})
        return f"<missing:{v}>"

    rewrite_refs(inner, t, lookup, missing)
    for k in ("id", "link") + tuple(t.volatile):
        inner.pop(k, None)
    inner = strip_keys(inner, {"link"})
    secrets = extract_secrets(inner, t, fp_key, inline)
    return object_name(t, inner), {root: inner}, secrets


def normalize_policy(obj):
    return strip_keys(copy.deepcopy(obj), POLICY_VOLATILE)


def condition_refs(cond):
    """Names of library conditions referenced anywhere inside cond."""
    out = []
    if isinstance(cond, dict):
        if cond.get("conditionType") == "ConditionReference" and cond.get("name"):
            out.append(cond["name"])
        for v in cond.values():
            out += condition_refs(v)
    elif isinstance(cond, list):
        for v in cond:
            out += condition_refs(v)
    return out


def resolve_condition_refs(cond, name_to_id, unresolved):
    """Set the target's id on every ConditionReference, by name."""
    if isinstance(cond, dict):
        if cond.get("conditionType") == "ConditionReference" and cond.get("name"):
            cid = name_to_id.get(cond["name"])
            if cid is None:
                unresolved.append(cond["name"])
            else:
                cond["id"] = cid
        for v in cond.values():
            resolve_condition_refs(v, name_to_id, unresolved)
    elif isinstance(cond, list):
        for v in cond:
            resolve_condition_refs(v, name_to_id, unresolved)
    return cond
