"""Rebuild an ISE deployment from config/.

Objects are matched by name (or key fields) and compared after the same
normalisation export uses; equal objects are left alone, so re-runs are safe.
Without write=True nothing changes and actions are reported as a plan.

AD groups and AD conditions only validate after the join point joins the
domain, hence two stages:

  objects  every ERS object (AD join point without its groups), trusted CAs
  (operator joins AD here: `ise-config ad-join`, or the GUI)
  policy   AD groups, library conditions, policy sets and their rules
"""

import copy
import os
import secrets as pysecrets
import string

from .client import ISEError
from .export import Resolving, load_json
from .normalize import (comparable, condition_refs, inject_secrets,
                        normalize_ers, normalize_policy, object_name,
                        resolve_condition_refs, rewrite_refs, unwrap)
from .registry import BY_RES, ERS_TYPES, OPTIONAL_KINDS, POLICY_SERVICES, RULE_KINDS

PLANNED = "<planned>"

# The trusted-certificate list reports usage as one string, e.g.
# "Infrastructure,Endpoints,AdminAuth"; import takes one flag per usage.
TRUSTED_FOR = {"Infrastructure": "trustForIseAuth",
               "Endpoints": "trustForClientAuth",
               "AdminAuth": "trustForCertificateBasedAdminAuth",
               "Cisco Services": "trustForCiscoServicesAuth"}


def _files(path):
    if not os.path.isdir(path):
        return []
    return [load_json(os.path.join(path, f))
            for f in sorted(os.listdir(path)) if f.endswith(".json")]


def _password():
    # Meets ISE's default internal-user password policy.
    alphabet = string.ascii_letters + string.digits
    core = "".join(pysecrets.choice(alphabet) for _ in range(20))
    return "Aa1!" + core


