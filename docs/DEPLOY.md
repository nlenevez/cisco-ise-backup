# Deployment

Nightly export of each ISE cluster from a Linux host, by cron or by an
Ansible playbook with credentials in a vault, pushing to a git server
(GitLab, Bitbucket, Gitea, plain SSH) over SSH. No CI runner, pip, package
index or internet access is needed, so it works on an air-gapped network.
Examples use RHEL 8.

```
  RHEL 8 export host (cron or ansible, Python 3.12, git)
      | HTTPS 443 (ERS + OpenAPI, read-only account)    | SSH (deploy / access key)
      v                                                 v
  ISE PAN, one per cluster                    on-prem GitLab or Bitbucket
                                                cisco-ise-backup  tool, read
                                                <cluster>-config  data, write
```

## 1. The tool on your git server

The data repos find the tool as `../cisco-ise-backup.git`, so it must be
called **`cisco-ise-backup`** and live beside them: the same GitLab group,
or the same Bitbucket project.

- **Connected, or with a one-way repo sync into the air gap:** mirror
  `https://github.com/nlenevez/cisco-ise-backup` to that location (a
  GitLab pull mirror, Bitbucket mirror, or your sync tool). Nothing else
  to do.
- **Media only:** in a clone outside, `scripts/make-airgap-bundle.sh main dist`
  writes a git bundle, a tarball and `SHA256SUMS`. Inside, after
  `sha256sum -c SHA256SUMS`, create the empty repo and push the bundle:
  `git clone cisco-ise-backup.bundle cisco-ise-backup && cd cisco-ise-backup && git push <ssh URL> main`.

| | GitLab | Bitbucket Data Center |
|---|---|---|
| Container | group, e.g. `network/ise` | project, e.g. `ISE` |
| SSH URL shape | `git@gitlab:network/ise/cisco-ise-backup.git` | `ssh://git@bitbucket:7999/ise/cisco-ise-backup.git` |
| Default branch | `main` | older installs default to `master` (set the branch per cluster) |

## 2. Each ISE cluster: an API account

On each cluster's primary PAN:

1. **Administration > System > Settings > API Settings > API Service
   Settings**: enable **ERS** and **Open API**. ERS is off by default
   since 3.4.
2. Create a dedicated admin (e.g. `ise-export`) in **ERS Operator**. If
   ERS answers 401 while `/ers/sdk` answers 200, the account is not in an
   ERS group. Anything else it cannot read is listed in
   `config/export-errors.json` after the first export.
3. For RADIUS shared secrets to be exported, turn on **Show Password in
   Plaintext**; otherwise they come back empty.

The tool is tested on 3.5; on 3.2 to 3.4 it reads what those releases
offer and lists what they lack in `manifest.json`. Posture export needs
3.5 Patch 4.

## 3. The RHEL 8 export host

```sh
sudo dnf install git python3.12           # plus ansible-core for step 5b
sudo useradd -r -m -d /home/ise-export ise-export
sudo mkdir -p /srv/ise /etc/ise-export /var/log/ise-export
sudo chown ise-export: /srv/ise /var/log/ise-export
sudo chown root:ise-export /etc/ise-export && sudo chmod 750 /etc/ise-export
```

RHEL 8's `/usr/bin/python3` is **3.6, too old**: always set
`PYTHON=/usr/bin/python3.12` (env file, inventory, or shell). Without it
the tool stops and says so.

**SSH key**, as `ise-export`:

```sh
ssh-keygen -t ed25519 -N '' -f ~/.ssh/ise_export -C ise-export@$(hostname)
ssh-keyscan -p <port> <git server> >> ~/.ssh/known_hosts   # check the fingerprint
```

| | read on `cisco-ise-backup` | write on each `<cluster>-config` |
|---|---|---|
| GitLab | Deploy key (Settings > Repository > Deploy keys) | same key, enabled with **Grant write permissions** |
| Bitbucket | Repository access key, **Read** | Repository access key, **Read/Write** |

Avoid a project-level Read/Write key in Bitbucket: it could write to the
tool too.

**Network**: TCP 443 to every PAN; SSH to the git server.

## 4. Each cluster: a data repo

Create an empty `<cluster>-config` repo beside `cisco-ise-backup`, then as
`ise-export`:

```sh
export GIT_SSH_COMMAND="ssh -i ~/.ssh/ise_export -o BatchMode=yes"
cd /srv/ise
git clone <ssh URL of site-a-config> site-a && cd site-a
git switch -c main                               # empty repo: start the branch
git submodule add ../cisco-ise-backup.git tool   # resolved against origin
cp tool/ise-config.example.yml ise-config.yml
cp tool/contrib/data-repo/gitignore .gitignore
```

