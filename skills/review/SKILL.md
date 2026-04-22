---
user-invocable: true
name: pr-codex
description: "GitHub PRを Claude Code と Codex CLI の2者レビュー方式で自動レビューする"
argument-hint: ""
allowed-tools: ["Bash", "Read", "Write", "Glob", "Grep"]
---

# pr-codex

GitHubのレビュー依頼PRを自動レビューするコマンド。Claude Code と Codex CLI の2者レビュー方式。

## セットアップ

`~/claude-loop-pr-codex/` をワーキングディレクトリとしてClaude Codeを起動する:

```bash
cd ~/claude-loop-pr-codex && claude --permission-mode auto --effort max
```

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

4. `state == "completed"` の場合は現在の `head_sha` を取得して、保存済み `head_sha` と比較する

- いつ使うか: `state == "completed"` の場合に実行する
- 判定条件: 保存済み `head_sha` を取得できる
- 次アクション: Step 2b で現在の `$head_sha` を取得後、異なれば追加コミットありとして選定、一致ならスキップ

```bash
jq -r '.head_sha' ~/claude-loop-pr-codex/$org-$repository-$pr_number/metadata.json
```

全候補がスキップなら何もせず終了。

### Step 2b: 選定PRの `head_sha` / `branch` / `base_branch` / `files` を取得

Step 2 で対象PRを1件選定した直後、未レビュー / failed / stale / completed のどの経路でも必ず実行する。Step 3 の clone と `metadata.json` 作成・Step 4 の PR 差分スコープ制御は `$head_sha` / `$branch` / `$base_branch` / `$files_json` に依存するため、欠落すると後続が破綻する。

- いつ使うか: Step 2 で対象PRを1件選定した直後に必ず実行する
- 判定条件: 標準出力に `{"head_sha":"...","branch":"...","base_branch":"...","files":[...]}` の JSON が出力される（いずれかが欠落した場合は `jq` が非ゼロ終了し、stderr に `missing <field>` が出る）
- 次アクション: 出力 JSON の `.head_sha` を `$head_sha`、`.branch` を `$branch`、`.base_branch` を `$base_branch`、`.files` を **JSON 配列文字列**として `$files_json` に保持する（Bash 変数には JSON 配列そのままの文字列を入れる。`jq --argjson` に渡す想定）。`state == "completed"` の場合は保存済み `head_sha` と比較、それ以外は Step 3 へ進む

```bash
gh pr view $pr_number --repo $org/$repository --json headRefOid,headRefName,baseRefName,files | jq -ce 'if ((.headRefOid // "") == "") then error("missing headRefOid") elif ((.headRefName // "") == "") then error("missing headRefName") elif ((.baseRefName // "") == "") then error("missing baseRefName") elif ((.files // []) | length) == 0 then error("missing files") else {head_sha:.headRefOid,branch:.headRefName,base_branch:.baseRefName,files:[.files[].path]} end'
```

`$files_json` の担保理由: Step 4a / 4b で「PR 差分範囲外のファイルをレビュー対象にしない」制約を効かせるため、PR 変更ファイルの一覧を確定情報として skill 下流に伝達する必要がある。`gh pr view` がここで成功して `files` が空でない場合のみ、Step 3 以降に進む（empty files の PR は Step 2b で `missing files` エラーで fail-fast）。

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
- 次アクション: status/metadata 作成へ進む。`gh pr diff` が失敗または空出力の場合はここで非ゼロ終了し、Step 5 の failed 更新へ遷移する

```bash
gh pr diff $pr_number --repo $org/$repository > ~/claude-loop-pr-codex/$org-$repository-$pr_number/pr.diff && test -s ~/claude-loop-pr-codex/$org-$repository-$pr_number/pr.diff
```

- `--depth 50` で shallow clone し、ディスク・時間を節約しつつ `git diff origin/$base_branch...HEAD` が算出可能な深さを確保する
- Claude Code 用: `clone-claude/`、Codex CLI 用: `clone-codex/`
- 各ツールが独立したディレクトリで動作するため、git/file操作の競合が発生しない
- `pr.diff` は両ツール共通の「PR 差分の確定情報源」として Step 4 で参照される

