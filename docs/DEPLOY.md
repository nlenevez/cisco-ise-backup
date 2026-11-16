# Deployment

Nightly export of each ISE cluster from **cron on a Linux host**, pushing
to a git server (GitLab, Bitbucket, Gitea, plain SSH) over SSH. No CI
runner, pip, package index or internet access is needed, so it works on
an air-gapped network. Examples use RHEL 8.

```
  RHEL 8 export host (cron, Python 3.12, git)
      | HTTPS 443 (ERS + OpenAPI, read-only account)    | SSH (deploy / access key)
      v                                                 v
  ISE PAN, one per cluster                    on-prem GitLab or Bitbucket
                                                cisco-ise-backup  tool, read
                                                <cluster>-config  data, write
```

With network access, clone this repo onto your git server and skip to
step 3. Air-gapped, only the tool crosses the gap, inbound; cluster data
is created inside and never comes back out.

## 1. Outside: build the bundle

In a clone of this repo:

```sh
scripts/make-airgap-bundle.sh main dist
```

`dist/cisco-ise-backup-<date>-<commit>/` holds `cisco-ise-backup.bundle` (git, with full
history), the same tree as a `.tar.gz`, `MANIFEST.txt` and `SHA256SUMS`.
Note the SHA-256 lines separately from the media, then carry the
directory in.

## 2. Inside: verify and import the tool

```sh
cd /media/cisco-ise-backup-<date>-<commit>
sha256sum -c SHA256SUMS                  # compare with the lines noted outside
git bundle verify cisco-ise-backup.bundle
```

Create an empty repo called **`cisco-ise-backup`** next to where the data repos
will live: the same GitLab group, or the same Bitbucket project. The data
repos point at it as `../cisco-ise-backup.git`, so the name and location matter.
Then push the bundle into it:

```sh
git clone cisco-ise-backup.bundle cisco-ise-backup && cd cisco-ise-backup
git remote set-url origin <ssh URL of the new cisco-ise-backup repo>
git push origin main
```

| | GitLab | Bitbucket Data Center |
|---|---|---|
| Container | group, e.g. `network/ise` | project, e.g. `ISE` |
| SSH URL shape | `git@gitlab:network/ise/cisco-ise-backup.git` | `ssh://git@bitbucket:7999/ise/cisco-ise-backup.git` |
| Default branch | `main` | check: older installs default to `master` (set `EXPORT_BRANCH`) |

## 3. Each ISE cluster: an API account

On each cluster's primary PAN:

1. **Administration > System > Settings > API Settings > API Service
   Settings**: enable **ERS** and **Open API**. ERS has been off by default
   since 3.4.
2. Create a dedicated admin (e.g. `ise-export`) in **ERS Operator**
   (read-only ERS). If ERS answers 401 while `/ers/sdk` answers 200, the
   account is not in an ERS group. Anything else it cannot read is listed
   in `config/export-errors.json` after the first export, so start there.
3. For RADIUS shared secrets to be exported, turn on **Show Password in
   Plaintext**. Without it RADIUS keys come back empty, and a rebuild
   would need every NAD's key re-entered by hand.

Versions: the tool is tested on 3.5; on 3.2 to 3.4 it reads what those
releases offer and lists the collections they lack in `manifest.json`.
The posture export API needs 3.5 Patch 4.

## 4. The RHEL 8 export host

```sh
sudo dnf install git python3.12           # if not already there
sudo useradd -r -m -d /home/ise-export ise-export
sudo mkdir -p /srv/ise /etc/ise-export /var/log/ise-export
sudo chown ise-export: /srv/ise /var/log/ise-export
sudo chown root:ise-export /etc/ise-export && sudo chmod 750 /etc/ise-export
```

RHEL 8's `/usr/bin/python3` is **3.6, too old**. Every call sets
`PYTHON=/usr/bin/python3.12` (in the env file below, or exported in your
shell); without it the tool stops with a message saying so.

**SSH key.** As `ise-export`:

```sh
ssh-keygen -t ed25519 -N '' -f ~/.ssh/ise_export -C ise-export@$(hostname)
ssh-keyscan -p <port> <git server> >> ~/.ssh/known_hosts   # check the fingerprint
```

Register the public half:

| | read on `cisco-ise-backup` | write on each `<cluster>-config` |
|---|---|---|
| GitLab | Deploy key (Settings > Repository > Deploy keys) | the same key, enabled on the project with **Grant write permissions** |
| Bitbucket | Repository access key, **Read** | Repository access key, **Read/Write** |

Do not use a project-level Read/Write key in Bitbucket: it would also
let the export host write to the tool.

**Network**: TCP 443 from this host to every PAN; SSH to the git server.

## 5. Each cluster: a data repo

Create an empty `<cluster>-config` repo beside `cisco-ise-backup`, then as
`ise-export`:

```sh
export GIT_SSH_COMMAND="ssh -i ~/.ssh/ise_export -o BatchMode=yes"
cd /srv/ise
git clone <ssh URL of site-a-config> site-a && cd site-a
git switch -c main                         # empty repo: start the branch
git submodule add ../cisco-ise-backup.git tool   # relative: resolved against origin
cp tool/ise-config.example.yml ise-config.yml
cp tool/contrib/data-repo/gitignore .gitignore
```

Edit `ise-config.yml`:

- `source_host`: the primary PAN, by name or address.
- `protected_hosts`: **every** production node, by name and address.
  `apply` refuses to write to them.
- `tls_fingerprints`: pin the PAN's admin certificate if it is not signed
  by a CA in the RHEL trust store:
  `echo | openssl s_client -connect PAN:443 2>/dev/null | openssl x509 -noout -fingerprint -sha256`.
  Renewing that certificate stops the export until the pin is updated.

Credentials go in `/etc/ise-export/site-a.env` (owner `root:ise-export`,
mode `0640`), never in the repo:

```sh
ISE_USERNAME=ise-export
ISE_PASSWORD=...
PYTHON=/usr/bin/python3.12
GIT_SSH_COMMAND="ssh -i /home/ise-export/.ssh/ise_export -o BatchMode=yes"
# EXPORT_BRANCH=master    # Bitbucket repo whose default branch is master
```

First run by hand, then look before committing:

```sh
set -a; . /etc/ise-export/site-a.env; set +a
tool/ise-config export
cat config/export-errors.json             # want []
cat config/manifest.json                  # counts look right?
git add -A && git commit -m "first export" && git push -u origin main
```

## 6. Cron

`crontab -e` as `ise-export`, one line per cluster, staggered so that
eight clusters do not hit the git server at once:

```
15 2 * * * cd /srv/ise/site-a && tool/contrib/nightly-export.sh /etc/ise-export/site-a.env >>/var/log/ise-export/site-a.log 2>&1
25 2 * * * cd /srv/ise/site-b && tool/contrib/nightly-export.sh /etc/ise-export/site-b.env >>/var/log/ise-export/site-b.log 2>&1
```

Each run fetches, exports, runs `check`, commits only if something
changed, pushes, and ends with a single line:

```
2026-11-18T15:16:02Z result: ok (exit 0)
2026-11-18T15:16:02Z result: ALARM (exit 2)     check saw a collection shrink; still committed
2026-11-18T15:16:02Z result: FAILED (exit 1)    ISE, git or the host failed; nothing pushed
```

A lock in the repo's `.git` stops a slow run overlapping the next one.

Add `/var/log/ise-export/*.log` to logrotate (`weekly`, `rotate 8`).

**Watch it.** No one reads cron output in an air-gapped estate, so point
your monitoring at the logs: alert when the last `result:` line is not
`ok`, or is older than 26 hours. A quiet night makes no commit, so the
age of the repo's last commit is not a health signal.

## 7. Updating the tool

Outside: build a new bundle (step 1). Inside, after `sha256sum -c`:

```sh
cd cisco-ise-backup                            # the clone from step 2
git pull /media/cisco-ise-backup-<new>/cisco-ise-backup.bundle main
git push origin main
```

Then move each data repo to it, one cluster at a time:

```sh
cd /srv/ise/site-a
git -C tool fetch origin && git -C tool checkout origin/main
git add tool && git commit -m "tool: update to $(git -C tool rev-parse --short HEAD)"
git push
```

Each data repo is pinned to a tool commit, so an update never reaches a
cluster until you move that cluster.

## 8. Rebuilding a cluster

[RESTORE-SOP.md](RESTORE-SOP.md), run from this host or any workstation
with git, Python 3.8+ and HTTPS to the new nodes. On RHEL 8 set
`PYTHON=/usr/bin/python3.12` first.
