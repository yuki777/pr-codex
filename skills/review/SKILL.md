---
user-invocable: true
name: pr-codex
description: "GitHub PRを Claude Code と Codex CLI の2者レビュー方式で自動レビューする"
argument-hint: "[--deep|--standard]"
allowed-tools: ["Bash", "Read", "Write", "Glob", "Grep"]
---

# pr-codex

GitHubのレビュー依頼PRを自動レビューするコマンド。Claude Code と Codex CLI の2者レビュー方式。

## 引数

The user invoked this with: `$ARGUMENTS`

起動直後に Claude 側で `$ARGUMENTS` を解析し、レビュー深度の明示指定を `$depth_requested` として保持する。

- 引数なし: `$depth_requested = ""`（run-plan の自動推定または default を使う）
- `--deep`: `$depth_requested = "deep"`
- `--standard`: `$depth_requested = "standard"`

上記以外の引数、または複数引数が含まれる場合は、ユーザーに `unsupported argument: <value>。使える引数は --deep または --standard です。` と報告して **処理を中断** する。未知の引数を silent ignore してレビューを続行してはならない。

`--deep` は高リスク・小規模 PR を深く見るための手動指定、`--standard` は高速 path を明示する指定である。どちらも `/pr-codex:send` の自動投稿範囲を広げるものではない。

## セットアップ

`~/claude-loop-pr-codex/` をワーキングディレクトリとしてClaude Codeを起動する:

```bash
cd ~/claude-loop-pr-codex && claude --permission-mode auto --effort max
```

Codex CLI 側のレビュー実行は、本スキル内で `-m gpt-5.5` を指定して実行する。

起動後:

```
/loop 10m /pr-codex:review
```

ワーキングディレクトリを `~/claude-loop-pr-codex/` にすることで、配下のファイルに直接アクセスできる。

## フロー

### Step 1: レビュー対象PR候補の取得

GitHub Search API でレビュー依頼されている Open PR を取得する。
Notifications API と異なり、リポジトリの Watch 設定に依存しない。

各テンプレートはコードブロックの内容をそのまま1回のシェル実行単位として使うこと。変数（`$MY_LOGIN`, `$org`, `$repository`, `$pr_number`, `$title`, `$pr_url`, `$branch`, `$base_branch`, `$head_sha`, `$files_json`, `$started_at`, `$finished_at`, `$exit_code` など）の置換以外の改変は不可。

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

### Step 2b: 選定PRの `repository_full_name` / `head_sha` / `base_sha` / `branch` / `base_branch` / `merge_commit_sha` / `files` を取得

Step 2 で対象PRを1件選定した直後、未レビュー / failed / stale / completed のどの経路でもまず `repository_full_name` / `head_sha` / `base_sha` / `branch` / `base_branch` / `merge_commit_sha` を取得する。`repository_full_name` は fork head repo ではなく、GitHub review の投稿先と同じ **base repo** (`.base.repo.full_name`) とする。`state == "completed"` の場合はこの時点で保存済み `head_sha` と比較し、同一ならスキップして PR 変更ファイル一覧は取得しない。未レビュー / failed / stale、または completed だが `head_sha` が変わっている場合だけ、完全な `files[]` を REST API paginate で取得して Step 3 へ進む。Step 3 の clone と `metadata.json` / `run-plan.json` 作成・Step 4 の PR 差分スコープ制御・Step 4c の canonical findings 生成は `$repository_full_name` / `$head_sha` / `$base_sha` / `$branch` / `$base_branch` / `$merge_commit_sha` / `$files_json` に依存するため、選定後に欠落すると後続が破綻する。

まず `repository_full_name` / `head_sha` / `base_sha` / `branch` / `base_branch` / `merge_commit_sha` を取得する。

- いつ使うか: Step 2 で対象PRを1件選定した直後に必ず実行する
- 判定条件: 標準出力に `{"repository_full_name":"...","head_sha":"...","base_sha":"...","branch":"...","base_branch":"...","merge_commit_sha":"..."}` の JSON が出力される（required field のいずれかが欠落した場合は `gh api --jq` が非ゼロ終了し、stderr に `missing <field>` が出る。`merge_commit_sha` は open PR では空文字列でよい）
- 次アクション: 出力 JSON の `.repository_full_name` を `$repository_full_name`、`.head_sha` を `$head_sha`、`.base_sha` を `$base_sha`、`.branch` を `$branch`、`.base_branch` を `$base_branch`、`.merge_commit_sha` を `$merge_commit_sha` に保持する。`state == "completed"` の場合は、続く保存済み `head_sha` 比較テンプレートを先に実行する。一致したらこの候補をスキップし、異なる場合だけ **別テンプレートで完全な `files[]` を取得**して Step 3 へ進む。`state != "completed"` の場合はそのまま完全な `files[]` を取得する

```bash
gh api repos/$org/$repository/pulls/$pr_number --jq '
  {
    repository_full_name: .base.repo.full_name,
    head_sha: .head.sha,
    base_sha: .base.sha,
    branch: .head.ref,
    base_branch: .base.ref,
    merge_commit_sha: (.merge_commit_sha // "")
  }
  | if ((.repository_full_name // "") == "") then error("missing repository_full_name")
    elif ((.head_sha // "") == "") then error("missing head_sha")
    elif ((.base_sha // "") == "") then error("missing base_sha")
    elif ((.branch // "") == "") then error("missing branch")
    elif ((.base_branch // "") == "") then error("missing base_branch")
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

1つ目の `gh api --jq` の出力が `{"repository_full_name":"octo/example","head_sha":"deadbeef01","base_sha":"cafebabe02","branch":"feat/dark-mode","base_branch":"main","merge_commit_sha":""}`、2つ目の `jq -sce` の出力が `["src/theme.ts","src/App.tsx"]` の場合、以下のように Bash 変数へ保持する:

- `$repository_full_name = octo/example`（base repo の owner/repo 形式の文字列。fork PR でも投稿先 repo と一致させる）
- `$head_sha = deadbeef01`（文字列そのまま）
- `$base_sha = cafebabe02`（文字列そのまま）
- `$branch = feat/dark-mode`（文字列そのまま）
- `$base_branch = main`（文字列そのまま）
- `$merge_commit_sha = ""`（open PR では空文字列。merge commit が取得できる場合はその SHA）
- `$files_json = ["src/theme.ts","src/App.tsx"]`（**JSON 配列そのままの文字列**。Step 3 の metadata.json 生成で `jq --argjson files "$files_json"` に渡す）

`$files_json` は 2つ目のテンプレートの標準出力そのままを JSON 配列文字列として保持したもの。`$repository_full_name` は canonical findings の `pr.repository` にそのまま使うが、必ず `.base.repo.full_name` 由来の投稿先 repo とし、`.head.repo.full_name` 由来の fork repo を入れてはならない。`$base_sha` は `pr.base_sha` と `metadata.json.base_sha` の両方に使う。`$merge_commit_sha` は empty string の場合に限り metadata では `null` に正規化してよい。抽出は Claude 側で 2 回の出力を読み取って変数へ分解する（シェル側で追加の `jq` パイプは挟まない。1 テンプレート = 1 シェル実行単位の原則に従う）。

### Step 3: 作業ディレクトリの準備

- いつ使うか: Step 2 で対象PRを1件選定した直後に実行する
- 判定条件: 作業ディレクトリが存在する
- 次アクション: Desktop シンボリックリンク作成へ進む

```bash
install -d ~/claude-loop-pr-codex/$org-$repository-$pr_number
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