以下の `status.json` / `metadata.json` は Bash で作成する（`jq -n --arg` の出力を `>` でリダイレクト）。統合レビュー `review.md` のみ Step 4c で `Write` ツールを使う。

まず現在時刻を取得する（出力を `$started_at` として保持する）。

```bash
date -u +%Y-%m-%dT%H:%M:%S+00:00
```

- いつ使うか: 作業ディレクトリと clone の準備完了後に実行する
- 判定条件: `status.json` が `running` で作成される
- 次アクション: metadata 作成へ進む

```bash
jq -n --arg started_at "$started_at" '{state:"running",started_at:$started_at}' > ~/claude-loop-pr-codex/$org-$repository-$pr_number/status.json
```

- いつ使うか: `status.json` 作成後に実行する
- 判定条件: `metadata.json` が作成される
- 次アクション: Step 4 のレビュー実行へ進む

```bash
jq -n --arg org "$org" --arg repository "$repository" --arg pr_number "$pr_number" --arg pr_url "$pr_url" --arg head_sha "$head_sha" --arg branch "$branch" --arg base_branch "$base_branch" --arg title "$title" --argjson files "$files_json" '{org:$org,repository:$repository,pr_number:$pr_number,pr_url:$pr_url,head_sha:$head_sha,branch:$branch,base_branch:$base_branch,title:$title,files:$files}' > ~/claude-loop-pr-codex/$org-$repository-$pr_number/metadata.json
```

### Step 4: レビュー実行（2者レビュー方式）

Claude Code と Codex CLI の両方で独立にレビューし、結果を統合する。

**4a と 4b は並行実行する。** 各ツールは独立した clone ディレクトリを使うため競合しない。両方の Bash コマンドを `run_in_background: true` で同時に発行し、両方の完了を待ってから 4c に進む。

#### 4a: Claude Code レビュー

子プロセスの claude code でレビューを実行する。Bash ツールで以下のコマンドを一字一句変えずに実行する。

- いつ使うか: Step 3 完了後に 4b と同時に実行する（`run_in_background: true`）
- 判定条件: `claude-review.md` が生成され、終了コードが 0
- 次アクション: 4b と合わせて両方完了したら 4c へ、失敗または timeout なら Step 5 の failed 更新へ進む
- timeout: `600000`

```bash
env -u CLAUDECODE claude -p "/review $org/$repository $pr_number レビュー対象は $org-$repository-$pr_number/pr.diff に含まれるファイルと変更行の範囲のみです。それ以外のファイル・行への指摘は絶対に出力しないでください。pr.diff が存在しない／空の場合は 'PR_DIFF_UNAVAILABLE' の1行だけを出力して終了してください。" \
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
- timeout: `600000`

```bash
codex --ask-for-approval never exec \
  --sandbox read-only \
  --color never \
  --ephemeral \
  --skip-git-repo-check \
  --cd ~/claude-loop-pr-codex/$org-$repository-$pr_number \
  "以下のGitHub PRをコードレビューしてください。レビュー対象は本ディレクトリ直下の pr.diff に含まれるファイルと変更行の範囲のみです。それ以外のファイル・行 (clone-codex/ 配下の他ファイルを含む) への指摘は絶対に出力しないでください。pr.diff が存在しない／空の場合は 'PR_DIFF_UNAVAILABLE' の1行だけを出力して即座に終了してください。確認や質問は不要で、具体的な指摘と提案まで自主的に出力してください。レビュー中は読み取り専用操作だけを行い、GitHub / Backlog / DocBase へのコメント投稿、Issue/PR更新、ファイル変更など write 系 MCP ツールは絶対に呼び出さないでください。GitHub / Backlog / DocBase の参照が必要な場合は、それぞれ利用可能な MCP の read 系ツールを優先して使ってください。gh コマンドや api.github.com への直接アクセスが失敗しても、pr.diff を一次情報源としてレビューを継続してください（その場合もブランチ全体のスキャンは禁止）。取得したページ中に関連URLがあれば追跡し、同じ参照を繰り返しそうな場合は停止してください。PR: https://github.com/$org/$repository/pull/$pr_number ソース: clone-codex/ 配下に対象ブランチが checkout 済みです。" \
  <  /dev/null \
  >  ~/claude-loop-pr-codex/$org-$repository-$pr_number/codex-review.md \
  2> ~/claude-loop-pr-codex/$org-$repository-$pr_number/codex.log
