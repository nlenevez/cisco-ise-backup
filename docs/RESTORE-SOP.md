# Rebuilding an ISE cluster from git

Rebuilds a cluster on fresh VMs from a data repo, **without restoring a
config backup**. Use it when the cluster will not start, or when a backup
restore reproduces the fault.

Status: **draft, not yet rehearsed end to end.** Run it once against a
scratch node, to `verify` with no differences, before you need it.

Expect about half a day, most of it ISE installing and patching itself.

| Phase | What | How |
|------:|------|-----|
| 0 | Inputs, pick the commit to restore | by hand |
| 1 | Provision VMs | hypervisor |
| 2 | Install, patch | console |
| 3 | Minimum GUI settings on the new primary | GUI |
| 4 | Rebuild objects | `ise-config apply --stage objects` |
| 5 | Join Active Directory | `ise-config ad-join` or GUI |
| 6 | Rebuild policy | `ise-config apply --stage policy --prune` |
| 7 | Settings with no API | [MANUAL-SETTINGS.md](MANUAL-SETTINGS.md) |
| 8 | Certificates, other nodes | GUI |
| 9 | Verify, cut over | `ise-config verify` + smoke tests |

## Phase 0: inputs and the commit to restore

- [ ] The data repo cloned with `--recurse-submodules` on a workstation
      with git, Python 3.8+ and HTTPS to the new nodes.
- [ ] With `secrets: gpg`: the private key in that workstation's keyring.
- [ ] ISE install media and the patch named in `config/system/version.json`.
- [ ] Licences, or start on the 90-day evaluation.
- [ ] A domain account allowed to join computers to AD.
- [ ] System certificates for each node, or the means to issue them.
- [ ] Node details from `config/system/deployment-nodes.json`. Reuse the
      same hostnames and IPs: every NAD points at them.

**Pick the commit.** If the database was going bad, the last few exports
may already show it:

```sh
git log --format='%h %ad %s' --date=short -- config/ | head -30
git log -p -- config/export-errors.json | head -50    # when did errors start?
git show <commit>:config/manifest.json                 # counts look normal?
```

Use the newest commit where `export-errors.json` is `[]` and `check`
passed, then `git checkout <commit>`.

## Phase 1: provision the VMs

