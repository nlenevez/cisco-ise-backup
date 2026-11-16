#!/bin/sh
# Publish the tree of a commit to another repo as one new commit on top of
# that repo's history. Source history is never pushed.
#
#   scripts/publish-snapshot.sh REMOTE_URL "message" [DENY_FILE] [REF]
#
# REF defaults to main. DENY_FILE: extended regexes, one per line; the
# publish is refused if any tracked file matches one (case-insensitive).
# Author and committer: PUBLISH_NAME and PUBLISH_EMAIL (required).
set -eu

url=${1:?remote URL}
msg=${2:?commit message}
deny=${3:-}
ref=${4:-main}
: "${PUBLISH_NAME:?set PUBLISH_NAME}"
: "${PUBLISH_EMAIL:?set PUBLISH_EMAIL}"

cd "$(git rev-parse --show-toplevel)"
commit=$(git rev-parse --verify "$ref^{commit}")
tree=$(git rev-parse "$commit^{tree}")

if [ -n "$(git ls-tree "$commit" config)" ]; then
    echo "refusing: $ref contains config/" >&2
    exit 1
fi
if [ -n "$deny" ]; then
    hits=$(git grep -I -n -i -E -f "$deny" "$commit" -- . ':!ise_config/_vendor' || true)
    if [ -n "$hits" ]; then
        echo "refusing: deny-list matches:" >&2
        echo "$hits" >&2
        exit 1
    fi
fi

git fetch --quiet "$url" "+refs/heads/main:refs/publish/main"
parent=$(git rev-parse refs/publish/main)
if [ "$(git rev-parse "$parent^{tree}")" = "$tree" ]; then
    echo "nothing to publish: $url main already has this tree"
    exit 0
fi

new=$(GIT_AUTHOR_NAME="$PUBLISH_NAME" GIT_AUTHOR_EMAIL="$PUBLISH_EMAIL" \
      GIT_COMMITTER_NAME="$PUBLISH_NAME" GIT_COMMITTER_EMAIL="$PUBLISH_EMAIL" \
      git commit-tree "$tree" -p "$parent" -m "$msg")
git diff --stat "$parent" "$new"
git push "$url" "$new:refs/heads/main"
git update-ref -d refs/publish/main
echo "published $(git rev-parse --short "$new") from $(git rev-parse --short "$commit")"