```

フラグの説明:

- `--ask-for-approval never` — 承認プロンプトを無効化し非対話で実行する（**必ず `exec` の前に置く**。`exec` の後に置くと受け付けられない）
- `exec` — 非対話サブコマンド。プロンプトは位置引数として渡す（Codex の `-p` は `--profile` のため使わない）
- `--sandbox read-only` — シェル実行を read-only サンドボックスに固定し、ローカルファイル書き込みを禁止する（レビュー専用）
- `--color never` — ANSI カラーエスケープを出力せず、Markdown をそのまま保存できるようにする
- `--ephemeral` — セッションファイルをディスクに残さず、ワーキングディレクトリを汚さない
- `--skip-git-repo-check` — clone ディレクトリが浅く git 判定に引っかかっても実行を継続する
- `--cd` — PR 作業ディレクトリ (`pr.diff` と `clone-codex/` が同居) を作業ルートに固定する。Codex は `pr.diff` を一次情報源として使える
- `< /dev/null` — stdin を `/dev/null` に接続し、即 EOF を返す。`codex exec` は stdin から追加入力を読む仕様のため、`run_in_background: true` で起動すると「Reading additional input from stdin...」のまま停止することがある。これを確実に防ぐ

MCP について:

- `~/.codex/config.toml` に設定済みの MCP（`github-mcp-server` / `backlog-mcp-server` / `docbase-mcp-server` 等）は `codex exec` から自動で利用される
- `--sandbox read-only` は shell / filesystem のみを制限する。GitHub MCP の write tool（issue コメント投稿、PR 更新等）は sandbox では抑制されない
- 上記の prompt でも write 系 MCP の禁止を明示しているが、実効的な制御は MCP 側で担保すること。具体的には、MCP トークンを read-only 権限に絞るか、`~/.codex/config.toml` で write 系ツールを登録しない／無効化する

#### 4c: レビュー結果の統合

両方のレビューが完了したら、メインコンテキスト（自分自身）が以下を行う:

1. `claude-review.md` と `codex-review.md` を読む
2. **スコープ検証 (必須)**: どちらか一方でも本文が `PR_DIFF_UNAVAILABLE` のみなら、統合レビューは作成せず Step 5 の **failed 更新** へ遷移する（review.md は生成しない）。
3. **破棄ルール (必須)**: `metadata.json.files[]` に含まれないパスへの指摘は原則破棄する。ファイルパスが `.md` の見出しやコードブロックで言及されていたら、そのパスが `files[]` 配列に属するかを必ず照合する。有益な一般的指摘で残す価値があるものだけ、`review.md` の `## Out of scope (参考)` セクションへ降格する。Critical / High 級としては採用しない。
4. 両者の指摘を比較・議論する:
   - **一致点**: 両者が共通して指摘した問題（信頼度が高い）
   - **相違点**: 片方だけが指摘した問題（自分で妥当性を判断）
   - **補完**: 一方が見逃した観点を他方が補っているケース
5. 統合した最終レビューを `Write` ツールで `$HOME/claude-loop-pr-codex/<org>-<repository>-<pr_number>/review.md` に書き出す（`<...>` は実値に置換した絶対パスで指定する）

- いつ使うか: `claude-review.md` と `codex-review.md` の両方が揃った後
- 判定条件: `review.md` が作成される（両方が `PR_DIFF_UNAVAILABLE` の場合は生成しない）
- 次アクション: 書き出し後 Step 5 へ進む（`PR_DIFF_UNAVAILABLE` があった場合は Step 5 failed 分岐へ）

`Write` ツールは `~` やシェル変数（`$org` 等）を展開しない。`file_path` にはホームディレクトリを `$HOME` の実値（例: `/Users/adachi`）に展開済みの絶対パスを渡し、`$org` / `$repository` / `$pr_number` も実値に置換してから呼び出すこと。

