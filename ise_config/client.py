"""HTTP client for the ISE ERS (/ers/config) and OpenAPI (/api/v1) APIs.

ERS lists are paged SearchResults; ERS objects are wrapped in one root key,
e.g. {"NetworkDevice": {...}}. OpenAPI responses are {"response": ...}.
Both APIs must be enabled under Administration > System > Settings > API Settings.
"""

import time

import requests
import urllib3
from requests.adapters import HTTPAdapter


class ISEError(Exception):
    def __init__(self, method, url, status, body):
        self.method, self.url, self.status, self.body = method, url, status, body
        super().__init__(f"{method} {url} -> HTTP {status}: {self.summary()}")

    def summary(self):
        # ERS errors are {"ERSResponse": {"messages": [{"title": ...}]}};
        # OpenAPI ones are {"message": ...} or {"response": {"message": ...}}.
        b = self.body
        if isinstance(b, dict):
            msgs = (b.get("ERSResponse") or {}).get("messages") or []
            if msgs:
                return "; ".join(m.get("title", "") for m in msgs)
            for k in ("message", "error"):
                if b.get(k):
                    return str(b[k])
            r = b.get("response")
            if isinstance(r, dict) and r.get("message"):
                return str(r["message"])
        return str(b)[:300]


class _Pinned(HTTPAdapter):
    """Accept exactly one certificate, by SHA-256 fingerprint."""

    def __init__(self, fingerprint, **kw):
        self.fingerprint = fingerprint.replace(":", "").lower()
        super().__init__(**kw)

    def init_poolmanager(self, *a, **kw):
        kw["assert_fingerprint"] = self.fingerprint
        super().init_poolmanager(*a, **kw)


class ISEClient:
    PAGE = 100

    def __init__(self, host, username, password, verify=True, timeout=90,
                 fingerprint=None):
        self.host = host
        self.base = f"https://{host}"
        self.timeout = timeout
        self.s = requests.Session()
        self.s.auth = (username, password)
        self.s.headers.update({"Accept": "application/json",
                               "Content-Type": "application/json"})
        self.s.verify = verify
        if fingerprint:
            self.s.verify = False
            self.s.mount("https://", _Pinned(fingerprint))
        if self.s.verify is False:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        self._csrf = None

    def _req(self, method, path, **kw):
        url = self.base + path
        for attempt in range(5):
            r = self.s.request(method, url, timeout=self.timeout, **kw)
            # Throttling, service restarts and some GET 500s are transient.
            if (r.status_code in (429, 502, 503, 504)
                    or (r.status_code == 500 and method == "GET")) and attempt < 4:
                time.sleep(2 ** attempt)
                continue
            # The CSRF token dies with the ERS session after 60s idle.
            if (r.status_code == 403 and self._csrf and method != "GET"
                    and attempt < 4):
                self._csrf = None
                self.s.headers.pop("X-CSRF-Token", None)
                self._prepare_write()
                continue
            break
        if r.status_code >= 400:
            try:
                body = r.json()
            except ValueError:
                body = r.text
            raise ISEError(method, path, r.status_code, body)
        return r

    def _prepare_write(self):
        # With CSRF enforcement on, writes need a token from a GET sent with
        # "X-CSRF-Token: fetch"; with it off, no token is returned.
        if self._csrf is not None:
            return
        r = self.s.get(self.base + "/ers/config/networkdevicegroup",
                       params={"size": 1}, timeout=self.timeout,
                       headers={"X-CSRF-Token": "fetch"})
        self._csrf = r.headers.get("X-CSRF-Token", "")
        if self._csrf:
            self.s.headers["X-CSRF-Token"] = self._csrf

    # -- ERS ---------------------------------------------------------------

    def ers_list(self, res):
        out, page = [], 1
        while True:
            r = self._req("GET", f"/ers/config/{res}",
                          params={"size": self.PAGE, "page": page})
            sr = r.json().get("SearchResult", {})
            out.extend(sr.get("resources") or [])
            if not sr.get("nextPage"):
                return out
            page += 1

    def ers_get(self, res, oid):
        return self._req("GET", f"/ers/config/{res}/{oid}").json()

    def ers_id_by_name(self, res, name):
        from urllib.parse import quote
        doc = self._req("GET", f"/ers/config/{res}/name/{quote(name, safe='')}").json()
        return next(iter(doc.values())).get("id")

    def ers_create(self, res, body):
        self._prepare_write()
        r = self._req("POST", f"/ers/config/{res}", json=body)
        return r.headers.get("Location", "").rstrip("/").rsplit("/", 1)[-1]

    def ers_update(self, res, oid, body):
        self._prepare_write()
        self._req("PUT", f"/ers/config/{res}/{oid}", json=body)

    def ers_delete(self, res, oid):
        self._prepare_write()
        self._req("DELETE", f"/ers/config/{res}/{oid}")

    def ers_put(self, path, body):
        self._prepare_write()
        r = self._req("PUT", f"/ers/config/{path}", json=body)
        return r.json() if r.content else None

    # -- OpenAPI -----------------------------------------------------------

    def api(self, method, path, body=None):
        if method != "GET":
            self._prepare_write()
        r = self._req(method, "/api/v1" + path, json=body)
        if not r.content:
            return None
        d = r.json()
        return d["response"] if isinstance(d, dict) and "response" in d else d

    def api_raw(self, path):
        return self._req("GET", "/api/v1" + path, headers={"Accept": "*/*"})
