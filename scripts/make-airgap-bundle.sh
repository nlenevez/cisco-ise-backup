#!/bin/sh
# Build a carry-in bundle of the tool for an air-gapped site.
#
#   scripts/make-airgap-bundle.sh [REF] [OUTDIR]     (defaults: main, ./dist)
#
# Writes OUTDIR/cisco-ise-backup-<date>-<commit>/: cisco-ise-backup.bundle (branch main,
# full history), a source .tar.gz, MANIFEST.txt and SHA256SUMS.
# See docs/DEPLOY.md.
set -eu

ref=${1:-main}
outroot=${2:-dist}

cd "$(git rev-parse --show-toplevel)"

commit=$(git rev-parse --verify "$ref^{commit}")
short=$(git rev-parse --short=10 "$commit")
date=$(git show -s --format=%cd --date=format:%Y%m%d "$commit")
out="$outroot/cisco-ise-backup-$date-$short"

# The tool repo must never carry cluster data, in any commit.
if [ -n "$(git rev-list -n 1 "$commit" -- config)" ]; then
    echo "refusing: the history of $ref contains a config/ tree (cluster data)" >&2
    exit 1
fi

if [ -e "$out" ]; then
    echo "$out already exists" >&2
    exit 1
fi
mkdir -p "$out"

# git bundle cannot rename refs: stage the commit as main in a scratch repo.
stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
git init --quiet --bare "$stage"
git push --quiet "$stage" "$commit:refs/heads/main"
git -C "$stage" symbolic-ref HEAD refs/heads/main
git -C "$stage" bundle create --quiet "$(cd "$out" && pwd)/cisco-ise-backup.bundle" HEAD main
git bundle verify --quiet "$out/cisco-ise-backup.bundle"

git archive --format=tar.gz --prefix=cisco-ise-backup/ -o "$out/cisco-ise-backup-$short.tar.gz" "$commit"

cat >"$out/MANIFEST.txt" <<EOF
cisco-ise-backup $short
commit:  $commit
date:    $(git show -s --format=%ci "$commit")
subject: $(git show -s --format=%s "$commit")

cisco-ise-backup.bundle   git clone cisco-ise-backup.bundle cisco-ise-backup
cisco-ise-backup-$short.tar.gz  the same tree, without history
Install: docs/DEPLOY.md
EOF

(cd "$out" && sha256sum cisco-ise-backup.bundle "cisco-ise-backup-$short.tar.gz" MANIFEST.txt >SHA256SUMS)

echo "$out"
cat "$out/SHA256SUMS"
