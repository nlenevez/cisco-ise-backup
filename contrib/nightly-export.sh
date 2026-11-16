#!/bin/sh
# Nightly export for one ISE cluster, for cron. No CI runner needed.
#
#   tool/contrib/nightly-export.sh [ENV_FILE]
#
# Run from the root of the cluster's data repo. Steps:
#   1. bring the branch up to date with the remote (GitLab, Bitbucket, any git)
#   2. tool/ise-config export, then check
#   3. commit config/ if anything changed, and push
#   4. exit with check's code (2 = alarm), after the evidence is pushed
#
# ENV_FILE (optional) is sourced first; keep it outside the repo, mode 0600:
#   ISE_USERNAME=...            read-only API admin on the PAN (required)
#   ISE_PASSWORD=...
#   EXPORT_REMOTE=origin        remote to fetch from and push to; "none" =
#                               commit locally only
#   EXPORT_BRANCH=main
#   PYTHON=/usr/bin/python3.12  needed on RHEL 8, whose python3 is 3.6
#   GIT_SSH_COMMAND="ssh -i /home/ise-export/.ssh/ise_export -o BatchMode=yes"
#
# Exit codes: 0 ok, 2 check alarm (committed and pushed anyway), other =
# the run failed. The last line of output always says which.
set -eu

if [ $# -ge 1 ]; then
    set -a
    # shellcheck disable=SC1090
    . "$1"
    set +a
fi

: "${ISE_USERNAME:?ISE_USERNAME is not set}"
: "${ISE_PASSWORD:?ISE_PASSWORD is not set}"
remote=${EXPORT_REMOTE:-origin}
branch=${EXPORT_BRANCH:-main}

if [ ! -f ise-config.yml ] || [ ! -x tool/ise-config ]; then
    echo "run from the root of a data repo (ise-config.yml and tool/ expected)" >&2
    exit 1
fi

stamp() { date -u +%Y-%m-%dT%H:%M:%SZ; }
finish() {
    r=$?
    case $r in
        0) word=ok ;;
        2) word=ALARM ;;
        *) word=FAILED ;;
    esac
    echo "$(stamp) result: $word (exit $r)"
}
trap finish EXIT

# One run per data repo at a time: a slow export must not overlap the next.
exec 9>"$(git rev-parse --git-dir)/ise-export.lock"
if ! flock -n 9; then
    echo "another export is already running here" >&2
    exit 1
fi

echo "$(stamp) export start in $(pwd)"

# Commit identity: whatever git is configured with, else a service name.
if ! git config user.name >/dev/null; then
    export GIT_AUTHOR_NAME="ISE nightly export" GIT_COMMITTER_NAME="ISE nightly export"
fi
if ! git config user.email >/dev/null; then
    export GIT_AUTHOR_EMAIL="ise-export@$(hostname)" GIT_COMMITTER_EMAIL="ise-export@$(hostname)"
fi

if [ "$remote" != none ]; then
    git fetch --quiet "$remote" "$branch"
    git checkout --quiet -B "$branch" "$remote/$branch"
else
    git checkout --quiet "$branch"
fi
git submodule update --init --quiet

tool/ise-config export

# check compares the new manifest with the last commit; its output becomes
# the commit body. Its exit code waits until after the push, so a bad
# night's evidence is in git before anything raises an alarm.
out=$(mktemp)
set +e
tool/ise-config check >"$out" 2>&1
check_rc=$?
set -e
cat "$out"

git add -A config
if git diff --cached --quiet; then
    echo "no configuration changes"
else
    n=$(git diff --cached --name-only | wc -l | tr -d ' ')
    {
        echo "export: $(date -u +%Y-%m-%d), $n file(s) changed"
        echo
        cat "$out"
    } >"$out.msg"
    git commit --quiet -F "$out.msg"
    if [ "$remote" != none ]; then
        # Plain push: no -o push options, which Bitbucket refuses.
        git push --quiet "$remote" "$branch"
    fi
    echo "committed $(git rev-parse --short HEAD)"
fi
rm -f "$out" "$out.msg"
exit $check_rc