class Applier:
    def __init__(self, client, cfgdir, secret_values=None, write=False,
                 prune=False, log=print, inline=True):
        self.c = client
        self.dir = cfgdir
        self.secrets = secret_values or {}
        self.write = write
        self.prune = prune
        self.inline = inline
        self.log = log
        self.counts = {}
        self.failures = []
        self.warnings = []
        self.generated = {}       # internal user -> generated password
        self.name_to_id = {}      # res -> {name: id}
        self.id_to_name = {}      # res -> {id: name}
        self.planned = {}         # res -> {names that would be created}

    # -- bookkeeping -------------------------------------------------------

    def act(self, action, where, detail=""):
        self.counts[action] = self.counts.get(action, 0) + 1
        if action != "unchanged":
            self.log(f"{'' if self.write else '[plan] '}{action:9} {where}"
                     + (f"  ({detail})" if detail else ""))

    def fail(self, where, why):
        self.failures.append(f"{where}: {why}")
        self.log(f"FAILED    {where}: {why}")

    def warn(self, where, why):
        self.warnings.append(f"{where}: {why}")
        self.log(f"WARNING   {where}: {why}")

    # -- ERS ----------------------------------------------------------------

    def _index(self, t):
        """List the target's objects of type t and refresh the id maps.

        Returns {name: id}. For key-named types every object is fetched so
        its key can be computed the same way export computes it.
        """
        listed = self.c.ers_list(t.res)
        self.id_to_name[t.res] = Resolving(self.c, t.res, {r["id"]: r.get("name") for r in listed})
        if t.key:
            idx = {}
            for r in listed:
                name, _, _ = normalize_ers(self.c.ers_get(t.res, r["id"]), t,
                                           self.id_to_name, [])
                idx[name] = r["id"]
        else:
            idx = {r.get("name"): r["id"] for r in listed}
        self.name_to_id[t.res] = {r.get("name"): r["id"] for r in listed}
        return idx

    def _lookup(self, target, name):
        got = self.name_to_id.get(target, {}).get(name)
        if got is None and isinstance(name, str) and not name.startswith("<missing:"):
            # Hidden built-ins (the ANY SGT) are absent from lists.
            try:
                got = self.c.ers_id_by_name(target, name)
                self.name_to_id.setdefault(target, {})[name] = got
            except ISEError:
                got = None
        if got is None and not self.write and name in self.planned.get(target, ()):
            return PLANNED
        return got

    @staticmethod
    def _ordered(t, docs):
        if t.res == "networkdevicegroup":
            # Hierarchy is encoded in the name: "Location#All Locations#Site".
            return sorted(docs, key=lambda d: unwrap(d)[1].get("name", "").count("#"))
        if t.parent:
            # Parents before children, by repeated passes.
            names = {unwrap(d)[1].get("name") for d in docs}
            done, out, rest = set(), [], list(docs)
            while rest:
                ready = [d for d in rest
                         if unwrap(d)[1].get(t.parent) not in names
                         or unwrap(d)[1].get(t.parent) in done]
                if not ready:
                    ready = rest
                for d in ready:
                    out.append(d)
                    done.add(unwrap(d)[1].get("name"))
                rest = [d for d in rest if d not in ready]
            return out
        return docs

    def ers(self, only=None, defer=True):
        for t in ERS_TYPES:
            if only and t.res not in only:
                continue
            docs = _files(os.path.join(self.dir, "ers", t.res))
            try:
                idx = self._index(t)
            except ISEError as e:
                if docs:
                    self.fail(f"ers/{t.res}", f"cannot list on target: {e}")
                continue
            if t.lookup_only or t.list_only or not docs:
                continue
            for doc in self._ordered(t, docs):
                self._ers_one(t, doc, idx, defer)

    def _ers_one(self, t, doc, idx, defer):
        root, inner = unwrap(copy.deepcopy(doc))
        name = object_name(t, inner)
        where = f"ers/{t.res}/{name}"
        oid = idx.get(name)
        skip = set(t.defer_fields) if defer else set()
        for f in skip:
            inner.pop(f, None)

        if oid:
            try:
                cur_name, cur, _ = normalize_ers(self.c.ers_get(t.res, oid), t,
                                                 self.id_to_name, [], inline=self.inline)
            except ISEError as e:
                self.fail(where, f"cannot read target copy: {e}")
                return
            cur_root, cur_inner = unwrap(cur)
            for f in skip:
                cur_inner.pop(f, None)
            if comparable(cur_inner) == comparable(inner):
                self.act("unchanged", where)
                return

        unresolved = []

        def missing(ref, v):
            unresolved.append(f"{ref.path} -> {ref.target} {v!r}")
            return v

        rewrite_refs(inner, t, self._lookup, missing)
        if unresolved:
            self.fail(where, "unresolved references: " + "; ".join(unresolved))
            return
        lacking = inject_secrets(inner, t, self.secrets.get(where, {}))
        if t.res == "internaluser" and "password" in lacking and not oid:
            # ERS never returns internal user passwords: set a random one.
            pw = _password()
            inner["password"] = pw
            inner["changePassword"] = True
            self.generated[name] = pw
            lacking.remove("password")
        if lacking:
            self.warn(where, "no value for secret(s): " + ", ".join(lacking))

        body = {root: inner}
        if oid and t.no_update and not t.update_op:
            self.warn(where, "differs from config/ but ERS cannot update this "
                      "type; change it in the GUI")
        elif oid:
            inner["id"] = oid
            self.act("update", where)
            if self.write:
                try:
                    if t.update_op:
                        self.c.ers_put(f"{t.res}/{oid}/{t.update_op}", body)
                    else:
                        self.c.ers_update(t.res, oid, body)
                except ISEError as e:
                    self.fail(where, str(e))
        else:
            self.act("create", where)
            if self.write:
                try:
                    new = self.c.ers_create(t.res, body)
                except ISEError as e:
                    self.fail(where, str(e))
                    return
                idx[name] = new
                self.name_to_id.setdefault(t.res, {})[inner.get("name", name)] = new
                self.id_to_name.setdefault(t.res, {})[new] = inner.get("name", name)
            else:
                self.planned.setdefault(t.res, set()).add(inner.get("name", name))

    # -- Trusted certificates -----------------------------------------------

    def trusted_certs(self):
        docs = _files(os.path.join(self.dir, "system", "trusted-certificates"))
        if not docs:
            return
        try:
            have = {c.get("friendlyName") for c in self._paged("/certs/trusted-certificate")}
        except ISEError as e:
            self.fail("trusted-certificates", f"cannot list on target: {e}")
            return
        for d in docs:
            name = d.get("friendlyName")
            where = f"system/trusted-certificates/{name}"
            if d.get("internalCA"):
                # ISE's own CA chain: a fresh node generates its own.
                continue
            if name in have:
                self.act("unchanged", where)
                continue
            if not d.get("pem"):
                self.fail(where, "no PEM in export")
                continue
            body = {"name": name, "description": d.get("description") or "",
                    "data": d["pem"], "allowOutOfDateCert": True,
                    "allowBasicConstraintCAFalse": True}
            usages = [u.strip() for u in (d.get("trustedFor") or "").split(",")]
            for u in usages:
                if u in TRUSTED_FOR:
                    body[TRUSTED_FOR[u]] = True
                elif u:
                    self.warn(where, f"unknown trust usage {u!r} not carried over")
            self.act("create", where)
            if self.write:
                try:
                    self.c.api("POST", "/certs/trusted-certificate/import", body)
                except ISEError as e:
                    self.fail(where, str(e))

    def _paged(self, path, size=100):
        out, page = [], 1
        while True:
            got = self.c.api("GET", f"{path}?page={page}&size={size}") or []
            out += got
            if len(got) < size:
                return out
            page += 1

    # -- Policy -------------------------------------------------------------

    def policy(self):
        for svc in POLICY_SERVICES:
            root = os.path.join(self.dir, "policy", svc)
            if not os.path.isdir(root):
                continue
            base = f"/policy/{svc}"
            cond_ids = self._conditions(svc, base, root)
            if cond_ids is None:
                continue
            self._policy_sets(svc, base, root, cond_ids)
            ge = os.path.join(root, "global-exceptions.json")
            if os.path.exists(ge):
                self._rules(f"{base}/policy-set/global-exception", load_json(ge),
                            f"policy/{svc}/global-exceptions", cond_ids, True)

    def _resolve(self, obj, cond_ids, where):
        unresolved = []
        resolve_condition_refs(obj, cond_ids, unresolved)
        if unresolved:
            self.fail(where, "unknown library condition(s): " + ", ".join(unresolved))
            return False
        return True

    def _conditions(self, svc, base, root):
        cond_ids = {}
        for coll, sub in (("conditions", "condition"), ("time-conditions", "time-condition")):
            docs = _files(os.path.join(root, coll))
            try:
                target = {c["name"]: c for c in (self.c.api("GET", f"{base}/{sub}") or [])}
            except ISEError as e:
                if docs:
                    self.fail(f"policy/{svc}/{coll}", f"cannot list on target: {e}")
                    return None
                continue
            cond_ids.update({n: c["id"] for n, c in target.items()})
            names = {d["name"] for d in docs}

            # Library conditions can reference each other: dependencies first.
            done, rest, order = set(), list(docs), []
            while rest:
                ready = [d for d in rest
                         if all(r in done or r not in names for r in condition_refs(d))] or rest
                order += ready
                done |= {d["name"] for d in ready}
                rest = [d for d in rest if d not in ready]

            for d in order:
                where = f"policy/{svc}/{coll}/{d['name']}"
                tgt = target.get(d["name"])
                if tgt and comparable(normalize_policy(tgt)) == comparable(d):
                    self.act("unchanged", where)
                    continue
                body = copy.deepcopy(d)
                if not self._resolve(body, cond_ids, where):
                    continue
                if tgt:
                    body["id"] = tgt["id"]
                    self.act("update", where)
                    if self.write:
                        try:
                            self.c.api("PUT", f"{base}/{sub}/{tgt['id']}", body)
                        except ISEError as e:
                            self.fail(where, str(e))
                else:
                    self.act("create", where)
                    if self.write:
                        try:
                            resp = self.c.api("POST", f"{base}/{sub}", body) or {}
                            cond_ids[d["name"]] = resp.get("id")
                        except ISEError as e:
                            self.fail(where, str(e))
                    else:
                        cond_ids[d["name"]] = PLANNED
        return cond_ids

    def _policy_sets(self, svc, base, root, cond_ids):
        docs = sorted(_files(os.path.join(root, "policy-sets")),
                      key=lambda d: d["policySet"].get("rank", 0))
        try:
            target = self.c.api("GET", f"{base}/policy-set") or []
        except ISEError as e:
            self.fail(f"policy/{svc}/policy-sets", f"cannot list on target: {e}")
            return
        by_name = {s["name"]: s for s in target}
        default = next((s for s in target if s.get("default")), None)
        wanted = {d["policySet"]["name"] for d in docs}

        if self.prune:
            for s in target:
                if s["name"] not in wanted and not s.get("default"):
                    where = f"policy/{svc}/policy-sets/{s['name']}"
                    self.act("delete", where)
                    if self.write:
                        try:
                            self.c.api("DELETE", f"{base}/policy-set/{s['id']}")
                        except ISEError as e:
                            self.fail(where, str(e))

        for d in docs:
            ps = d["policySet"]
            where = f"policy/{svc}/policy-sets/{ps['name']}"
            tgt = default if ps.get("default") else by_name.get(ps["name"])
            sid = tgt["id"] if tgt else None
            if tgt and comparable(normalize_policy(tgt)) == comparable(ps):
                self.act("unchanged", where)
            else:
                body = copy.deepcopy(ps)
                if not self._resolve(body, cond_ids, where):
                    continue
                if tgt:
                    body["id"] = sid
                    self.act("update", where)
                    if self.write:
                        try:
                            self.c.api("PUT", f"{base}/policy-set/{sid}", body)
                        except ISEError as e:
                            self.fail(where, str(e))
                else:
                    self.act("create", where)
                    if self.write:
                        try:
                            sid = (self.c.api("POST", f"{base}/policy-set", body) or {}).get("id")
                        except ISEError as e:
                            self.fail(where, str(e))
                            continue
            for kind in RULE_KINDS:
                self._rules(f"{base}/policy-set/{sid}/{kind}" if sid else None,
                            d.get(kind, []), f"{where}/{kind}", cond_ids,
                            kind in OPTIONAL_KINDS)

    def _rules(self, path, rules, where, cond_ids, may_404):
        target = []
        if path:
            try:
                target = self.c.api("GET", path) or []
            except ISEError as e:
                if not (may_404 and e.status == 404):
                    self.fail(where, f"cannot list on target: {e}")
                    return
        by_name = {(r.get("rule") or {}).get("name"): r for r in target}
        default = next((r for r in target if (r.get("rule") or {}).get("default")), None)
        wanted = {(r.get("rule") or {}).get("name") for r in rules}

        # Prune first so leftover built-in rules do not push ranks around.
        if self.prune:
            for r in target:
                rr = r.get("rule") or {}
                if rr.get("name") not in wanted and not rr.get("default"):
                    self.act("delete", f"{where}/{rr.get('name')}")
                    if self.write:
                        try:
                            self.c.api("DELETE", f"{path}/{rr['id']}")
                        except ISEError as e:
                            self.fail(f"{where}/{rr.get('name')}", str(e))

        for r in sorted(rules, key=lambda r: (r.get("rule") or {}).get("rank", 0)):
            rr = r.get("rule") or {}
            w = f"{where}/{rr.get('name')}"
            tgt = default if rr.get("default") else by_name.get(rr.get("name"))
            if tgt and comparable(normalize_policy(tgt)) == comparable(r):
                self.act("unchanged", w)
                continue
            body = copy.deepcopy(r)
            if not self._resolve(body, cond_ids, w):
                continue
            if tgt:
                body["rule"]["id"] = tgt["rule"]["id"]
                self.act("update", w)
                if self.write:
                    try:
                        self.c.api("PUT", f"{path}/{tgt['rule']['id']}", body)
                    except ISEError as e:
                        self.fail(w, str(e))
            else:
                self.act("create", w)
                if self.write and path:
                    try:
                        self.c.api("POST", path, body)
                    except ISEError as e:
                        self.fail(w, str(e))

    # -- entry points ---------------------------------------------------------

    def run(self, stage="all"):
        if stage in ("objects", "all"):
            self.trusted_certs()
            self.ers(defer=True)
        if stage in ("policy", "all"):
            if stage == "policy":
                # Fill the id maps every later reference needs.
                for t in ERS_TYPES:
                    try:
                        self._index(t)
                    except ISEError:
                        pass
            self.ers(only={"activedirectory"}, defer=False)
            self.policy()
        return not self.failures


def ad_join(client, name, username, password, all_nodes=True):
    t = BY_RES["activedirectory"]
    jp = {r.get("name"): r["id"] for r in client.ers_list(t.res)}
    if name not in jp:
        raise SystemExit(f"no join point named {name!r} on {client.host}")
    op = "joinAllNodes" if all_nodes else "join"
    body = {"OperationAdditionalData": {"additionalData": [
        {"name": "username", "value": username},
        {"name": "password", "value": password}]}}
    client.ers_put(f"activedirectory/{jp[name]}/{op}", body)