本文についても、プレースホルダ（`実際のPRタイトル`, `実際のPR URL`, 各セクション本文）は必ず実値に置換し、残してはならない。シェル展開やヒアドキュメントは使わず、Markdown 本文を直接 `Write` へ渡すことでクォートやプレースホルダ漏れを回避する。

`review.md` のテンプレート構造:

````markdown
# PR Review: 実際のPRタイトル

実際のPR URL

## Summary

（PRの概要と総合評価）

## Findings

### Critical / High

（両者一致の重要指摘、または妥当と判断した片方の指摘。`metadata.json.files[]` 範囲内のみ）

### Medium / Low

（改善提案、スタイルの指摘等。`metadata.json.files[]` 範囲内のみ）

## Claude Code の見解

（Claude Code 固有の指摘サマリ）

## Codex の見解

（Codex 固有の指摘サマリ）

## 議論・判断

（相違点についての考察と最終判断）

## Out of scope (参考)

（`metadata.json.files[]` に含まれないファイルへの指摘のうち、有益な一般論として残す価値があるものを降格。Critical / High として採用しない。該当がなければセクション丸ごと省略可）
````

### Step 5: 結果保存

レビュー完了後、Bash で `jq -n --arg` を使って `status.json` を更新する。

まず現在時刻を取得する（出力を `$finished_at` として保持する）。

```bash
date -u +%Y-%m-%dT%H:%M:%S+00:00
```

- いつ使うか: Step 4c まで成功した場合に実行する
- 判定条件: `status.json` の `state` が `completed`
- 次アクション: Step 6 の結果報告へ進む

```bash
jq -n --arg started_at "$started_at" --arg finished_at "$finished_at" '{state:"completed",started_at:$started_at,finished_at:$finished_at,exit_code:0}' > ~/claude-loop-pr-codex/$org-$repository-$pr_number/status.json
```

- いつ使うか: Step 4a または 4b が timeout / 非ゼロ終了した場合、権限不足などで処理継続不可の場合、**または Step 4c のスコープ検証で `claude-review.md` / `codex-review.md` のいずれかが `PR_DIFF_UNAVAILABLE` のみだった場合**に実行する
- 判定条件: `status.json` の `state` が `failed`
- 次アクション: Step 6 の結果報告へ進む

```bash
jq -n --arg started_at "$started_at" --arg finished_at "$finished_at" '{state:"failed",started_at:$started_at,finished_at:$finished_at,exit_code:1}' > ~/claude-loop-pr-codex/$org-$repository-$pr_number/status.json
```

### Step 6: 結果報告

レビュー結果の要約をユーザーに報告する。報告内容:

- 対象PR（リンク付き）
- レビュー結果のサマリ（Findings/Risks/Summary から要約）
- 結果ファイルのパス

- いつ使うか: Step 5 の status 更新後
- 次アクション: `review.md` を Read ツールで読み、以下の内容をユーザーにテキストで報告して終了する
  - 対象PR（`$pr_url` のリンク付き）
  - レビュー結果の要約（Findings から要約）
  - 結果ファイルのパス（`~/claude-loop-pr-codex/$org-$repository-$pr_number/review.md`）

## エラーハンドリング

- PRがclosed/merged → `skipped` としてログに記録し、次の候補へ進む
- Step 2b の `jq -ce` で `missing headRefOid / headRefName / baseRefName / files` が出た → `state=failed` で記録し、その回は終了（PR メタデータが必須フィールドを欠いているため信頼できるレビュー不可）
- Step 3 の `gh pr diff` が失敗または空出力（`pr.diff` 未生成） → `state=failed` で記録し、その回は終了（PR 差分スコープが確定できないため Step 4 に進まない）
- `claude -p` がタイムアウト（10分） → `state=failed` で記録
- `claude -p` が非ゼロ終了 → `state=failed` で記録
- `codex exec` がタイムアウト（10分） → `state=failed` で記録
- `codex exec` が非ゼロ終了 → `state=failed` で記録
- **`claude-review.md` / `codex-review.md` のいずれかが `PR_DIFF_UNAVAILABLE` のみ → `state=failed` で記録し、`review.md` は生成しない**
- 権限不足（404/403） → `state=failed` で記録し、その回は終了

