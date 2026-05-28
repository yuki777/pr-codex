---
user-invocable: true
name: pr-codex
description: "GitHub PRを Claude Code と Codex CLI の2者レビュー方式で自動レビューする"
argument-hint: "[<PR URL|PR number>]"
allowed-tools: ["Bash", "Read", "Write", "Glob", "Grep"]
---

# pr-codex

GitHubのレビュー依頼PRを自動レビューするコマンド。Claude Code と Codex CLI の2者レビュー方式。

## 引数

The user invoked this with: `$ARGUMENTS`

起動直後に Claude 側で `$ARGUMENTS` を解析し、レビュー対象の直接指定を `$review_target` として保持する。レビュー深度 (`standard` / `deep`) は引数では受け付けず、Step 3 の `run-plan.json` で常に自動判定する。

- 引数なし: `$review_target = ""`。従来どおり Search API でレビュー依頼 PR を自動検索・選定する
- `https://github.com/<org>/<repo>/pull/<number>`: 指定された PR を直接レビューする
- `<number>`: 現在の git repository の `origin` を対象 repo として、指定された PR 番号を直接レビューする

上記以外の引数、複数引数、または `--deep` / `--standard` などのオプションが含まれる場合は、ユーザーに `unsupported argument: <value>。使える引数は PR URL または PR 番号のみです。depth は自動判定します。` と報告して **処理を中断** する。未知の引数を silent ignore してレビューを続行してはならない。

PR 番号のみの指定は、現在の working directory が対象 repository の git checkout であり `gh repo view --json nameWithOwner --jq .nameWithOwner` で `owner/repo` を解決できる場合だけ有効とする。`~/claude-loop-pr-codex` など repository context がない場所で PR 番号のみが指定された場合は、推測せず PR URL 指定を案内して中断する。

## セットアップ

`~/claude-loop-pr-codex/` をワーキングディレクトリとしてClaude Codeを起動する:

```bash
plugin_root="${CLAUDE_PLUGIN_ROOT:-$(python3 - <<'PY'
import os
from pathlib import Path
roots = []
if os.environ.get("CLAUDE_CODE_PLUGIN_CACHE_DIR"):
    roots.append(Path(os.environ["CLAUDE_CODE_PLUGIN_CACHE_DIR"]).expanduser())
if os.environ.get("CLAUDE_CONFIG_DIR"):
    roots.append(Path(os.environ["CLAUDE_CONFIG_DIR"]).expanduser() / "plugins" / "cache")
roots.append(Path.home() / ".claude" / "plugins" / "cache")
markers = []
for root in roots:
    markers.extend(root.glob("**/pr-codex/tasks/validate_findings.py"))
if not markers:
    raise SystemExit("CLAUDE_PLUGIN_ROOT is unset and pr-codex plugin root was not found")
marker = max((m.resolve() for m in markers), key=lambda p: (p.stat().st_mtime_ns, str(p)))
print(marker.parents[1])
PY
)}"
test -d "$plugin_root/tasks" && test -d "$plugin_root/schemas"
```

```bash
cd ~/claude-loop-pr-codex && claude --permission-mode auto --effort max
```

Codex CLI 側のレビュー実行は、本スキル内で `-m gpt-5.5` を指定して実行する。

起動後:

```
/loop 10m /pr-codex:review
/pr-codex:review https://github.com/org/repo/pull/123
/pr-codex:review 123
```

ワーキングディレクトリを `~/claude-loop-pr-codex/` にすることで、配下のファイルに直接アクセスできる。

## フロー

### Step 0: 引数解析と直接指定の解決

`$ARGUMENTS` は Claude が解釈済みの文字列として扱い、shell で再分割しない。空文字列、PR URL 1 個、PR 番号 1 個だけを受け付ける。

- 引数なしの場合: `$target_mode = "auto"` とし、Step 1 の Search API に進む
- PR URL の場合: URL から `$org` / `$repository` / `$pr_number` を取り出し、`$target_mode = "direct"` として Step 2 の自動選定をスキップする。URL は `https://github.com/<org>/<repo>/pull/<number>` だけを受け付ける
- PR 番号の場合: `gh repo view --json nameWithOwner --jq .nameWithOwner` で現在の repository を解決し、その `owner/repo` と PR 番号から `$org` / `$repository` / `$pr_number` を設定する。repository を解決できない場合は、PR URL 指定を案内して中断する

直接指定時は、`$pr_url = "https://github.com/$org/$repository/pull/$pr_number"` として保持する。`$title` と canonical な `$pr_url` は Step 2b の `gh api repos/$org/$repository/pulls/$pr_number --jq ...` で取得した値を優先する。

### Step 1: レビュー対象PR候補の取得

`$target_mode == "direct"` の場合、Step 1 は実行しない。指定された `$org` / `$repository` / `$pr_number` を使い、Step 2 の requested reviewers / approve 済み判定もスキップして、直接指定 PR の status 判定へ進む。

GitHub Search API でレビュー依頼されている Open PR を取得する。
Notifications API と異なり、リポジトリの Watch 設定に依存しない。

各テンプレートはコードブロックの内容をそのまま1回のシェル実行単位として使うこと。変数（`$MY_LOGIN`, `$org`, `$repository`, `$pr_number`, `$title`, `$pr_url`, `$branch`, `$base_branch`, `$head_sha`, `$files_json`, `$started_at`, `$finished_at`, `$exit_code`, `$failed_stage` など）の置換以外の改変は不可。

まず自分のログイン名を取得する。

- いつ使うか: Step 1 の開始時に必ず実行する
- 判定条件: 標準出力が空でない
- 次アクション: 出力を `$MY_LOGIN` として次の検索テンプレートに使う

```bash
gh api user | jq -r '.login'
```

取得したログイン名を `$MY_LOGIN` として、Search API でレビュー依頼PRを検索する。

- いつ使うか: `$MY_LOGIN` 取得後に必ず実行する
- 判定条件: 各行から `org`, `repository`, `pr_number`, `title`, `pr_url` を取得できる
- 次アクション: 上から順に Step 2 の判定テンプレートへ渡す

```bash
gh api -H "Accept: application/vnd.github+json" \
  "/search/issues?q=is:pr+state:open+draft:false+review-requested:$MY_LOGIN&sort=updated&order=desc&per_page=100" \
  | jq -c '.items[] | {
    org: (.repository_url | split("/")[-2]),
    repository: (.repository_url | split("/")[-1]),
    pr_number: .number,
    title,
    pr_url: (.pull_request.html_url // .html_url)
  }'
```

**クエリパラメータの説明:**

- `is:pr` - PR のみ（Issue を除外）
- `state:open` - Open な PR のみ
- `draft:false` - Draft PR を除外
- `review-requested:$MY_LOGIN` - 自分がレビュー依頼されている PR（チームレビュー依頼も含む）
- `sort=updated&order=desc` - 更新日時の降順（最新を優先。best match のデフォルトだと古い PR が漏れる）
- `per_page=100` - 最大 100 件取得

**注意事項:**

- Search API はインデックスベースのため、レビュー依頼から数分の遅延が発生し得る（10分ポーリングなら許容範囲）
- レスポンスに `head_sha` と `branch` は含まれない（Step 2b で選定PRに対して必ず取得する）
- `review-requested:USERNAME` は GitHub docs 上、ユーザー直接指定とチーム経由の両方を含むと明記されている

### Step 2: 候補PRの選定

`$target_mode == "direct"` の場合、この自動選定は実行しない。指定 PR はレビュー依頼の有無や approve 済み状態に関係なく対象にできるため、requested reviewers / approve 済み判定をスキップする。ただし `status.json` による冪等性チェックは維持する。

取得した候補（`$org`, `$repository`, `$pr_number`, `$title`, `$pr_url`）を上から順に走査し、以下の条件で最初の1件を選定する:

1. 自分が実際にレビュー対象かチェック

- いつ使うか: 各候補PRの最初の判定で実行する
- 判定条件: 出力された `users` または `teams` を後続テンプレートで判定できる
- 次アクション: user 直接指定または team 経由指定なら次へ、どちらでもなければスキップ

```bash
gh api repos/$org/$repository/pulls/$pr_number/requested_reviewers | jq '{users: [.users[].login], teams: [.teams[].slug]}'
```

- いつ使うか: 上の requested reviewers 出力に `.teams[].slug` が1件以上ある場合のみ実行する
- 判定条件: 出力された team slug 一覧に requested team slug が含まれるなら team 経由レビュー対象
- 次アクション: 含まれるなら approve 済み判定へ、含まれないなら user 直接指定も確認し、どちらでもなければスキップ

```bash
gh api user/teams | jq -r '.[].slug'
```

2. 自分がすでに approve 済みかチェック

- いつ使うか: レビュー対象であると判定できた候補に対して実行する
- 判定条件: 出力に `$MY_LOGIN` が含まれるなら approve 済み
- 次アクション: approve 済みならスキップ、含まれなければ status 判定へ進む

```bash
gh pr view $pr_number --repo $org/$repository --json reviews | jq -r '.reviews[] | select(.state == "APPROVED") | .author.login'
```

3. `status.json` を確認

- いつ使うか: approve 済みでない候補に対して実行する
- 判定条件: ファイルが存在しなければ未レビュー
- 次アクション: 存在しなければ選定、存在すれば内容判定へ進む

```bash
test -f ~/claude-loop-pr-codex/$org-$repository-$pr_number/status.json
```

- いつ使うか: `status.json` が存在する場合に実行する
- 判定条件: `state` が `failed` なら再実行対象
- 次アクション: `failed` なら選定、それ以外は次の状態判定へ進む

```bash
jq -r '.state' ~/claude-loop-pr-codex/$org-$repository-$pr_number/status.json
```

- いつ使うか: `state == "running"` の場合に実行する
- 判定条件: `started_at` から30分超過なら stale
- 次アクション: stale なら選定、30分以内ならスキップ

```bash
jq -r '.started_at' ~/claude-loop-pr-codex/$org-$repository-$pr_number/status.json
```

4. `state == "completed"` の場合は Step 2b で現在の `head_sha` だけを先に取得し、保存済み `head_sha` と比較するため、**ここでは選定を確定せずに必ず Step 2b に進む**。同一 `head_sha` なら PR 変更ファイル一覧の取得は行わずスキップする。

全候補がスキップなら何もせず終了。

#### 直接指定 PR の status 判定

`$target_mode == "direct"` の場合は、Search API の候補走査ではなく、指定された `$org` / `$repository` / `$pr_number` の `status.json` だけを確認する。`status.json` が存在しない、`state == "failed"`、または `state == "running"` だが 30 分超過で stale の場合は Step 2b へ進む。`state == "running"` かつ 30 分以内ならスキップする。`state == "completed"` の場合は Step 2b で現在の `head_sha` を取得し、保存済み `head_sha` と一致すればスキップ、異なれば追加コミットありとしてレビューを再実行する。

### Step 2b: 選定PRの `repository_full_name` / `head_sha` / `base_sha` / `branch` / `base_branch` / `merge_commit_sha` / `title` / `pr_url` / `files` を取得

Step 2 で対象PRを1件選定した直後、または Step 0 で直接指定 PR を解決した直後、未レビュー / failed / stale / completed のどの経路でもまず `repository_full_name` / `head_sha` / `base_sha` / `branch` / `base_branch` / `merge_commit_sha` / `title` / `pr_url` を取得する。`repository_full_name` は fork head repo ではなく、GitHub review の投稿先と同じ **base repo** (`.base.repo.full_name`) とする。`state == "completed"` の場合はこの時点で保存済み `head_sha` と比較し、同一ならスキップして PR 変更ファイル一覧は取得しない。未レビュー / failed / stale、または completed だが `head_sha` が変わっている場合だけ、完全な `files[]` を REST API paginate で取得して Step 3 へ進む。Step 3 の clone と `metadata.json` / `run-plan.json` 作成・Step 4 の PR 差分スコープ制御・Step 4c の canonical findings 生成は `$repository_full_name` / `$head_sha` / `$base_sha` / `$branch` / `$base_branch` / `$merge_commit_sha` / `$title` / `$pr_url` / `$files_json` に依存するため、選定後に欠落すると後続が破綻する。

まず `repository_full_name` / `head_sha` / `base_sha` / `branch` / `base_branch` / `merge_commit_sha` / `title` / `pr_url` を取得する。

- いつ使うか: Step 2 で対象PRを1件選定した直後、または Step 0 で直接指定 PR を解決した直後に必ず実行する
- 判定条件: 標準出力に `{"repository_full_name":"...","head_sha":"...","base_sha":"...","branch":"...","base_branch":"...","merge_commit_sha":"...","title":"...","pr_url":"..."}` の JSON が出力される（required field のいずれかが欠落した場合は `gh api --jq` が非ゼロ終了し、stderr に `missing <field>` が出る。`merge_commit_sha` は open PR では空文字列でよい）
- 次アクション: 出力 JSON の `.repository_full_name` を `$repository_full_name`、`.head_sha` を `$head_sha`、`.base_sha` を `$base_sha`、`.branch` を `$branch`、`.base_branch` を `$base_branch`、`.merge_commit_sha` を `$merge_commit_sha`、`.title` を `$title`、`.pr_url` を `$pr_url` に保持する。`state == "completed"` の場合は、続く保存済み `head_sha` 比較テンプレートを先に実行する。一致したらこの候補をスキップし、異なる場合だけ **別テンプレートで完全な `files[]` を取得**して Step 3 へ進む。`state != "completed"` の場合はそのまま完全な `files[]` を取得する

```bash
gh api repos/$org/$repository/pulls/$pr_number --jq '
  {
    repository_full_name: .base.repo.full_name,
    head_sha: .head.sha,
    base_sha: .base.sha,
    branch: .head.ref,
    base_branch: .base.ref,
    merge_commit_sha: (.merge_commit_sha // ""),
    title: .title,
    pr_url: .html_url
  }
  | if ((.repository_full_name // "") == "") then error("missing repository_full_name")
    elif ((.head_sha // "") == "") then error("missing head_sha")
    elif ((.base_sha // "") == "") then error("missing base_sha")
    elif ((.branch // "") == "") then error("missing branch")
    elif ((.base_branch // "") == "") then error("missing base_branch")
    elif ((.title // "") == "") then error("missing title")
    elif ((.pr_url // "") == "") then error("missing pr_url")
    else . end
'
```

#### `state == "completed"` の場合の保存済み `head_sha` 比較

- いつ使うか: Step 2 で `state == "completed"` と判定し、上の `gh api --jq` で現在の `$head_sha` を取得した直後に実行する
- 判定条件: 保存済み `head_sha` を取得できる
- 次アクション: 保存済みと現在 (`$head_sha`) が異なれば追加コミットありとしてこの候補を選定し、PR 変更ファイル一覧の取得へ進む。一致するなら PR 変更ファイル一覧は取得せず、この候補はスキップして Step 2 で次の候補に戻る

```bash
jq -r '.head_sha' ~/claude-loop-pr-codex/$org-$repository-$pr_number/metadata.json
```

#### PR 変更ファイル一覧の取得

続いて、選定が確定した PR の変更ファイル一覧を **REST API の paginate** で全件取得する。`gh pr view --json files` は 100 件で truncate され得るため、`files_changed > 100` 判定と `metadata.json.files[]` の完全性を守るにはこのテンプレートを使う必要がある。

- いつ使うか: `state != "completed"` の候補、または `state == "completed"` だが保存済み `head_sha` と現在の `$head_sha` が異なる候補に対して実行する
- 判定条件: 標準出力に `[` で始まる非空の JSON 配列が出力される。`set -o pipefail` により `gh api --paginate` が途中ページ出力後に失敗した場合もパイプライン全体が非ゼロ終了する
- 次アクション: 出力された JSON 配列そのものを `$files_json` として保持する（Bash 変数には JSON 配列文字列をそのまま入れる）。empty files の PR は `missing files` エラーで fail-fast する

```bash
set -o pipefail && gh api repos/$org/$repository/pulls/$pr_number/files --paginate | jq -sce '[.[][] | .filename] | if length == 0 then error("missing files") else . end'
```

`$files_json` の担保理由: Step 4a / 4b で「PR 差分範囲外のファイルをレビュー対象にしない」制約を効かせるため、PR 変更ファイルの一覧を確定情報として skill 下流に伝達する必要がある。Step 2b では REST paginate を使って 101 ファイル以上の PR でも完全な一覧を保持する。

#### 変数の保持例