> 注: 下記の awk スクリプト内 `$NF` `$3` は awk の自動フィールド変数であり、シェル変数ではない。
> 実値置換せず、テンプレートそのままを Bash ツールへ渡すこと。`$org` `$repository` `$pr_number` のみ実値置換する。

```bash
awk '
  /^diff --git/ {
    path = $NF
    sub(/^b\//, "", path)
    next
  }
  /^@@/ {
    spec = $3
    sub(/^\+/, "", spec)
    n = split(spec, a, ",")
    start = a[1] + 0
    len = (n == 2 ? a[2] + 0 : 1)
    if (len > 0) printf "%s\tL%d-L%d\n", path, start, start + len - 1
  }
' ~/claude-loop-pr-codex/$org-$repository-$pr_number/pr.diff > ~/claude-loop-pr-codex/$org-$repository-$pr_number/pr.diff.ranges.txt
```

- `--depth 50` で shallow clone し、ディスク・時間を節約しつつ `git diff origin/$base_branch...HEAD` が算出可能な深さを確保する
- Claude Code 用: `clone-claude/`、Codex CLI 用: `clone-codex/`
- 各ツールが独立したディレクトリで動作するため、git/file操作の競合が発生しない
- `pr.diff` は両ツール共通の「PR 差分の確定情報源」として Step 4 で参照される
- `pr.diff.ranges.txt` は `pr.diff` から抽出した「コメント可能行範囲」の確定情報源として Step 4 で参照される

以下の `status.json` / `metadata.json` / `run-plan.json` は Bash で作成する（`jq -n --arg` / `--argjson` / `--slurpfile` / `--rawfile` の出力を `>` でリダイレクト）。`findings.verified.json` / `validation-report.json` / `review.md` は Step 4c で `Write` ツールを使い、runtime gate 通過後に final path へ反映する。

まず現在時刻を取得する（出力を `$started_at` として保持する）。

```bash
date -u +%Y-%m-%dT%H:%M:%S+00:00
```

- いつ使うか: 作業ディレクトリと clone の準備完了後に実行する
- 判定条件: `status.json` が `running` で作成される
- 次アクション: metadata 作成へ進む

```bash
jq -n --arg started_at "$started_at" --arg head_sha "$head_sha" '{state:"running",started_at:$started_at,head_sha:$head_sha}' > ~/claude-loop-pr-codex/$org-$repository-$pr_number/status.json
```

- いつ使うか: `status.json` 作成後に実行する
- 判定条件: `metadata.json` が作成される
- 次アクション: `run-plan.json` 作成へ進む

```bash
jq -n --arg org "$org" --arg repository "$repository" --arg repository_full_name "$repository_full_name" --argjson pr_number "$pr_number" --arg pr_url "$pr_url" --arg head_sha "$head_sha" --arg base_sha "$base_sha" --arg branch "$branch" --arg base_branch "$base_branch" --arg merge_commit_sha "$merge_commit_sha" --arg title "$title" --argjson files "$files_json" '{org:$org,repository:$repository,repository_full_name:$repository_full_name,pr_number:$pr_number,pr_url:$pr_url,head_sha:$head_sha,base_sha:$base_sha,branch:$branch,base_branch:$base_branch,merge_commit_sha:(if $merge_commit_sha == "" then null else $merge_commit_sha end),title:$title,files:$files}' > ~/claude-loop-pr-codex/$org-$repository-$pr_number/metadata.json
```

- いつ使うか: `metadata.json` 作成直後に必ず実行する
- 判定条件: `run-plan.json` が作成され、`files_changed` / `hunks` / `lines_added` / `lines_removed` / `risk_tags` / `selected_hunters` / `depth_actual` / `depth_source` / `depth_reason` / `depth_requested` / `depth_downgraded` / `depth_downgrade_reason` / `recommended_mode` / `skip_reason` / `estimated_stages` / `estimated_timeout_ms` / `actual_duration_ms` / `actual_tokens` が埋まる
- 次アクション: Step 4 前処理へ進む

`run-plan.json` は Step 2b の `files[]` と Step 3 の `pr.diff`、および起動時に解析した `$depth_requested` を使う preflight artifact。M1 では `selected_hunters` は常に `["claude","codex"]` を出力し、将来 F4 の選定ロジックに差し替える前提で固定値とする。`recommended_mode == "skip"` は「/loop では skip 推奨、手動では警告のみ」の**提案値**であり、M1 の既定では実際のレビューを止めず `focused fallback` で継続する。

`depth_actual`（`standard` / `deep`）と `recommended_mode`（`standard` / `focused` / `skip`）は直交した軸として扱う。depth は「1観点あたりの掘り下げ深さ」、recommended_mode は「対象観点の絞り込み」を表すため、`depth_actual="deep"` かつ `recommended_mode="focused"` のような組み合わせも有効である。

モード切替の暫定ルール:

- 明示 `--deep` → `depth_actual = "deep"`、`depth_source = "argument"`（ただし `lines_added + lines_removed > 5000` なら `standard` に強制降格し、`depth_downgraded = true` / `depth_downgrade_reason` を記録）
- 明示 `--standard` → `depth_actual = "standard"`、`depth_source = "argument"`
- 引数なし、かつ `risk_tags` に `security` または `data_migration` を含み、`files_changed <= 20` かつ `lines_added + lines_removed <= 1500` → `depth_actual = "deep"`、`depth_source = "auto"`
- 引数なしで上記に該当しない場合 → `depth_actual = "standard"`、`depth_source = "default"`
- `lines_added + lines_removed > 5000` → `--deep` 明示時も `depth_actual = "standard"` に強制
- `files_changed > 50` → `recommended_mode = "focused"` に切替
- `files_changed > 100` → `recommended_mode = "skip"` と `skip_reason` を設定（ただし M1 の既定実行は skip せず focused fallback）

推定 timeout は `min(1200000, 300000 + files_changed*30000 + hunks*15000 + (lines_added+lines_removed)*100 + sensitive_risk_count*90000)` の暫定式で求める。`sensitive_risk_count` は `risk_tags` のうち `security` / `data_migration` の件数。

