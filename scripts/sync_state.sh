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
# pending_entries.json exemption (2026-08-05): confirmed live -- the daily
# universe screen resets this file to `{}` every morning (see
# check_universe_screen.py) while an hourly cycle can independently be
# mid-write with its own real pending-setup dict, and if both land close
# together a genuine same-file conflict on THIS file alone was blocking the
# entire push, including real trade data (positions.json/trade_history.json)
# in the same commit. The actual fix: pending_entries.json is intraday-only,
# low-stakes scratch state (worst case on a bad merge: a mid-confirmation
# candidate restarts its %K/%D cross next cycle -- never a safety issue), so
# it's excluded from the strict conflict check entirely -- strip_data_path()
# removes it from all three trees before merge-tree runs, then
# graft_data_blob() re-adds THIS run's own version afterward (local always
# wins for this one file, since it's the most-recently-computed value). A
# genuine conflict in any other file still fails loudly exactly as before --
# this narrows what gets auto-resolved, it doesn't loosen the conflict check
# in general.
#
# --merge-base must be a real commit, not a bare tree (2026-08-18 fix,
# confirmed live on the MA Pullback agent's second scheduled hourly cycle):
# this 2026-08-05 comment used to claim the opposite ("investigated and
# ruled out an alternate 'merge-base needs a commit not a tree' theory --
# tested directly, git accepts a tree object for --merge-base identically to
# a commit here") based on local testing at the time. That was wrong, or at
# least not portable -- the actual cloud sandbox's git build rejected every
# single concurrent-push merge with "expected commit type, but the object
# dereferences to tree type" regardless of whether the two runs' changes
# genuinely overlapped, misreporting every ordinary concurrent push as an
# unresolvable conflict. `git merge-tree`'s own docs describe --merge-base
# as taking "that commit," not a tree, so the earlier tolerance was
# undocumented git-version-specific leniency, not a supported behavior to
# rely on. Fixed by wrapping the stripped base tree in a throwaway,
# unreferenced commit object (git commit-tree, never pushed anywhere -- see
# stripped_base_commit below) before passing it to --merge-base, so this no
# longer depends on which git build happens to be running it.
#
# Usage:
#   scripts/sync_state.sh pull   # refresh local data/ from origin/bot-state
#   scripts/sync_state.sh push   # commit current data/ to origin/bot-state
set -euo pipefail

# Hang-proofing (2026-08-14, at the user's direction, after a live hourly-
# signal-check cycle froze mid-run -- reconciliation completed, then nothing
# for 14+ minutes with no error, right around where this script's push()
# runs). git fetch/push are network calls with no built-in timeout; in a
# non-interactive cloud session, a stalled connection or a credential helper
# waiting on a prompt that will never come blocks forever instead of
# erroring -- unlike almost every other failure mode in this script, which
# already fails fast and loud. GIT_TERMINAL_PROMPT=0 makes git refuse to
# prompt at all (fails immediately instead of blocking on stdin); every
# network git call below is also wrapped in `timeout` so a stalled TCP
# connection can't hang past GIT_NET_TIMEOUT_SECONDS either. This doesn't
# explain every stalled cycle seen so far (some never got far enough to
# reach this script at all -- see the checkout-freshness guard added
# 2026-08-07 for a different, earlier-stage class of staleness), but it
# closes a real gap: an unattended script should never be able to hang
# silently and indefinitely on a single git command.
export GIT_TERMINAL_PROMPT=0
GIT_NET_TIMEOUT_SECONDS="${GIT_NET_TIMEOUT_SECONDS:-30}"

# git_net <git-args...> -- run a network-touching git command (fetch/push)
# under a hard timeout, when `timeout` is available. Exit code 124
# specifically means "timed out" (see `timeout`'s own man page) -- callers
# that branch on the exit code use that to give an accurate message rather
# than conflating a timeout with an ordinary git failure (e.g. "no branch
# yet"). Degrades to plain `git` (no timeout) if `timeout` isn't on PATH --
# confirmed absent on a bare macOS dev machine (BSD userland, no GNU
# coreutils) -- so this can't turn into a hard dependency that breaks a
# working environment that happens to lack it; GIT_TERMINAL_PROMPT=0 above
# still applies either way and is the more load-bearing half of this fix.
git_net() {
    if command -v timeout >/dev/null 2>&1; then
        timeout "$GIT_NET_TIMEOUT_SECONDS" git "$@"
    else
        git "$@"
    fi
}

cmd="${1:-}"
cd "$(git rev-parse --show-toplevel)"
BASE_MARKER=".bot_state_base"

# PENDING_EXEMPT_PATH: the one file excluded from strict conflict detection
# (see header comment). Path is relative to data/, matching how it appears
# in `git ls-tree <tree> data`'s output.
PENDING_EXEMPT_PATH="pending_entries.json"

