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
# Concurrency (2026-08-04 fix): if two routine runs' pull-modify-push windows
# overlap -- a manual trigger landing near a scheduled one, e.g. -- naively
# re-fetching origin/bot-state right before building the push commit is NOT
# safe, even though it looks safe. Verified experimentally: if the other
# run's push has already landed by the time this run re-fetches, the parent
# ref looks perfectly valid (a clean fast-forward), so git doesn't reject
# anything -- but the LOCAL data/ working directory still predates the other
# run's changes, so the resulting commit silently overwrites/reverts them.
# No error, no rejection, just quietly wrong data. That's the actual root
# cause of positions.json drifting from the real account, not a rare exact-
# timing collision.
#
# The fix: pull() records the exact commit it fetched into .bot_state_base
# (gitignored, lives at repo root, NOT inside data/ so it never gets synced
# as state itself). push() treats that recorded commit as the merge BASE --
# not "whatever origin/bot-state happens to be when push() runs" -- and
# always compares it against the CURRENT origin/bot-state tip:
#   - base == current tip: nobody else pushed since our pull, straightforward
#     fast-forward, no merge needed.
#   - base != current tip: someone else pushed since our pull. 3-way merge
#     (git merge-tree --write-tree) our pending tree against their tip, using
#     the recorded base as the common ancestor, before building the commit.
#     A clean merge (the normal case -- two runs almost never touch the same
#     JSON region) just proceeds. A genuine conflict (same key, same file,
#     both runs changed it differently) is NOT auto-resolved -- that needs a
#     human, so push() fails loudly instead of guessing which side wins.
# The retry loop exists for the (rarer) case where yet another push lands in
# the gap between our merge and our own push attempt.
#
# Usage:
#   scripts/sync_state.sh pull   # refresh local data/ from origin/bot-state
#   scripts/sync_state.sh push   # commit current data/ to origin/bot-state
set -euo pipefail

cmd="${1:-}"
cd "$(git rev-parse --show-toplevel)"
BASE_MARKER=".bot_state_base"

pull() {
    if ! git fetch origin bot-state 2>/dev/null; then
        echo "sync_state pull: no bot-state branch on origin yet -- nothing to pull, starting fresh" >&2
        rm -f "$BASE_MARKER"
        exit 0
    fi
    git archive origin/bot-state | tar -x
    git rev-parse origin/bot-state > "$BASE_MARKER"
    echo "sync_state pull: data/ refreshed from origin/bot-state ($(git rev-parse origin/bot-state))"
}

push() {
    if [ ! -d data ]; then
        echo "sync_state push: no data/ directory to push" >&2
        exit 1
    fi

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

    local pending_tree
    pending_tree=$(printf "040000 tree %s\tdata\n" "$data_tree" | git mktree)

    # base_commit: what this process's local data/ was actually derived from
    # (recorded by pull()) -- NOT re-derived from a fresh fetch, since that's
    # exactly the bug this whole redesign fixes. Missing marker (push()
    # called without a prior pull() in this checkout) degrades to treating
    # the current remote tip as base -- best effort, not the safe path.
    local base_commit base_tree
    if [ -f "$BASE_MARKER" ]; then
        base_commit=$(cat "$BASE_MARKER")
    else
        git fetch origin bot-state 2>/dev/null || true
        base_commit=$(git rev-parse --verify -q origin/bot-state 2>/dev/null || echo "")
        echo "sync_state push: no $BASE_MARKER from a prior pull() -- falling back to the current remote tip as merge base (degraded safety, run pull() first when possible)" >&2
    fi
    base_tree=$([ -n "$base_commit" ] && git rev-parse "${base_commit}^{tree}" || git hash-object -t tree /dev/null)

    if [ "$base_tree" = "$pending_tree" ]; then
        echo "sync_state push: no state changes since last pull -- skipping"
        exit 0
    fi

    local msg cur_parent cur_tree new_commit attempt max_attempts push_err
    msg="state update $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    cur_parent="$base_commit"
    cur_tree="$pending_tree"
    max_attempts=5
    attempt=0

    while true; do
        attempt=$((attempt + 1))

        # Always check the CURRENT remote tip against our recorded base --
        # this is the check that has to happen on every attempt, not just
        # after a rejection, since a stale-but-fast-forwardable push is the
        # actual failure mode here (see header).
        git fetch origin bot-state 2>/dev/null || true
        local remote_head
        remote_head=$(git rev-parse --verify -q origin/bot-state 2>/dev/null || echo "")

        if [ -n "$remote_head" ] && [ "$remote_head" != "$cur_parent" ]; then
            echo "sync_state push: bot-state moved since our pull (attempt $attempt/$max_attempts) -- merging before pushing" >&2
            local remote_tree merge_out merge_status merge_err merged_tree
            remote_tree=$(git rev-parse "${remote_head}^{tree}")
            merge_err=$(mktemp)
            set +e
            merge_out=$(git merge-tree --write-tree --merge-base="$base_tree" "$remote_tree" "$cur_tree" 2>"$merge_err")
            merge_status=$?
            set -e

            if [ "$merge_status" -ne 0 ]; then
                echo "sync_state push: bot-state has a genuine conflicting concurrent change (both runs edited the same data differently) -- refusing to auto-resolve. Local data/ still holds this run's version, uncommitted. A human needs to reconcile data/ against origin/bot-state manually." >&2
                cat "$merge_err" >&2
                rm -f "$merge_err"
                return 1
            fi
            rm -f "$merge_err"

            merged_tree=$(echo "$merge_out" | head -1)
            cur_parent="$remote_head"
            cur_tree="$merged_tree"
            base_tree="$remote_tree"
        fi

        if [ -n "$cur_parent" ]; then
            new_commit=$(git commit-tree "$cur_tree" -p "$cur_parent" -m "$msg")
        else
            new_commit=$(git commit-tree "$cur_tree" -m "$msg")
        fi

        push_err=$(mktemp)
        if git push origin "${new_commit}:refs/heads/bot-state" 2>"$push_err"; then
            rm -f "$push_err"
            # Re-materialize data/ from what's now actually on bot-state --
            # a merged push can include changes from the other run that
            # weren't in our local working copy, so the caller's own
            # subsequent reads of data/ must reflect the true merged state.
            git archive "$new_commit" | tar -x
            git rev-parse "$new_commit" > "$BASE_MARKER"
            echo "sync_state push: bot-state updated to $new_commit"
            return 0
        fi

        if ! grep -qiE "non-fast-forward|fetch first|stale info|rejected" "$push_err"; then
            echo "sync_state push: push failed for a reason unrelated to a concurrent update -- not retrying:" >&2
            cat "$push_err" >&2
            rm -f "$push_err"
            return 1
        fi
        rm -f "$push_err"

        if [ "$attempt" -ge "$max_attempts" ]; then
            echo "sync_state push: gave up after $attempt attempts -- bot-state keeps moving concurrently. Local data/ still holds this run's changes, uncommitted; nothing was lost, it just isn't pushed. Needs a human to investigate the overlapping routine runs." >&2
            return 1
        fi
        # loop back around -- next iteration's fetch will see whatever just landed
    done
}

case "$cmd" in
    pull) pull ;;
    push) push ;;
    *)
        echo "usage: $0 {pull|push}" >&2
        exit 1
        ;;
esac