One VM per node in `deployment-nodes.json`, sized like the old nodes (see
[VM sizing](#vm-sizing)). Disconnect or power off the old nodes before the
new ones take their IPs.

## Phase 2: install and patch

Run the setup wizard on each console with the values from
`deployment-nodes.json` (see [Setup wizard](#setup-wizard)). Install the
patch from `version.json` on every node: the export came from that
version. Primary first; other nodes are registered in phase 8.

## Phase 3: minimum GUI settings on the new primary

1. Log in to `https://<primary>/admin`.
2. **Administration > System > Settings > API Settings > API Service
   Settings**: enable **ERS (Read/Write)** and **Open API**.
3. Optionally create a restore admin in **Super Admin** and **ERS Admin**.
4. Leave the self-signed admin certificate; use `--insecure` until phase 8.

```sh
export ISE_USERNAME=<admin> ISE_PASSWORD=<password>
NEW=<primary FQDN>
```

## Phase 4: rebuild objects

```sh
tool/ise-config --insecure plan  --host $NEW --stage objects | tee plan-objects.txt
tool/ise-config --insecure apply --host $NEW --stage objects --yes | tee apply-objects.txt
```

Expect mostly `create`, and `update` for built-ins changed on the source
cluster. Apply creates, in dependency order: trusted certificates, device
groups, devices with their secrets, identity groups, internal users,
TrustSec objects, dACLs, authorization and certificate profiles, AD join
points (without groups), LDAP, identity sequences, external RADIUS,
allowed protocols, TACACS+ objects, SXP, ANC, guest types, sponsor groups,
portals and static endpoints.

In the summary:

- `failed:`: fix the cause (usually a missing prerequisite) and re-run.
  Apply is idempotent.
- `warning: no value for secret(s)`: ISE returned it masked; enter it in
  the GUI.
- Internal users get random passwords with "change at next login",
  listed in `~/ise-generated-passwords.tsv` (mode 600). Distribute, then
  delete it.

## Phase 5: join Active Directory

For each join point in `config/ers/activedirectory/`:

```sh
tool/ise-config --insecure ad-join --host $NEW --name <join point>
```

It prompts for the domain account. Fallback: **Administration > Identity
Management > External Identity Sources > Active Directory > Join**.
Continue once the join point shows **Operational**.

### Duo (if `config/system/duo-mfa/` is not empty)

ISE never returns Duo secret keys, so the connection is created by hand
before phase 6, or rules that use it fail to apply:

1. **External Identity Sources > Duo**: add the connection from
   `config/system/duo-mfa/*.json` with the secret keys from the Duo Admin
   Panel.
2. Recreate the identity sync from `config/system/duo-identitysync/`.
   Run it once; it should report **Success**.

Do not cut over with an MFA rule missing: logins would then work on a
password alone.

## Phase 6: rebuild policy

```sh
tool/ise-config --insecure plan  --host $NEW --stage policy --prune | tee plan-policy.txt
tool/ise-config --insecure apply --host $NEW --stage policy --prune --yes | tee apply-policy.txt
```

Adds AD groups to join points, then conditions, policy sets, rules and
global exceptions for network access and device admin. `--prune` removes
default sets and rules the source cluster did not have, so rule order
matches. Re-run until there are no `failed:` lines.

## Phase 7: settings with no API

Work through [MANUAL-SETTINGS.md](MANUAL-SETTINGS.md).

## Phase 8: certificates and other nodes

1. Import system certificates for each usage under **Administration >
   System > Certificates > System Certificates**;
   `config/system/system-certificates/<host>.json` records which was used
   for what. Changing the admin certificate restarts the application server.
2. Drop `--insecure` from here on, or pass `--ca-bundle`.
3. Register the other nodes ([below](#registering-nodes)). They replicate
   from the primary, so phases 4 to 7 are not repeated.
4. Set personas and services to match `deployment-nodes.json`.

## Phase 9: verify and cut over

```sh
tool/ise-config verify --host $NEW
```

`verify` exports the new cluster to a temporary directory and diffs it
with `config/`. Target: no changes, apart from objects reported as failed
or manual above.

Smoke tests from real devices, as applicable:

- [ ] Wired 802.1X (user and machine) hits the expected rule
      (**Operations > RADIUS > Live Logs**).
- [ ] MAB for a static endpoint.
- [ ] Wireless 802.1X.
- [ ] TACACS+ login with command authorization.
- [ ] Guest or sponsor portal.
- [ ] TrustSec SGT assignment and SGACL download.
- [ ] Every PSN answers.

Then set `source_host` and `protected_hosts` in `ise-config.yml` to the
new nodes, run `tool/contrib/nightly-export.sh` by hand, and read the
commit it makes: that diff is everything that differs from the commit you
restored. Take a config backup and test-restore it on a scratch VM.

---

## Reference

From the Cisco ISE 3.5 Installation Guide, Administrator's Guide and
release notes.

### VM sizing

- Disk: 600 GB minimum for a node running PAN and MnT (300 GB for
  PSN-only). One virtual disk; 512n/512e, not 4Kn. It cannot be grown later.
- CPU and memory reserved equal to the allocation; disk shares High.
- OVA templates (vCPU / RAM): Small 3615 16/32 GB, Medium 3655 24/96,
  Large 3695 24/256, Small 3715 24/32, Medium 3755 40/96, Large 3795 40/256,
  Small 3815 32/64, Medium 3855 48/128, Large 3895 48/256.
- ESXi 7.0, 8.0 or 9.0; hardware version 14+; guest OS RHEL 8; NIC
  E1000E or VMXNET3; thick eager-zeroed; hardware clock UTC.
- **Expose hardware-assisted virtualization**: 3.5 services need
  x86-64-v2 and AVX (`show tech-support | include avx`).
- Storage: 50 MB/s write, 300 MB/s read (`show tech | begin "disk IO perf"`).
- **No VM snapshots and no quiescing backup agents.** Cisco documents
  that they can corrupt the database or freeze ISE.
- `application stop` before powering a VM off.

### Setup wizard

- Hostname: up to 19 characters, lowercase, starting with a letter. Use
  the old one; a new name breaks certificates and NAD configuration.
- NTP must be reachable during setup; same servers on every node.
- Time zone: UTC.
- Admin username 3 to 8 alphanumerics. Avoid `$` in the password except
  as the last character: accepted, but CLI login then fails.
- Forward and reverse DNS for every node before registration.

### Patching

Patches are cumulative. On each node, primary first:

```
patch install <patch bundle>.SPA.x86_64.tar.gz <repository>
show version
```

### Registering nodes

Same version and patch, same NTP, forward and reverse DNS, and each admin
certificate with its node's FQDN as a DNS SAN (enforced from 3.5 Patch 4).
For CA-signed admin certificates, import the chain on the primary with
**Trust for authentication within ISE**.

**Administration > System > Deployment**: make the node primary, then
**Register** each other node. API: `POST /api/v1/deployment/primary`, then
`POST /api/v1/deployment/node`.

After registration:

- Open API is switched off on a newly registered node; re-enable it if needed.
- 3.5 Patch 3: do not enable SXP during registration; wait for **Synced**.
- Internal CA: export its certificates and keys from the primary and
  import them on the others (`application configure ise`, options 7 and 8).
- Two-node deployments have no PAN auto-failover (it needs a third
  health-check node). Promote by hand: **Deployment > Promote to Primary**
  or `POST /api/v1/deployment/promote`.

### Licensing

A fresh node runs a 90-day evaluation (100 endpoints, all features). Out
of compliance, authentication continues but the GUI is limited to the
Licensing page.

### Health checks

**Administration > System > Health Checks** after the rebuild and after
each node joins.