```bash
jq -n --arg depth_requested "$depth_requested" --slurpfile metadata ~/claude-loop-pr-codex/$org-$repository-$pr_number/metadata.json --rawfile diff ~/claude-loop-pr-codex/$org-$repository-$pr_number/pr.diff '
  def explicit_depth:
    if $depth_requested == "" or $depth_requested == "null" then null
    elif $depth_requested == "deep" or $depth_requested == "standard" then $depth_requested
    else error("unsupported depth_requested: " + $depth_requested)
    end;
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
  def files_changed: (files | length);
  def total_lines: (lines_added + lines_removed);
  def sensitive_risk_count: (risk_tags | map(select(. == "security" or . == "data_migration")) | length);
  def auto_deep: (sensitive_risk_count > 0 and files_changed <= 20 and total_lines <= 1500);
  def depth_downgraded: (explicit_depth == "deep" and total_lines > 5000);
  def depth_requested_out: explicit_depth;
  def depth_source:
    if explicit_depth != null then "argument"
    elif auto_deep then "auto"
    else "default"
    end;
  def depth_actual:
    if total_lines > 5000 then "standard"
    elif explicit_depth != null then explicit_depth
    elif auto_deep then "deep"
    else "standard"
    end;
  def depth_reason:
    if explicit_depth == "deep" and total_lines > 5000 then "requested --deep but changed lines > 5000; forced standard to preserve the 20 minute timeout"
    elif explicit_depth == "deep" then "requested --deep"
    elif explicit_depth == "standard" then "requested --standard"
    elif total_lines > 5000 then "changed lines > 5000; selected standard to preserve the 20 minute timeout"
    elif auto_deep then "risk_tags include security or data_migration and PR size is <= 20 files / <= 1500 changed lines; selected deep"
    else "no depth argument and no high-risk small-PR signal; selected default standard"
    end;
  def depth_downgrade_reason:
    if depth_downgraded then "requested --deep but changed lines > 5000; forced standard to preserve the 20 minute timeout"
    else null
    end;
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
    estimated_stages: estimated_stages,
    estimated_timeout_ms: estimated_timeout_ms,
    actual_duration_ms: null,
    actual_tokens: null
  }
' > ~/claude-loop-pr-codex/$org-$repository-$pr_number/run-plan.json && test -s ~/claude-loop-pr-codex/$org-$repository-$pr_number/run-plan.json
```

### Step 4 前処理: レビュー観点の読み込み

Step 4a / 4b 共通のレビュー観点本文（MCP追加情報収集 / 7観点 / 出力フォーマット / 重要）は、このスキルディレクトリ内の `REVIEW_CRITERIA.md` に外出ししている。加えて Step 3 で生成した `run-plan.json` を読み、preflight に応じた `{RUN_PLAN_GUIDANCE}` と `{DEPTH_GUIDANCE}` を組み立てる。4a / 4b のプロンプトには `{REVIEW_CRITERIA}` / `{RUN_PLAN_GUIDANCE}` / `{DEPTH_GUIDANCE}` プレースホルダが埋め込まれており、**Claude 自身が Bash ツール呼び出し前にメモリ上で実値へ置換する**（シェル側で `$()` 展開は行わない）。

- いつ使うか: Step 3 完了後、Step 4a / 4b 起動前に必ず実行する
- 判定条件: `REVIEW_CRITERIA.md` の全文と `run-plan.json` の内容を Read ツールで取得できる
- 次アクション:
  - 4a / 4b の Bash コマンド文字列中の `{REVIEW_CRITERIA}` を、下記の `{REVIEW_CRITERIA}` / `{RUN_PLAN_GUIDANCE}` / `{DEPTH_GUIDANCE}` 共通のエスケープ規則（`\` → `\\`、`"` → `\"`、`$` → `\$`、`` ` `` → `\``）に従って整形した本文で置換してから Bash ツールに渡す
  - `run-plan.json` から `.files_changed` / `.hunks` / `.lines_added` / `.lines_removed` / `.risk_tags` / `.selected_hunters` / `.depth_actual` / `.depth_source` / `.depth_reason` / `.depth_requested` / `.depth_downgraded` / `.depth_downgrade_reason` / `.recommended_mode` / `.skip_reason` / `.estimated_stages` / `.estimated_timeout_ms` を保持する。Step 5 の `jq --argjson` に再利用するため、`.risk_tags` と `.selected_hunters` はそれぞれ `$risk_tags_json` / `$selected_hunters_json` として **JSON 配列文字列のまま** 保持し、数値項目も `$files_changed` / `$hunks` / `$lines_added` / `$lines_removed` / `$estimated_stages` / `$estimated_timeout_ms` として保持したうえで、以下の方針で `{RUN_PLAN_GUIDANCE}` と `{DEPTH_GUIDANCE}` を組み立てて置換する

`{RUN_PLAN_GUIDANCE}` の組み立て規則:

- 先頭に `recommended_mode` / `risk_tags` / `estimated_timeout_ms` を箇条書きで明記する
- `risk_tags` / `selected_hunters` は **生の JSON を埋め込まず**、`, ` 区切りの平文（空なら `none`）へ整形してから使う
- `recommended_mode == "standard"` の場合: 既存どおり 7観点をフルに使う
- `recommended_mode == "focused"` の場合: **security / bug / test** を最優先とし、スタイル / リネーム / 軽微な改善は correctness に直結するものだけ残す
- `recommended_mode == "skip"` の場合: `/loop` では skip 推奨水準だが、**M1 の既定は skip せず focused fallback** と明記する。実レビューでも `focused` と同じ重点に絞り、確証の弱い指摘を増やさない
- `risk_tags` に `security` または `data_migration` が含まれる場合: そのタグに対応するファイル群を最優先で確認する
- `recommended_mode` は depth と直交するため、focused / skip fallback でも `depth_actual == "deep"` なら下の `{DEPTH_GUIDANCE}` の深掘り指示を維持する

`{DEPTH_GUIDANCE}` の組み立て規則:

- 先頭に `depth_actual` / `depth_source` / `depth_requested` / `depth_downgraded` / `depth_reason` を箇条書きで明記する。`depth_downgrade_reason` が非 null の場合も併記する
- `depth_actual == "standard"` の場合: 変更行周辺と直接の呼び出し元 / 呼び出し先を優先し、広域探索・仮説列挙・低確度の横展開より 20 分以内完了を優先する
- `depth_actual == "deep"` の場合: 変更行から到達する呼び出し元 / 呼び出し先、設定・スキーマ・権限境界、テスト差分を追加で確認し、反証検討を厚くする。ただしレビュー対象スコープは `pr.diff` と `metadata.json.files[]` に限定し、投稿対象の severity / post_policy は広げない
- `depth_downgraded == true` の場合: ユーザーが `--deep` を指定していても 5000 行ガードにより standard として扱い、広域探索を増やさない
- `{REVIEW_CRITERIA}` / `{RUN_PLAN_GUIDANCE}` / `{DEPTH_GUIDANCE}` を bash double-quote 内へ差し込む前に、3つとも `\` → `\\`、`"` → `\"`、`$` → `\$`、`` ` `` → `\`` の順でエスケープする