1つ目の `gh api --jq` の出力が `{"repository_full_name":"octo/example","head_sha":"deadbeef01","base_sha":"cafebabe02","branch":"feat/dark-mode","base_branch":"main","merge_commit_sha":"","title":"Dark mode","pr_url":"https://github.com/octo/example/pull/123"}`、2つ目の `jq -sce` の出力が `["src/theme.ts","src/App.tsx"]` の場合、以下のように Bash 変数へ保持する:

- `$repository_full_name = octo/example`（base repo の owner/repo 形式の文字列。fork PR でも投稿先 repo と一致させる）
- `$head_sha = deadbeef01`（文字列そのまま）
- `$base_sha = cafebabe02`（文字列そのまま）
- `$branch = feat/dark-mode`（文字列そのまま）
- `$base_branch = main`（文字列そのまま）
- `$merge_commit_sha = ""`（open PR では空文字列。merge commit が取得できる場合はその SHA）
- `$title = Dark mode`（文字列そのまま）
- `$pr_url = https://github.com/octo/example/pull/123`（文字列そのまま）
- `$files_json = ["src/theme.ts","src/App.tsx"]`（**JSON 配列そのままの文字列**。Step 3 の metadata.json 生成で `jq --argjson files "$files_json"` に渡す）

`$files_json` は 2つ目のテンプレートの標準出力そのままを JSON 配列文字列として保持したもの。`$repository_full_name` は canonical findings の `pr.repository` にそのまま使うが、必ず `.base.repo.full_name` 由来の投稿先 repo とし、`.head.repo.full_name` 由来の fork repo を入れてはならない。`$base_sha` は `pr.base_sha` と `metadata.json.base_sha` の両方に使う。`$merge_commit_sha` は empty string の場合に限り metadata では `null` に正規化してよい。`$title` / `$pr_url` は Search API や URL parse 由来の値より Step 2b の Pulls API 由来を優先する。抽出は Claude 側で 2 回の出力を読み取って変数へ分解する（シェル側で追加の `jq` パイプは挟まない。1 テンプレート = 1 シェル実行単位の原則に従う）。

### Step 3: 作業ディレクトリの準備

- いつ使うか: Step 2 で対象PRを1件選定した直後に実行する
- 判定条件: 作業ディレクトリが存在する
- 次アクション: Desktop シンボリックリンク作成へ進む

```bash
install -d ~/claude-loop-pr-codex/$org-$repository-$pr_number
```

### Step 3a: CI read-only gate artifact の生成

対象 PR の GitHub Actions / status checks を read-only で取得し、`ci-status.json` と `ci-summary.md` を生成する。ここでは GitHub への write、workflow rerun、cancel は行わない。`statusCheckRollup` が使えない古い `gh` 環境では、同じ `ci_status.py` に REST の `pulls/{number}` と checks/status endpoint の JSON を渡す fallback を使う。

- いつ使うか: 作業ディレクトリ作成後、review hunter を起動する前に必ず実行する
- 判定条件: `ci-status.json` と `ci-summary.md` が生成され、`ci-status.json.policy.github_writes == false` / `rerun == false` / `cancel == false` である
- 次アクション: `ci-status.json.state` を review/send/developer bridge の判断材料として保持する。`failure` / `pending` は reviewer へコンテキストとして渡すが、この step 自体で投稿や rerun はしない

```bash
plugin_root="${CLAUDE_PLUGIN_ROOT:-$(python3 - <<'PY'
import os
from pathlib import Path
roots = []
if os.environ.get("CLAUDE_CODE_PLUGIN_CACHE_DIR"):
    roots.append(Path(os.environ["CLAUDE_CODE_PLUGIN_CACHE_DIR"]).expanduser())
if os.environ.get("CLAUDE_CONFIG_DIR"):
    roots.append(Path(os.environ["CLAUDE_CONFIG_DIR"]).expanduser() / "plugins" / "cache")
roots.append(Path.home() / ".claude" / "plugins" / "cache")
markers = []
for root in roots:
    markers.extend(root.glob("**/pr-codex/tasks/validate_findings.py"))
if not markers:
    raise SystemExit("CLAUDE_PLUGIN_ROOT is unset and pr-codex plugin root was not found")
marker = max((m.resolve() for m in markers), key=lambda p: (p.stat().st_mtime_ns, str(p)))
print(marker.parents[1])
PY
)}"
gh api repos/$org/$repository/pulls/$pr_number > ~/claude-loop-pr-codex/$org-$repository-$pr_number/ci-pull.json && \
gh pr view $pr_number --repo $org/$repository --json statusCheckRollup --jq '.statusCheckRollup' > ~/claude-loop-pr-codex/$org-$repository-$pr_number/ci-status-rollup.json && \
gh api "repos/$org/$repository/actions/runs?branch=$branch&head_sha=$head_sha&per_page=10" > ~/claude-loop-pr-codex/$org-$repository-$pr_number/ci-workflow-runs.json && \
python3 "$plugin_root/tasks/ci_status.py" \
  --pull-json ~/claude-loop-pr-codex/$org-$repository-$pr_number/ci-pull.json \
  --status-check-rollup-json ~/claude-loop-pr-codex/$org-$repository-$pr_number/ci-status-rollup.json \
  --workflow-runs-json ~/claude-loop-pr-codex/$org-$repository-$pr_number/ci-workflow-runs.json \
  --out-json ~/claude-loop-pr-codex/$org-$repository-$pr_number/ci-status.json \
  --out-md ~/claude-loop-pr-codex/$org-$repository-$pr_number/ci-summary.md && \
jq -e '.read_only == true and .policy.github_writes == false and .policy.rerun == false and .policy.cancel == false' ~/claude-loop-pr-codex/$org-$repository-$pr_number/ci-status.json >/dev/null
```

failed job log を読む必要がある場合も read-only download に限定し、raw log は永続化せず一時ファイルから `--failed-log job=/tmp/...` で `tasks/ci_status.py` に渡す。`ci-summary.md` には secret-like text / local path が scrub された短い要約だけを残す。

- 旧 `gh` fallback: `gh pr view --json statusCheckRollup` が使えない場合は、`gh api --paginate repos/$org/$repository/commits/$head_sha/check-runs?per_page=100`（または combined status の `statuses`）を `--status-check-rollup-json` に渡して同じ helper で正規化する。check-runs API はページング対象なので、pagination なしで最初のページだけを gate 入力にしてはならない。

```bash
plugin_root="${CLAUDE_PLUGIN_ROOT:-$(python3 - <<'PY'
import os
from pathlib import Path
roots = []
if os.environ.get("CLAUDE_CODE_PLUGIN_CACHE_DIR"):
    roots.append(Path(os.environ["CLAUDE_CODE_PLUGIN_CACHE_DIR"]).expanduser())
if os.environ.get("CLAUDE_CONFIG_DIR"):
    roots.append(Path(os.environ["CLAUDE_CONFIG_DIR"]).expanduser() / "plugins" / "cache")
roots.append(Path.home() / ".claude" / "plugins" / "cache")
markers = []
for root in roots:
    markers.extend(root.glob("**/pr-codex/tasks/validate_findings.py"))
if not markers:
    raise SystemExit("CLAUDE_PLUGIN_ROOT is unset and pr-codex plugin root was not found")
marker = max((m.resolve() for m in markers), key=lambda p: (p.stat().st_mtime_ns, str(p)))
print(marker.parents[1])
PY
)}"
gh api repos/$org/$repository/pulls/$pr_number > ~/claude-loop-pr-codex/$org-$repository-$pr_number/ci-pull.json && \
set -o pipefail && gh api --paginate "repos/$org/$repository/commits/$head_sha/check-runs?per_page=100" | jq -sc '{check_runs: [.[].check_runs[]?]}' > ~/claude-loop-pr-codex/$org-$repository-$pr_number/ci-status-rollup.json && \
gh api "repos/$org/$repository/actions/runs?branch=$branch&head_sha=$head_sha&per_page=10" > ~/claude-loop-pr-codex/$org-$repository-$pr_number/ci-workflow-runs.json && \
python3 "$plugin_root/tasks/ci_status.py" \
  --pull-json ~/claude-loop-pr-codex/$org-$repository-$pr_number/ci-pull.json \
  --status-check-rollup-json ~/claude-loop-pr-codex/$org-$repository-$pr_number/ci-status-rollup.json \
  --workflow-runs-json ~/claude-loop-pr-codex/$org-$repository-$pr_number/ci-workflow-runs.json \
  --out-json ~/claude-loop-pr-codex/$org-$repository-$pr_number/ci-status.json \
  --out-md ~/claude-loop-pr-codex/$org-$repository-$pr_number/ci-summary.md
```

- いつ使うか: 作業ディレクトリ作成後に実行する
- 判定条件: Desktop シンボリックリンクが存在する
- 次アクション: clone の初回作成または更新へ進む

```bash
ln -sfn ~/claude-loop-pr-codex/$org-$repository-$pr_number ~/Desktop/$org-$repository-$pr_number
```

PRブランチのソースコードを各ツール用に個別に clone する。初回 clone と既存 clone 更新を明確に分離する。PR 差分 (`base_branch...head`) を算出可能にするため、head を `--depth 50` で clone し、さらに `base_branch` も同じ深さで fetch する。

- いつ使うか: `clone-claude` が存在しない初回のみ実行する
- 判定条件: clone が正常作成される
- 次アクション: `clone-claude` の base 取り込みへ進む

```bash
gh repo clone $org/$repository ~/claude-loop-pr-codex/$org-$repository-$pr_number/clone-claude -- --branch $branch --depth 50
```

- いつ使うか: 上の `clone-claude` 初回 clone 直後に実行する
- 判定条件: `base_branch` が clone 内に fetch される
- 次アクション: `clone-codex` 初回 clone テンプレートへ進む

```bash
git -C ~/claude-loop-pr-codex/$org-$repository-$pr_number/clone-claude fetch origin $base_branch --depth 50
```

- いつ使うか: `clone-codex` が存在しない初回のみ実行する
- 判定条件: clone が正常作成される
- 次アクション: `clone-codex` の base 取り込みへ進む

```bash
gh repo clone $org/$repository ~/claude-loop-pr-codex/$org-$repository-$pr_number/clone-codex -- --branch $branch --depth 50
```

- いつ使うか: 上の `clone-codex` 初回 clone 直後に実行する
- 判定条件: `base_branch` が clone 内に fetch される
- 次アクション: PR diff 生成テンプレートへ進む

```bash
git -C ~/claude-loop-pr-codex/$org-$repository-$pr_number/clone-codex fetch origin $base_branch --depth 50
```

- いつ使うか: `clone-claude` が既に存在する再実行時のみ実行する
- 判定条件: fetch と checkout が成功する
- 次アクション: `clone-claude` の base 再取り込みへ進む

```bash
git -C ~/claude-loop-pr-codex/$org-$repository-$pr_number/clone-claude fetch origin $branch --depth 50 && git -C ~/claude-loop-pr-codex/$org-$repository-$pr_number/clone-claude checkout FETCH_HEAD
```

- いつ使うか: 上の `clone-claude` 再実行 fetch/checkout 直後に実行する
- 判定条件: `base_branch` が最新化される
- 次アクション: `clone-codex` 再実行 fetch テンプレートへ進む

```bash
git -C ~/claude-loop-pr-codex/$org-$repository-$pr_number/clone-claude fetch origin $base_branch --depth 50
```

- いつ使うか: `clone-codex` が既に存在する再実行時のみ実行する
- 判定条件: fetch と checkout が成功する
- 次アクション: `clone-codex` の base 再取り込みへ進む

```bash
git -C ~/claude-loop-pr-codex/$org-$repository-$pr_number/clone-codex fetch origin $branch --depth 50 && git -C ~/claude-loop-pr-codex/$org-$repository-$pr_number/clone-codex checkout FETCH_HEAD
```

- いつ使うか: 上の `clone-codex` 再実行 fetch/checkout 直後に実行する
- 判定条件: `base_branch` が最新化される
- 次アクション: PR diff 生成テンプレートへ進む

```bash
git -C ~/claude-loop-pr-codex/$org-$repository-$pr_number/clone-codex fetch origin $base_branch --depth 50
```

PR 差分を unified diff として保存する。Step 4a / 4b のレビュー対象スコープ制御に使う。

- いつ使うか: 両 clone と base fetch が完了した直後に必ず実行する（初回/再実行どちらも）
- 判定条件: `pr.diff` が非空で生成される
- 次アクション: コメント可能行範囲の抽出へ進む。`gh pr diff` が失敗または空出力の場合はここで非ゼロ終了し、Step 5 の failed 更新へ遷移する

```bash
gh pr diff $pr_number --repo $org/$repository > ~/claude-loop-pr-codex/$org-$repository-$pr_number/pr.diff && test -s ~/claude-loop-pr-codex/$org-$repository-$pr_number/pr.diff
```

`pr.diff` から GitHub Reviews API でコメント可能な新ファイル側 hunk 範囲を抽出し、`pr.diff.ranges.txt` として保存する。Step 4a / 4b のレビュー生成と Step 4c の統合時自己検証に使う。

- いつ使うか: `pr.diff` 生成直後に必ず実行する
- 判定条件: `pr.diff.ranges.txt` が作成される
- 次アクション: status/metadata 作成へ進む

```bash
plugin_root="${CLAUDE_PLUGIN_ROOT:-$(python3 - <<'PY'
import os
from pathlib import Path
roots = []
if os.environ.get("CLAUDE_CODE_PLUGIN_CACHE_DIR"):
    roots.append(Path(os.environ["CLAUDE_CODE_PLUGIN_CACHE_DIR"]).expanduser())
if os.environ.get("CLAUDE_CONFIG_DIR"):
    roots.append(Path(os.environ["CLAUDE_CONFIG_DIR"]).expanduser() / "plugins" / "cache")
roots.append(Path.home() / ".claude" / "plugins" / "cache")
markers = []
for root in roots:
    markers.extend(root.glob("**/pr-codex/tasks/validate_findings.py"))
if not markers:
    raise SystemExit("CLAUDE_PLUGIN_ROOT is unset and pr-codex plugin root was not found")
marker = max((m.resolve() for m in markers), key=lambda p: (p.stat().st_mtime_ns, str(p)))
print(marker.parents[1])
PY
)}"
test -f "$plugin_root/skills/lib/extract-diff-ranges.awk" && awk -f "$plugin_root/skills/lib/extract-diff-ranges.awk" ~/claude-loop-pr-codex/$org-$repository-$pr_number/pr.diff > ~/claude-loop-pr-codex/$org-$repository-$pr_number/pr.diff.ranges.txt
```

`plugin_root` はセットアップで自己解決済みの値を使う。`test -f` が失敗した場合は、同じ fallback block を再実行して plugin root を再解決し、まだ root を確定できない場合は silent な空ファイル生成を避けるため Step 5 の failed 更新へ遷移する。

### Step 3b: BEAR.Sunday 判定

対象リポジトリが BEAR.Sunday かどうかを `composer.json` の `bear/sunday` 依存だけで判定し、該当する場合だけ Step 4a / 4b の hunter prompt に BEAR.Sunday 固有観点を追加する。これは既存の Claude + Codex の2者レビューを置き換えるものではなく、候補収集の追加観点である。`bear-review` 由来の指摘も Step 4c の既存の verifier / severity classification / posting policy を必ず通し、直接投稿対象にしてはならない。

- いつ使うか: clone と `pr.diff.ranges.txt` 生成が完了した直後、`run-plan.json` 作成前に必ず実行する
- 判定条件: 終了コード 0 なら BEAR.Sunday、非 0 なら BEAR.Sunday ではない。`composer.json` がない / 壊れている / `bear/sunday` がない場合も通常レビューは継続する
- 次アクション: Step 4 前処理でこの終了コードと `bear-review` skill の有無を使い、`{BEAR_REVIEW_GUIDANCE}` を組み立てる

```bash
jq -e '.require | has("bear/sunday")' ~/claude-loop-pr-codex/$org-$repository-$pr_number/clone-codex/composer.json >/dev/null
```

BEAR.Sunday 判定は `bear/sunday` dependency だけを見る。`bear/resource` など他の `bear/*` package や `src/Resource` / `src/Module` などの layout signal だけでは BEAR.Sunday と判定しない。PHPMD / composer / vendor-bin / BEAR.Skills が存在しないプロジェクトでも落ちないよう、非 0 は skip 理由として扱い、通常レビューは継続する。

