# Hermes Agent automation for pr-codex

This directory contains the Phase 0 implementation for Issue #28 (`hermes-agent`)
plus the Phase 1B default-off publication policy for Issue #43.  It keeps cron
focused on polling/delta detection and delegates real work to Hermes Kanban tasks
assigned to profile-specific workers.

## Scope

Phase 0 is a read-only observer:

- Poll GitHub Issues, PRs, reviews, review comments, and unresolved review threads.
- Create/deduplicate Hermes Kanban tasks for new deltas.
- Seed existing open Issues/PRs so the first run only picks up future changes.
- Produce local/Discord daily and health summaries.
- Do **not** post to GitHub, push commits, change labels, approve, request changes, close, or merge.

Phase 1B adds a dry-run/default-off issue-triager publisher.  It may append an
Issue triage recommendation comment only when explicitly run with
`PR_CODEX_HERMES_ISSUE_TRIAGE_PUBLISH=1`, `--publish`, and `--sink github`.
It still must not edit labels, milestones, assignees, titles, close/reopen,
lock/unlock, or edit existing comments.

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
    issue_triager_publish.py          # Phase 1B dry-run/default-off Issue comment publisher
    pr_codex_watch.py                # GitHub delta watcher -> Kanban task
    pr_codex_kanban_health.py        # blocked/stale/retry/ready checks
    pr_codex_daily_digest.py         # daily Issue/PR/Kanban summary
```

The runtime state defaults to the global Hermes root (or `PR_CODEX_HERMES_ROOT` when set):

```text
$PR_CODEX_HERMES_ROOT/automation/pr-codex/state.json
$PR_CODEX_HERMES_ROOT/automation/pr-codex/tasks.jsonl   # outbox fallback when Hermes is absent
```

The installer also passes absolute `--state` and `--outbox` paths into generated cron prompts so Hermes profile-specific `HOME` values do not split seeded watcher state from scheduled watcher state. Direct consumers of `pr-codex.phase0.json` should set `PR_CODEX_HERMES_ROOT` to the same global Hermes root before executing the JSON cron command templates; the templates intentionally avoid `~/.hermes` fallbacks.

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

Environment defaults can be overridden with `PR_CODEX_REPO`, `PR_CODEX_HERMES_BOARD`, `PR_CODEX_HERMES_TENANT`, and `PR_CODEX_HERMES_ROOT`. The installer deliberately ignores ambient `HERMES_HOME` because Hermes profile sessions may set it to `~/.hermes/profiles/<profile>`; installation targets the global Hermes root unless `PR_CODEX_HERMES_ROOT` says otherwise.

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

Evaluate a completed issue-triager handoff for public publication safety.  This
prints a JSON dry-run report by default and does not write to GitHub:

```bash
python3 hermes/scripts/issue_triager_publish.py \
  --issue 43 \
  --triage triage-result.json \
  --comments issue-comments.json \
  --json
```

Actual GitHub posting is opt-in and append-only:

```bash
PR_CODEX_HERMES_ISSUE_TRIAGE_PUBLISH=1 \
python3 hermes/scripts/issue_triager_publish.py \
  --issue 43 \
  --triage triage-result.json \
  --fetch-comments \
  --publish \
  --sink github \
  --json
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

PR review tasks intentionally track new PRs once and subsequent head SHA changes.
Metadata-only PR edits at the same head (title/body/base/draft changes) are not
task-generating in Phase 0; use a later phase or a dedicated metadata watcher if
that signal becomes operationally important.

Comments containing the sentinel below are ignored only when they were authored
by trusted Hermes automation, so a public marker pasted by an external commenter
does not hide actionable feedback:

```markdown
<!-- hermes-auto:pr-codex pr-review v1 pr=25 head=<sha> -->
```

Phase 1B issue-triager publication uses a more specific sentinel and publisher
idempotency key:

```markdown
<!-- hermes-auto:pr-codex issue-triage v1 issue=#<N> hash=<sha8> -->
```

```text
issue_triage:publish:#<N>:<sha8>
```

`hash` is the first eight characters of SHA-256 over the scrubbed public comment
body, excluding the sentinel.  The publisher first reads existing Issue comments,
trusts only comments that have both the sentinel and an allowed author/app, and
skips when the same hash already exists.  If the scrubbed conclusion changes, it
appends a new comment with the new hash rather than editing prior comments.
Untrusted comments that paste the marker are not treated as Hermes publications.

By default, the repository owner (`yuki777` for this repo) is the trusted comment
author because the current GitHub auth posts automation comments as that user.
Override or extend this with comma-separated environment variables when using a
dedicated bot or GitHub App:

```bash
export PR_CODEX_HERMES_AUTO_AUTHORS="yuki777,pr-codex-bot"
export PR_CODEX_HERMES_AUTO_APPS="pr-codex-hermes"
```

## Phase 1B Issue triage publication policy

`issue-triager` may publish only small recommendation comments built from an
allow-list:

- classification (`bug` / `feature` / `docs` / `infra` / `other`)
- priority recommendation
- suggested labels, clearly marked as proposals only
- duplicate/related Issue numbers
- dependencies and ready/blocked status
- recommended next action in 1–3 short lines

The scrubber removes or masks secrets, API keys, tokens, credentials, Bearer
headers, env secrets, local private paths (`/Users/...`, `/home/...`,
`~/.hermes/...`, `.agent-orchestrator/...`), raw logs/stack traces, raw
GraphQL/REST payloads, private Hermes operational details, and overlong text. If
the candidate has no public substance after scrubbing, the publisher skips with
`skip_reason: "all-redacted"`.

Phase 1B never performs Issue edits: no close/reopen, label mutation, milestone
mutation, assignee mutation, lock/unlock, title edit, or previous-comment edit.
`PR_CODEX_HERMES_ISSUE_TRIAGE_PUBLISH=1` is required for GitHub writes; otherwise
the script remains a JSON dry-run report.

## Profile policy highlights

- `issue-triager`: classification, label proposals, duplicate/related Issue checks,
  dependency graph, ready/blocked split, recommended implementation order, and
  Phase 1B dry-run/default-off publication with scrubbed append-only comments.
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