さらに canonical findings の `producer.version` を埋めるため、同じ `$CLAUDE_PLUGIN_ROOT` を基準に `$CLAUDE_PLUGIN_ROOT/.claude-plugin/plugin.json` を Read ツールで取得し、`.version` を `$plugin_version` として保持する。`findings.verified.json` の `producer.version` は空文字列不可のため、取得に失敗した場合は Step 5 の **failed 更新** へ遷移する。`schemas/findings.v1.json` も同じ基準で Read し、Step 4c の schema validation に使う。

パス解決: Read ツールの `file_path` には `REVIEW_CRITERIA.md` の絶対パスを渡す。プラグイン環境では `$CLAUDE_PLUGIN_ROOT/skills/review/REVIEW_CRITERIA.md` に配置される。`$CLAUDE_PLUGIN_ROOT` が未設定・不明な場合は以下の Bash で値を取得してから絶対パスを組み立てる。

- いつ使うか: `$CLAUDE_PLUGIN_ROOT` の値が不明で Read に渡す絶対パスを確定できない場合に実行する
- 判定条件: 標準出力が非空
- 次アクション: 出力値を先頭に `skills/review/REVIEW_CRITERIA.md` を連結した絶対パスを Read に渡す。空の場合は Glob ツールで `**/pr-codex/skills/review/REVIEW_CRITERIA.md` を検索してヒットしたパスを使う

```bash
echo "$CLAUDE_PLUGIN_ROOT"
```

### Step 4: レビュー実行（2者レビュー方式）

Claude Code と Codex CLI の両方で独立にレビューし、結果を統合する。

**4a と 4b は並行実行する。** 各ツールは独立した clone ディレクトリを使うため競合しない。両方の Bash コマンドを `run_in_background: true` で同時に発行し、両方の完了を待ってから 4c に進む。Step 4 前処理で読み込んだ観点本文で `{REVIEW_CRITERIA}` / `{RUN_PLAN_GUIDANCE}` / `{DEPTH_GUIDANCE}` を置換した **完全体のコマンド文字列**を Bash ツールへ渡すこと。

#### 4a: Claude Code レビュー

子プロセスの claude code でレビューを実行する。Bash ツールで以下のコマンドを一字一句変えずに実行する。

- いつ使うか: Step 3 完了後に 4b と同時に実行する（`run_in_background: true`）
- 判定条件: `claude-review.md` が生成され、終了コードが 0
- 次アクション: 4b と合わせて両方完了したら 4c へ、失敗または timeout なら Step 5 の failed 更新へ進む
- timeout: `1200000`

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

Codex CLI を使い、同じPRをレビューさせる。Bash ツールで以下のコマンドを一字一句変えずに実行する。

- いつ使うか: Step 3 完了後に 4a と同時に実行する（`run_in_background: true`）
- 判定条件: `codex-review.md` が生成され、終了コードが 0
- 次アクション: 4a と合わせて両方完了したら 4c へ、失敗または timeout なら Step 5 の failed 更新へ進む
- timeout: `1200000`

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

両方のレビューが完了したら、メインコンテキスト（自分自身）が以下を行う:

1. `claude-review.md` / `codex-review.md` / `pr.diff.ranges.txt` / `metadata.json` / `run-plan.json` を読み、さらに canonical schema として `$CLAUDE_PLUGIN_ROOT/schemas/findings.v1.json` を Read する（パス解決は Step 4 前処理の `REVIEW_CRITERIA.md` と同じく `$CLAUDE_PLUGIN_ROOT` 基準で行う）
2. **スコープ検証 (必須)**: どちらか一方でも本文が `PR_DIFF_UNAVAILABLE` のみなら、統合成果物は作成せず Step 5 の **failed 更新** へ遷移する（`findings.verified.json` / `review.md` は生成しない）
3. まず 2 つの生レビューから finding 候補を正規化し、**`findings.verified.json` をメモリ上で先に構築する**。`review.md` も同じくメモリ上でこの canonical artifact から派生生成し、ID / fingerprint 再計算 / 件数 gate と temp file への同梱 validator 検証を通すまで final path へは書き出さない。`findings.verified.json` は `schemas/findings.v1.json` に従い、最低限以下を満たす:
   - top-level: `schema_version = "findings.v1"`, `producer`, `pr`, `generated_at`, `findings[]`
   - `producer.name` は `pr-codex`、`producer.version` は Step 4 前処理で `$CLAUDE_PLUGIN_ROOT/.claude-plugin/plugin.json` から読んだ `$plugin_version`、`producer.run_id` は `<org>-<repository>-<pr_number>-<head_sha>` のような再生成可能な値にする
   - `pr.repository` は `metadata.json.repository_full_name`（base repo の owner/repo 形式。GitHub review 投稿先と同一）を使い、`pr.number` / `pr.base_sha` / `pr.head_sha` も `metadata.json` から埋める。fork PR でも `.head.repo.full_name` は使わない。`merge_commit_sha` は `metadata.json.merge_commit_sha` が `null` でない場合のみ入れる
   - canonical finding では **`source_agent` 単数形は使わず**、`source_agents[]` と `merged_from[]` を使う。`merged_from[]` には生レビュー中の見出しや内部IDなど、由来追跡できる文字列を入れる
   - `id` は M1 では **`fingerprint` と完全に同じ値**に固定する。`fingerprint` は README の「fingerprint 正準アルゴリズム」で定義された `lowercase_hex(sha256(path + "\x1f" + category + "\x1f" + normalized_title + "\x1f" + (primary_symbol || "")))` だけを使う。別のハッシュ、UUID、連番、`run_id` 付き ID は使わない
   - `normalized_title` は `title` を Unicode NFKC → Unicode lowercase → 連続空白を ASCII space 1 個へ畳み込み → 前後 trim → 末尾の Unicode punctuation（General Category P*）をなくなるまで除去 → 最後に右 trim、の順で正規化する。`primary_symbol` は `title` 内で最初に backtick で囲まれた symbol を前後 trim した値に固定し、存在しない場合は空文字列にする
   - `location` は `{path, start_line, end_line?, side, diff_hunk_ref?}`。本 workflow では head 基準のため `side` は通常 `RIGHT` を使う
   - `category` は schema enum の `bug` / `security` / `performance` / `tests` / `design` / `code_quality` / `consistency` / `runtime_error` のいずれかだけを使う。`bugs` や `security_issue` のような自由ラベルは禁止。人間向けの細分類が必要な場合だけ、`fingerprint` 入力外の `category_label` に入れる
   - `title` は短い見出し、`problem` / `reason` / `suggestion` は review.md の 3 点組にそのまま再利用できる粒度で書く
   - `axes` は `{real, triggerable, impactful, general}` の 4 軸を必ず埋める。各軸は `yes` / `no` / `unknown` のいずれかだけを使い、severity だけから `yes` を推測してはならない。4軸判定では、採用したい理由ではなく**落とす理由を優先探索**し、`unknown` を `yes` 扱いしない:
     - `real`: この場所で本当に問題があるなら `yes`。誤解 / 仕様通り / 既存議論で解決済みなら `no`。推測または再現不能なら `unknown`
     - `triggerable`: 2 者一致だけでなく同一 trigger path、または verifier / テスト / CI / 静的解析で実環境のコードパス発火を確認できたら `yes`。静的に到達不能 / dead code なら `no`。発火条件が再現不能なら `unknown`
     - `impactful`: PR 文脈で data loss / security / 仕様不一致など merge を止めるべき影響範囲を具体的に説明できるなら `yes`。影響限定的・ローカル・軽微なら `no`。影響範囲が確認できないなら `unknown`
     - `general`: 横展開が必要なパターン or 同種の他箇所があるなら `yes`。この箇所固有なら `no`。横展開可能性が確認できないなら `unknown`
   - `evidence_level` は根拠の強さに応じて 5 段から 1 つだけ決定論的に選ぶ。1 つの finding で複数段の条件を満たす場合は最も高い到達段階に揃える:
     - `suspicion`: hunter が候補として挙げただけ。具体的根拠なし
     - `corroborated`: 静的解析・型・lint・他箇所のパターン・2 者の同一指摘で裏付け
     - `trigger_path_identified`: head diff 上で発火条件が特定できる
     - `impact_explained`: 影響範囲と修正方針が具体的に書ける
     - `verified`: 反証検討を経て採用 (verifier / 再現テスト / CI / 静的解析で確認)
   - **例外規則**: CI / type system / 既存 lint で検出される類の「明白な静的解析的バグ」は、trigger path が再現できなくても `corroborated` かつ `impact_explained` の両方が成立し、`evidence[]` に `type` が `static_analysis` / `ci_log` / `test` のいずれかで含まれる場合に限り、`verified` に昇格させてよい。`type: manual_review` のみでの昇格は禁止
   - `suspicion` は schema 上 `posting.explanation_postable=false` を強制するため、GitHub 投稿対象にしない
   - **採用基準による severity 降格**: severity を仮決定した後、4軸 gate または `evidence_level` の採用基準を満たさない場合は severity を以下の通り降格する:
     - `must_fix` は `axes.real == "yes"` / `axes.triggerable == "yes"` / `axes.impactful == "yes"` / (`axes.general == "yes"` または `evidence_level in {"impact_explained", "verified"}`) / `evidence_level == "verified"` をすべて満たす場合だけ採用する。同梱 validator も同じ条件を致命エラーとして強制する
     - `must_fix` で `evidence_level < verified` かつ例外規則 (`corroborated + impact_explained` + 静的解析的根拠) を満たさない場合 → `should_fix` に降格
     - `should_fix` で `evidence_level < corroborated` の場合 → `note` に降格し `post_policy=local_only` / `audience=human_reviewer`
     - 降格は canonical findings 確定前に1度だけ行い、降格後の severity を以後の posting 判定で使う
   - `posting` は M1 の `/pr-codex:send` が **Must Fix のみ自動投稿**する前提に合わせ、`{post_policy, explanation_postable, not_postable_reason?, audience?}` を severity ごとに固定する:
     - `must_fix`: 4 軸 gate とコメント可能行範囲を満たし、`evidence_level == "verified"` かつ説明投稿が安全なものだけ `post_policy=inline` / `explanation_postable=true`。それ以外は `must_fix` として採用せず、採用基準による severity 降格後の `should_fix` / `note` として扱う
     - `should_fix`: M1 では GitHub inline 自動投稿対象外のため、説明投稿が安全なら `post_policy=body_summary` / `explanation_postable=true` を既定にする。範囲外・低根拠・投稿に不向きなものは `post_policy=local_only` / `audience=human_reviewer` とする。`not_postable_reason` は `explanation_postable=false` の場合だけ付け、`explanation_postable=true` や `post_policy=inline` と同居させない。`should_fix` に `post_policy=inline` は付けない
     - `nit`: M1 では GitHub inline 自動投稿対象外のため、`post_policy=body_summary` / `explanation_postable=true` を既定にする。範囲外または低根拠なら `local_only` に退避する。`not_postable_reason` は `explanation_postable=false` の場合だけ付ける
     - `note`: `post_policy=local_only` 固定で `audience` を必須にする（既定は `human_reviewer`）。`evidence_level=suspicion` の場合は `explanation_postable=false` / `not_postable_reason=low_evidence_suspicion` を必ず付ける
   - 4 軸 gate 不通過で Must Fix から降格する finding は、`severity="should_fix"` / `posting.post_policy="local_only"` / `posting.audience="human_reviewer"` とし、`severity_disputed=true` / `merger_rule_applied="conservative_min_until_verifier_available"` / `verifier_required=true` / `severity_by_source` を必ず付ける。`severity_by_source` には source agents ごとの当初判定（例: `{"claude":"must_fix","codex":"must_fix"}`）を入れる。`axes` はそのまま保持し、`reason` 末尾に `(4軸ゲート不通過: GENERAL=unknown)` のような人間可読マーカーを付ける（validator では正規表現検出しない）。降格された finding は `review.md` の `## 補足` に「(参考: 4軸ゲート不通過)」付きで残し、`## 改善提案 (Should Fix)` には載せない
   - `fingerprint` の入力は README 記載どおり `path` / `category` / `normalized_title` / `primary_symbol` に固定し、`line` は含めない
   - JSON Schema Draft 2020-12 だけでは sibling equality (`id == fingerprint`) や fingerprint 再計算を標準機能だけで強制しづらいため、Step 4c の runtime gate として **全 finding で `id == fingerprint` と正準 fingerprint 再計算一致を確認**する。1 件でもずれたら failed とし、completed にしてはならない
   - **`created_at` は finding 個別には書かない**。Issue #16 の最新 comment と参照 gist を優先し、canonical runtime artifact では top-level `generated_at` に集約する
