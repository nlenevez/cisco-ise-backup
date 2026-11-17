# cisco-ise-backup

Cisco ISE configuration as code. A nightly job reads a cluster over the ERS
and OpenAPI APIs into JSON files in git; a restore rebuilds a fresh cluster
from those files through the same APIs.

Written against ISE 3.5; reads 3.2 and later (collections an older release
lacks are listed in the manifest, not treated as errors).

> **Status:** `export`, `check` and `plan` are in daily use. `apply`,
> `ad-join` and `verify` against a fresh node have not yet been rehearsed
> end to end. Test on a scratch node before relying on them.

## Why not restore a config backup

A config backup is a physical copy of the ISE database: if the database is
corrupt, the restore is too. This is a *logical* copy. Each object is read
as JSON and written back through the API, which validates it on the way in.
Sessions, logs, profiler history and internal tables never come across.
Keep taking backups; use this as the clean-room route.

It also gives you:

- **History.** Each night's changes are a commit: `git log -p config/`.
- **An early warning.** Objects that can no longer be read appear in
  `export-errors.json`; a collection that shrinks sharply makes `check`
  exit 2.

## Requirements

Python 3.8+ and git. Nothing to install: dependencies are vendored as pure
Python in `ise_config/_vendor/`, so it runs on an air-gapped host with no
pip or package index. If the default `python3` is older (RHEL 8 ships 3.6),
set `PYTHON=/path/to/python3.x`.

On ISE: ERS and Open API enabled (Administration > System > Settings > API
Settings), and an admin in **ERS Operator** for export. `apply` needs ERS
Admin and Super Admin.

## Layout: one tool, one data repo per cluster

This repo is the tool. Each cluster's export lives in its own data repo,
with this repo as a submodule at `tool/`:

```
<cluster>-config/
  ise-config.yml             hosts, protected hosts, TLS pin (no credentials)
  tool/                      submodule -> cisco-ise-backup
  config/
    manifest.json            object counts, version, patches, unavailable collections
    export-errors.json       what could not be read (should be [])
    ers/<resource>/*.json    devices, groups, users, profiles, dACLs, TrustSec,
                             identity stores, TACACS+, guest
    policy/<service>/        conditions, policy sets and rules, global exceptions
    rbac/                    admin users, groups, menu/data access
    system/                  nodes, version, certificates, repositories, Duo,
                             settings snapshot
```

The submodule URL is relative (`../cisco-ise-backup.git`): keep the tool and data
repos side by side in the same group or project, on any git server.

Objects reference each other by **name**, not UUID, since a fresh node
mints new UUIDs. A reference to nothing is written as `<missing:uuid>`
and reported.

## Usage

From the root of a data repo:

```sh
export ISE_USERNAME=... ISE_PASSWORD=...

tool/ise-config export                 # read source_host into config/
tool/ise-config check                  # compare with last commit; exit 2 = alarm
tool/ise-config plan  --host NEW       # what apply would do (read-only)
tool/ise-config apply --host NEW --stage objects --yes
tool/ise-config ad-join --host NEW --name <join point>
tool/ise-config apply --host NEW --stage policy --prune --yes
tool/ise-config verify --host NEW      # export NEW and diff with config/
```

### MAB audit

```sh
tool/ise-config mab-audit -o mab.csv          # active MAB sessions
tool/ise-config mab-audit --all -o all.csv    # every session, with is_mab
```

Read-only, from the MnT REST API: lists active sessions, reads each one's
detail, and keeps those that authenticated by MAB (method `mab` or
protocol `Lookup`). One CSV row per endpoint: switch, port, IP, identity
group, endpoint profile, authorization profile, PSN, time, and from the
endpoint database whether it has a static group assignment. The summary
counts MAB endpoints with no static assignment: devices let in by
profiling or a default rule rather than an allow-list.

- The account needs **MnT Admin** (or Super Admin) and, for the static
  assignment columns, ERS read. Without ERS those columns stay empty.
- Point `--host` (or `mnt_host` in `ise-config.yml`) at the primary MnT
  node.
- Active sessions only. For history, use the RADIUS Authentications
  report filtered on Authentication Method = mab.

`apply` and `ad-join` refuse any host in `protected_hosts`, matched by
name, address and the node names the target reports. `plan` runs
anywhere: `plan --host <PAN>` shows drift between git and the live cluster.

### A new cluster

1. Create an empty `<cluster>-config` repo beside `cisco-ise-backup`.
2. `git submodule add ../cisco-ise-backup.git tool`
3. Copy `tool/ise-config.example.yml` to `ise-config.yml` and
   `tool/contrib/data-repo/gitignore` to `.gitignore`; fill in the former.
4. Run `tool/ise-config export` by hand, check `export-errors.json`, commit.
5. Schedule it: cron running `tool/contrib/nightly-export.sh`, or the
   Ansible playbook in `contrib/ansible/` with credentials in a vault
   ([docs/DEPLOY.md](docs/DEPLOY.md)).

To update the tool in a data repo: `git -C tool pull origin main`, then
commit the submodule pointer.

## Secrets

Shared secrets (RADIUS and TACACS+ keys, bind passwords) are kept inline
in each object's file by default, so a restore needs nothing else. Treat
data repos accordingly. With `secrets: gpg` they move to
`config/secrets.json.gpg`, encrypted to a public key you supply. Values
ISE never returns (internal user passwords, Duo secret keys) are recorded
as `{"$secret": "unavailable"}`. RADIUS secrets are only returned with
**Show Password in Plaintext** enabled.

## Not covered

Posture, client provisioning, most system settings (logging, SMTP,
security and protocol settings, admin access), network device profiles,
RADIUS dictionaries and certificate private keys have no usable API.
They are rebuilt by hand from [docs/MANUAL-SETTINGS.md](docs/MANUAL-SETTINGS.md).

## Docs

- [docs/DEPLOY.md](docs/DEPLOY.md): install, cron or Ansible, air-gapped sites
- [docs/RESTORE-SOP.md](docs/RESTORE-SOP.md): rebuilding a cluster
- [docs/MANUAL-SETTINGS.md](docs/MANUAL-SETTINGS.md): what to rebuild by hand
