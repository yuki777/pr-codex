#!/usr/bin/env bash
set -eu

# Bootstrap pr-codex Hermes Phase 0 automation on a local machine.
# The script copies repo-managed scripts/profiles into ~/.hermes, creates the
# pr-codex board, seeds watcher state, and optionally creates Hermes cron jobs.

REPO="${PR_CODEX_REPO:-yuki777/pr-codex}"
BOARD="${PR_CODEX_HERMES_BOARD:-pr-codex}"
TENANT="${PR_CODEX_HERMES_TENANT:-yuki777/pr-codex}"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
WITH_CRON=0
FORCE=0

usage() {
  cat <<EOF
Usage: $0 [--with-cron] [--force]

Options:
  --with-cron  Create the three Hermes cron jobs. Without this, commands are printed only.
  --force      Overwrite profile SOUL.md files for the five pr-codex profiles.

Environment:
  PR_CODEX_REPO=$REPO
  PR_CODEX_HERMES_BOARD=$BOARD
  PR_CODEX_HERMES_TENANT=$TENANT
  HERMES_HOME=$HERMES_HOME
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --with-cron) WITH_CRON=1 ;;
    --force) FORCE=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

if ! command -v gh >/dev/null 2>&1; then
  echo "error: gh is required" >&2
  exit 1
fi
if ! command -v hermes >/dev/null 2>&1; then
  echo "error: hermes is required for installation; scripts can still be reviewed in the repo" >&2
  exit 1
fi

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
ROOT_DIR="$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)"

mkdir -p "$HERMES_HOME/scripts" "$HERMES_HOME/automation/pr-codex"
install -m 0755 "$ROOT_DIR/hermes/scripts/_pr_codex_common.py" "$HERMES_HOME/scripts/_pr_codex_common.py"
install -m 0755 "$ROOT_DIR/hermes/scripts/pr_codex_watch.py" "$HERMES_HOME/scripts/pr_codex_watch.py"
install -m 0755 "$ROOT_DIR/hermes/scripts/pr_codex_daily_digest.py" "$HERMES_HOME/scripts/pr_codex_daily_digest.py"
install -m 0755 "$ROOT_DIR/hermes/scripts/pr_codex_kanban_health.py" "$HERMES_HOME/scripts/pr_codex_kanban_health.py"

for profile in issue-triager pr-reviewer review-triager developer sheriff; do
  profile_dir="$HERMES_HOME/profiles/$profile"
  if [ ! -d "$profile_dir" ]; then
    hermes profile create "$profile" --clone >/dev/null 2>&1 || hermes profile create "$profile"
  fi
  mkdir -p "$profile_dir"
  source_profile="$ROOT_DIR/hermes/profiles/$profile.md"
  if [ "$FORCE" -eq 1 ] || [ ! -s "$profile_dir/SOUL.md" ]; then
    cp "$source_profile" "$profile_dir/SOUL.md"
  else
    cp "$source_profile" "$profile_dir/SOUL.pr-codex.md"
    echo "kept existing $profile_dir/SOUL.md; wrote SOUL.pr-codex.md"
  fi
  hermes -p "$profile" skills install devops/kanban-worker >/dev/null 2>&1 || true
done

hermes kanban boards create "$BOARD" \
  --name "pr-codex automation" \
  --description "Phase 0 GitHub watcher tasks for $REPO" \
  --switch >/dev/null 2>&1 || hermes kanban boards switch "$BOARD"

python3 "$HERMES_HOME/scripts/pr_codex_watch.py" \
  --repo "$REPO" \
  --board "$BOARD" \
  --tenant "$TENANT" \
  --seed \
  --sink print \
  --json

WATCH_PROMPT="Run the local Phase 0 watcher command and report a concise summary only: python3 $HERMES_HOME/scripts/pr_codex_watch.py --repo $REPO --board $BOARD --tenant $TENANT --sink hermes --json"
HEALTH_PROMPT="Run the local Phase 0 Kanban health command and report only anomalies: python3 $HERMES_HOME/scripts/pr_codex_kanban_health.py --repo $REPO --board $BOARD --tenant $TENANT --sink hermes --json"
DIGEST_PROMPT="Run the local Phase 0 daily digest command and deliver the summary: python3 $HERMES_HOME/scripts/pr_codex_daily_digest.py --repo $REPO --board $BOARD --tenant $TENANT --sink hermes --json"

if [ "$WITH_CRON" -eq 1 ]; then
  hermes -p sheriff cron create "every 10m" "$WATCH_PROMPT" --name "pr-codex-watch-github"
  hermes -p sheriff cron create "every 30m" "$HEALTH_PROMPT" --name "pr-codex-kanban-health"
  hermes -p sheriff cron create "0 9 * * *" "$DIGEST_PROMPT" --name "pr-codex-daily-digest"
else
  cat <<EOF

Cron creation was not requested. Review existing jobs first, then run:

hermes -p sheriff cron create "every 10m" "$WATCH_PROMPT" --name "pr-codex-watch-github"
hermes -p sheriff cron create "every 30m" "$HEALTH_PROMPT" --name "pr-codex-kanban-health"
hermes -p sheriff cron create "0 9 * * *" "$DIGEST_PROMPT" --name "pr-codex-daily-digest"

Then start/ensure the gateway scheduler:

hermes gateway start
EOF
fi