4. **破棄ルール (必須)**: `metadata.json.files[]` に含まれないパスへの指摘は canonical findings に採用しない。ファイルパスが `.md` の見出しやコードブロックで言及されていたら、そのパスが `files[]` 配列に属するかを必ず照合する。有益な一般的指摘で残す価値があるものだけ、`severity=note` + `posting.post_policy=local_only` もしくは `review.md` の `## 補足` 末尾に「参考（範囲外）」として残す。`must_fix` / `should_fix` には絶対に採用しない
5. **コメント可能行範囲の自己検証 (必須)**: `must_fix` として採用する各 finding について、`location.path` と `location.start_line` / `location.end_line` が `pr.diff.ranges.txt` の同一 `path` の範囲内に収まるかをメインコンテキストで検証する。単一行は `start_line`、複数行は `[start_line, end_line]` の両端が同じ hunk 範囲内にある場合だけ有効とする。範囲外なら、同一ファイルの最も近いコメント可能行へ `location` を差し替え、`problem` または `reason` に `(参考: 元の行 path:L<行番号>)` を補足する。同一ファイルにコメント可能行がない場合は `must_fix` には採用せず、`note` / `local_only` または `## 補足` に退避する。`should_fix` / `nit` は M1 では inline 自動投稿対象外だが、`review.md` の参照性を保つため、可能な限り diff 範囲内の head 側行番号を使う
6. **4軸 gate (必須)**: `must_fix` として採用する各 finding は、temp 書き出し前に `axes.real == "yes"` / `axes.triggerable == "yes"` / `axes.impactful == "yes"` / (`axes.general == "yes"` または `evidence_level in {"impact_explained", "verified"}`) / `evidence_level == "verified"` をメインコンテキストで検証する。通過しない finding は上記の降格ポリシーを適用する。`validation-report.json` を出す場合は unknown 軸数 / unknown または no を理由に降格した件数 / gate 後の Must Fix 件数 / ladder 分布 (`evidence_level_counts: {suspicion, corroborated, trigger_path_identified, impact_explained, verified}`) / `must_fix_verified_ratio` / `exception_promotion_count` を記録する
7. severity が衝突した場合は **conservative min** を採用し、`severity_disputed=true`, `severity_by_source`, `merger_rule_applied="conservative_min_until_verifier_available"`, `verifier_required=true` を記録する。validation status (`metadata_files_member`, `diff_range_valid`) は canonical findings には入れず、必要なら副成果物 `validation-report.json` に分離する
8. 生レビューを内部的に比較し、最終 findings へ統合する。この比較過程は `review.md` に書かない:
   - **一致点**: 同一原因・同一影響・同一箇所の重複指摘をまとめ、採用判断の信頼度評価に使う
   - **相違点**: 一部の生レビューにだけある指摘は、`pr.diff` と checkout 済みソースで 4軸 (`real` / `triggerable` / `impactful` / `general`) を明示的に埋め、落とす理由（`no` / `unknown`）を優先探索して採否と重要度を決める。採用したい理由だけで Must Fix にしない
   - **補完**: 見落とされていた観点が補われている場合は、根拠を確認したうえで最終 findings に反映する
   - `review.md` には最終判断のみを書く。レビュー実行者名 / モデル名 / どの生レビュー由来かを示す表現 / `両者一致` / `片方のみ` のような由来表現は書かない
9. `review.md` は **`findings.verified.json` から派生生成** する。`must_fix` → `## 重大な問題 (Must Fix)`, `should_fix` かつ `post_policy=body_summary` → `## 改善提案 (Should Fix)`, `nit` → `## 軽微な指摘 (Nit)`, `note` や `post_policy=local_only/suppress` の項目 → `## 補足` に対応させる。`## 総評` と `## 良い点` は人間向け要約として記述してよいが、Must Fix / Should Fix の件数や内容が canonical findings と矛盾してはならない
10. `run-plan.json` で `skip_reason != null`、`recommended_mode != "standard"`、`depth_actual != "standard"`、`depth_source != "default"`、または `depth_downgraded == true` のいずれかに該当する場合は、`review.md` の `## 補足` に preflight 情報を最低限残す。`skip_reason != null` の場合は `- preflight: <skip_reason>。M1 の既定では focused fallback でレビューを継続した。` を含める。加えて `files_changed` / `lines_added` / `lines_removed` / `recommended_mode` / `depth_actual` / `depth_source` / `depth_reason` / `risk_tags` を明記し、レビュー範囲や重点が意図的に変わった事実を受け手が判断できるようにする
11. **fingerprint 整合 gate (必須)**: 全 finding で `id == fingerprint` を確認し、さらに `path` / `category` / `title` から README の正準アルゴリズムで fingerprint を再計算した値が JSON 内の `fingerprint` と一致することを、後述の `tasks/validate_findings.py` で検証する。1 件でもずれたら Step 5 の **failed 更新** へ遷移し、final artifact を書き出してはならない
12. **件数一致 gate (必須)**: `findings.verified.json` の `severity=must_fix` 件数と、派生生成した `review.md` の `## 重大な問題 (Must Fix)` 見出し件数は **100% 一致** させる。1 件でもずれたら Step 5 の **failed 更新** へ遷移し、completed にしてはならない
13. 上記 runtime gate を通過した場合のみ、`findings.verified.json` / `review.md`（必要なら `validation-report.json` も）をまず `*.tmp` へ `Write` ツールで書き出す
14. **同梱 validator gate (必須)**: `findings.verified.json.tmp` を `tasks/validate_findings.py` で検証する。必須フィールド欠落、型不一致、enum 不一致、`posting` / `evidence_level` 条件違反、4軸 gate 違反、`pr.number` 非整数、RFC3339 / URI format 不正、`end_line < start_line`、`id != fingerprint`、fingerprint 再計算不一致、`metadata.json` の投稿先 repo / PR number / head/base SHA と `findings.verified.json.pr.*` の不一致など 1 件でも contract に反したら Step 5 の **failed 更新** へ遷移し、final artifact を書き出してはならない
15. temp write と同梱 validator が成功した場合のみ Bash の `mv` で final path へ反映する。途中で temp write / validator / `mv` のいずれかが失敗した場合は Step 5 の **failed 更新** へ遷移し、completed にしてはならない

- いつ使うか: `claude-review.md` と `codex-review.md` の両方が揃った後
- 判定条件: 全 finding で `id == fingerprint` が成り立ち、`review.md` と Must Fix 件数が一致し、`findings.verified.json.tmp` が同梱 validator を通過したうえで temp file → final path の反映まで完了する（`PR_DIFF_UNAVAILABLE` の場合は生成しない）
- 次アクション: 書き出し後 Step 5 へ進む（`PR_DIFF_UNAVAILABLE` / `id != fingerprint` / 件数不一致 / temp write failure / validator failure / `mv` failure があった場合は Step 5 failed 分岐へ）

`Write` ツールは `~` やシェル変数（`$org` 等）を展開しない。`file_path` にはホームディレクトリを `$HOME` の実値（例: `/Users/adachi`）に展開済みの絶対パスを渡し、`$org` / `$repository` / `$pr_number` も実値に置換してから呼び出すこと。`findings.verified.json` の JSON 本文も、プレースホルダを残さず実値で埋める。temp file を使う場合も同様に絶対パスで指定する。

