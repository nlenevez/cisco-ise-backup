"""ise-config command line.

  export   read the source ISE into config/ (read-only)
  check    compare config/ with the last commit; exit 2 on suspicious change
  plan     show what apply would do to a target (read-only)
  apply    rebuild a target from config/ (refuses protected hosts)
  ad-join  join a rebuilt AD join point to its domain
  verify   export the target to a temp dir and diff it against config/
  mab-audit  list active sessions that authenticated by MAB (read-only, MnT)

Credentials come from ISE_USERNAME / ISE_PASSWORD, or are prompted for.
"""

import argparse
import getpass
import json
import os
import socket
import subprocess
import sys
import tempfile

import requests
import yaml

from .apply import Applier, ad_join
from .client import ISEClient, ISEError
from .export import Exporter
from .secrets import BUNDLE, read_bundle


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def load_settings(path):
    if os.path.exists(path):
        with open(path) as f:
            return yaml.safe_load(f) or {}
    return {}


def client_for(args, host, settings=None):
    user = os.environ.get("ISE_USERNAME") or input(f"ISE username for {host}: ")
    pw = os.environ.get("ISE_PASSWORD") or getpass.getpass(f"ISE password for {user}@{host}: ")
    verify = False if args.insecure else (args.ca_bundle or os.environ.get("ISE_CA_BUNDLE") or True)
    pins = (settings or {}).get("tls_fingerprints") or {}
    fp = None if args.insecure else pins.get(host)
    return ISEClient(host, user, pw, verify=verify, fingerprint=fp)


def _addrs(host):
    try:
        return {a[4][0] for a in socket.getaddrinfo(host, 443)}
    except OSError:
        return set()


def guard(client, settings):
    """Refuse to write to a protected host, matched by name, address or the
    node names the target reports."""
    protected = [h.lower() for h in settings.get("protected_hosts") or []]
    if not protected:
        raise SystemExit("refusing to write: protected_hosts is empty in ise-config.yml")
    host = client.host.lower()
    bad = host in protected or host.split(".")[0] in {p.split(".")[0] for p in protected}
    paddrs = set().union(*(_addrs(p) for p in protected))
    bad = bad or bool(_addrs(host) & paddrs)
    try:
        for n in client.api("GET", "/deployment/node") or []:
            for k in ("hostname", "fqdn"):
                v = (n.get(k) or "").lower()
                if v and (v in protected or v.split(".")[0] in {p.split(".")[0] for p in protected}):
                    bad = True
            if n.get("ipAddress") in paddrs:
                bad = True
    except ISEError as e:
        raise SystemExit(f"refusing to write: cannot read target's node list to check it: {e}")
    if bad:
        raise SystemExit(f"refusing to write: {client.host} is (or is part of) a protected deployment")


def cmd_export(args, settings):
    host = args.host or settings.get("source_host")
    if not host:
        raise SystemExit("no source host: pass --host or set source_host")
    c = client_for(args, host, settings)
    ex = Exporter(c, args.config, settings, fp_key=os.environ.get("SECRET_FINGERPRINT_KEY"), log=log)
    manifest, errors = ex.run(recipient_file=settings.get("secrets_public_key", "keys/restore.asc"),
                              secrets=not args.no_secrets)
    log(json.dumps(manifest["counts"], indent=2))
    log(f"{len(errors)} export error(s)")
    return 0