# strip_data_path <tree> <path-under-data> -- returns a new tree identical
# to <tree> but with data/<path> removed entirely (a no-op, returning <tree>
# unchanged, if that path wasn't present). Used to make a 3-way merge blind
# to a specific low-stakes file so a conflict there can never block it.
strip_data_path() {
    local tree="$1" path="$2" dt new_dt
    dt=$(git ls-tree "$tree" data | awk '{print $3}')
    if [ -z "$dt" ]; then
        echo "$tree"
        return 0
    fi
    new_dt=$(git ls-tree "$dt" | grep -v "	${path}\$" || true)
    if [ -z "$new_dt" ]; then
        # data/ would become empty (this exempted path was its only entry)
        # -- point "data" at an actual empty tree object rather than an
        # empty-list `git mktree`, which would produce a root tree with no
        # "data" entry at all and break the "root tree = exactly one data
        # entry" invariant the rest of this script relies on. Vanishingly
        # unlikely in practice (data/ always has candidates.json etc.
        # alongside pending_entries.json), but correct is cheap here.
        local empty_tree
        empty_tree=$(printf "" | git mktree)
        printf "040000 tree %s\tdata\n" "$empty_tree" | git mktree
        return 0
    fi
    new_dt=$(echo "$new_dt" | git mktree)
    printf "040000 tree %s\tdata\n" "$new_dt" | git mktree
}

# graft_data_blob <tree> <path-under-data> <blob-sha> -- returns a new tree
# identical to <tree> but with data/<path> set to <blob-sha> (added or
# replaced). Used to re-insert the exempted file after a merge that never
# saw it.
graft_data_blob() {
    local tree="$1" path="$2" blob="$3" dt new_dt
    dt=$(git ls-tree "$tree" data | awk '{print $3}')
    new_dt=$( { [ -n "$dt" ] && git ls-tree "$dt" | grep -v "	${path}\$"; printf "100644 blob %s\t%s\n" "$blob" "$path"; } | git mktree )
    printf "040000 tree %s\tdata\n" "$new_dt" | git mktree
}

pull() {
    local fetch_status=0
    git_net fetch origin bot-state 2>/dev/null || fetch_status=$?
    if [ "$fetch_status" -ne 0 ]; then
        if [ "$fetch_status" -eq 124 ]; then
            echo "sync_state pull: git fetch timed out after ${GIT_NET_TIMEOUT_SECONDS}s -- treating as a real failure, not a missing branch. Not proceeding with a possibly-stale local data/." >&2
            exit 1
        fi
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
        git_net fetch origin bot-state 2>/dev/null || true
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
        git_net fetch origin bot-state 2>/dev/null || true
        local remote_head
        remote_head=$(git rev-parse --verify -q origin/bot-state 2>/dev/null || echo "")

        if [ -n "$remote_head" ] && [ "$remote_head" != "$cur_parent" ]; then
            echo "sync_state push: bot-state moved since our pull (attempt $attempt/$max_attempts) -- merging before pushing" >&2
            local remote_tree merge_out merge_status merge_err merged_tree
            local our_pending_blob stripped_base stripped_remote stripped_cur
            remote_tree=$(git rev-parse "${remote_head}^{tree}")

            # Our own pending_entries.json (may be absent) -- carried through
            # the merge separately, see header comment and strip/graft above.
            our_pending_blob=$(git rev-parse -q --verify "${cur_tree}:data/${PENDING_EXEMPT_PATH}" 2>/dev/null || echo "")
            stripped_base=$(strip_data_path "$base_tree" "$PENDING_EXEMPT_PATH")
            stripped_remote=$(strip_data_path "$remote_tree" "$PENDING_EXEMPT_PATH")
            stripped_cur=$(strip_data_path "$cur_tree" "$PENDING_EXEMPT_PATH")

            # git merge-tree wants commits for ALL THREE operands, not bare
            # trees -- not just --merge-base (2026-08-18 fix wrapped only
            # that one). Confirmed live 2026-08-19: a cycle with a genuinely
            # clean, non-conflicting concurrent update still failed with
            # "expected commit type, but the object dereferences to tree
            # type" because stripped_remote/stripped_cur (both bare trees
            # from strip_data_path, which returns mktree output) were passed
            # straight through -- same class of error the 2026-08-18 fix
            # addressed for --merge-base, just left on the other two
            # arguments. Wrap all three the same way. None of these commits
            # are ever referenced by any branch/tag -- just unreachable
            # objects git will eventually gc, nothing to clean up.
            local stripped_base_commit stripped_remote_commit stripped_cur_commit
            stripped_base_commit=$(git commit-tree "$stripped_base" -m "sync_state.sh synthetic merge-base")
            stripped_remote_commit=$(git commit-tree "$stripped_remote" -m "sync_state.sh synthetic remote")
            stripped_cur_commit=$(git commit-tree "$stripped_cur" -m "sync_state.sh synthetic local")

            merge_err=$(mktemp)
            set +e
            merge_out=$(git merge-tree --write-tree --merge-base="$stripped_base_commit" "$stripped_remote_commit" "$stripped_cur_commit" 2>"$merge_err")
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
            if [ -n "$our_pending_blob" ]; then
                merged_tree=$(graft_data_blob "$merged_tree" "$PENDING_EXEMPT_PATH" "$our_pending_blob")
            fi
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
        if git_net push origin "${new_commit}:refs/heads/bot-state" 2>"$push_err"; then
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
