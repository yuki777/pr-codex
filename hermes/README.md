# Hermes Agent automation for pr-codex

This directory contains the Phase 0 implementation for Issue #28 (`hermes-agent`).
It keeps cron focused on polling/delta detection and delegates real work to Hermes
Kanban tasks assigned to profile-specific workers.

## Scope

Phase 0 is a read-only observer:

- Poll GitHub Issues, PRs, reviews, review comments, and unresolved review threads.
- Create/deduplicate Hermes Kanban tasks for new deltas.
- Seed existing open Issues/PRs so the first run only picks up future changes.
- Produce local/Discord daily and health summaries.
- Do **not** post to GitHub, push commits, change labels, approve, request changes, close, or merge.

## Files

```text
hermes/
  install_phase0.sh                  # local bootstrap helper
  pr-codex.phase0.json               # machine-readable Phase 0 config
  profiles/                          # profile SOUL.md templates
    issue-triager.md
    pr-reviewer.md
    review-triager.md
    developer.md
    sheriff.md
  scripts/
    _pr_codex_common.py
    pr_codex_watch.py                # GitHub delta watcher -> Kanban task
    pr_codex_kanban_health.py        # blocked/stale/retry/ready checks
    pr_codex_daily_digest.py         # daily Issue/PR/Kanban summary
```

The runtime state defaults to:

```text
~/.hermes/automation/pr-codex/state.json
~/.hermes/automation/pr-codex/tasks.jsonl   # outbox fallback when Hermes is absent
```

## Bootstrap

Hermes is not required to run the unit tests, but it is required for a real Kanban
installation. The setup uses documented Hermes primitives: profiles have isolated
state directories and aliases, Kanban boards are selected with `--board`, tasks are
created with `hermes kanban create ... --assignee ... --idempotency-key ...`, and
cron jobs are created with `hermes cron create` / profile aliases.

```bash
# Copy scripts/profiles, create board, and seed state without creating tasks.
./hermes/install_phase0.sh

# After reviewing existing cron jobs, create the three scheduled jobs too.
./hermes/install_phase0.sh --with-cron
```

The installer creates or updates:

- profiles: `issue-triager`, `pr-reviewer`, `review-triager`, `developer`, `sheriff`
- board: `pr-codex`
- scripts under `~/.hermes/scripts/`
- state under `~/.hermes/automation/pr-codex/`
- optional cron jobs:
  - `pr-codex-watch-github` every 10 minutes
  - `pr-codex-kanban-health` every 30 minutes
  - `pr-codex-daily-digest` daily at 09:00

Run the gateway scheduler after cron creation:

```bash
hermes gateway start
```

## Manual operation

Seed current open Issues/PRs so historical items do not create a burst of tasks:

```bash
python3 hermes/scripts/pr_codex_watch.py --seed --sink print --json
```

Run the watcher against GitHub and create Kanban tasks when Hermes is installed:

```bash
python3 hermes/scripts/pr_codex_watch.py --sink hermes --json
```

Run without Hermes by using the append-only outbox:

```bash
python3 hermes/scripts/pr_codex_watch.py --sink outbox --json
```

Generate reports:

```bash
python3 hermes/scripts/pr_codex_kanban_health.py --sink none
python3 hermes/scripts/pr_codex_daily_digest.py --sink none
```

Set `HERMES_DISCORD_WEBHOOK_URL` or `DISCORD_WEBHOOK_URL` to post health/digest
output to Discord.

## Idempotency and self-comment filtering

The watcher stores event keys in `state.json` and also passes the same key to
Hermes Kanban via `--idempotency-key` when creating tasks. Examples:

```text
issue:new:#27
issue:update:#27:<updated_at>
pr:new:#25:<head_sha>
pr:update:#25:<head_sha>
review:new:#25:<review_id>
review_comment:new:#25:<comment_id>
review_thread:unresolved:#25:<thread_id>
```

Comments containing the sentinel below are ignored so Hermes does not react to its
own automated GitHub posts in later phases:

```markdown
<!-- hermes-auto:pr-codex pr-review v1 pr=25 head=<sha> -->
```

## Profile policy highlights

- `issue-triager`: classification, label proposals, duplicate/related Issue checks,
  dependency graph, ready/blocked split, and recommended implementation order.
- `pr-reviewer`: focuses on `pr-codex` review/send contracts and posts only
  Must Fix/High confidence findings when posting is enabled in a later phase.
- `review-triager`: decides whether new PR feedback needs action and recommends a
  `developer` task only when clear.
- `developer`: may push only to same-repo PR branches, never `main`, never forks,
  never force-push/merge, and only after tests pass.
- `sheriff`: health checks, blocked/stale surfacing, and daily digest.

## References

- Hermes Kanban board isolation, task concepts, and idempotent create: <https://hermes-agent.nousresearch.com/docs/user-guide/features/kanban>
- Hermes profile isolation and aliases: <https://hermes-agent.nousresearch.com/docs/user-guide/profiles/>
- Hermes cron creation and gateway scheduler behavior: <https://hermes-agent.lzw.me/docs/en/user-guide/features/cron>
