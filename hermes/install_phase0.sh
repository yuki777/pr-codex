#!/usr/bin/env bash
set -eu

# Bootstrap pr-codex Hermes Phase 0 automation on a local machine.
# The script copies repo-managed scripts/profiles into ~/.hermes, creates the
# pr-codex board, seeds watcher state, and optionally creates Hermes cron jobs.

REPO="${PR_CODEX_REPO:-yuki777/pr-codex}"
BOARD="${PR_CODEX_HERMES_BOARD:-pr-codex}"
TENANT="${PR_CODEX_HERMES_TENANT:-yuki777/pr-codex}"
HERMES_ROOT="${PR_CODEX_HERMES_ROOT:-$HOME/.hermes}"
STATE_PATH="$HERMES_ROOT/automation/pr-codex/state.json"
OUTBOX_PATH="$HERMES_ROOT/automation/pr-codex/tasks.jsonl"
WITH_CRON=0
FORCE=0

usage() {
  cat <<EOF
Usage: $0 [--with-cron] [--force]

Options:
  --with-cron  Create the four Hermes cron jobs. Without this, commands are printed only.
  --force      Overwrite profile SOUL.md files for the five pr-codex profiles.

Environment:
  PR_CODEX_REPO=$REPO
  PR_CODEX_HERMES_BOARD=$BOARD
  PR_CODEX_HERMES_TENANT=$TENANT
  PR_CODEX_HERMES_ROOT=$HERMES_ROOT
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
if ! gh api user >/dev/null 2>&1; then
  echo "error: gh is installed but cannot call GitHub API; run gh auth login or set GH_TOKEN" >&2
  exit 1
fi
if ! command -v hermes >/dev/null 2>&1; then
  echo "error: hermes is required for installation; scripts can still be reviewed in the repo" >&2
  exit 1
fi

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
ROOT_DIR="$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)"

mkdir -p "$HERMES_ROOT/scripts" "$HERMES_ROOT/automation/pr-codex"
install -m 0755 "$ROOT_DIR/hermes/scripts/_pr_codex_common.py" "$HERMES_ROOT/scripts/_pr_codex_common.py"
install -m 0755 "$ROOT_DIR/hermes/scripts/pr_codex_watch.py" "$HERMES_ROOT/scripts/pr_codex_watch.py"
install -m 0755 "$ROOT_DIR/hermes/scripts/issue_triager_publish.py" "$HERMES_ROOT/scripts/issue_triager_publish.py"
install -m 0755 "$ROOT_DIR/hermes/scripts/pr_codex_daily_digest.py" "$HERMES_ROOT/scripts/pr_codex_daily_digest.py"
install -m 0755 "$ROOT_DIR/hermes/scripts/pr_codex_kanban_health.py" "$HERMES_ROOT/scripts/pr_codex_kanban_health.py"
install -m 0755 "$ROOT_DIR/hermes/scripts/pr_codex_developer_bridge.py" "$HERMES_ROOT/scripts/pr_codex_developer_bridge.py"

for profile in issue-triager pr-reviewer review-triager developer sheriff; do
  profile_dir="$HERMES_ROOT/profiles/$profile"
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

python3 "$HERMES_ROOT/scripts/pr_codex_watch.py" \
  --repo "$REPO" \
  --board "$BOARD" \
  --tenant "$TENANT" \
  --state "$STATE_PATH" \
  --outbox "$OUTBOX_PATH" \
  --seed \
  --sink print \
  --json

WATCH_PROMPT="Run the local Phase 0 watcher command and report a concise summary only: python3 $HERMES_ROOT/scripts/pr_codex_watch.py --repo $REPO --board $BOARD --tenant $TENANT --state $STATE_PATH --outbox $OUTBOX_PATH --sink hermes --json"
HEALTH_PROMPT="Run the local Phase 0 Kanban health command and report only anomalies: python3 $HERMES_ROOT/scripts/pr_codex_kanban_health.py --repo $REPO --board $BOARD --tenant $TENANT --outbox $OUTBOX_PATH --sink hermes --json"
DIGEST_PROMPT="Run the local Phase 0 daily digest command and deliver the summary: python3 $HERMES_ROOT/scripts/pr_codex_daily_digest.py --repo $REPO --board $BOARD --tenant $TENANT --state $STATE_PATH --outbox $OUTBOX_PATH --sink hermes --json"
DEVELOPER_BRIDGE_PROMPT="Run the local developer bridge command and deliver output only if it creates or dispatches work: python3 $HERMES_ROOT/scripts/pr_codex_developer_bridge.py --repo $REPO --board $BOARD --json"

cron_exists() {
  hermes -p sheriff cron list 2>/dev/null | grep -F -- "$1" >/dev/null 2>&1
}

create_cron_once() {
  name="$1"
  schedule="$2"
  prompt="$3"
  if cron_exists "$name"; then
    echo "cron $name already exists; skipping"
  else
    hermes -p sheriff cron create "$schedule" "$prompt" --name "$name"
  fi
}

if [ "$WITH_CRON" -eq 1 ]; then
  create_cron_once "pr-codex-watch-github" "every 10m" "$WATCH_PROMPT"
  create_cron_once "pr-codex-kanban-health" "every 30m" "$HEALTH_PROMPT"
  create_cron_once "pr-codex-daily-digest" "0 9 * * *" "$DIGEST_PROMPT"
  create_cron_once "pr-codex-developer-bridge" "every 15m" "$DEVELOPER_BRIDGE_PROMPT"
else
  cat <<EOF

Cron creation was not requested. Review existing jobs first, then run:

hermes -p sheriff cron create "every 10m" "$WATCH_PROMPT" --name "pr-codex-watch-github"
hermes -p sheriff cron create "every 30m" "$HEALTH_PROMPT" --name "pr-codex-kanban-health"
hermes -p sheriff cron create "0 9 * * *" "$DIGEST_PROMPT" --name "pr-codex-daily-digest"
hermes -p sheriff cron create "every 15m" "$DEVELOPER_BRIDGE_PROMPT" --name "pr-codex-developer-bridge"

Then start/ensure the gateway scheduler:

hermes gateway start
EOF
fi
