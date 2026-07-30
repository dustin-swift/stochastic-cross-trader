#!/usr/bin/env bash
# Sync data/ (positions.json, candidates.json, daily_pnl.json, logs/,
# finviz_export.csv) against a dedicated `bot-state` branch on origin --
# NOT part of the code history on main. Cloud routines run in fresh clones
# each time, so this is how state survives between runs.
#
# Design: bot-state's root tree contains exactly one entry, a `data` folder,
# mirroring main's layout. Push uses git plumbing (write-tree/ls-tree/mktree/
# commit-tree) to build and push a bot-state commit directly -- it never
# checks out bot-state, never switches HEAD, and never touches main's commit
# history. Safe to run from any branch, including mid-run on a cloud agent's
# checkout of main.
#
# Usage:
#   scripts/sync_state.sh pull   # refresh local data/ from origin/bot-state
#   scripts/sync_state.sh push   # commit current data/ to origin/bot-state
set -euo pipefail

cmd="${1:-}"
cd "$(git rev-parse --show-toplevel)"

pull() {
    if ! git fetch origin bot-state 2>/dev/null; then
        echo "sync_state pull: no bot-state branch on origin yet -- nothing to pull, starting fresh" >&2
        exit 0
    fi
    git archive origin/bot-state | tar -x
    echo "sync_state pull: data/ refreshed from origin/bot-state ($(git rev-parse origin/bot-state))"
}

push() {
    if [ ! -d data ]; then
        echo "sync_state push: no data/ directory to push" >&2
        exit 1
    fi

    git fetch origin bot-state 2>/dev/null || true
    local parent
    # --verify makes this properly fail (empty output, nonzero exit) on an
    # unresolvable ref -- plain `git rev-parse <ref>` instead just echoes the
    # input back literally and exits 0, which silently broke this check.
    parent=$(git rev-parse --verify -q origin/bot-state 2>/dev/null || echo "")

    # Stage data/ into a throwaway index just to compute its tree object --
    # `-f` because data/ is gitignored on this branch. Reset immediately
    # after so this never leaves stray staged changes lying around.
    git add -f data/
    local full_tree data_tree
    full_tree=$(git write-tree)
    data_tree=$(git ls-tree "$full_tree" data | awk '{print $3}')
    git reset -q

    if [ -z "$data_tree" ]; then
        echo "sync_state push: data/ tree could not be computed (is data/ empty?)" >&2
        exit 1
    fi

    local root_tree new_commit msg
    root_tree=$(printf "040000 tree %s\tdata\n" "$data_tree" | git mktree)

    if [ -n "$parent" ]; then
        local parent_tree
        # Compare like-for-like: parent's root tree (which IS a "data"-only
        # tree per this branch's layout) against the newly built wrapping
        # tree -- NOT against data_tree, which is one level too deep and
        # would never match, silently defeating this no-op check.
        parent_tree=$(git rev-parse "${parent}^{tree}")
        if [ "$parent_tree" = "$root_tree" ]; then
            echo "sync_state push: no state changes since last push -- skipping"
            exit 0
        fi
    fi

    msg="state update $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    if [ -n "$parent" ]; then
        new_commit=$(git commit-tree "$root_tree" -p "$parent" -m "$msg")
    else
        new_commit=$(git commit-tree "$root_tree" -m "$msg")
    fi

    git push origin "${new_commit}:refs/heads/bot-state"
    echo "sync_state push: bot-state updated to $new_commit"
}

case "$cmd" in
    pull) pull ;;
    push) push ;;
    *)
        echo "usage: $0 {pull|push}" >&2
        exit 1
        ;;
esac