temp file 書き出し後、final artifact へ反映する前に以下の同梱 validator を必ず実行する。`$CLAUDE_PLUGIN_ROOT` が shell 環境で未設定の場合は、Step 4 前処理で解決した plugin root の絶対パスに置換してから Bash ツールへ渡す（コマンド構造は変えない）。この validator は stdlib-only で、npm cache やネットワークを使わず、作業ディレクトリ外へ書き込まない。

```bash
python3 $CLAUDE_PLUGIN_ROOT/tasks/validate_findings.py --schema $CLAUDE_PLUGIN_ROOT/schemas/findings.v1.json --data ~/claude-loop-pr-codex/$org-$repository-$pr_number/findings.verified.json.tmp --metadata ~/claude-loop-pr-codex/$org-$repository-$pr_number/metadata.json
```

temp file を final artifact に反映する際は、以下の `mv` テンプレートだけを使う。`review.md` を先に反映し、その後 `findings.verified.json` を反映する。これにより `findings.verified.json` だけが残る状態を避け、completed 更新前に send primary path の前提が成立しないようにする。

```bash
mv ~/claude-loop-pr-codex/$org-$repository-$pr_number/review.md.tmp ~/claude-loop-pr-codex/$org-$repository-$pr_number/review.md
```

```bash
mv ~/claude-loop-pr-codex/$org-$repository-$pr_number/findings.verified.json.tmp ~/claude-loop-pr-codex/$org-$repository-$pr_number/findings.verified.json
```

副成果物 `validation-report.json` を出す場合のみ、最後に以下を実行してよい。

```bash
mv ~/claude-loop-pr-codex/$org-$repository-$pr_number/validation-report.json.tmp ~/claude-loop-pr-codex/$org-$repository-$pr_number/validation-report.json
```

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

まず現在時刻を取得する（出力を `$finished_at` として保持する）。

```bash
date -u +%Y-%m-%dT%H:%M:%S+00:00
```

- いつ使うか: Step 4c まで成功した場合に最初に実行する
- 判定条件: `run-plan.json` の `actual_duration_ms` がミリ秒で埋まる
- 次アクション: 成功したら completed `status.json` 更新へ進む。失敗したらこの回は completed にせず、failed 分岐へ遷移する

```bash
tmp_run_plan=~/claude-loop-pr-codex/$org-$repository-$pr_number/run-plan.json.tmp
jq -n --argjson files_changed "$files_changed" --argjson hunks "$hunks" --argjson lines_added "$lines_added" --argjson lines_removed "$lines_removed" --argjson risk_tags "$risk_tags_json" --argjson selected_hunters "$selected_hunters_json" --arg depth_actual "$depth_actual" --arg depth_source "$depth_source" --arg depth_reason "$depth_reason" --arg depth_requested "$depth_requested" --argjson depth_downgraded "$depth_downgraded" --arg depth_downgrade_reason "$depth_downgrade_reason" --arg recommended_mode "$recommended_mode" --arg skip_reason "$skip_reason" --argjson estimated_stages "$estimated_stages" --argjson estimated_timeout_ms "$estimated_timeout_ms" --arg started_at "$started_at" --arg finished_at "$finished_at" '{
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
  estimated_stages: $estimated_stages,
  estimated_timeout_ms: $estimated_timeout_ms,
  actual_duration_ms: (((($finished_at | strptime("%Y-%m-%dT%H:%M:%S+00:00") | mktime) - ($started_at | strptime("%Y-%m-%dT%H:%M:%S+00:00") | mktime)) * 1000)),
  actual_tokens: null
}' > "$tmp_run_plan" && test -s "$tmp_run_plan" && mv "$tmp_run_plan" ~/claude-loop-pr-codex/$org-$repository-$pr_number/run-plan.json
```

- いつ使うか: 上の `run-plan.json` 更新成功直後に実行する
- 判定条件: `status.json` の `state` が `completed` になる
- 次アクション: Step 6 の結果報告へ進む

```bash
jq -n --arg started_at "$started_at" --arg finished_at "$finished_at" --arg head_sha "$head_sha" '{state:"completed",started_at:$started_at,finished_at:$finished_at,exit_code:0,head_sha:$head_sha}' > ~/claude-loop-pr-codex/$org-$repository-$pr_number/status.json
```

- いつ使うか: Step 4a または 4b が timeout / 非ゼロ終了した場合、権限不足などで処理継続不可の場合、**または Step 4c のスコープ検証で `claude-review.md` / `codex-review.md` のいずれかが `PR_DIFF_UNAVAILABLE` のみだった場合**に実行する
- 判定条件: `status.json` の `state` が `failed`
- 次アクション: Step 6 の結果報告へ進む

```bash
jq -n --arg started_at "$started_at" --arg finished_at "$finished_at" --arg head_sha "$head_sha" '{state:"failed",started_at:$started_at,finished_at:$finished_at,exit_code:1,head_sha:$head_sha}' > ~/claude-loop-pr-codex/$org-$repository-$pr_number/status.json
```

### Step 6: 結果報告

レビュー結果の要約をユーザーに報告する。報告内容:

- 対象PR（リンク付き）
- レビュー結果のサマリ（総評 / 重大な問題 / 改善提案 から要約）
- 結果ファイルのパス
- いつ使うか: Step 5 の status 更新後
- 次アクション: `review.md` を Read ツールで読み、以下の内容をユーザーにテキストで報告して終了する
  - 対象PR（`$pr_url` のリンク付き）
  - レビュー結果の要約（総評 + 重大な問題の件数と代表例、改善提案の件数を含める）
  - 結果ファイルのパス（`~/claude-loop-pr-codex/$org-$repository-$pr_number/findings.verified.json` と `~/claude-loop-pr-codex/$org-$repository-$pr_number/review.md`）

## エラーハンドリング

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
- **`findings.verified.json.tmp` が同梱 validator による `schemas/findings.v1.json` validation / fingerprint 再計算 / format / range validation に失敗 → `state=failed` で記録し、final artifact は反映しない**
- **`findings.verified.json` のいずれかの finding で `id != fingerprint` → `state=failed` で記録し、final artifact は反映しない**
- **`findings.verified.json` の Must Fix 件数と `review.md` の Must Fix 見出し件数が不一致 → `state=failed` で記録し、send へ進めない**
- **`*.tmp` の Write または temp → final の `mv` が失敗 → `state=failed` で記録し、completed にしない**
- 権限不足（404/403） → `state=failed` で記録し、その回は終了

## ファイル構成

スキル本体（プラグイン側に同梱・参照のみ、作業ディレクトリには置かない）:

```
$CLAUDE_PLUGIN_ROOT/skills/review/
  ├── SKILL.md                ← 本ファイル
  └── REVIEW_CRITERIA.md      ← 4a / 4b 共通のレビュー観点本文。Step 4 前処理で Read し、{REVIEW_CRITERIA} プレースホルダに置換
$CLAUDE_PLUGIN_ROOT/tasks/
  └── validate_findings.py    ← canonical findings の schema / fingerprint / format / range validator
```

実行時の作業ディレクトリ:

```
~/claude-loop-pr-codex/
  └── $org-$repository-$pr_number/
        ├── status.json
        ├── metadata.json        ← org/repository/repository_full_name/pr_number/pr_url/head_sha/base_sha/branch/base_branch/merge_commit_sha/title/files を含む
        ├── run-plan.json        ← preflight 指標。Step 5 成功時に actual_duration_ms / actual_tokens を追記
        ├── pr.diff              ← PR 差分 (unified diff)。Step 4a/4b のスコープ確定情報源
        ├── pr.diff.ranges.txt   ← コメント可能行範囲。Step 4a/4b と Step 4c の行番号検証に使う
        ├── clone-claude/        ← Claude Code 用 shallow clone (depth 50, base fetch 済み)
        ├── clone-codex/         ← Codex CLI 用 shallow clone (depth 50, base fetch 済み)
        ├── claude-review.md     ← Claude Code の生レビュー
        ├── codex-review.md      ← Codex CLI の生レビュー
        ├── findings.verified.json ← canonical findings (`schemas/findings.v1.json`)
        ├── validation-report.json ← validation の副成果物（optional）
        ├── review.md            ← 統合レビュー（最終成果物）
        ├── findings.verified.json.tmp  ← Step 4c の一時ファイル（失敗時に残り得る）
        ├── validation-report.json.tmp  ← Step 4c の一時ファイル（optional）
        ├── review.md.tmp        ← Step 4c の一時ファイル（失敗時に残り得る）
        ├── claude.log
        └── codex.log
```

## 実装上の制約

本スキルは Claude Code を `--permission-mode auto` で起動することを前提とする（README の「使い方」参照）。auto mode でも、許可済みツールやコマンドの内容によっては分類器の判断で承認が必要になり得るため、本スキルではテンプレートに明示された操作だけを実行する。

ローカルの書き込みは作業ディレクトリ `~/claude-loop-pr-codex/` 配下に限り、`clone-claude/` / `clone-codex/` の作成と更新、`status.json` / `metadata.json` / `run-plan.json` / `pr.diff` / `pr.diff.ranges.txt` / `claude.log` / `codex.log` / `claude-review.md` / `codex-review.md` / `findings.verified.json` / `validation-report.json` / `review.md` と、それらの `*.tmp` 一時ファイル作成のみ許可する。schema / fingerprint validation のために `python3 $CLAUDE_PLUGIN_ROOT/tasks/validate_findings.py ...` を実行してよいが、validator は成果物を書き換えず検証だけに使う。

許可ルールは以下の allowlist に従う。

1. 最上位ルール: テンプレートに明示された構文のみ許可する。テンプレート外の構文追加は禁止
2. 各テンプレートは 1テンプレート = 1シェル実行単位として扱う
3. テンプレートの改変は変数置換のみ許可する。フラグ、引数順、引用符、リダイレクト、パイプ、演算子はテンプレート記載どおりに使う
4. シェル演算子はテンプレート中に明示された `|` `<` `>` `2>` `&&` のみ許可する。パイプラインの upstream 失敗検知のため、テンプレート中に明示された `set -o pipefail &&` は削除せずそのまま使う
5. JSON 生成は `jq -n --arg` / `--argjson` / `--slurpfile` / `--rawfile` を使う。ヒアドキュメントで JSON を直接組み立てない
6. ファイル書き込みの使い分け:
   - `findings.verified.json.tmp` / `validation-report.json.tmp` / `review.md.tmp` は `Write` ツールで書き出し、gate 通過後に `mv` で final name へ反映する（`file_path` は `~` / `$...` を展開しないため、実値の絶対パスを渡す）
   - `status.json` / `metadata.json` / `run-plan.json` は `jq -n --arg` / `--argjson` / `--slurpfile` / `--rawfile` の出力を `Bash` の `>` で書く
   - `pr.diff` は Step 3 の `gh pr diff` の標準出力を `>` でリダイレクトして作成する
   - `pr.diff.ranges.txt` は Step 3 の `awk` の標準出力を `>` でリダイレクトして作成する
   - `claude-review.md` / `codex-review.md` / `claude.log` / `codex.log` は Step 4a / 4b の標準出力・標準エラーを `>` / `2>` でリダイレクトして作成する
7. Step 4a / 4b の timeout は必ず `1200000` に固定する
8. テンプレートに明示された `git fetch` / `git checkout FETCH_HEAD` / `python3 $CLAUDE_PLUGIN_ROOT/tasks/validate_findings.py ...` / temp file から final artifact への `mv` / 成果物ファイル作成以外の状態変更操作は実行しない。禁止例: `git push` / `git merge` / `git reset --*` / `git clean -fd[x]` / `git stash` / `git commit` / `git tag` / `git branch -D`、`rm -rf` 系、`gh pr` / `gh issue` の write 操作、および GitHub / Backlog / DocBase の write 系 MCP ツール
9. 1回の実行で選定・処理する PR は 1 件のみとする
10. Step 4a / 4b のプロンプト中に含まれる `{REVIEW_CRITERIA}` / `{RUN_PLAN_GUIDANCE}` / `{DEPTH_GUIDANCE}` プレースホルダは、Step 4 前処理で Read した `REVIEW_CRITERIA.md` と `run-plan.json` を元に Claude 側で置換したうえで、Bash ツールに渡す完全体のコマンド文字列として使う。`{REVIEW_CRITERIA}` / `{RUN_PLAN_GUIDANCE}` / `{DEPTH_GUIDANCE}` のいずれも bash double-quote 内で安全になるよう、差し込み前に **`\` → `\\`、`"` → `\"`、`$` → `\$`、`` ` `` → `\``** の順でエスケープする。シェルでのコマンド置換 (`$()`) やヒアドキュメントは使わない

補助注記（いずれもテンプレート一字一句原則の具体適用例）:

- Step 2b の metadata 取得テンプレートだけは、`gh` の version-dependent な `gh pr view --json headRefOid/baseRefOid` を避けるため `gh api ... --jq '...'` を明示的に使う。それ以外のテンプレートへ任意に `--jq` を追加しない。`gh pr view --jq` は使わない
- `set -o pipefail &&` が明示されたテンプレートでは、`gh api --paginate` など upstream の非ゼロ終了を最後段の `jq` 成功で握りつぶさないため、必ずテンプレートどおりに残す
- `$()` は使わない。コマンド置換はテンプレートに含まれず、auto mode でも承認プロンプトや停止要因になり得る（変数展開 `$org` 等はテンプレート内で使用する）
- `for` / `while` / `while read` / `xargs` などのループ・反復構文は使わない。テンプレート外であり、実行単位・ログの再現性を崩す
- Codex CLI のグローバルオプション（`--ask-for-approval` 等）は `exec` サブコマンドよりも前に置くこと。`exec` の後ろに付けると受け付けられず非対話実行が止まる
