"""Read-only export of an ISE deployment into config/.

Layout:
  manifest.json                 object counts per collection (+ ISE version)
  export-errors.json            everything that could not be read cleanly
  secrets.json.gpg              see secrets.py
  ers/<resource>/<name>.json    one file per ERS object, references by name
  policy/<service>/conditions/<name>.json
  policy/<service>/policy-sets/<name>.json   set + its rules, in rank order
  policy/<service>/global-exceptions.json
  system/                       reference data (nodes, versions, certificates,
                                settings); not all of it is re-applied

Nothing here writes to ISE.
"""

import io
import json
import os
import zipfile
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote as requests_quote

from .client import ISEError
from .normalize import dumps, normalize_ers, normalize_policy, slug, strip_keys
from .registry import ERS_TYPES, OPTIONAL_KINDS, POLICY_SERVICES, RULE_KINDS
from .secrets import write_bundle


class Tree:
    """Writes one directory's files and removes the ones no longer present.

    keep() preserves the last copy of an object that failed to read, so a
    read error never looks like a deletion.
    """

    def __init__(self, path):
        self.path = path
        self.files = {}
        self.kept = set()

    def put(self, name, doc):
        fn = slug(name) + ".json"
        if fn in self.files:
            # Same name twice in one collection should not happen; keep both.
            fn = slug(f"{name}#{len(self.files)}") + ".json"
        self.files[fn] = dumps(doc)

    def keep(self, name):
        self.kept.add(slug(name) + ".json")

    def sync(self):
        os.makedirs(self.path, exist_ok=True)
        for fn in os.listdir(self.path):
            if fn.endswith(".json") and fn not in self.files and fn not in self.kept:
                os.remove(os.path.join(self.path, fn))
        for fn, text in self.files.items():
            p = os.path.join(self.path, fn)
            try:
                with open(p) as f:
                    if f.read() == text:
                        continue
            except FileNotFoundError:
                pass
            with open(p, "w") as f:
                f.write(text)


def write_file(path, doc):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(dumps(doc))


class Resolving(dict):
    """id -> name map that falls back to GET by id.

    Some built-ins (e.g. the ANY SGT) are missing from ERS lists but readable by id.
    """

    def __init__(self, client, res, rows):
        super().__init__(rows)
        self.c, self.res = client, res

    def get(self, oid, default=None):
        if oid in self:
            return self[oid]
        try:
            name = next(iter(self.c.ers_get(self.res, oid).values())).get("name")
        except ISEError:
            name = None
        self[oid] = name
        return name if name is not None else default