- `--depth 50` で shallow clone し、ディスク・時間を節約しつつ `git diff origin/$base_branch...HEAD` が算出可能な深さを確保する
- Claude Code 用: `clone-claude/`、Codex CLI 用: `clone-codex/`
- 各ツールが独立したディレクトリで動作するため、git/file操作の競合が発生しない
- `pr.diff` は両ツール共通の「PR 差分の確定情報源」として Step 4 で参照される
- `pr.diff.ranges.txt` は `pr.diff` から抽出した「コメント可能行範囲」の確定情報源として Step 4 で参照される

以下の `status.json` / `metadata.json` / `run-plan.json` は Bash で作成する（`jq -n --arg` / `--argjson` / `--slurpfile` / `--rawfile` の出力を `>` でリダイレクト）。`findings.verified.json` / `validation-report.json` / `review-rounds.json` / `review.md` は Step 4c で `Write` ツールを使い、runtime gate 通過後に final path へ反映する。
以下の `status.json` / `metadata.json` / `run-plan.json` は Bash で作成する（`jq -n --arg` / `--argjson` / `--slurpfile` / `--rawfile` の出力を `>` でリダイレクト）。`findings.candidates.json` / `findings.verified.json` / `validation-report.json` / `review.md` は Step 4c で `Write` ツールを使い、runtime gate 通過後に final path へ反映する。

まず現在時刻を取得する（出力を `$started_at` として保持する）。

```bash
date -u +%Y-%m-%dT%H:%M:%S+00:00
```

- いつ使うか: 作業ディレクトリと clone の準備完了後に実行する
- 判定条件: `status.json` が `running` で作成され、`tasks/validate_status.py` を通過する
- 次アクション: metadata 作成へ進む

```bash
plugin_root="${CLAUDE_PLUGIN_ROOT:-$(python3 - <<'PY'
import os
from pathlib import Path
roots = []
if os.environ.get("CLAUDE_CODE_PLUGIN_CACHE_DIR"):
    roots.append(Path(os.environ["CLAUDE_CODE_PLUGIN_CACHE_DIR"]).expanduser())
if os.environ.get("CLAUDE_CONFIG_DIR"):
    roots.append(Path(os.environ["CLAUDE_CONFIG_DIR"]).expanduser() / "plugins" / "cache")
roots.append(Path.home() / ".claude" / "plugins" / "cache")
markers = []
for root in roots:
    markers.extend(root.glob("**/pr-codex/tasks/validate_findings.py"))
if not markers:
    raise SystemExit("CLAUDE_PLUGIN_ROOT is unset and pr-codex plugin root was not found")
marker = max((m.resolve() for m in markers), key=lambda p: (p.stat().st_mtime_ns, str(p)))
print(marker.parents[1])
PY
)}"
jq -n --arg started_at "$started_at" --arg head_sha "$head_sha" '{state:"running",started_at:$started_at,head_sha:$head_sha,stage:"ranker",failed_stage:null}' > ~/claude-loop-pr-codex/$org-$repository-$pr_number/status.json && python3 "$plugin_root/tasks/validate_status.py" --data ~/claude-loop-pr-codex/$org-$repository-$pr_number/status.json
```

- いつ使うか: `status.json` 作成後に実行する
- 判定条件: `metadata.json` が作成される
- 次アクション: `run-plan.json` 作成へ進む

```bash
jq -n --arg org "$org" --arg repository "$repository" --arg repository_full_name "$repository_full_name" --argjson pr_number "$pr_number" --arg pr_url "$pr_url" --arg head_sha "$head_sha" --arg base_sha "$base_sha" --arg branch "$branch" --arg base_branch "$base_branch" --arg merge_commit_sha "$merge_commit_sha" --arg title "$title" --argjson files "$files_json" '{org:$org,repository:$repository,repository_full_name:$repository_full_name,pr_number:$pr_number,pr_url:$pr_url,head_sha:$head_sha,base_sha:$base_sha,branch:$branch,base_branch:$base_branch,merge_commit_sha:(if $merge_commit_sha == "" then null else $merge_commit_sha end),title:$title,files:$files}' > ~/claude-loop-pr-codex/$org-$repository-$pr_number/metadata.json
```

- いつ使うか: `metadata.json` 作成直後に必ず実行する
- 判定条件: `run-plan.json` が作成され、`files_changed` / `hunks` / `lines_added` / `lines_removed` / `risk_tags` / `selected_hunters` / `depth_actual` / `recommended_mode` / `skip_reason` / `estimated_stages` / `estimated_timeout_ms` / `actual_duration_ms` / `actual_tokens` / `review_loop` (halting policy と round metrics 初期値) が埋まる
- 次アクション: Step 4 前処理へ進む

`run-plan.json` は Step 2b の `files[]` と Step 3 の `pr.diff` を使う preflight artifact。`review_loop.halting_policy` には F5 の `max_rounds=3` / `time_budget_ms=estimated_timeout_ms` / `no_new_evidence_rounds=1` / `repeated_contradiction_limit=2` / `verifier_fail_policy=local_artifact_only` / `insufficient_evidence_policy=suppress_to_local_artifact` を固定で埋め、Step 4c が `review-rounds.json` と Step 5 の round metrics に引き継ぐ。M1 では `selected_hunters` は常に `["claude","codex"]` を出力し、将来 F4 の選定ロジックに差し替える前提で固定値とする。`recommended_mode == "skip"` は「/loop では skip 推奨、手動では警告のみ」の**提案値**であり、M1 の既定では実際のレビューを止めず `focused fallback` で継続する。
- 判定条件: `run-plan.json` が作成され、`files_changed` / `hunks` / `lines_added` / `lines_removed` / `risk_tags` / `selected_hunters` / `depth_actual` / `depth_source` / `depth_reason` / `depth_requested` / `depth_downgraded` / `depth_downgrade_reason` / `recommended_mode` / `skip_reason` / `routing_decision` / `estimated_stages` / `estimated_timeout_ms` / `actual_duration_ms` / `actual_tokens` / `cost` が埋まる
- 次アクション: Step 4 前処理へ進む

`run-plan.json` は Step 2b の `files[]` と Step 3 の `pr.diff` を使う preflight artifactで、**logical stage: ranker** の正式な出力である。レビュー深度は引数で受け付けず、`risk_tags` / PR サイズ / 大規模ガードから決定論的に自動判定する。M2 では `routing_decision` に token/duration/file-count/risk proxy 由来の `budget_class` と logical `model_profile` を残す。M3 では `cost` に provider/CLI が実際に報告した actual USD cost だけを記録し、pricing table や token からの USD 推定は持たない。`cost.source="unavailable"` の場合は推測せず null のまま残す。実プロバイダ名・実モデル名・private config は絶対に書かない。`selected_hunters` は ranker 出力の interface として配列のまま維持するが、F4 では常に `["claude","codex"]` を出力し、`routing_decision.route` も M2 では常に `"claude+codex"` とする。route enum は将来 F4 の specialist routing で拡張できるよう、ここでは固定値の hook のみ残す。`recommended_mode == "skip"` は「/loop では skip 推奨、手動では警告のみ」の**提案値**であり、M1/F4/M2 の既定では実際のレビューを止めず `focused fallback` で継続する。

`depth_actual`（`standard` / `deep`）と `recommended_mode`（`standard` / `focused` / `skip`）は直交した軸として扱う。depth は「1観点あたりの掘り下げ深さ」、recommended_mode は「対象観点の絞り込み」を表すため、`depth_actual="deep"` かつ `recommended_mode="focused"` のような組み合わせも有効である。

判定ロジックの canonical source は直後の `jq` テンプレート内の `def auto_deep` / `def depth_actual` / `def recommended_mode` / `def budget_class` / `def model_profile` とし、散文はその読み取り補助に限定する。条件が重なる場合は `jq` の評価順を優先し、表の `file-count rules` / `line-count rules` / `mode/depth rules` は同じ canonical def を参照する。`total_lines > 5000` では `depth_actual = "standard"` に強制する。

| 条件 | depth_actual | recommended_mode | budget_class | model_profile |
|---|---|---|---|---|
| `risk_tags` に `security` または `data_migration` を含み、`files_changed <= 20` かつ `total_lines <= 1500` | `deep`（auto） | file-count rules | line-count rules | mode/depth rules |
| 上記以外 | `standard` | file-count rules | line-count rules | mode/depth rules |
| `files_changed > 100` | depth rules | `skip` | `large` | `focused-fallback` |
| `50 < files_changed <= 100` | depth rules | `focused` | line-count rules | `focused-fallback` |
| `files_changed <= 50` | depth rules | `standard` | line-count rules | `deep` if `depth_actual == "deep"`, else `standard` |
| `files_changed <= 10` かつ `total_lines <= 500` かつ `sensitive_risk_count == 0` | depth rules | file-count rules | `small` | mode/depth rules |
| `files_changed <= 50` かつ `total_lines <= 5000`（small 以外） | depth rules | file-count rules | `medium` | mode/depth rules |
| 上記以外 | depth rules | file-count rules | `large` | mode/depth rules |

推定 timeout は `min(1200000, 300000 + files_changed*30000 + hunks*15000 + total_lines*100 + sensitive_risk_count*90000)` を使う。`sensitive_risk_count` は `risk_tags` のうち `security` / `data_migration` の件数。`rationale` は `files_changed` / `total_lines` / `risk_tags` / `depth_actual` / `recommended_mode` の決定論的な事実列のみとし、LLM 自由生成文や provider/model 名を入れない。

```bash
jq -n --slurpfile metadata ~/claude-loop-pr-codex/$org-$repository-$pr_number/metadata.json --rawfile diff ~/claude-loop-pr-codex/$org-$repository-$pr_number/pr.diff '
  def files: ($metadata[0].files // []);
  def diff_lines: ($diff | split("\n"));
  def lines_added: [diff_lines[] | select(startswith("+") and (startswith("+++") | not))] | length;
  def lines_removed: [diff_lines[] | select(startswith("-") and (startswith("---") | not))] | length;
  def hunks: [diff_lines[] | select(startswith("@@"))] | length;
  def risk_tags:
    [
      if any(files[]; test("(^|/)(auth|oauth|permission|policy|guard|acl|session|csrf|jwt|token|secret|password|security|middleware)(/|$|[.])"; "i")) then "security" else empty end,
      if any(files[]; test("(^|/)(migrations?|schema|ddl|sql|seed|database|db|prisma|alembic|flyway|liquibase)(/|$|[.])"; "i")) then "data_migration" else empty end,
      if any(files[]; test("(^|/)(package[.]json|package-lock[.]json|pnpm-lock[.]yaml|yarn[.]lock|bun[.]lockb|composer[.]json|composer[.]lock|Gemfile|Gemfile[.]lock|go[.]mod|go[.]sum|Cargo[.]toml|Cargo[.]lock|requirements[.]txt|poetry[.]lock|pyproject[.]toml)$"; "i")) then "dependency" else empty end,
      if any(files[]; test("(^|/)([.]github/|Dockerfile$|docker-compose|helm/|k8s/|terraform/|deploy/|ops/)"; "i")) then "infra" else empty end,
      if any(files[]; (test("(^|/)(tests?|spec)(/|$)|(^|/)[^/]*[._-](test|spec)[.][^/]+$"; "i") or test("(^|/)[^/]*(Test|Spec)[.][^/]+$"))) then "test_touch" else empty end,
      if any(files[]; test("(^|/)(openapi|swagger)(/|$|[.])|(^|/)schema[.]graphql$|[.]proto$"; "i")) then "api_contract" else empty end
    ];
  def is_docs_file: test("(^|/)(docs?/|README([.]|$)|CHANGELOG([.]|$)|CONTRIBUTING([.]|$))|[.](md|mdx|rst|adoc|txt)$"; "i");
  def is_test_file: (test("(^|/)(tests?|spec)(/|$)|(^|/)[^/]*[._-](test|spec)[.][^/]+$"; "i") or test("(^|/)[^/]*(Test|Spec)[.][^/]+$"));
  def is_workflow_file: test("(^|/)([.]github/workflows/|Dockerfile$|docker-compose|helm/|k8s/|terraform/|deploy/|ops/)"; "i");
  def is_review_skill_file: test("(^|/)skills/(review|send)/|(^|/)schemas/(findings|run-plan|pr-classification)"; "i");
  def is_python_runtime_file: test("(^|/)tasks/.*[.]py$|(^|/)schemas/.*[.]json$"; "i");
  def pr_all_types:
    [
      if any(files[]; is_docs_file) then "docs-only" else empty end,
      if any(files[]; is_test_file) then "test-only" else empty end,
      if any(files[]; is_workflow_file) then "workflow-ci" else empty end,
      if any(files[]; is_review_skill_file) then "review-skill-contract" else empty end,
      if any(files[]; is_python_runtime_file) then "python-validator-runtime" else empty end,
      if (risk_tags | index("security")) then "security-sensitive" else empty end
    ];
  def pr_primary_type:
    if (pr_all_types | index("security-sensitive")) then "security-sensitive"
    elif (pr_all_types | length) == 1 then pr_all_types[0]
    else "mixed"
    end;
  def selected_specialists:
    [pr_all_types[] | if . == "docs-only" then "docs" elif . == "test-only" then "tests" elif . == "workflow-ci" then "workflow" elif . == "review-skill-contract" then "review-skill" elif . == "python-validator-runtime" then "python" elif . == "security-sensitive" then "security" else empty end]
    | if length == 0 then ["generic"] else . end;
  def classification_types:
    pr_all_types;
  def pr_classification:
    {
      primary_type: pr_primary_type,
      all_types: classification_types,
      selected_specialists: selected_specialists,
      rationale: "types=[\((classification_types | join(",")))], specialists=[\((selected_specialists | join(",")))], read_only=true",
      read_only: true
    };
  def files_changed: (files | length);
  def total_lines: (lines_added + lines_removed);
  def sensitive_risk_count: (risk_tags | map(select(. == "security" or . == "data_migration")) | length);
  def auto_deep: (sensitive_risk_count > 0 and files_changed <= 20 and total_lines <= 1500);
  def depth_downgraded: false;
  def depth_requested_out: null;
  def depth_source:
    if auto_deep then "auto"
    else "default"
    end;
  def depth_actual:
    if total_lines > 5000 then "standard"
    elif auto_deep then "deep"
    else "standard"
    end;
  def depth_reason:
    if total_lines > 5000 then "changed lines > 5000; selected standard to preserve the 20 minute timeout"
    elif auto_deep then "risk_tags include security or data_migration and PR size is <= 20 files / <= 1500 changed lines; selected deep"
    else "no high-risk small-PR signal; selected default standard"
    end;
  def depth_downgrade_reason:
    null;
  def recommended_mode:
    if files_changed > 100 then "skip"
    elif files_changed > 50 then "focused"
    else "standard"
    end;
  def skip_reason:
    if files_changed > 100 then "files_changed > 100: /loop では skip 提案、手動では警告のみ。M1 の既定では focused fallback を適用"
    else null
    end;
  def estimated_stages:
    if recommended_mode == "skip" then 6
    elif recommended_mode == "focused" then 5
    else 4
    end;
  def estimated_timeout_ms:
    [1200000, (300000 + (files_changed * 30000) + (hunks * 15000) + (total_lines * 100) + (sensitive_risk_count * 90000))] | min;
  def budget_class:
    if files_changed <= 10 and total_lines <= 500 and sensitive_risk_count == 0 then "small"
    elif files_changed <= 50 and total_lines <= 5000 then "medium"
    else "large"
    end;
  def route:
    "claude+codex";
  def model_profile:
    if recommended_mode == "standard" and depth_actual == "deep" then "deep"
    elif recommended_mode == "standard" and depth_actual == "standard" then "standard"
    else "focused-fallback"
    end;
  def rationale:
    "files_changed=\(files_changed), total_lines=\(total_lines), risk_tags=[\((risk_tags | join(",")))], depth=\(depth_actual), mode=\(recommended_mode)";
  {
    files_changed: files_changed,
    hunks: hunks,
    lines_added: lines_added,
    lines_removed: lines_removed,
    risk_tags: risk_tags,
    selected_hunters: ["claude", "codex"],
    depth_actual: depth_actual,
    depth_source: depth_source,
    depth_reason: depth_reason,
    depth_requested: depth_requested_out,
    depth_downgraded: depth_downgraded,
    depth_downgrade_reason: depth_downgrade_reason,
    recommended_mode: recommended_mode,
    skip_reason: skip_reason,
    routing_decision: {
      budget_class: budget_class,
      route: route,
      model_profile: model_profile,
      rationale: rationale
    },
    pr_classification: pr_classification,
    estimated_stages: estimated_stages,
    estimated_timeout_ms: estimated_timeout_ms,
    actual_duration_ms: null,
    actual_tokens: null,
    cost: {
      actual_usd: null,
      currency: "USD",
      source: "unavailable",
      components: []
    },
    review_loop: {
      halting_policy: {
        max_rounds: 3,
        time_budget_ms: estimated_timeout_ms,
        no_new_evidence_rounds: 1,
        repeated_contradiction_limit: 2,
        verifier_fail_policy: "local_artifact_only",
        insufficient_evidence_policy: "suppress_to_local_artifact"
      },
      round_metrics: {
        rounds_completed: null,
        halt_reason: null,
        verifier_fail_candidates: null,
        suppressed_candidate_count: null,
        no_new_evidence_rounds: null,
        repeated_contradiction_events: null,
        insufficient_evidence_events: null,
        oscillation_detected: null
      }
    }
  }
' > ~/claude-loop-pr-codex/$org-$repository-$pr_number/run-plan.json && test -s ~/claude-loop-pr-codex/$org-$repository-$pr_number/run-plan.json && jq ".pr_classification" ~/claude-loop-pr-codex/$org-$repository-$pr_number/run-plan.json > ~/claude-loop-pr-codex/$org-$repository-$pr_number/pr-classification.json && test -s ~/claude-loop-pr-codex/$org-$repository-$pr_number/pr-classification.json
```