def cmd_check(args, settings):
    path = os.path.join(args.config, "manifest.json")
    with open(path) as f:
        new = json.load(f)["counts"]
    try:
        old = json.loads(subprocess.run(
            ["git", "show", f"{args.against}:{path}"], check=True,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout)["counts"]
    except (subprocess.CalledProcessError, KeyError, ValueError):
        log("no previous manifest to compare with")
        old = {}
    with open(os.path.join(args.config, "export-errors.json")) as f:
        errors = json.load(f)

    th = settings.get("check", {})
    pct, floor = th.get("max_drop_percent", 10), th.get("max_drop_objects", 5)
    alarms = []
    for k in sorted(set(old) | set(new)):
        o, n = old.get(k, 0), new.get(k, 0)
        if o != n:
            log(f"{k}: {o} -> {n}")
        if o - n > floor and (o - n) * 100 > pct * o:
            alarms.append(f"{k} dropped {o} -> {n}")
        if k in old and k not in new:
            alarms.append(f"{k} missing from export")
    if errors:
        alarms.append(f"{len(errors)} export error(s), see export-errors.json")
    for a in alarms:
        log(f"ALARM: {a}")
    return 2 if alarms else 0


def _secrets(args, settings):
    if args.no_secrets or settings.get("secrets", "inline") == "inline":
        # Inline secrets are already in the object files.
        return {}
    path = args.secrets or os.path.join(args.config, BUNDLE)
    if not os.path.exists(path):
        raise SystemExit(f"no secrets bundle at {path}; pass --secrets or --no-secrets")
    return read_bundle(path)


def cmd_apply(args, settings, write):
    c = client_for(args, args.host, settings)
    if write:
        guard(c, settings)
        if not args.yes:
            raise SystemExit("apply writes to the target; re-run with --yes "
                             "(run `plan` first to see what it will do)")
    # A plan compares without secret values, so it needs no private key.
    a = Applier(c, args.config, _secrets(args, settings) if write else {}, write=write, prune=args.prune,
                log=log, inline=settings.get("secrets", "inline") == "inline")
    ok = a.run(stage=args.stage)
    log(f"\n{'applied' if write else 'plan'}: " +
        ", ".join(f"{k} {v}" for k, v in sorted(a.counts.items())))
    for w in a.warnings:
        log(f"warning: {w}")
    for f in a.failures:
        log(f"failed:  {f}")
    if a.generated:
        out = args.password_report
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            for u, p in sorted(a.generated.items()):
                f.write(f"{u}\t{p}\n")
        log(f"{len(a.generated)} internal user(s) given new passwords (change at "
            f"first login): {out}")
    return 0 if ok else 1


def cmd_ad_join(args, settings):
    c = client_for(args, args.host, settings)
    guard(c, settings)
    user = input("AD account with rights to join computers: ")
    pw = getpass.getpass(f"password for {user}: ")
    ad_join(c, args.name, user, pw)
    log(f"join requested for {args.name}; check Administration > Identity "
        "Management > External Identity Sources > Active Directory")
    return 0


def cmd_verify(args, settings):
    c = client_for(args, args.host, settings)
    with tempfile.TemporaryDirectory() as tmp:
        Exporter(c, tmp, settings, log=lambda m: None).run(secrets=False)
        rc = 0
        for sub in ("ers", "policy", "system/trusted-certificates"):
            r = subprocess.run(["git", "diff", "--no-index", "--stat",
                                os.path.join(args.config, sub), os.path.join(tmp, sub)])
            rc |= r.returncode
    log("target matches config/" if rc == 0 else "target differs from config/ (above)")
    return 1 if rc else 0


def cmd_mab_audit(args, settings):
    from .sessions import SessionAudit, summary, write_csv
    host = args.host or settings.get("mnt_host") or settings.get("source_host")
    if not host:
        raise SystemExit("no MnT host: pass --host or set mnt_host")
    c = client_for(args, host, settings)
    audit = SessionAudit(c, workers=settings.get("workers", 8), log=log)
    try:
        sessions, rows = audit.run(include_all=args.all, endpoints=not args.no_endpoints)
    except ISEError as e:
        if e.status == 401:
            raise SystemExit(f"{host}: HTTP 401 from the MnT API; the account needs MnT Admin "
                             "(or Super Admin), and the host must be an MnT node")
        raise
    if args.output == "-":
        write_csv(rows, sys.stdout, include_all=args.all)
    else:
        with open(args.output, "w", newline="") as f:
            write_csv(rows, f, include_all=args.all)
        log(f"{len(rows)} row(s) written to {args.output}")
    for line in summary(sessions, rows):
        log(line)
    if audit.ended:
        log(f"{audit.ended} session(s) ended before their detail could be read")
    for e in audit.errors[:20]:
        log(f"error: {e}")
    if len(audit.errors) > 20:
        log(f"... {len(audit.errors) - 20} more error(s)")
    return 1 if audit.errors else 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="ise-config")
    p.add_argument("--settings", default="ise-config.yml")
    p.add_argument("--config", default="config", help="export directory")
    p.add_argument("--ca-bundle", help="CA bundle for the ISE admin certificate")
    p.add_argument("--insecure", action="store_true",
                   help="skip TLS verification (fresh node with self-signed cert)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("export")
    s.add_argument("--host")
    s.add_argument("--no-secrets", action="store_true")

    s = sub.add_parser("check")
    s.add_argument("--against", default="HEAD")

    for name in ("plan", "apply"):
        s = sub.add_parser(name)
        s.add_argument("--host", required=True)
        s.add_argument("--stage", choices=("objects", "policy", "all"), default="all")
        s.add_argument("--prune", action="store_true",
                       help="delete policy sets/rules on the target that are not in config/")
        s.add_argument("--secrets", help="secrets bundle (.gpg) or plaintext JSON")
        s.add_argument("--no-secrets", action="store_true")
        s.add_argument("--password-report", default=os.path.expanduser("~/ise-generated-passwords.tsv"))
        if name == "apply":
            s.add_argument("--yes", action="store_true")

    s = sub.add_parser("ad-join")
    s.add_argument("--host", required=True)
    s.add_argument("--name", required=True, help="join point name")

    s = sub.add_parser("verify")
    s.add_argument("--host", required=True)

    s = sub.add_parser("mab-audit")
    s.add_argument("--host", help="MnT node (default: mnt_host, then source_host)")
    s.add_argument("--output", "-o", default="-", help="CSV file (default: stdout)")
    s.add_argument("--all", action="store_true",
                   help="include every session, with an is_mab column")
    s.add_argument("--no-endpoints", action="store_true",
                   help="skip the endpoint database lookup (static group/profile)")

    args = p.parse_args(argv)
    settings = load_settings(args.settings)
    try:
        return dispatch(args, settings)
    except requests.ConnectionError as e:
        log(f"cannot reach ISE: {e}")
        return 1


def dispatch(args, settings):
    if args.cmd == "export":
        return cmd_export(args, settings)
    if args.cmd == "check":
        return cmd_check(args, settings)
    if args.cmd == "plan":
        args.yes = False
        return cmd_apply(args, settings, write=False)
    if args.cmd == "apply":
        return cmd_apply(args, settings, write=True)
    if args.cmd == "ad-join":
        return cmd_ad_join(args, settings)
    if args.cmd == "verify":
        return cmd_verify(args, settings)
    if args.cmd == "mab-audit":
        return cmd_mab_audit(args, settings)


if __name__ == "__main__":
    sys.exit(main())