class Exporter:
    def __init__(self, client, outdir, cfg, fp_key=None, log=print):
        self.c = client
        self.out = outdir
        self.cfg = cfg
        self.fp_key = fp_key
        self.inline = cfg.get("secrets", "inline") == "inline"
        self.log = log
        self.errors = []
        self.unavailable = []
        self.counts = {}
        self.secrets = {}
        self.id_to_name = {}
        self.workers = int(cfg.get("workers", 8))

    def err(self, where, e):
        if isinstance(e, ISEError) and e.status == 404:
            # Not on this ISE version: recorded in the manifest, not an error.
            self.unavailable.append(where)
            self.log(f"n/a   {where}: not available on this node")
            return
        self.errors.append({"where": where, "error": str(e)})
        self.log(f"ERROR {where}: {e}")

    # -- ERS ---------------------------------------------------------------

    def ers(self, only=None):
        listed = {}
        for t in ERS_TYPES:
            if t.res == "endpoint" and self.cfg.get("endpoints", "static") == "none":
                continue
            try:
                listed[t.res] = self.c.ers_list(t.res)
            except ISEError as e:
                if e.status in (404, 405) and t.optional:
                    self.log(f"skip {t.res}: not available on this node")
                else:
                    self.err(f"ers/{t.res} list", e)
                continue
            self.id_to_name[t.res] = Resolving(
                self.c, t.res, {r["id"]: r.get("name") for r in listed[t.res]})

        for t in ERS_TYPES:
            if t.lookup_only or t.res not in listed or (only and t.res not in only):
                continue
            tree = Tree(os.path.join(self.out, "ers", t.res))
            items = listed[t.res]
            self.log(f"ers/{t.res}: {len(items)} objects")

            def fetch(r, t=t):
                if t.list_only:
                    row = {k: v for k, v in r.items() if k not in ("id", "link")}
                    return r, {t.res: row}, None
                try:
                    return r, self.c.ers_get(t.res, r["id"]), None
                except Exception as e:  # noqa: BLE001 - recorded, not raised
                    return r, None, e

            with ThreadPoolExecutor(self.workers) as pool:
                results = list(pool.map(fetch, items))

            n = 0
            for r, doc, e in results:
                if e is not None:
                    self.err(f"ers/{t.res}/{r.get('name') or r['id']}", e)
                    tree.keep(r.get("name") or r["id"])
                    continue
                try:
                    name, fdoc, sec = normalize_ers(
                        doc, t, self.id_to_name, self.errors, self.fp_key, inline=self.inline,
                        where=f"ers/{t.res}/{r.get('name') or r['id']}")
                except Exception as e:  # noqa: BLE001 - malformed object
                    self.err(f"ers/{t.res}/{r.get('name') or r['id']} normalise", e)
                    tree.keep(r.get("name") or r["id"])
                    continue
                if t.res == "endpoint" and not self._keep_endpoint(fdoc):
                    continue
                name = name or r.get("name") or r["id"]
                tree.put(name, fdoc)
                if sec:
                    self.secrets[f"ers/{t.res}/{name}"] = sec
                n += 1
            tree.sync()
            self.counts[f"ers/{t.res}"] = n

    def _keep_endpoint(self, fdoc):
        if self.cfg.get("endpoints", "static") == "all":
            return True
        (_, ep), = fdoc.items()
        return bool(ep.get("staticGroupAssignment") or ep.get("staticProfileAssignment"))

    # -- Policy ------------------------------------------------------------

    def policy(self):
        for svc in POLICY_SERVICES:
            base = f"/policy/{svc}"
            root = os.path.join(self.out, "policy", svc)

            for coll, sub in (("conditions", "condition"),
                              ("time-conditions", "time-condition"),
                              ("network-conditions", "network-condition")):
                try:
                    items = self.c.api("GET", f"{base}/{sub}") or []
                except ISEError as e:
                    if not (coll == "network-conditions" and e.status == 404):
                        self.err(f"policy/{svc}/{coll}", e)
                    continue
                tree = Tree(os.path.join(root, coll))
                for it in items:
                    tree.put(it.get("name") or it.get("id"), normalize_policy(it))
                tree.sync()
                self.counts[f"policy/{svc}/{coll}"] = len(items)

            try:
                sets = self.c.api("GET", f"{base}/policy-set") or []
            except ISEError as e:
                self.err(f"policy/{svc}/policy-set", e)
                continue
            tree = Tree(os.path.join(root, "policy-sets"))
            nrules = 0
            for ps in sets:
                doc = {"policySet": normalize_policy(ps)}
                ok = True
                for kind in RULE_KINDS:
                    try:
                        rules = self.c.api("GET", f"{base}/policy-set/{ps['id']}/{kind}") or []
                    except ISEError as e:
                        if kind in OPTIONAL_KINDS and e.status == 404:
                            rules = []
                        else:
                            self.err(f"policy/{svc}/{ps.get('name')}/{kind}", e)
                            ok = False
                            break
                    rules = sorted(rules, key=lambda r: (r.get("rule") or {}).get("rank", 0))
                    doc[kind] = [normalize_policy(r) for r in rules]
                    nrules += len(rules)
                if ok:
                    tree.put(ps["name"], doc)
                else:
                    tree.keep(ps["name"])
            tree.sync()
            self.counts[f"policy/{svc}/policy-sets"] = len(sets)
            self.counts[f"policy/{svc}/rules"] = nrules

            try:
                ge = self.c.api("GET", f"{base}/policy-set/global-exception") or []
                ge = sorted(ge, key=lambda r: (r.get("rule") or {}).get("rank", 0))
                write_file(os.path.join(root, "global-exceptions.json"),
                           [normalize_policy(r) for r in ge])
                self.counts[f"policy/{svc}/global-exceptions"] = len(ge)
            except ISEError as e:
                self.err(f"policy/{svc}/global-exception", e)

    # -- System reference data ---------------------------------------------

    def api_paged(self, path, size=100):
        out, page = [], 1
        while True:
            sep = "&" if "?" in path else "?"
            got = self.c.api("GET", f"{path}{sep}page={page}&size={size}") or []
            out += got
            if len(got) < size:
                return out
            page += 1

    def system(self):
        root = os.path.join(self.out, "system")
        nodes = self.nodes = []
        try:
            nodes = self.nodes = self.c.api("GET", "/deployment/node") or []
            write_file(os.path.join(root, "deployment-nodes.json"),
                       strip_keys(nodes, {"link", "nodeStatus"}))
        except ISEError as e:
            self.err("system/deployment-nodes", e)
        try:
            self.version = self.c.api("GET", "/patch")
            write_file(os.path.join(root, "version.json"), strip_keys(self.version, {"link"}))
        except ISEError as e:
            self.version = None
            self.err("system/version", e)

        try:
            certs = self.api_paged("/certs/trusted-certificate")
            tree = Tree(os.path.join(root, "trusted-certificates"))
            for cert in certs:
                meta = strip_keys(cert, {"id", "link"})
                try:
                    meta["pem"] = self._trusted_pem(cert["id"])
                except Exception as e:  # noqa: BLE001
                    self.err(f"system/trusted-certificate/{cert.get('friendlyName')}", e)
                tree.put(cert.get("friendlyName") or cert["id"], meta)
            tree.sync()
            self.counts["system/trusted-certificates"] = len(certs)
        except ISEError as e:
            self.err("system/trusted-certificates", e)

        tree = Tree(os.path.join(root, "system-certificates"))
        for node in nodes:
            host = node.get("hostname")
            if not host:
                continue
            try:
                certs = self.api_paged(f"/certs/system-certificate/{host}")
                tree.put(host, strip_keys(certs, {"id", "link"}))
            except ISEError as e:
                self.err(f"system/system-certificates/{host}", e)
                tree.keep(host)
        tree.sync()

        try:
            repos = self.c.api("GET", "/repository") or []
            write_file(os.path.join(root, "repositories.json"),
                       strip_keys(repos, {"link", "password"}))
        except ISEError as e:
            self.err("system/repositories", e)

        try:
            dicts = self.c.api("GET", "/policy/network-access/dictionaries") or []
            write_file(os.path.join(root, "dictionaries.json"),
                       sorted((strip_keys(d, {"id", "link"}) for d in dicts),
                              key=lambda d: d.get("name", "")))
            self.counts["system/dictionaries"] = len(dicts)
        except ISEError as e:
            self.err("system/dictionaries", e)
        try:
            write_file(os.path.join(root, "proxy.json"),
                       strip_keys(self.c.api("GET", "/system-settings/proxy"), {"link", "password"}))
        except ISEError as e:
            self.err("system/proxy", e)

    def duo(self):
        """Duo MFA connections and identity syncs (ISE 3.3+).

        ISE never returns the Duo secret keys (sKey is null).
        """
        root = os.path.join(self.out, "system")
        for coll, path, key, name in (
                ("duo-mfa", "/duo-mfa/mfa", "mfa", "name"),
                ("duo-identitysync", "/duo-identitysync/identitysync",
                 "identitySync", "name")):
            try:
                items = self.c.api("GET", path) or []
            except ISEError as e:
                if e.status != 404:
                    self.err(f"system/{coll}", e)
                continue
            tree = Tree(os.path.join(root, coll))
            for it in items:
                try:
                    full = (self.c.api("GET", f"{path}/{requests_quote(it[name])}") or {}).get(key) or it
                except ISEError as e:
                    self.err(f"system/{coll}/{it[name]}", e)
                    tree.keep(it[name])
                    continue
                full = strip_keys(full, {"link", "syncStatus", "lastSync", "directoryID"})
                if isinstance(full.get("adGroups"), list):
                    # ISE returns these in a different order every call.
                    full["adGroups"].sort(key=lambda g: str(g.get("name")))
                tree.put(it[name], full)
            tree.sync()
            self.counts[f"system/{coll}"] = len(items)

    # Settings with a GET but no ERS object model: exported for reference,
    # not applied. "ers:" paths are ERS singletons.
    SETTINGS = (
        ("license/registration", "/license/system/register"),
        ("license/connection-type", "/license/system/connection-type"),
        ("license/tier-state", "/license/system/tier-state"),
        ("posture/general", "/posture/setting/general"),
        ("posture/continuous-monitoring", "/posture/setting/continuousmonitoring"),
        ("posture/update", "/posture/setting/update"),
        ("posture/requirements", "/posture/requirement"),
        ("provisioning/policy", "/provisioning/policy"),
        ("provisioning/resources", "/provisioning/resource/generic"),
        ("trustsec/general", "/trustsec/general-settings"),
        ("trustsec/matrix-workflow", "/trustsec/matrix-workflow-settings"),
        ("trustsec/matrices", "/trustsec/matrix"),
        ("trustsec/radius-servers", "/trustsec/radius-server"),
        ("trustsec/sxp-domains", "/trustsec/sxp/domain"),
        ("endpoint-custom-attributes", "/endpoint-custom-attribute"),
        ("stop-replication", "/stop-replication"),
        ("light-session-directory", "/lsd/updateLsdSettings"),
        ("profiler-resiliency", "/profiler/resiliency/config"),
        ("pan-ha", "/deployment/pan-ha"),
        ("rest-id-store", "ers:restidstoresettings"),
    )
    # Fields that change on their own.
    SETTINGS_VOLATILE = {"link", "id", "consumptionCounter", "daysOutOfCompliance",
                         "lastAuthorization", "compliance", "lastSync", "syncStatus"}

    def settings(self, nodes):
        root = os.path.join(self.out, "system", "settings")
        n = 0
        for fname, path in self.SETTINGS:
            try:
                if path.startswith("ers:"):
                    doc = self.c._req("GET", f"/ers/config/{path[4:]}").json()
                else:
                    doc = self.c.api("GET", path)
            except ISEError as e:
                if e.status not in (400, 404):  # absent or feature disabled
                    self.err(f"system/settings/{fname}", e)
                continue
            write_file(os.path.join(root, f"{fname}.json"),
                       strip_keys(doc, self.SETTINGS_VOLATILE))
            n += 1
        # Profiler probes differ per node.
        for node in nodes:
            host = node.get("hostname")
            try:
                doc = self.c.api("GET", f"/profile/{host}")
            except ISEError as e:
                if e.status != 404:
                    self.err(f"system/settings/profiler-probes/{host}", e)
                continue
            write_file(os.path.join(root, "profiler-probes", f"{host}.json"),
                       strip_keys(doc, self.SETTINGS_VOLATILE))
            n += 1
        self.counts["system/settings"] = n

    # Admin RBAC: (file, path, list key, name key, fetch each item for detail)
    RBAC = (("admin-users", "/rbac/admin-user", "adminUserList", "name", False),
            ("admin-groups", "/rbac/admin-group", "adminGroupList", "name", False),
            ("menu-access", "/rbac/menu-access", "menuAccessList", "name", True),
            ("data-access", "/rbac/data-access", "dataAccessList", "name", True),
            ("policies", "/rbac/policy", "RbacPolicyList", "policyName", False))

    def rbac(self):
        root = os.path.join(self.out, "rbac")
        for fname, path, key, name, detail in self.RBAC:
            try:
                items = (self.c.api("GET", path) or {}).get(key) or []
                if detail:
                    full = []
                    for it in items:
                        d = self.c.api("GET", f"{path}/{requests_quote(it[name])}") or {}
                        # Detail responses wrap the object in one dict-valued key.
                        obj = next((v for k, v in d.items() if isinstance(v, dict)), it)
                        full.append(obj)
                    items = full
                items = sorted((strip_keys(i, {"id", "link", "password"}) for i in items),
                               key=lambda i: str(i.get(name, "")))
                write_file(os.path.join(root, f"{fname}.json"), items)
                self.counts[f"rbac/{fname}"] = len(items)
            except ISEError as e:
                self.err(f"rbac/{fname}", e)

    def _trusted_pem(self, cid):
        r = self.c.api_raw(f"/certs/trusted-certificate/export/{cid}")
        ctype = r.headers.get("Content-Type", "")
        if "json" in ctype:
            def find(o):
                if isinstance(o, str) and "BEGIN CERTIFICATE" in o:
                    return o
                if isinstance(o, dict):
                    o = list(o.values())
                if isinstance(o, list):
                    for v in o:
                        got = find(v)
                        if got:
                            return got
                return None
            pem = find(r.json())
        elif r.content[:2] == b"PK":
            with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                pem = z.read(z.namelist()[0]).decode()
        else:
            pem = r.text
        if not pem or "BEGIN CERTIFICATE" not in pem:
            raise ValueError(f"no PEM in export response ({ctype})")
        return pem.strip() + "\n"

    # -- Top level -----------------------------------------------------------

    def run(self, recipient_file=None, secrets=True):
        self.version = None
        self.ers()
        self.policy()
        self.system()
        self.settings(self.nodes)
        self.duo()
        self.rbac()
        manifest = {"format": 1, "counts": dict(sorted(self.counts.items()))}
        if isinstance(self.version, dict):
            manifest["iseVersion"] = self.version.get("iseVersion")
            manifest["patches"] = sorted(p.get("patchNumber") for p in
                                         self.version.get("patchVersion") or [])
        if self.unavailable:
            manifest["unavailable"] = sorted(set(self.unavailable))
        write_file(os.path.join(self.out, "manifest.json"), manifest)
        write_file(os.path.join(self.out, "export-errors.json"),
                   sorted(self.errors, key=lambda e: (e["where"], e["error"])))
        if secrets and not self.inline:
            write_bundle(self.out, self.secrets, recipient_file, self.fp_key, self.log)
        return manifest, self.errors


def load_json(path):
    with open(path) as f:
        return json.load(f)