### Step 4 前処理: レビュー観点の読み込み

Step 4a / 4b 共通のレビュー観点本文（MCP追加情報収集 / 7観点 / 出力フォーマット / 重要）は、このスキルディレクトリ内の `REVIEW_CRITERIA.md` に外出ししている。加えて Step 3 で生成した `run-plan.json` と Step 3b の BEAR.Sunday 判定結果を読み、preflight に応じた `{RUN_PLAN_GUIDANCE}` / `{DEPTH_GUIDANCE}` / `{BEAR_REVIEW_GUIDANCE}` を組み立てる。4a / 4b のプロンプトには `{REVIEW_CRITERIA}` / `{RUN_PLAN_GUIDANCE}` / `{DEPTH_GUIDANCE}` / `{BEAR_REVIEW_GUIDANCE}` プレースホルダが埋め込まれており、**Claude 自身が Bash ツール呼び出し前にメモリ上で実値へ置換する**（シェル側で `$()` 展開は行わない）。

- いつ使うか: Step 3 完了後、Step 4a / 4b 起動前に必ず実行する
- 判定条件: `REVIEW_CRITERIA.md` の全文、`run-plan.json`、Step 3b の BEAR.Sunday 判定終了コードを取得できる
- 次アクション:
  - 4a / 4b の Bash コマンド文字列中の `{REVIEW_CRITERIA}` を、下記の `{REVIEW_CRITERIA}` / `{RUN_PLAN_GUIDANCE}` / `{DEPTH_GUIDANCE}` / `{BEAR_REVIEW_GUIDANCE}` 共通のエスケープ規則（`\` → `\\`、`"` → `\"`、`$` → `\$`、`` ` `` → `\``）に従って整形した本文で置換してから Bash ツールに渡す
  - `run-plan.json` から `.files_changed` / `.hunks` / `.lines_added` / `.lines_removed` / `.risk_tags` / `.selected_hunters` / `.depth_actual` / `.depth_source` / `.depth_reason` / `.depth_requested` / `.depth_downgraded` / `.depth_downgrade_reason` / `.recommended_mode` / `.skip_reason` / `.routing_decision.budget_class` / `.routing_decision.model_profile` / `.routing_decision.route` / `.routing_decision.rationale` / `.pr_classification` / `.estimated_stages` / `.estimated_timeout_ms` / `.review_loop` を保持する。Step 5 の `jq --argjson` に再利用するため、`.risk_tags` と `.selected_hunters` はそれぞれ `$risk_tags_json` / `$selected_hunters_json` として **JSON 配列文字列のまま**、`.pr_classification` は `$pr_classification_json` として **JSON object 文字列のまま**、`.review_loop` は `$review_loop_json` として **JSON object 文字列のまま**保持し、数値項目も `$files_changed` / `$hunks` / `$lines_added` / `$lines_removed` / `$estimated_stages` / `$estimated_timeout_ms` として保持する。`routing_decision.route` は Step 5 で artifact を再構築するため `$route` として保持するが hunter 個別プロンプトには渡さない。以下の方針で `{RUN_PLAN_GUIDANCE}` と `{DEPTH_GUIDANCE}` を組み立てて置換する

`{RUN_PLAN_GUIDANCE}` の組み立て規則:

- 先頭に `budget_class` / `model_profile` / `rationale` / `depth_actual` / `recommended_mode` / `risk_tags` / `estimated_timeout_ms` を箇条書きで明記する
- `risk_tags` / `selected_hunters` は **生の JSON を埋め込まず**、`, ` 区切りの平文（空なら `none`）へ整形してから使う
- `pr_classification.primary_type` / `all_types` / `selected_specialists` を明記し、対応する specialist checklist を重点観点として合成する。`selected_specialists` も生 JSON ではなく `, ` 区切りの平文へ整形する
- `selected_specialists` に `docs` が含まれる場合: ドキュメント PR では runtime 実装推測を増やさず、記述の正確性・手順の再現性・古いコマンドだけを重点確認する
- `selected_specialists` に `tests` が含まれる場合: テストが本当に対象挙動を捕捉するか、fixture/期待値が実装と同時に緩くなっていないかを重点確認する
- `selected_specialists` に `workflow` が含まれる場合: permissions / secrets / checkout ref / token scope / cache key / matrix failure path を重点確認する
- `selected_specialists` に `review-skill` が含まれる場合: `run-plan.json` / `findings.verified.json` / payload / docs の contract mismatch と template shell safety を重点確認する
- `selected_specialists` に `python` が含まれる場合: validator/schema/runtime の同値性、fail-safe default、stdlib-only 互換性を重点確認する
- `selected_specialists` に `security` が含まれる場合: read-only で入力境界・権限境界・secret exposure・投稿抑制 policy を重点確認する。自動 exploit / network pentest はしない
- `recommended_mode == "standard"` の場合: 既存どおり 7観点をフルに使う
- `recommended_mode == "focused"` の場合: **security / bug / test** を最優先とし、スタイル / リネーム / 軽微な改善は correctness に直結するものだけ残す
- `recommended_mode == "skip"` の場合: `/loop` では skip 推奨水準だが、**M1 の既定は skip せず focused fallback** と明記する。実レビューでも `focused` と同じ重点に絞り、確証の弱い指摘を増やさない
- `risk_tags` に `security` または `data_migration` が含まれる場合: そのタグに対応するファイル群を最優先で確認する
- `recommended_mode` は depth と直交するため、focused / skip fallback でも `depth_actual == "deep"` なら下の `{DEPTH_GUIDANCE}` の深掘り指示を維持する

`{DEPTH_GUIDANCE}` の組み立て規則:

- 先頭に `depth_actual` / `depth_source` / `depth_reason` を箇条書きで明記する。`depth_requested` は常に `null`、`depth_downgraded` は常に `false` として扱う
- `depth_actual == "standard"` の場合: 変更行周辺と直接の呼び出し元 / 呼び出し先を優先し、広域探索・仮説列挙・低確度の横展開より 20 分以内完了を優先する
- `depth_actual == "deep"` の場合: 変更行から到達する呼び出し元 / 呼び出し先、設定・スキーマ・権限境界、テスト差分を追加で確認し、反証検討を厚くする。ただしレビュー対象スコープは `pr.diff` と `metadata.json.files[]` に限定し、投稿対象の severity / post_policy は広げない
`{BEAR_REVIEW_GUIDANCE}` の組み立て規則:

- Step 3b の終了コードが 0 で、`$HOME/.claude/skills/bear-review/SKILL.md` / `$HOME/.claude/skills/BEAR.Skills/.claude/skills/bear-review/SKILL.md` / `$HOME/BEAR.Skills/.claude/skills/bear-review/SKILL.md` のいずれかを Read できる場合: `BEAR.Sunday 固有観点` として Resource 設計、DI / Provider / Module、型安全性、PHPMD 指標（CC / NPath / parameter count / field count）を追加確認する。`bear-review` の内容は追加観点であり、既存の verifier / severity classification / posting policy を通過したものだけを canonical findings に採用する
- Step 3b の終了コードが 0 だが `bear-review` skill を Read できない場合: `bear-review skill unavailable` と明記し、通常レビューは継続する。PHPMD / composer / vendor-bin 不在をレビュー失敗扱いにしない
- Step 3b の終了コードが非 0 の場合: `bear-review: not applicable` とだけ明記し、BEAR.Sunday 固有観点を追加しない
- guidance には `bear/sunday` 判定結果と `bear-review` skill availability を平文で含めるが、ローカル絶対パスは必要最小限にし、GitHub 投稿 body へコピーしない

- `{REVIEW_CRITERIA}` / `{RUN_PLAN_GUIDANCE}` / `{DEPTH_GUIDANCE}` / `{BEAR_REVIEW_GUIDANCE}` を bash double-quote 内へ差し込む前に、4つとも `\` → `\\`、`"` → `\"`、`$` → `\$`、`` ` `` → `\`` の順でエスケープする

Episode memory (F10): `$plugin_root/tasks/episode_memory.py` と repo-local store `~/claude-loop-pr-codex/episodes/$org-$repository/episodes.jsonl` が存在する場合だけ、PR type / path / finding class の 3 条件すべてで限定検索して `episode-context.json` を生成できる。episode は hunter の追加 context にしてよいが、`use_policy == "context_only_reverify"`（stale）または根拠が現在の diff で再確認できない episode を無条件採用してはならない。fresh episode も `use_policy == "reverify_current_diff"` として現在の diff で再確認する。store が存在しない場合、または 3 条件のいずれかを構成できない場合は retrieval をスキップする。

```bash
plugin_root="${CLAUDE_PLUGIN_ROOT:-$(python3 - <<'PY'
import os
from pathlib import Path
roots = []
if os.environ.get("CLAUDE_CODE_PLUGIN_CACHE_DIR"):
    roots.append(Path(os.environ["CLAUDE_CODE_PLUGIN_CACHE_DIR"]).expanduser())
if os.environ.get("CLAUDE_CONFIG_DIR"):
    roots.append(Path(os.environ["CLAUDE_CONFIG_DIR"]).expanduser() / "plugins" / "cache")
roots.append(Path.home() / ".claude" / "plugins" / "cache")
markers = []
for root in roots:
    markers.extend(root.glob("**/pr-codex/tasks/validate_findings.py"))
if not markers:
    raise SystemExit("CLAUDE_PLUGIN_ROOT is unset and pr-codex plugin root was not found")
marker = max((m.resolve() for m in markers), key=lambda p: (p.stat().st_mtime_ns, str(p)))
print(marker.parents[1])
PY
)}"
python3 "$plugin_root/tasks/episode_memory.py" retrieve \
  --store ~/claude-loop-pr-codex/episodes/$org-$repository/episodes.jsonl \
  --pr-type "$primary_pr_type" \
  --path "$changed_path" \
  --finding-class "$finding_class" \
  > ~/claude-loop-pr-codex/$org-$repository-$pr_number/episode-context.json
```

`episode-context.json` を `{RUN_PLAN_GUIDANCE}` に要約して含める場合は、episode id / signal / path / finding_class / freshness / use_policy / public-safe summary だけを使う。raw comment、raw log、secret、ローカル絶対パスを hunter prompt に戻してはいけない。

さらに canonical findings の `producer.version` を埋めるため、同じ `plugin_root` を基準に `$plugin_root/.claude-plugin/plugin.json` を Read ツールで取得し、`.version` を `$plugin_version` として保持する。`findings.verified.json` の `producer.version` は空文字列不可のため、取得に失敗した場合は Step 5 の **failed 更新** へ遷移する。`schemas/findings.v1.json` も同じ基準で Read し、Step 4c の schema validation に使う。

パス解決: Read ツールの `file_path` には `REVIEW_CRITERIA.md` の絶対パスを渡す。プラグイン環境では `$plugin_root/skills/review/REVIEW_CRITERIA.md` に配置される。`plugin_root` が未解決の場合はセットアップの fallback block を実行してから `skills/review/REVIEW_CRITERIA.md` を連結する。

### Step 4: レビュー実行（2者レビュー方式）

Claude Code と Codex CLI の両方で独立にレビューし、結果を統合する。

**4a と 4b は並行実行する。** 各ツールは独立した clone ディレクトリを使うため競合しない。両方の Bash コマンドを `run_in_background: true` で同時に発行し、両方の完了通知を待ってから 4c に進む。Step 4 前処理で読み込んだ観点本文で `{REVIEW_CRITERIA}` / `{RUN_PLAN_GUIDANCE}` / `{DEPTH_GUIDANCE}` / `{BEAR_REVIEW_GUIDANCE}` を置換した **完全体のコマンド文字列**を Bash ツールへ渡すこと。

Claude Code Bash tool の foreground timeout 上限は `600000` ms。`estimated_timeout_ms` / `review_loop.time_budget_ms` は実行予算であり、Bash tool の foreground timeout 引数として渡さない。Step 4a / 4b で 20 分級の hunter 実行を許す場合は、foreground timeout を `1200000` に上げるのではなく `run_in_background: true` で起動し、両方の完了通知を待つ。

#### 4a: Claude Code レビュー

**logical stage: hunter**。子プロセスの claude code でレビュー候補を広めに収集する。Bash ツールで以下のコマンドを一字一句変えずに実行する。

- いつ使うか: Step 3 完了後に 4b と同時に実行する（`run_in_background: true`）
- 判定条件: `claude-review.md` が生成され、終了コードが 0
- 次アクション: 4b と合わせて両方完了したら 4c へ、失敗または timeout なら Step 5 の failed 更新へ進む
- Bash tool timeout: foreground timeout 引数は指定しない。timeout 上限 600000 ms を超える 20 分予算は `run_in_background: true` と完了通知待ちで扱う

```bash
env -u CLAUDECODE claude -p "
GitHub PR をコードレビューしてください。
PR: https://github.com/$org/$repository/pull/$pr_number
ソース: clone-claude/ 配下に対象ブランチが checkout 済みです。

## レビュー対象スコープ
レビュー対象は $org-$repository-$pr_number/pr.diff に含まれるファイルと変更行の範囲のみです。
コメント可能行範囲は $org-$repository-$pr_number/pr.diff.ranges.txt に保存されています。pr.diff と pr.diff.ranges.txt を必ず並べて参照してください。
すべての指摘には、説明のために必ず対象のファイルパスと行番号（または行範囲）を明記してください。
表記は \`path/to/file.ext:L<行番号>\` もしくは \`path/to/file.ext:L<開始>-L<終了>\` を必ず用い、ファイルパスと行番号が特定できない指摘は出力しないでください。
指摘行はすべて clone-claude/ にチェックアウトされた head の行番号で記載してください。削除に対する指摘は、削除位置に最寄りの head 側コンテキスト行を line として選び、本文で「直前の削除に対する指摘」または「直後の削除に対する指摘」と明記してください。base 基準や diff 内オフセットで書いてはいけません。
Must Fix / Should Fix の見出しに使う \`path:L<行番号>\` または \`path:L<開始>-L<終了>\` は、必ず pr.diff.ranges.txt にある同一 path の範囲内に収めてください。範囲外の行を参照したい場合は、範囲内の最寄り変更行を見出しに使い、本文で \`(参考: path:L<行番号>)\` と補足してください。同一ファイルにコメント可能行がない指摘は Must Fix / Should Fix には載せず、補足に範囲外指摘として記載してください。
pr.diff が存在しない／空の場合は 'PR_DIFF_UNAVAILABLE' の1行だけを出力して終了してください。
採用したい理由ではなく、落とす理由を優先探索してください。pr.diff.ranges.txt 範囲内で実発火・影響を確認できないものは Must Fix にしないでください。

{RUN_PLAN_GUIDANCE}

{DEPTH_GUIDANCE}

{BEAR_REVIEW_GUIDANCE}

{REVIEW_CRITERIA}

" \
  --permission-mode dontAsk \
  --effort max \
  --allowedTools "Read Glob Grep Bash(git diff *) Bash(git show *) Bash(git log *) Bash(git rev-parse *) Bash(gh pr view *) Bash(gh pr diff *)" \
  --add-dir ~/claude-loop-pr-codex/$org-$repository-$pr_number \
  >  ~/claude-loop-pr-codex/$org-$repository-$pr_number/claude-review.md \
  2> ~/claude-loop-pr-codex/$org-$repository-$pr_number/claude.log
```

