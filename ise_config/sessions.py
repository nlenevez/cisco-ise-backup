"""Active-session audit over the MnT REST API (XML only; needs MnT Admin).

ActiveList gives every session's MAC; Session/MACAddress/<mac> gives its
detail, including how it authenticated. MAB shows as authentication method
"mab" or protocol "Lookup".
"""

import csv
import xml.etree.ElementTree as ET
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

from .client import ISEError

COLUMNS = [
    "calling_station_id", "user_name", "authentication_method",
    "authentication_protocol", "identity_group", "endpoint_policy",
    "selected_azn_profiles", "network_device_name", "nas_ip_address",
    "nas_port_id", "framed_ip_address", "acs_server", "auth_acs_timestamp",
]
ENDPOINT_COLUMNS = ["static_group", "static_profile", "endpoint_description"]

XML = {"Accept": "application/xml"}


def _flat(elem):
    return {c.tag: (c.text or "").strip() for c in elem if len(c) == 0}


def is_mab(s):
    return (s.get("authentication_method", "").lower() == "mab"
            or s.get("authentication_protocol", "").lower() == "lookup")


class SessionAudit:
    def __init__(self, client, workers=8, log=print):
        self.c, self.workers, self.log = client, workers, log
        self.errors = []
        self.ended = 0

    def active_macs(self):
        r = self.c._req("GET", "/admin/API/mnt/Session/ActiveList", headers=XML)
        root = ET.fromstring(r.content)
        macs = []
        for s in root.iter("activeSession"):
            mac = (s.findtext("calling_station_id") or "").strip()
            if mac and mac not in macs:
                macs.append(mac)
        return macs

    def detail(self, mac):
        try:
            r = self.c._req("GET", f"/admin/API/mnt/Session/MACAddress/{quote(mac, safe=':-.')}",
                            headers=XML)
        except ISEError as e:
            if e.status == 404:
                self.ended += 1     # session ended after ActiveList was read
            else:
                self.errors.append(f"{mac}: HTTP {e.status}")
            return None
        root = ET.fromstring(r.content)
        node = root if root.tag == "sessionParameters" else root.find(".//sessionParameters")
        if node is None:
            self.errors.append(f"{mac}: no sessionParameters")
            return None
        d = _flat(node)
        d.setdefault("calling_station_id", mac)
        return d

    def endpoint(self, mac):
        """Static assignments from the endpoint database (needs ERS read)."""
        r = self.c._req("GET", "/ers/config/endpoint", params={"filter": f"mac.EQ.{mac}"})
        res = r.json().get("SearchResult", {}).get("resources") or []
        if not res:
            return {"static_group": "", "static_profile": "", "endpoint_description": "(not in endpoint database)"}
        ep = self.c.ers_get("endpoint", res[0]["id"]).get("ERSEndPoint", {})
        group = ""
        if ep.get("staticGroupAssignment") and ep.get("groupId"):
            try:
                group = self.c.ers_get("endpointgroup", ep["groupId"]).get("EndPointGroup", {}).get("name", "")
            except ISEError:
                group = ep["groupId"]
        return {
            "static_group": group,
            "static_profile": "yes" if ep.get("staticProfileAssignment") else "",
            "endpoint_description": ep.get("description", ""),
        }

    def run(self, include_all=False, endpoints=True):
        macs = self.active_macs()
        self.log(f"{len(macs)} active session MAC(s)")
        with ThreadPoolExecutor(self.workers) as pool:
            sessions = [s for s in pool.map(self.detail, macs) if s]
        rows = [s for s in sessions if include_all or is_mab(s)]
        for s in rows:
            s["is_mab"] = "yes" if is_mab(s) else ""
        if endpoints and rows:
            try:
                self.endpoint(rows[0]["calling_station_id"])
            except ISEError as e:
                self.log(f"endpoint database not readable (HTTP {e.status}); "
                         "static assignment columns left empty")
                endpoints = False
        if endpoints:
            with ThreadPoolExecutor(self.workers) as pool:
                for s, ep in zip(rows, pool.map(self._endpoint_safe, [s["calling_station_id"] for s in rows])):
                    s.update(ep)
        return sessions, rows

    def _endpoint_safe(self, mac):
        try:
            return self.endpoint(mac)
        except ISEError as e:
            self.errors.append(f"{mac}: endpoint lookup HTTP {e.status}")
            return {}


def write_csv(rows, f, include_all=False):
    cols = (["is_mab"] if include_all else []) + COLUMNS + ENDPOINT_COLUMNS
    w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in sorted(rows, key=lambda r: (r.get("network_device_name", ""), r.get("nas_port_id", ""),
                                         r.get("calling_station_id", ""))):
        w.writerow({c: r.get(c, "") for c in cols})


def summary(sessions, rows):
    mab = [s for s in sessions if is_mab(s)]
    lines = [f"{len(sessions)} session(s) read, {len(mab)} MAB"]
    methods = Counter((s.get("authentication_method") or "?", s.get("authentication_protocol") or "?")
                      for s in sessions)
    lines.append("by method/protocol: " + ", ".join(f"{m}/{p} {n}" for (m, p), n in methods.most_common()))
    for key in ("identity_group", "selected_azn_profiles"):
        c = Counter(s.get(key) or "-" for s in mab)
        if c:
            lines.append(f"MAB by {key}: " + ", ".join(f"{k} {n}" for k, n in c.most_common(10)))
    no_static = sum(1 for r in rows if is_mab(r) and "static_group" in r and not r.get("static_group"))
    if any("static_group" in r for r in rows):
        lines.append(f"MAB endpoints with no static group assignment: {no_static}")
    return lines