Edit `ise-config.yml`:

- `source_host`: the primary PAN.
- `protected_hosts`: **every** production node, by name and address.
  `apply` refuses to write to them.
- `tls_fingerprints`: pin the PAN's admin certificate if no CA in the
  RHEL trust store signed it. Renewing the certificate stops the export
  until the pin is updated.

First export by hand, then look before committing:

```sh
export ISE_USERNAME=ise-export PYTHON=/usr/bin/python3.12
read -rs ISE_PASSWORD && export ISE_PASSWORD
tool/ise-config export
cat config/export-errors.json             # want []
cat config/manifest.json                  # counts look right?
git add -A && git commit -m "first export" && git push -u origin main
```

## 5. Scheduling

Both options run `tool/contrib/nightly-export.sh`: fetch, export, `check`,
commit only if something changed, push. It ends with one line:

```
2026-11-18T15:16:02Z result: ok (exit 0)
2026-11-18T15:16:02Z result: ALARM (exit 2)     check saw a collection shrink; still committed
2026-11-18T15:16:02Z result: FAILED (exit 1)    ISE, git or the host failed; nothing pushed
```

A lock in the repo's `.git` stops a slow run overlapping the next.

### 5a. Cron with env files

`/etc/ise-export/site-a.env` (owner `root:ise-export`, mode `0640`):

```sh
ISE_USERNAME=ise-export
ISE_PASSWORD='...'
PYTHON=/usr/bin/python3.12
GIT_SSH_COMMAND="ssh -i /home/ise-export/.ssh/ise_export -o BatchMode=yes"
# EXPORT_BRANCH=master
```

`crontab -e` as `ise-export`, staggered:

```
15 2 * * * cd /srv/ise/site-a && tool/contrib/nightly-export.sh /etc/ise-export/site-a.env >>/var/log/ise-export/site-a.log 2>&1
25 2 * * * cd /srv/ise/site-b && tool/contrib/nightly-export.sh /etc/ise-export/site-b.env >>/var/log/ise-export/site-b.log 2>&1
```

### 5b. Ansible with a vault

`contrib/ansible/ise-export.yml` runs every cluster in turn with
credentials from an Ansible Vault file, cloning any data repo that is
missing. Tested with ansible-core 2.16 (RHEL 8.10 AppStream).

```sh
mkdir -p ~/ise-ansible && cd ~/ise-ansible
cp /srv/ise/site-a/tool/contrib/ansible/ise-export.yml .
cp /srv/ise/site-a/tool/contrib/ansible/inventory.example.yml inventory.yml   # edit
ansible-vault create vault.yml            # layout: tool/contrib/ansible/vault.example.yml
echo '<vault password>' > ~/.ise-vault-pass && chmod 600 ~/.ise-vault-pass
ansible-playbook -i inventory.yml ise-export.yml --vault-password-file ~/.ise-vault-pass
```

`vault_ise_credentials` holds one `username`/`password` per cluster name,
or a `default` entry for the rest. The play writes each cluster's
credentials to a mode-0600 file in a private temporary directory, passes
the file to the script, and deletes it afterwards, so they never appear
in `-vvv` output or `ps`. Every cluster is attempted; the play fails at
the end, naming each cluster that reported ALARM or FAILED.

Cron, as `ise-export`:

```
15 2 * * * cd ~/ise-ansible && ansible-playbook -i inventory.yml ise-export.yml --vault-password-file ~/.ise-vault-pass >>/var/log/ise-export/ansible.log 2>&1
```

### Watch it

Rotate `/var/log/ise-export/*.log` (`weekly`, `rotate 8`). Alert when the
last `result:` line (cron) or the `PLAY RECAP` (Ansible) shows a failure,
or when the log has not been written for 26 hours. A quiet night makes no
commit, so the age of the last commit is not a health signal.

## 6. Updating the tool

Once the mirror (or a new bundle) brings a new version to
`cisco-ise-backup`, move each data repo to it, one cluster at a time:

```sh
cd /srv/ise/site-a
git -C tool fetch origin && git -C tool checkout origin/main
git add tool && git commit -m "tool: update to $(git -C tool rev-parse --short HEAD)"
git push
```

Each data repo is pinned to a tool commit, so an update never reaches a
cluster until you move it there.

## 7. Rebuilding a cluster

[RESTORE-SOP.md](RESTORE-SOP.md), from this host or any workstation with
git, Python 3.8+ and HTTPS to the new nodes.