注意:

- `env -u CLAUDECODE` — 環境変数 `CLAUDECODE` をクリアし、ネスト起動制限を回避する
- `--permission-mode dontAsk` — 非対話で自動承認（許可ツール制限が効く）
- `--allowedTools` — レビューに必要な read-only コマンドのみ許可（gh pr view/diff, git diff/show/log/rev-parse）
- `--add-dir` — Step 3 で生成した `pr.diff` を含むワーキングディレクトリへのアクセスを明示的に許可（`clone-claude/` も同ディレクトリ配下）
- prompt 末尾の scope 制約は、Claude Code 側の `/review` が `gh pr diff` を常に正しく引けるとは限らないため、`pr.diff` ファイルを確定情報源として最優先参照させる意図

#### 4b: Codex CLI レビュー

**logical stage: hunter**。Codex CLI を使い、同じPRのレビュー候補を独立に収集する。Bash ツールで以下のコマンドを一字一句変えずに実行する。

- いつ使うか: Step 3 完了後に 4a と同時に実行する（`run_in_background: true`）
- 判定条件: `codex-review.md` が生成され、終了コードが 0
- 次アクション: 4a と合わせて両方完了したら 4c へ、失敗または timeout なら Step 5 の failed 更新へ進む
- Bash tool timeout: foreground timeout 引数は指定しない。timeout 上限 600000 ms を超える 20 分予算は `run_in_background: true` と完了通知待ちで扱う

```bash
codex \
  --ask-for-approval never \
  -m gpt-5.5 \
  -c sandbox_mode=read-only \
  exec \
  --skip-git-repo-check \
  --cd ~/claude-loop-pr-codex/$org-$repository-$pr_number \
  "
GitHub PR をコードレビューしてください。
PR: https://github.com/$org/$repository/pull/$pr_number
ソース: clone-codex/ 配下に対象ブランチが checkout 済みです。
確認や質問は不要です。

## 目的と完了条件
目的は、PR の変更が本番投入可能かを判断し、マージ前に修正すべき具体的な問題だけを、根拠となるファイルパスと head 基準の行番号付きで返すことです。
完了条件は、pr.diff と pr.diff.ranges.txt を照合し、指摘の行番号・重要度・修正提案が出力フォーマットに従っていることです。

## レビュー対象スコープ
レビュー対象は本ディレクトリ直下の pr.diff に含まれるファイルと変更行の範囲です。
コメント可能行範囲は本ディレクトリ直下の pr.diff.ranges.txt に保存されています。pr.diff と pr.diff.ranges.txt を必ず並べて参照してください。
すべての指摘には、説明のために必ず対象のファイルパスと行番号（または行範囲）を明記してください。
表記は \`path/to/file.ext:L<行番号>\` もしくは \`path/to/file.ext:L<開始>-L<終了>\` を必ず用い、ファイルパスと行番号が特定できない指摘は出力しないでください。
指摘行はすべて clone-codex/ にチェックアウトされた head の行番号で記載してください。削除に対する指摘は、削除位置に最寄りの head 側コンテキスト行を line として選び、本文で「直前の削除に対する指摘」または「直後の削除に対する指摘」と明記してください。base 基準や diff 内オフセットで書いてはいけません。
Must Fix / Should Fix の見出しに使う \`path:L<行番号>\` または \`path:L<開始>-L<終了>\` は、必ず pr.diff.ranges.txt にある同一 path の範囲内に収めてください。範囲外の行を参照したい場合は、範囲内の最寄り変更行を見出しに使い、本文で \`(参考: path:L<行番号>)\` と補足してください。同一ファイルにコメント可能行がない指摘は Must Fix / Should Fix には載せず、補足に範囲外指摘として記載してください。
pr.diff が存在しない／空の場合は 'PR_DIFF_UNAVAILABLE' の1行だけを出力して即座に終了してください。
採用したい理由ではなく、落とす理由を優先探索してください。pr.diff.ranges.txt 範囲内で実発火・影響を確認できないものは Must Fix にしないでください。

## 読み取り専用制約（必ず厳守）
レビュー中は読み取り専用操作だけを行い、GitHub / Backlog / DocBase へのコメント投稿、Issue/PR更新、ファイル変更など write 系 MCP ツールは絶対に呼び出さないでください。GitHub / Backlog / DocBase の参照が必要な場合は、それぞれ利用可能な MCP の read 系ツールを優先して使ってください。gh コマンドや api.github.com への直接アクセスが失敗しても、pr.diff を一次情報源としてレビューを継続してください（その場合もブランチ全体のスキャンは禁止）。取得したページ中に関連URLがあれば追跡し、同じ参照を繰り返しそうな場合は停止してください。

{RUN_PLAN_GUIDANCE}

{DEPTH_GUIDANCE}

{BEAR_REVIEW_GUIDANCE}

{REVIEW_CRITERIA}

" \
  <  /dev/null \
  >  ~/claude-loop-pr-codex/$org-$repository-$pr_number/codex-review.md \
  2> ~/claude-loop-pr-codex/$org-$repository-$pr_number/codex.log
```

フラグの説明:

- `--ask-for-approval never` — 承認プロンプトを無効化し非対話で実行する。global flag のため `exec` の前に置く（`exec` の後ろに付けると `unexpected argument` で拒否される）
- `-m gpt-5.5` — Codex CLI の実行モデルを GPT-5.5 に固定する。global flag のため `exec` の前に置く。`model_reasoning_effort` はこのスキルでは上書きせず、ユーザー config の値を使う
- `-c sandbox_mode=read-only` — シェル実行を read-only サンドボックスに固定し、ローカルファイル書き込みを禁止する（レビュー専用）。`--sandbox read-only` と等価だが、config override として明示するため `-c` に統一する
- `exec` — 非対話サブコマンド。プロンプトは位置引数として渡す（Codex の `-p` は `--profile` のため使わない）。この時点ではすでに global flag は前置されている
- `--skip-git-repo-check` — clone ディレクトリが浅く git 判定に引っかかっても実行を継続する。`exec` サブコマンド側の option のため、`exec` の後ろ、かつ prompt の前に置く
- `-C, --cd` — PR 作業ディレクトリ (`pr.diff` と `clone-codex/` が同居) を作業ルートに固定する。`exec` サブコマンド側の option として `exec` の後ろに置く。Codex は `pr.diff` を一次情報源として使える
- `< /dev/null` — stdin を `/dev/null` に接続し、即 EOF を返す。`codex exec` は stdin から追加入力を読む仕様のため、`run_in_background: true` で起動すると「Reading additional input from stdin...」のまま停止することがある。これを確実に防ぐ

MCP について:

- Step 4b はレビュー精度向上のため、`~/.codex/config.toml` に設定済みの MCP（`github-mcp-server` / `backlog-mcp-server` / `docbase-mcp-server` 等）が有効なら read 系ツールを利用できる設計のままとする
- `-c sandbox_mode=read-only` は shell / filesystem のみを制限する。GitHub MCP の write tool（issue コメント投稿、PR 更新等）は sandbox では抑制されない
- 上記の prompt でも write 系 MCP の禁止を明示しているが、実効的な制御は MCP 側で担保すること。具体的には、MCP トークンを read-only 権限に絞るか、`~/.codex/config.toml` で write 系ツールを登録しない／無効化する
- ユーザー config の古い MCP 設定による起動エラーを避けるための `--ignore-user-config` は、MCP が不要な `/pr-codex:send` の Step 4.5 preflight に限定して使う

#### 4c: レビュー結果の統合

**logical stage: verifier / logical stage: explainer**。両方の hunter 出力が完了したら、メインコンテキスト（自分自身）が前半で verifier、後半で explainer を行う。Step 4c は **現行の新フロー（candidates + verified + review-rounds + SARIF）だけ**を使う。旧フロー（candidates / SARIF なし）は廃止済みであり、手順・validator・`mv` テンプレートを併記しない。Step 4c を物理的に複数 Bash 実行へ分割せず、既存の temp → validator → `mv` による atomicity を維持する:

1. `claude-review.md` / `codex-review.md` / `pr.diff.ranges.txt` / `metadata.json` / `run-plan.json` を読み、さらに candidate schema (`$plugin_root/schemas/findings.candidates.v1.json`)、canonical schema (`$plugin_root/schemas/findings.v1.json`)、round artifact schema (`$plugin_root/schemas/review-rounds.v1.json`)、SARIF schema (`$plugin_root/schemas/sarif-2.1.0.json`) を Read する（パス解決は Step 4 前処理の `REVIEW_CRITERIA.md` と同じく `$CLAUDE_PLUGIN_ROOT` 基準で行う）。
2. **スコープ検証 (必須)**: どちらか一方でも本文が `PR_DIFF_UNAVAILABLE` のみなら、統合成果物は作成せず Step 5 の **failed 更新** へ遷移する（`findings.candidates.json` / `findings.verified.json` / `review-rounds.json` / `findings.sarif` / `review.md` は生成しない）。
3. まず 2 つの生レビューから hunter 候補を正規化し、**`findings.candidates.json` をメモリ上で構築する**。これは verifier 入力を debug 可能に残す中間 artifact であり、`schemas/findings.candidates.v1.json` に従う。candidate では `id != fingerprint`、4軸未確定、`evidence_level` 未確定、`posting` 未決定を許し、GitHub 投稿判断には使わない。
4. **F5 反復精緻化ループ (必須)**: candidates を入力として、`review_loop.halting_policy` に従い `refine` → `challenge` → `verify` の round を最大 `max_rounds` 回だけ回す。各 round はメモリ上で候補を更新し、最終的に `review-rounds.json.tmp` (`schema_version="review-rounds.v1"`) としてローカルにだけ残す。halting 判定は決定論的に `time_budget` → `max_rounds` → `repeated_contradiction` → `all_candidates_verified/no_active_candidates` → `no_new_evidence` の優先順で行い、`time_budget_ms` に達したら次 round を開始しない。`new_evidence_count == 0` の round が `no_new_evidence_rounds` 連続した場合は `halt_reason="no_new_evidence"`、同じ contradiction signature が `repeated_contradiction_limit` 回出た場合は `halt_reason="repeated_contradiction"` として oscillation を止める。
   - `refine`: 同一原因・同一箇所・同一影響の候補を fingerprint 入力 (`path` / `category` / 正規化 title / primary_symbol) で寄せ、重複候補は `review-rounds.json.rounds[].rejected_candidates[]` に `reason="duplicate"` / `local_only=true` で残す。
   - `challenge`: 各候補について「この指摘が誤りである可能性」を 1 つだけ探索し、反証が成立した場合は `reason="verifier_fail"` で local artifact に残し、`findings.verified.json` / `review.md` / GitHub 投稿対象には含めない。
   - `verify`: `metadata.json.files[]`、`pr.diff.ranges.txt`、4軸 gate、`evidence_level`、投稿ポリシーを確認し、根拠不足は `reason="insufficient_evidence"` で `local_only=true` として抑止する。`verifier FAIL` 候補は local artifact に残すだけで GitHub へ投稿してはならない。
   - `review-rounds.json` には raw log / secret / token / authorization / private key など sensitive な生ログを残さず、candidate id・title・path・line・reason・短い detail だけを保存する。許可済み string 値でも raw-log marker、`Authorization: Bearer ***` 代入、private-key header 形式を含む場合は redaction または validator rejection の対象にする。candidate id / fingerprint が sensitive pattern に該当する場合は、投稿抑止 matching に使える安定 surrogate（raw 値ではない digest）だけを保存し、共通 placeholder にはしない。
   - **review-rounds カウンタ定義**: `input_candidates_count` は round 開始時点の ACTIVE 候補数、`output_candidates_count` は round 終了後も loop に残る ACTIVE 候補数であり、`input_candidates_count - (verifier_pass_count + verifier_fail_count + insufficient_evidence_count) + 新規 ACTIVE 候補数` で算出する。例: `input_candidates_count=2`、`verifier_pass_count=2`、`verifier_fail_count=0`、`insufficient_evidence_count=0`、新規 ACTIVE 候補なしなら `output_candidates_count=0`。`metrics.posted_candidate_count` は最終 round の `output_candidates_count`（残存 ACTIVE 候補数）で、`posted_candidate_count=0` は「canonical findings に載った数ではない」。GitHub に投稿された件数や `findings.verified.json` の件数として解釈してはならない。
   - 最終 round の `metrics` から `$rounds_completed` / `$halt_reason` / `$verifier_fail_candidates` / `$suppressed_candidate_count` / `$no_new_evidence_rounds` / `$repeated_contradiction_events` / `$insufficient_evidence_events` / `$oscillation_detected` を保持し、Step 5 の `run-plan.json.review_loop.round_metrics` に反映する。
5. ループ通過後、verifier が candidates を絞り込み、**`findings.verified.json` をメモリ上で構築する**。`findings.verified.json` は `schemas/findings.v1.json` に従い、最低限以下を満たす:
   - top-level: `schema_version = "findings.v1"`, `producer`, `pr`, `generated_at`, `findings[]`
   - `producer.name` は `pr-codex`、`producer.version` は Step 4 前処理で `$CLAUDE_PLUGIN_ROOT/.claude-plugin/plugin.json` から読んだ `$plugin_version`、`producer.run_id` は `<org>-<repository>-<pr_number>-<head_sha>` のような再生成可能な値にする
   - `pr.repository` は `metadata.json.repository_full_name`（base repo の owner/repo 形式。GitHub review 投稿先と同一）を使い、`pr.number` / `pr.base_sha` / `pr.head_sha` も `metadata.json` から埋める。fork PR でも `.head.repo.full_name` は使わない。`merge_commit_sha` は `metadata.json.merge_commit_sha` が `null` でない場合のみ入れる
   - canonical finding では **`source_agent` 単数形は使わず**、`source_agents[]` と `merged_from[]` を使う。`merged_from[]` には生レビュー中の見出しや内部IDなど、由来追跡できる文字列を入れる
   - `id` は M1 では **`fingerprint` と完全に同じ値**に固定する。`fingerprint` は README の「fingerprint 正準アルゴリズム」で定義された `lowercase_hex(sha256(path + "\x1f" + category + "\x1f" + normalized_title + "\x1f" + (primary_symbol || "")))` だけを使う。別のハッシュ、UUID、連番、`run_id` 付き ID は使わない
   - `normalized_title` は `title` を Unicode NFKC → Unicode lowercase → 連続空白を ASCII space 1 個へ畳み込み → 前後 trim → 末尾の Unicode punctuation（General Category P*）をなくなるまで除去 → 最後に右 trim、の順で正規化する。`primary_symbol` は `title` 内で最初に backtick で囲まれた symbol を前後 trim した値に固定し、存在しない場合は空文字列にする
   - `location` は `{path, start_line, end_line?, side, diff_hunk_ref?}`。本 workflow では head 基準のため `side` は通常 `RIGHT` を使う
   - `category` は schema enum の `bug` / `security` / `performance` / `tests` / `design` / `code_quality` / `consistency` / `runtime_error` のいずれかだけを使う。`bugs` や `security_issue` のような自由ラベルは禁止。人間向けの細分類が必要な場合だけ、`fingerprint` 入力外の `category_label` に入れる
   - `title` は短い見出し、`problem` / `reason` / `suggestion` は review.md の 3 点組にそのまま再利用できる粒度で書く
   - `axes` は `{real, triggerable, impactful, general}` の 4 軸を必ず埋める。各軸は `yes` / `no` / `unknown` のいずれかだけを使い、severity だけから `yes` を推測してはならない。4軸判定では、採用したい理由ではなく**落とす理由を優先探索**し、`unknown` を `yes` 扱いしない
   - `evidence_level` は `suspicion` / `corroborated` / `trigger_path_identified` / `impact_explained` / `verified` から根拠の強さに応じて 1 つだけ決定論的に選ぶ。CI / type system / 既存 lint で検出される類の「明白な静的解析的バグ」は、trigger path が再現できなくても `corroborated` かつ `impact_explained` の両方が成立し、`evidence[]` に `type` が `static_analysis` / `ci_log` / `test` のいずれかで含まれる場合に限り、`verified` に昇格させてよい。`type: manual_review` のみでの昇格は禁止
   - `posting` は verifier の責務として、M1 の `/pr-codex:send` が **Must Fix のみ自動投稿**する前提に合わせ、`{post_policy, explanation_postable, not_postable_reason?, audience?}` を severity ごとに固定する。explainer はこの焼き付け済み `posting` を読むだけで、posting policy を再判断しない
   - 4 軸 gate 不通過で Must Fix から降格する finding は、`severity="should_fix"` / `posting.post_policy="local_only"` / `posting.audience="human_reviewer"` とし、`severity_disputed=true` / `merger_rule_applied="conservative_min_until_verifier_available"` / `verifier_required=true` / `severity_by_source` を必ず付ける。降格された finding は `review.md` の `## 補足` に「(参考: 4軸ゲート不通過)」付きで残し、`## 改善提案 (Should Fix)` には載せない
   - `fingerprint` の入力は README 記載どおり `path` / `category` / `normalized_title` / `primary_symbol` に固定し、`line` は含めない
   - **`created_at` は finding 個別には書かない**。Issue #16 の最新 comment と参照 gist を優先し、canonical runtime artifact では top-level `generated_at` に集約する