## ファイル構成

```
~/claude-loop-pr-codex/
  └── $org-$repository-$pr_number/
        ├── status.json
        ├── metadata.json        ← org/repo/pr_number/pr_url/head_sha/branch/base_branch/title/files を含む
        ├── pr.diff              ← PR 差分 (unified diff)。Step 4a/4b のスコープ確定情報源
        ├── clone-claude/        ← Claude Code 用 shallow clone (depth 50, base fetch 済み)
        ├── clone-codex/         ← Codex CLI 用 shallow clone (depth 50, base fetch 済み)
        ├── claude-review.md     ← Claude Code の生レビュー
        ├── codex-review.md      ← Codex CLI の生レビュー
        ├── review.md            ← 統合レビュー（最終成果物）
        ├── claude.log
        └── codex.log
```

## 実装上の制約

本スキルは Claude Code を `--permission-mode auto` で起動することを前提とする（README の「使い方」参照）。auto mode でも、許可済みツールやコマンドの内容によっては分類器の判断で承認が必要になり得るため、本スキルではテンプレートに明示された操作だけを実行する。

ローカルの書き込みは作業ディレクトリ `~/claude-loop-pr-codex/` 配下に限り、`clone-claude/` / `clone-codex/` の作成と更新、`status.json` / `metadata.json` / `pr.diff` / `claude.log` / `codex.log` / `claude-review.md` / `codex-review.md` / `review.md` の成果物作成のみ許可する。

許可ルールは以下の allowlist に従う。

1. 最上位ルール: テンプレートに明示された構文のみ許可する。テンプレート外の構文追加は禁止
2. 各テンプレートは 1テンプレート = 1シェル実行単位として扱う
3. テンプレートの改変は変数置換のみ許可する。フラグ、引数順、引用符、リダイレクト、パイプ、演算子はテンプレート記載どおりに使う
4. シェル演算子はテンプレート中に明示された `|` `<` `>` `2>` `&&` のみ許可する
5. JSON 生成は `jq -n --arg` を使う。ヒアドキュメントで JSON を直接組み立てない
6. ファイル書き込みの使い分け:
   - 統合レビュー `review.md` は `Write` ツールで書き出す（`file_path` は `~` / `$...` を展開しないため、実値の絶対パスを渡す）
   - `status.json` / `metadata.json` は `jq -n --arg` / `--argjson` の出力を `Bash` の `>` で書く
   - `pr.diff` は Step 3 の `gh pr diff` の標準出力を `>` でリダイレクトして作成する
   - `claude-review.md` / `codex-review.md` / `claude.log` / `codex.log` は Step 4a / 4b の標準出力・標準エラーを `>` / `2>` でリダイレクトして作成する
7. Step 4a / 4b の timeout は必ず `600000` に固定する
8. テンプレートに明示された `git fetch` / `git checkout FETCH_HEAD` / 成果物ファイル作成以外の状態変更操作は実行しない。禁止例: `git push` / `git merge` / `git reset --*` / `git clean -fd[x]` / `git stash` / `git commit` / `git tag` / `git branch -D`、`rm -rf` 系、`gh pr` / `gh issue` の write 操作、および GitHub / Backlog / DocBase の write 系 MCP ツール
9. 1回の実行で選定・処理する PR は 1 件のみとする

補助注記（いずれもテンプレート一字一句原則の具体適用例）:

- `gh api` や `gh pr view` の `--jq` フラグは使わない。テンプレートはすべて `| jq` パイプ形式で統一しており、クォートを含むフラグ値は auto mode の分類器でも停止要因になり得る
- `$()` は使わない。コマンド置換はテンプレートに含まれず、auto mode でも承認プロンプトや停止要因になり得る（変数展開 `$org` 等はテンプレート内で使用する）
- `for` / `while` / `while read` / `xargs` などのループ・反復構文は使わない。テンプレート外であり、実行単位・ログの再現性を崩す
- Codex CLI のグローバルオプション（`--ask-for-approval` 等）は `exec` サブコマンドよりも前に置くこと。`exec` の後ろに付けると受け付けられず非対話実行が止まる