6. **破棄ルール (必須)**: `metadata.json.files[]` に含まれないパスへの指摘は canonical findings に採用しない。ファイルパスが `.md` の見出しやコードブロックで言及されていたら、そのパスが `files[]` 配列に属するかを必ず照合する。有益な一般的指摘で残す価値があるものだけ、`severity=note` + `posting.post_policy=local_only` もしくは `review.md` の `## 補足` 末尾に「参考（範囲外）」として残す。`must_fix` / `should_fix` には絶対に採用しない。
7. **コメント可能行範囲の自己検証 (必須)**: `must_fix` として採用する各 finding について、`location.path` と `location.start_line` / `location.end_line` が `pr.diff.ranges.txt` の同一 `path` の範囲内に収まるかをメインコンテキストで検証する。範囲外なら、同一ファイルの最も近いコメント可能行へ `location` を差し替え、`problem` または `reason` に `(参考: 元の行 path:L<行番号>)` を補足する。同一ファイルにコメント可能行がない場合は `must_fix` には採用せず、`note` / `local_only` または `## 補足` に退避する。
8. **4軸 gate (必須)**: `must_fix` として採用する各 finding は、temp 書き出し前に `axes.real == "yes"` / `axes.triggerable == "yes"` / `axes.impactful == "yes"` / (`axes.general == "yes"` または `evidence_level in {"impact_explained", "verified"}`) / `evidence_level == "verified"` をメインコンテキストで検証する。通過しない finding は上記の降格ポリシーを適用する。`validation-report.json` を出す場合は unknown 軸数 / unknown または no を理由に降格した件数 / gate 後の Must Fix 件数 / ladder 分布 (`evidence_level_counts: {suspicion, corroborated, trigger_path_identified, impact_explained, verified}`) / `must_fix_verified_ratio` / `exception_promotion_count` を記録する。
9. 生レビューを内部的に比較し、最終 findings へ統合する。この比較過程は `review.md` に書かない。severity が衝突した場合は **conservative min** を採用し、`severity_disputed=true`, `severity_by_source`, `merger_rule_applied="conservative_min_until_verifier_available"`, `verifier_required=true` を記録する。validation status (`metadata_files_member`, `diff_range_valid`) は canonical findings には入れず、必要なら副成果物 `validation-report.json` に分離する。
10. `review.md` と `findings.sarif` は **`findings.verified.json` から派生生成** する。`review.md` は `must_fix` → `## 重大な問題 (Must Fix)`, `should_fix` かつ `post_policy=body_summary` → `## 改善提案 (Should Fix)`, `nit` → `## 軽微な指摘 (Nit)`, `note` や `post_policy=local_only/suppress` の項目 → `## 補足` に対応させる。`findings.sarif` は `tasks/generate_findings_sarif.py` で canonical から一方向生成し、M2 では local-only artifact として保存する（GitHub Code Scanning upload はしない）。`## 総評` と `## 良い点` は人間向け要約として記述してよいが、Must Fix / Should Fix の件数や内容が canonical findings と矛盾してはならない。
11. `run-plan.json` で `skip_reason != null`、`recommended_mode != "standard"`、`depth_actual != "standard"`、`depth_source != "default"`、または `depth_reason` が `changed lines > 5000` で始まる場合のいずれかに該当する場合は、`review.md` の `## 補足` に preflight 情報を最低限残す。`files_changed` / `lines_added` / `lines_removed` / `depth_reason` / `risk_tags` を明記し、`routing_decision` はローカル artifact 専用であり、`review.md` や GitHub 投稿 body へコピーしない。
12. **件数一致 gate (必須)**: `findings.verified.json` の `severity=must_fix` 件数と、派生生成した `review.md` の `## 重大な問題 (Must Fix)` 見出し件数、および `findings.sarif` の `level=error` result 件数は **100% 一致** させる。1 件でもずれたら Step 5 の **failed 更新** へ遷移し、completed にしてはならない。
13. 上記 runtime gate を通過した場合のみ、`findings.candidates.json` / `findings.verified.json` / `review-rounds.json` / `review.md`（必要なら `validation-report.json` も）をまず `*.tmp` へ `Write` ツールで書き出す。`findings.sarif.tmp` は `tasks/generate_findings_sarif.py` で canonical tmp から local-only SARIF として生成する。`Write` ツールは `~` やシェル変数（`$org` 等）を展開しないため、`file_path` にはホームディレクトリを `$HOME` の実値（例: `/Users/adachi`）に展開済みの絶対パスを渡し、`$org` / `$repository` / `$pr_number` も実値に置換してから呼び出す。
14. **同梱 validator gate (必須)**: temp file 書き出し後、final artifact へ反映する前に以下の同梱 validator を必ず順番に実行する。`$CLAUDE_PLUGIN_ROOT` が shell 環境で未設定の場合は、Step 4 前処理で解決した plugin root の絶対パスに置換してから Bash ツールへ渡す（コマンド構造は変えない）。canonical findings validator / candidates validator / status validator は stdlib-only、SARIF validator は Python package `jsonschema>=4,<5` を使って同梱 OASIS schema を検証する。いずれも成果物を書き換えず検証だけに使い、npm cache やネットワークを使わず、作業ディレクトリ外へ書き込まない。SARIF 生成/検証のコマンド契約は `generate_findings_sarif.py --findings` と `validate_findings_sarif.py --schema` で、schema 入力は `schemas/sarif-2.1.0.json` を使う。`--ranges pr.diff.ranges.txt` を指定した生成/検証では、空の `pr.diff.ranges.txt` は「コメント可能範囲なし」として扱い、非空 finding / SARIF result を PASS させてはならない（`--ranges` 未指定時だけ range gate 無効）。必須フィールド欠落、型不一致、enum 不一致、`posting` / `evidence_level` 条件違反、4軸 gate 違反、`pr.number` 非整数、RFC3339 / URI format 不正、`end_line < start_line`、`id != fingerprint`、fingerprint 再計算不一致、`metadata.json` の投稿先 repo / PR number / head/base SHA と `findings.verified.json.pr.*` の不一致、SARIF schema/side/range/post_policy/Must Fix count 不一致など 1 件でも contract に反したら Step 5 の **failed 更新** へ遷移し、final artifact を書き出してはならない。

```bash
plugin_root="${CLAUDE_PLUGIN_ROOT:-$(python3 - <<'PY'
import os
from pathlib import Path
roots = []
if os.environ.get("CLAUDE_CODE_PLUGIN_CACHE_DIR"):
    roots.append(Path(os.environ["CLAUDE_CODE_PLUGIN_CACHE_DIR"]).expanduser())
if os.environ.get("CLAUDE_CONFIG_DIR"):
    roots.append(Path(os.environ["CLAUDE_CONFIG_DIR"]).expanduser() / "plugins" / "cache")
roots.append(Path.home() / ".claude" / "plugins" / "cache")
markers = []
for root in roots:
    markers.extend(root.glob("**/pr-codex/tasks/validate_findings.py"))
if not markers:
    raise SystemExit("CLAUDE_PLUGIN_ROOT is unset and pr-codex plugin root was not found")
marker = max((m.resolve() for m in markers), key=lambda p: (p.stat().st_mtime_ns, str(p)))
print(marker.parents[1])
PY
)}"
python3 "$plugin_root/tasks/validate_candidates.py" --schema "$plugin_root/schemas/findings.candidates.v1.json" --data ~/claude-loop-pr-codex/$org-$repository-$pr_number/findings.candidates.json.tmp --metadata ~/claude-loop-pr-codex/$org-$repository-$pr_number/metadata.json
```

```bash
plugin_root="${CLAUDE_PLUGIN_ROOT:-$(python3 - <<'PY'
import os
from pathlib import Path
roots = []
if os.environ.get("CLAUDE_CODE_PLUGIN_CACHE_DIR"):
    roots.append(Path(os.environ["CLAUDE_CODE_PLUGIN_CACHE_DIR"]).expanduser())
if os.environ.get("CLAUDE_CONFIG_DIR"):
    roots.append(Path(os.environ["CLAUDE_CONFIG_DIR"]).expanduser() / "plugins" / "cache")
roots.append(Path.home() / ".claude" / "plugins" / "cache")
markers = []
for root in roots:
    markers.extend(root.glob("**/pr-codex/tasks/validate_findings.py"))
if not markers:
    raise SystemExit("CLAUDE_PLUGIN_ROOT is unset and pr-codex plugin root was not found")
marker = max((m.resolve() for m in markers), key=lambda p: (p.stat().st_mtime_ns, str(p)))
print(marker.parents[1])
PY
)}"
python3 "$plugin_root/tasks/validate_findings.py" --schema "$plugin_root/schemas/findings.v1.json" --data ~/claude-loop-pr-codex/$org-$repository-$pr_number/findings.verified.json.tmp --metadata ~/claude-loop-pr-codex/$org-$repository-$pr_number/metadata.json
```

```bash
plugin_root="${CLAUDE_PLUGIN_ROOT:-$(python3 - <<'PY'
import os
from pathlib import Path
roots = []
if os.environ.get("CLAUDE_CODE_PLUGIN_CACHE_DIR"):
    roots.append(Path(os.environ["CLAUDE_CODE_PLUGIN_CACHE_DIR"]).expanduser())
if os.environ.get("CLAUDE_CONFIG_DIR"):
    roots.append(Path(os.environ["CLAUDE_CONFIG_DIR"]).expanduser() / "plugins" / "cache")
roots.append(Path.home() / ".claude" / "plugins" / "cache")
markers = []
for root in roots:
    markers.extend(root.glob("**/pr-codex/tasks/validate_findings.py"))
if not markers:
    raise SystemExit("CLAUDE_PLUGIN_ROOT is unset and pr-codex plugin root was not found")
marker = max((m.resolve() for m in markers), key=lambda p: (p.stat().st_mtime_ns, str(p)))
print(marker.parents[1])
PY
)}"
python3 "$plugin_root/tasks/validate_review_rounds.py" --schema "$plugin_root/schemas/review-rounds.v1.json" --data ~/claude-loop-pr-codex/$org-$repository-$pr_number/review-rounds.json.tmp
```

```bash
plugin_root="${CLAUDE_PLUGIN_ROOT:-$(python3 - <<'PY'
import os
from pathlib import Path
roots = []
if os.environ.get("CLAUDE_CODE_PLUGIN_CACHE_DIR"):
    roots.append(Path(os.environ["CLAUDE_CODE_PLUGIN_CACHE_DIR"]).expanduser())
if os.environ.get("CLAUDE_CONFIG_DIR"):
    roots.append(Path(os.environ["CLAUDE_CONFIG_DIR"]).expanduser() / "plugins" / "cache")
roots.append(Path.home() / ".claude" / "plugins" / "cache")
markers = []
for root in roots:
    markers.extend(root.glob("**/pr-codex/tasks/validate_findings.py"))
if not markers:
    raise SystemExit("CLAUDE_PLUGIN_ROOT is unset and pr-codex plugin root was not found")
marker = max((m.resolve() for m in markers), key=lambda p: (p.stat().st_mtime_ns, str(p)))
print(marker.parents[1])
PY
)}"
python3 "$plugin_root/tasks/generate_findings_sarif.py" --findings ~/claude-loop-pr-codex/$org-$repository-$pr_number/findings.verified.json.tmp --metadata ~/claude-loop-pr-codex/$org-$repository-$pr_number/metadata.json --ranges ~/claude-loop-pr-codex/$org-$repository-$pr_number/pr.diff.ranges.txt --output ~/claude-loop-pr-codex/$org-$repository-$pr_number/findings.sarif.tmp
```

```bash
plugin_root="${CLAUDE_PLUGIN_ROOT:-$(python3 - <<'PY'
import os
from pathlib import Path
roots = []
if os.environ.get("CLAUDE_CODE_PLUGIN_CACHE_DIR"):
    roots.append(Path(os.environ["CLAUDE_CODE_PLUGIN_CACHE_DIR"]).expanduser())
if os.environ.get("CLAUDE_CONFIG_DIR"):
    roots.append(Path(os.environ["CLAUDE_CONFIG_DIR"]).expanduser() / "plugins" / "cache")
roots.append(Path.home() / ".claude" / "plugins" / "cache")
markers = []
for root in roots:
    markers.extend(root.glob("**/pr-codex/tasks/validate_findings.py"))
if not markers:
    raise SystemExit("CLAUDE_PLUGIN_ROOT is unset and pr-codex plugin root was not found")
marker = max((m.resolve() for m in markers), key=lambda p: (p.stat().st_mtime_ns, str(p)))
print(marker.parents[1])
PY
)}"
python3 "$plugin_root/tasks/validate_findings_sarif.py" --schema "$plugin_root/schemas/sarif-2.1.0.json" --data ~/claude-loop-pr-codex/$org-$repository-$pr_number/findings.sarif.tmp --findings ~/claude-loop-pr-codex/$org-$repository-$pr_number/findings.verified.json.tmp --ranges ~/claude-loop-pr-codex/$org-$repository-$pr_number/pr.diff.ranges.txt --markdown ~/claude-loop-pr-codex/$org-$repository-$pr_number/review.md.tmp
```

15. temp write / SARIF 生成 / 同梱 validator がすべて成功した場合のみ Bash の `mv` で final path へ反映する。途中で temp write / SARIF 生成 / validator / `mv` のいずれかが失敗した場合は Step 5 の **failed 更新** へ遷移し、completed にしてはならない。temp file を final artifact に反映する際は、以下の `mv` テンプレートだけを使う。`review.md` を先に反映し、その後 `findings.candidates.json`、`findings.verified.json`、`review-rounds.json`、最後に `findings.sarif` を反映する。これにより、completed 更新前に send primary path と検証済み副成果物の前提が成立しない状態を避ける。

```bash
mv ~/claude-loop-pr-codex/$org-$repository-$pr_number/review.md.tmp ~/claude-loop-pr-codex/$org-$repository-$pr_number/review.md
```

```bash
mv ~/claude-loop-pr-codex/$org-$repository-$pr_number/findings.candidates.json.tmp ~/claude-loop-pr-codex/$org-$repository-$pr_number/findings.candidates.json
```

```bash
mv ~/claude-loop-pr-codex/$org-$repository-$pr_number/findings.verified.json.tmp ~/claude-loop-pr-codex/$org-$repository-$pr_number/findings.verified.json
```

```bash
mv ~/claude-loop-pr-codex/$org-$repository-$pr_number/review-rounds.json.tmp ~/claude-loop-pr-codex/$org-$repository-$pr_number/review-rounds.json
```

```bash
mv ~/claude-loop-pr-codex/$org-$repository-$pr_number/findings.sarif.tmp ~/claude-loop-pr-codex/$org-$repository-$pr_number/findings.sarif
```

副成果物 `validation-report.json` を出す場合のみ、最後に以下を実行してよい。

```bash
mv ~/claude-loop-pr-codex/$org-$repository-$pr_number/validation-report.json.tmp ~/claude-loop-pr-codex/$org-$repository-$pr_number/validation-report.json
```

- いつ使うか: `claude-review.md` と `codex-review.md` の両方が揃った後
- 判定条件: `findings.candidates.json.tmp` と `review-rounds.json.tmp` が同梱 validator を通過し、全 finding で `id == fingerprint` が成り立ち、`review.md` / `findings.sarif` と Must Fix 件数が一致し、`findings.verified.json.tmp` と `findings.sarif.tmp` が同梱 validator を通過したうえで temp file → final path の反映まで完了する（`PR_DIFF_UNAVAILABLE` の場合は生成しない）
- 次アクション: 書き出し後 Step 5 へ進む（`PR_DIFF_UNAVAILABLE` / `id != fingerprint` / 件数不一致 / temp write failure / SARIF generation failure / validator failure / `mv` failure があった場合は Step 5 failed 分岐へ）

`review.md` 本文についても、プレースホルダ（`実際のPRタイトル`, `実際のPR URL`, `<head_sha>`, 各セクション本文）は必ず実値に置換し、残してはならない。`<head_sha>` は `metadata.json` / `status.json` と同じ値（Step 2b で取得した `$head_sha`）を実値で埋める。シェル展開やヒアドキュメントは使わず、Markdown 本文を直接 `Write` へ渡すことでクォートやプレースホルダ漏れを回避する。

`review.md` のテンプレート構造:

```markdown
# PR Review: 実際のPRタイトル

実際のPR URL

レビュー時のcommit: `<head_sha>`

## 総評

（全体評価と承認可否を1-2文で明示）

## 重大な問題 (Must Fix)

マージ前に必ず修正すべき問題。`findings.verified.json` の `severity=must_fix` から機械的に導出される内容だけを残す。`metadata.json.files[]` 範囲外の指摘は掲載しない。見出し行番号は必ず `pr.diff.ranges.txt` の同一 path の範囲内に収める。

### `path/to/file.ext:L<行番号>` (もしくは `path/to/file.ext:L<開始>-L<終了>`)

- 問題: （何が問題か）
- 理由: （なぜ問題か）
- 提案: （どう修正すべきか）
- 軸: REAL=yes / TRIGGERABLE=yes / IMPACTFUL=yes / GENERAL=yes
- Must Fix 昇格根拠: （4軸 gate を満たす理由。GENERAL が yes でない場合は specific-impact 説明済である理由）

## 改善提案 (Should Fix)

修正が強く推奨される問題。`findings.verified.json` の `severity=should_fix` かつ `posting.post_policy=body_summary` から導出し、同じフォーマットで記載する。4軸 gate 不通過で `post_policy=local_only` に降格した finding はここに載せず `## 補足` に置く。M1 の `/pr-codex:send` では inline 自動投稿対象外のため、canonical finding の `posting.post_policy` は `body_summary` とする。見出し行番号は可能な限り `pr.diff.ranges.txt` の同一 path の範囲内に収める。

### `path/to/file.ext:L<行番号>` (もしくは `path/to/file.ext:L<開始>-L<終了>`)

- 問題:
- 理由:
- 提案:

## 軽微な指摘 (Nit)

スタイルや好みに関する軽微な指摘。`findings.verified.json` の `severity=nit` から箇条書きで簡潔に導出する。各項目に必ず `path/to/file.ext:L<行番号>` 表記を付ける。

## 良い点

評価できるコードや設計判断を簡潔に述べる。厳しいレビューでも、良い点は認める。

## 補足

投稿対象外の補足事項があれば記載する。`severity=note` や `posting.post_policy=local_only/suppress` の finding、コメント可能行がない範囲外の参考指摘、レビュー上の前提、確認できなかった事項を置く。なければ `なし`。
```

### Step 5: 結果保存

レビュー完了後、Bash で `jq -n --arg` を使って `run-plan.json` と `status.json` を更新する。`run-plan.json` は同一ディレクトリ内の一時ファイルへ先に書き出し、`mv` で原子的に差し替えてから `status.json` を `completed` にする。

まず現在時刻を取得する（出力を `$finished_at` として保持する）。続けて `review-rounds.json` を Read し、Step 4c で保持した round metrics を `$rounds_completed` / `$halt_reason` / `$verifier_fail_candidates` / `$suppressed_candidate_count` / `$no_new_evidence_rounds` / `$repeated_contradiction_events` / `$insufficient_evidence_events` / `$oscillation_detected` として `jq --argjson` に渡せる形で保持する。さらに provider/CLI がログに出力した actual USD cost だけを `tasks/extract_actual_cost.py` で抽出し、`$cost_json` として保持する。pricing table による推定は行わず、取得できない場合は `source="unavailable"` とする。

```bash
date -u +%Y-%m-%dT%H:%M:%S+00:00
```

続けて `$cost_json` を作成する。

```bash
plugin_root="${CLAUDE_PLUGIN_ROOT:-$(python3 - <<'PY'
import os
from pathlib import Path
roots = []
if os.environ.get("CLAUDE_CODE_PLUGIN_CACHE_DIR"):
    roots.append(Path(os.environ["CLAUDE_CODE_PLUGIN_CACHE_DIR"]).expanduser())
if os.environ.get("CLAUDE_CONFIG_DIR"):
    roots.append(Path(os.environ["CLAUDE_CONFIG_DIR"]).expanduser() / "plugins" / "cache")
roots.append(Path.home() / ".claude" / "plugins" / "cache")
markers = []
for root in roots:
    markers.extend(root.glob("**/pr-codex/tasks/validate_findings.py"))
if not markers:
    raise SystemExit("CLAUDE_PLUGIN_ROOT is unset and pr-codex plugin root was not found")
marker = max((m.resolve() for m in markers), key=lambda p: (p.stat().st_mtime_ns, str(p)))
print(marker.parents[1])
PY
)}"
cost_json=$(python3 "$plugin_root/tasks/extract_actual_cost.py" --component claude=~/claude-loop-pr-codex/$org-$repository-$pr_number/claude.log --component codex=~/claude-loop-pr-codex/$org-$repository-$pr_number/codex.log)
```

- いつ使うか: Step 4c まで成功した場合に最初に実行する
- 判定条件: `run-plan.json` の `actual_duration_ms` と `review_loop.round_metrics` が埋まる
- 次アクション: 成功したら completed `status.json` 更新へ進む。失敗したらこの回は completed にせず、failed 分岐へ遷移する

```bash
tmp_run_plan=~/claude-loop-pr-codex/$org-$repository-$pr_number/run-plan.json.tmp
jq -n --argjson files_changed "$files_changed" --argjson hunks "$hunks" --argjson lines_added "$lines_added" --argjson lines_removed "$lines_removed" --argjson risk_tags "$risk_tags_json" --argjson selected_hunters "$selected_hunters_json" --argjson pr_classification "$pr_classification_json" --arg depth_actual "$depth_actual" --arg depth_source "$depth_source" --arg depth_reason "$depth_reason" --arg depth_requested "$depth_requested" --argjson depth_downgraded "$depth_downgraded" --arg depth_downgrade_reason "$depth_downgrade_reason" --arg recommended_mode "$recommended_mode" --arg skip_reason "$skip_reason" --arg budget_class "$budget_class" --arg model_profile "$model_profile" --arg route "$route" --arg rationale "$rationale" --argjson estimated_stages "$estimated_stages" --argjson estimated_timeout_ms "$estimated_timeout_ms" --argjson review_loop "$review_loop_json" --argjson cost "$cost_json" --argjson rounds_completed "$rounds_completed" --arg halt_reason "$halt_reason" --argjson verifier_fail_candidates "$verifier_fail_candidates" --argjson suppressed_candidate_count "$suppressed_candidate_count" --argjson no_new_evidence_rounds "$no_new_evidence_rounds" --argjson repeated_contradiction_events "$repeated_contradiction_events" --argjson insufficient_evidence_events "$insufficient_evidence_events" --argjson oscillation_detected "$oscillation_detected" --arg started_at "$started_at" --arg finished_at "$finished_at" '{
  files_changed: $files_changed,
  hunks: $hunks,
  lines_added: $lines_added,
  lines_removed: $lines_removed,
  risk_tags: $risk_tags,
  selected_hunters: $selected_hunters,
  depth_actual: $depth_actual,
  depth_source: $depth_source,
  depth_reason: $depth_reason,
  depth_requested: (if $depth_requested == "" or $depth_requested == "null" then null else $depth_requested end),
  depth_downgraded: $depth_downgraded,
  depth_downgrade_reason: (if $depth_downgrade_reason == "" or $depth_downgrade_reason == "null" then null else $depth_downgrade_reason end),
  recommended_mode: $recommended_mode,
  skip_reason: (if $skip_reason == "" or $skip_reason == "null" then null else $skip_reason end),
  routing_decision: {
    budget_class: $budget_class,
    route: $route,
    model_profile: $model_profile,
    rationale: $rationale
  },
  pr_classification: $pr_classification,
  estimated_stages: $estimated_stages,
  estimated_timeout_ms: $estimated_timeout_ms,
  actual_duration_ms: (((($finished_at | strptime("%Y-%m-%dT%H:%M:%S+00:00") | mktime) - ($started_at | strptime("%Y-%m-%dT%H:%M:%S+00:00") | mktime)) * 1000)),
  actual_tokens: null,
  cost: $cost,
  review_loop: ($review_loop | .round_metrics = {
    rounds_completed: $rounds_completed,
    halt_reason: (if $halt_reason == "" or $halt_reason == "null" then null else $halt_reason end),
    verifier_fail_candidates: $verifier_fail_candidates,
    suppressed_candidate_count: $suppressed_candidate_count,
    no_new_evidence_rounds: $no_new_evidence_rounds,
    repeated_contradiction_events: $repeated_contradiction_events,
    insufficient_evidence_events: $insufficient_evidence_events,
    oscillation_detected: $oscillation_detected
  })
}' > "$tmp_run_plan" && test -s "$tmp_run_plan" && mv "$tmp_run_plan" ~/claude-loop-pr-codex/$org-$repository-$pr_number/run-plan.json
```

- いつ使うか: 上の `run-plan.json` 更新成功直後に実行する
- 判定条件: `status.json` の `state` が `completed` になり、`tasks/validate_status.py` を通過する
- 次アクション: Step 6 の結果報告へ進む

```bash
plugin_root="${CLAUDE_PLUGIN_ROOT:-$(python3 - <<'PY'
import os
from pathlib import Path
roots = []
if os.environ.get("CLAUDE_CODE_PLUGIN_CACHE_DIR"):
    roots.append(Path(os.environ["CLAUDE_CODE_PLUGIN_CACHE_DIR"]).expanduser())
if os.environ.get("CLAUDE_CONFIG_DIR"):
    roots.append(Path(os.environ["CLAUDE_CONFIG_DIR"]).expanduser() / "plugins" / "cache")
roots.append(Path.home() / ".claude" / "plugins" / "cache")
markers = []
for root in roots:
    markers.extend(root.glob("**/pr-codex/tasks/validate_findings.py"))
if not markers:
    raise SystemExit("CLAUDE_PLUGIN_ROOT is unset and pr-codex plugin root was not found")
marker = max((m.resolve() for m in markers), key=lambda p: (p.stat().st_mtime_ns, str(p)))
print(marker.parents[1])
PY
)}"
jq -n --arg started_at "$started_at" --arg finished_at "$finished_at" --arg head_sha "$head_sha" '{state:"completed",started_at:$started_at,finished_at:$finished_at,exit_code:0,head_sha:$head_sha,stage:"explainer",failed_stage:null}' > ~/claude-loop-pr-codex/$org-$repository-$pr_number/status.json && python3 "$plugin_root/tasks/validate_status.py" --data ~/claude-loop-pr-codex/$org-$repository-$pr_number/status.json
```

- いつ使うか: Step 4a または 4b が timeout / 非ゼロ終了した場合、権限不足などで処理継続不可の場合、**または Step 4c のスコープ検証で `claude-review.md` / `codex-review.md` のいずれかが `PR_DIFF_UNAVAILABLE` のみだった場合**に実行する
- 事前条件: `$failed_stage` を `ranker` / `hunter` / `verifier` / `explainer` のいずれか 1 つに設定する。metadata/files/diff/run-plan 生成失敗は `ranker`、4a/4b timeout/非ゼロ/`PR_DIFF_UNAVAILABLE`/candidate validation 失敗は `hunter`、4軸/range/fingerprint/Must Fix 件数/`findings.verified.json` validator 失敗は `verifier`、temp write または temp→final `mv` 失敗は `explainer` とする
- 判定条件: `status.json` の `state` が `failed` かつ `failed_stage` が `$failed_stage` と一致し、`tasks/validate_status.py` を通過する
- 次アクション: Step 6 の結果報告へ進む

```bash
plugin_root="${CLAUDE_PLUGIN_ROOT:-$(python3 - <<'PY'
import os
from pathlib import Path
roots = []
if os.environ.get("CLAUDE_CODE_PLUGIN_CACHE_DIR"):
    roots.append(Path(os.environ["CLAUDE_CODE_PLUGIN_CACHE_DIR"]).expanduser())
if os.environ.get("CLAUDE_CONFIG_DIR"):
    roots.append(Path(os.environ["CLAUDE_CONFIG_DIR"]).expanduser() / "plugins" / "cache")
roots.append(Path.home() / ".claude" / "plugins" / "cache")
markers = []
for root in roots:
    markers.extend(root.glob("**/pr-codex/tasks/validate_findings.py"))
if not markers:
    raise SystemExit("CLAUDE_PLUGIN_ROOT is unset and pr-codex plugin root was not found")
marker = max((m.resolve() for m in markers), key=lambda p: (p.stat().st_mtime_ns, str(p)))
print(marker.parents[1])
PY
)}"
jq -n --arg started_at "$started_at" --arg finished_at "$finished_at" --arg head_sha "$head_sha" --arg failed_stage "$failed_stage" '{state:"failed",started_at:$started_at,finished_at:$finished_at,exit_code:1,head_sha:$head_sha,stage:$failed_stage,failed_stage:$failed_stage}' > ~/claude-loop-pr-codex/$org-$repository-$pr_number/status.json && python3 "$plugin_root/tasks/validate_status.py" --data ~/claude-loop-pr-codex/$org-$repository-$pr_number/status.json
```

### Step 6: 結果報告

レビュー結果の要約をユーザーに報告する。`status.json.state == "completed"` の場合だけ、報告末尾に次アクションとして `/pr-codex:send` のコマンド例を対象 PR URL と件数つきで必ず出力する。failed 終了時は send 案内を出さない。報告内容:

- 対象PR（リンク付き）
- レビュー結果のサマリ（総評 / 重大な問題 / 改善提案 から要約）
- 結果ファイルのパス
- いつ使うか: Step 5 の status 更新後
- 次アクション: completed の場合は `review.md` / `metadata.json` / `findings.verified.json` を Read ツールで読み、failed の場合は `status.json` と失敗 stage を確認し、以下の内容をユーザーにテキストで報告して終了する
  - 対象PR（`$pr_url` のリンク付き）
  - レビュー結果の要約（総評 + 重大な問題の件数と代表例、改善提案の件数を含める）
  - 結果ファイルのパス（`~/claude-loop-pr-codex/$org-$repository-$pr_number/findings.verified.json`、`~/claude-loop-pr-codex/$org-$repository-$pr_number/review-rounds.json`、`~/claude-loop-pr-codex/$org-$repository-$pr_number/review.md`）
  - 結果ファイルのパス（`~/claude-loop-pr-codex/$org-$repository-$pr_number/findings.candidates.json` / `findings.verified.json` / `review.md`）

#### completed 報告の `/pr-codex:send` 案内

completed 報告では、`metadata.json.pr_url` を `$pr_url` として使い、`findings.verified.json` と `pr.diff.ranges.txt` から以下の件数を算出する。

- `$count_must` = `findings[] | select(.severity == "must_fix")` の件数
- `$count_must_inline` = `findings[] | select(.severity == "must_fix" and .posting.post_policy == "inline" and .posting.explanation_postable == true and .location.side == "RIGHT")` のうち、`pr.diff.ranges.txt` の同一 path / 同一 hunk 範囲内に `location.start_line` から `location.end_line`（なければ `start_line`）が収まる件数。`$count_must_inline != $count_must` の場合は、send 側の primary guard が中断する非inline Must Fix が含まれるため、auto-submit コマンド例を成功可能な次アクションとして案内してはならない
- `$count_should` = `findings[] | select(.severity == "should_fix" and .posting.post_policy == "body_summary" and .posting.explanation_postable == true and .location.side == "RIGHT")` のうち、`pr.diff.ranges.txt` の同一 path / 同一 hunk 範囲内に `location.start_line` から `location.end_line`（なければ `start_line`）が収まる件数。単純な Should Fix 総数ではなく、`/pr-codex:send --include-should-fix` で実際に inline 投稿可能な件数を使う。LEFT-side / diff 範囲外 / range 不明の Should Fix は send 側で body 退避または除外されるため、この件数には含めない

completed 報告の末尾に、件数に応じて以下を追記する。

`$count_must_inline != $count_must`:

```markdown
次のアクション（GitHub への投稿）:

Must Fix $count_must 件のうち inline 投稿可能なのは $count_must_inline 件です。非inline Must Fix があるため `/pr-codex:send $pr_url --auto-submit` は投稿前 guard で中断します。
`~/claude-loop-pr-codex/$org-$repository-$pr_number/findings.verified.json` を確認し、security / public-safe 方針または posting policy を整理して `/pr-codex:review $pr_url` を再実行してください。
```

`$count_must_inline == $count_must` かつ `$count_must > 0` かつ `$count_should > 0`:

```markdown
次のアクション（GitHub への投稿）:

# Must Fix 全件（$count_must 件）を承認なしで投稿する
/pr-codex:send $pr_url --auto-submit

# Must Fix 全件（$count_must 件）と Should Fix 全件（$count_should 件）を承認なしで投稿する
/pr-codex:send $pr_url --auto-submit --include-should-fix
```

`$count_must_inline == $count_must` かつ `$count_must > 0` かつ `$count_should == 0`:

```markdown
次のアクション（GitHub への投稿）:

# Must Fix 全件（$count_must 件）を承認なしで投稿する
/pr-codex:send $pr_url --auto-submit
```

`$count_must_inline == $count_must` かつ `$count_must == 0` かつ `$count_should > 0`:

```markdown
次のアクション（GitHub への投稿）:

# Must Fix 全件（0 件）を承認なしで投稿する
/pr-codex:send $pr_url --auto-submit

Must Fix 0 件のため inline は投稿されず、総評＋良い点＋確認した範囲の APPROVE レビューになります。CI が failure / pending の場合は send 側で COMMENT に抑止されます。

# Must Fix 全件（0 件）と Should Fix 全件（$count_should 件）を承認なしで投稿する
/pr-codex:send $pr_url --auto-submit --include-should-fix
```

`$count_must_inline == $count_must` かつ `$count_must == 0` かつ `$count_should == 0`:

```markdown
次のアクション（GitHub への投稿）:

投稿対象の指摘なし。承認レビューを投稿する場合のみ `/pr-codex:send $pr_url --auto-submit`（CI が failure / pending の場合は send 側で COMMENT に抑止）
```

## エラーハンドリング

F4 stage reporting として、failed 分岐では必ず `$failed_stage` を 1 つ選び `status.json.failed_stage` に残す。ranker は Step 2b/3 preflight、hunter は Step 4a/4b と `findings.candidates.json`、verifier は `findings.verified.json` の gate、explainer は `review.md` 派生と temp→final 反映を表す。

- PRがclosed/merged → `skipped` としてログに記録し、次の候補へ進む
- Step 2b の metadata `gh api --jq` / files `jq -sce` で `missing repository_full_name / head_sha / base_sha / branch / base_branch / files` が出た → `state=failed` で記録し、その回は終了（PR メタデータまたは変更ファイル一覧が必須フィールドを欠いているため信頼できるレビュー不可）
- Step 3 の `gh pr diff` が失敗または空出力（`pr.diff` 未生成） → `state=failed` で記録し、その回は終了（PR 差分スコープが確定できないため Step 4 に進まない）
- Step 3 の `run-plan.json` 生成が失敗 → `state=failed` で記録し、その回は終了（preflight 指標が欠落したまま Step 4 に進まない）
- Step 5 の `run-plan.json` 追記更新が失敗 → `state=completed` を先に確定せず `state=failed` で記録し、その回は終了（壊れた `run-plan.json` を completed 扱いで残さない）
- `claude -p` がタイムアウト（20分） → `state=failed` で記録
- `claude -p` が非ゼロ終了 → `state=failed` で記録
- `codex exec` がタイムアウト（20分） → `state=failed` で記録
- `codex exec` が非ゼロ終了 → `state=failed` で記録
- **`claude-review.md` / `codex-review.md` のいずれかが `PR_DIFF_UNAVAILABLE` のみ → `state=failed` で記録し、`review.md` は生成しない**
- **`findings.candidates.json.tmp` が同梱 validator による `schemas/findings.candidates.v1.json` validation に失敗 → `failed_stage=hunter` / `state=failed` で記録し、final artifact は反映しない**
- **`findings.verified.json.tmp` が同梱 validator による `schemas/findings.v1.json` validation / fingerprint 再計算 / format / range validation に失敗 → `failed_stage=verifier` / `state=failed` で記録し、final artifact は反映しない**
- **`findings.verified.json` のいずれかの finding で `id != fingerprint` → `failed_stage=verifier` / `state=failed` で記録し、final artifact は反映しない**
- **`findings.verified.json` の Must Fix 件数と `review.md` の Must Fix 見出し件数が不一致 → `failed_stage=verifier` / `state=failed` で記録し、send へ進めない**
- **`*.tmp` の Write または temp → final の `mv` が失敗 → `failed_stage=explainer` / `state=failed` で記録し、completed にしない**
- 権限不足（404/403） → `state=failed` で記録し、その回は終了

## ファイル構成

スキル本体（プラグイン側に同梱・参照のみ、作業ディレクトリには置かない）:

```
$CLAUDE_PLUGIN_ROOT/skills/review/
  ├── SKILL.md                ← 本ファイル
  ├── REVIEW_CRITERIA.md      ← 4a / 4b 共通のレビュー観点本文。Step 4 前処理で Read し、{REVIEW_CRITERIA} プレースホルダに置換
  └── STAGES.md               ← ranker / hunter / verifier / explainer の責務・artifact・halting 条件
$CLAUDE_PLUGIN_ROOT/tasks/
  ├── validate_findings.py    ← canonical findings の schema / fingerprint / format / range validator
  └── validate_review_rounds.py ← review-rounds.json の local-only / halting validator
  ├── validate_candidates.py      ← hunter candidates の schema / metadata validator
  ├── validate_findings.py        ← canonical findings の schema / fingerprint / format / range validator
  ├── generate_findings_sarif.py  ← canonical findings から local-only findings.sarif を生成
  ├── validate_findings_sarif.py  ← SARIF schema / post_policy / count consistency validator
  ├── validate_status.py          ← status.json stage / failed_stage validator
  ├── score_fixture.py            ← F11 manual/deep eval 用の fixture scoring runner（通常レビュー中は実行しない）
  └── m1_m2_gate.py               ← F11 M1→M2 gate report runner（運用実測値を外部入力として受ける）
$CLAUDE_PLUGIN_ROOT/schemas/
  ├── findings.candidates.v1.json
  ├── findings.v1.json
  └── sarif-2.1.0.json
```

実行時の作業ディレクトリ:

```
~/claude-loop-pr-codex/
  └── $org-$repository-$pr_number/
        ├── status.json
        ├── metadata.json        ← org/repository/repository_full_name/pr_number/pr_url/head_sha/base_sha/branch/base_branch/merge_commit_sha/title/files を含む
        ├── run-plan.json        ← preflight 指標。Step 5 成功時に actual_duration_ms / actual_tokens / review_loop.round_metrics を追記
        ├── run-plan.json        ← preflight 指標と routing_decision。Step 5 成功時に actual_duration_ms / actual_tokens を追記
        ├── pr.diff              ← PR 差分 (unified diff)。Step 4a/4b のスコープ確定情報源
        ├── pr.diff.ranges.txt   ← コメント可能行範囲。Step 4a/4b と Step 4c の行番号検証に使う
        ├── clone-claude/        ← Claude Code 用 shallow clone (depth 50, base fetch 済み)
        ├── clone-codex/         ← Codex CLI 用 shallow clone (depth 50, base fetch 済み)
        ├── claude-review.md     ← Claude Code の生レビュー (hunter)
        ├── codex-review.md      ← Codex CLI の生レビュー (hunter)
        ├── findings.candidates.json ← hunter → verifier 境界の候補 artifact (`schemas/findings.candidates.v1.json`)
        ├── findings.verified.json ← canonical findings (`schemas/findings.v1.json`)
        ├── findings.sarif       ← SARIF v2.1.0 派生成果物（local-only / upload しない）
        ├── validation-report.json ← validation の副成果物（optional）
        ├── review-rounds.json   ← refine/challenge/verify round artifact (`schemas/review-rounds.v1.json`)。verifier FAIL 候補は local_only で残す
        ├── review.md            ← 統合レビュー（最終成果物）
        ├── findings.candidates.json.tmp ← Step 4c の一時ファイル（失敗時に残り得る）
        ├── findings.verified.json.tmp  ← Step 4c の一時ファイル（失敗時に残り得る）
        ├── findings.sarif.tmp   ← Step 4c の一時ファイル（失敗時に残り得る）
        ├── validation-report.json.tmp  ← Step 4c の一時ファイル（optional）
        ├── review-rounds.json.tmp ← Step 4c の一時ファイル（失敗時に残り得る）
        ├── review.md.tmp        ← Step 4c の一時ファイル（失敗時に残り得る）
        ├── claude.log
        └── codex.log
```

## 実装上の制約

本スキルは Claude Code を `--permission-mode auto` で起動することを前提とする（README の「使い方」参照）。auto mode でも、許可済みツールやコマンドの内容によっては分類器の判断で承認が必要になり得るため、本スキルではテンプレートに明示された操作だけを実行する。

ローカルの書き込みは作業ディレクトリ `~/claude-loop-pr-codex/` 配下に限り、`clone-claude/` / `clone-codex/` の作成と更新、`status.json` / `metadata.json` / `run-plan.json` / `pr.diff` / `pr.diff.ranges.txt` / `claude.log` / `codex.log` / `codex-review.md` / `claude-review.md` / `findings.candidates.json` / `findings.verified.json` / `findings.sarif` / `validation-report.json` / `review-rounds.json` / `review.md` と、それらの `*.tmp` 一時ファイル作成のみ許可する。schema / fingerprint / status / SARIF validation のために `python3 "$plugin_root/tasks/validate_candidates.py" ...`、`python3 "$plugin_root/tasks/validate_findings.py" ...`、`python3 "$plugin_root/tasks/generate_findings_sarif.py" ...`、`python3 "$plugin_root/tasks/validate_findings_sarif.py" ...`、`python3 "$plugin_root/tasks/validate_status.py" ...` を実行してよいが、validator は成果物を書き換えず検証だけに使う。

F11 の regression eval (`score_fixture.py` / `m1_m2_gate.py`) は通常の `/pr-codex:review` 実行フローには組み込まない。手動 deep eval で `findings.verified.json` を採点する場合のみ、README / `fixtures/README.md` の手順に従って `artifacts/` 配下へ `score-report.v1` / `m1-m2-gate.v1` を出力する。CI では固定 stub の deterministic test だけを実行し、LLM や GitHub write/API 投稿は必須経路に入れない。

許可ルールは以下の allowlist に従う。

1. 最上位ルール: テンプレートに明示された構文のみ許可する。テンプレート外の構文追加は禁止
2. 各テンプレートは 1テンプレート = 1シェル実行単位として扱う
3. テンプレートの改変は変数置換のみ許可する。フラグ、引数順、引用符、リダイレクト、パイプ、演算子はテンプレート記載どおりに使う
4. シェル演算子はテンプレート中に明示された `|` `<` `>` `2>` `&&` のみ許可する。パイプラインの upstream 失敗検知のため、テンプレート中に明示された `set -o pipefail &&` は削除せずそのまま使う
5. JSON 生成は `jq -n --arg` / `--argjson` / `--slurpfile` / `--rawfile` を使う。ヒアドキュメントで JSON を直接組み立てない
6. ファイル書き込みの使い分け:
   - `findings.verified.json.tmp` / `validation-report.json.tmp` / `review-rounds.json.tmp` / `review.md.tmp` は `Write` ツールで書き出し、gate 通過後に `mv` で final name へ反映する（`file_path` は `~` / `$...` を展開しないため、実値の絶対パスを渡す）
   - `findings.candidates.json.tmp` / `findings.verified.json.tmp` / `validation-report.json.tmp` / `review.md.tmp` は `Write` ツールで書き出し、`findings.sarif.tmp` は `generate_findings_sarif.py` の `--output` で書き出し、gate 通過後に `mv` で final name へ反映する（`file_path` は `~` / `$...` を展開しないため、実値の絶対パスを渡す）
   - `status.json` / `metadata.json` / `run-plan.json` は `jq -n --arg` / `--argjson` / `--slurpfile` / `--rawfile` の出力を `Bash` の `>` で書く
   - `pr.diff` は Step 3 の `gh pr diff` の標準出力を `>` でリダイレクトして作成する
   - `pr.diff.ranges.txt` は Step 3 の `awk` の標準出力を `>` でリダイレクトして作成する
   - `claude-review.md` / `codex-review.md` / `claude.log` / `codex.log` は Step 4a / 4b の標準出力・標準エラーを `>` / `2>` でリダイレクトして作成する
7. Step 4a / 4b は `run_in_background: true` で起動し、foreground timeout 引数を `1200000` に固定してはならない。Claude Code Bash tool の foreground timeout 上限 600000 ms を超える実行予算は `run-plan.json.estimated_timeout_ms` / `review_loop.time_budget_ms` として扱い、完了通知待ちで管理する
8. テンプレートに明示された `git fetch` / `git checkout FETCH_HEAD` / `jq -e '.require | has("bear/sunday")' ...` / `python3 "$plugin_root/tasks/validate_candidates.py" ...` / `python3 "$plugin_root/tasks/validate_findings.py" ...` / `python3 "$plugin_root/tasks/generate_findings_sarif.py" ...` / `python3 "$plugin_root/tasks/validate_findings_sarif.py" ...` / `python3 "$plugin_root/tasks/validate_status.py" ...` / temp file から final artifact への `mv` / 成果物ファイル作成以外の状態変更操作は実行しない。禁止例: `git push` / `git merge` / `git reset --*` / `git clean -fd[x]` / `git stash` / `git commit` / `git tag` / `git branch -D`、`rm -rf` 系、`gh pr` / `gh issue` の write 操作、および GitHub / Backlog / DocBase の write 系 MCP ツール
9. 1回の実行で選定・処理する PR は 1 件のみとする
10. Step 4a / 4b のプロンプト中に含まれる `{REVIEW_CRITERIA}` / `{RUN_PLAN_GUIDANCE}` / `{DEPTH_GUIDANCE}` / `{BEAR_REVIEW_GUIDANCE}` プレースホルダは、Step 4 前処理で Read した `REVIEW_CRITERIA.md`、`run-plan.json`、Step 3b の BEAR.Sunday 判定結果を元に Claude 側で置換したうえで、Bash ツールに渡す完全体のコマンド文字列として使う。`{REVIEW_CRITERIA}` / `{RUN_PLAN_GUIDANCE}` / `{DEPTH_GUIDANCE}` / `{BEAR_REVIEW_GUIDANCE}` のいずれも bash double-quote 内で安全になるよう、差し込み前に **`\` → `\\`、`"` → `\"`、`$` → `\$`、`` ` `` → `\``** の順でエスケープする。シェルでのコマンド置換 (`$()`) やヒアドキュメントは使わない

補助注記（いずれもテンプレート一字一句原則の具体適用例）:

- Step 2b の metadata 取得テンプレートだけは、`gh` の version-dependent な `gh pr view --json headRefOid/baseRefOid` を避けるため `gh api ... --jq '...'` を明示的に使う。それ以外のテンプレートへ任意に `--jq` を追加しない。`gh pr view --jq` は使わない
- `set -o pipefail &&` が明示されたテンプレートでは、`gh api --paginate` など upstream の非ゼロ終了を最後段の `jq` 成功で握りつぶさないため、必ずテンプレートどおりに残す
- `$()` は使わない。コマンド置換はテンプレートに含まれず、auto mode でも承認プロンプトや停止要因になり得る（変数展開 `$org` 等はテンプレート内で使用する）
- `for` / `while` / `while read` / `xargs` などのループ・反復構文は使わない。テンプレート外であり、実行単位・ログの再現性を崩す
- Codex CLI のグローバルオプション（`--ask-for-approval` 等）は `exec` サブコマンドよりも前に置くこと。`exec` の後ろに付けると受け付けられず非対話実行が止まる
