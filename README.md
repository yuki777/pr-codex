# pr-codex

GitHub PRを **Claude Code** と **Codex CLI** の2者レビュー方式で自動レビューする Claude Code プラグイン。

## 特徴

- **2者レビュー**: Claude Code と Codex CLI が独立してPRをレビューし、結果を統合
- **自動巡回**: `/loop` と組み合わせて定期的にレビュー依頼PRを検出・処理
- **冪等実行**: status.json による状態管理で、完了済みPRのスキップや失敗時の再実行に対応
- **最小権限**: 各レビューツールは読み取り専用で動作し、PRへのコメント投稿時はユーザー承認を得てから行う
- **生成と投稿を分離**: レビュー生成 (`/pr-codex:review`) と投稿 (`/pr-codex:send`) を別スキルに分け、投稿前にユーザーが内容を承認する

## 必要なもの

- Claude Code
- Codex CLI (`codex-cli 0.128.0` 以上、`codex --ask-for-approval never -m gpt-5.5 ... exec` が使えること)
- GitHub CLI (`gh`)
- `jq`（SKILL.md 内の全テンプレートで利用する。macOS 標準では未インストール）
- `python3`（同梱 validator `tasks/validate_findings.py` で `findings.verified.json` を検証するため）

## セットアップ

### インストール

```
/plugin marketplace add yuki777/pr-codex
/plugin install pr-codex@pr-codex
```

### アップデート

```
/plugin update pr-codex
```

### アンインストール

```
/plugin uninstall pr-codex
```

## 使い方

ワーキングディレクトリを作成し、Claude Code を起動:

```bash
mkdir -p ~/claude-loop-pr-codex
cd ~/claude-loop-pr-codex && claude --permission-mode auto --effort max
```

- `--permission-mode auto` — `/loop` を非対話で回すために auto mode で起動する。auto mode は分類器による安全チェックでツール実行を自動承認またはブロックするため、すべての操作が無条件に通るわけではない。本スキルはテンプレートに明示した操作だけを実行し、ローカル書き込みは `~/claude-loop-pr-codex/` 配下の成果物作成に限定する
- `--effort max` — `low` / `medium` / `high` / `xhigh` / `max` のうち `max` を指定し、レビュー時の推論深度を最も深くする
- Codex CLI 側のレビューと投稿前検証は、スキル内で `-m gpt-5.5` を指定して実行する。レビュー実行では `model_reasoning_effort` をスキル側で上書きせず、ユーザー config の値を使う。投稿前検証は `--ignore-user-config` でユーザー config から切り離す
- Codex CLI は `codex-cli 0.128.0` 以降のみ対応する。旧バージョン向けテンプレートは打ち切り、`--sandbox read-only` / `--color never` / `--ephemeral` を並べる旧形式ではなく、`-c sandbox_mode=read-only` と preflight 限定の `--ignore-user-config` を使う



```
# 手動実行でレビューする
/pr-codex:review

# 10分間隔で自動レビューする
/loop 10m /pr-codex:review
```

## レビューフロー

1. **PR候補の取得** — GitHub Search API で `review-requested` のPRを一覧取得
2. **候補の選定** — 未レビュー・失敗・追加コミットありの最初の1件を選定
3. **作業ディレクトリの準備** — PRブランチを各ツール用に個別に shallow clone
4. **2者レビュー実行** — Claude Code と Codex CLI が並行してレビュー
5. **結果の統合** — 両者の指摘を比較・議論し、`findings.verified.json` と `review.md` を生成
6. **結果報告** — レビュー結果の要約をユーザーに報告

## レビューの投稿

`/pr-codex:review` は `review.md` をローカル生成するのみで、PRへの投稿は行わない。投稿は別スキル `/pr-codex:send` を手動で実行する。

```
/pr-codex:send
```

`/pr-codex:send` の挙動:

1. `~/claude-loop-pr-codex/` 配下から `status.json` が `state:completed` でかつ `review.md` が存在するディレクトリを1件選定する（名前昇順の先頭1件）
2. `findings.verified.json` を一次入力として `Must Fix` を抽出し、`review.md` から `## 総評` / `## 良い点` を body に使う（移行期間は Markdown parser を fallback として残すが、`findings.verified.json` が存在するのに Must Fix 件数が `review.md` と一致しない場合は中断する）
3. GitHub Reviews API への payload サマリをユーザーに提示し、明示的な承認を得る
4. 承認後、`gh api --method POST .../reviews` で投稿（`event` は Must Fix ありなら `REQUEST_CHANGES`、なければ `COMMENT`。`APPROVE` は自動では出さない）
5. 投稿成功後、対象ディレクトリを `~/claude-loop-pr-codex/sent/$org-$repo-$pr-$head_sha_short/` に移動する（同一 PR でも HEAD 更新後の再投稿履歴が衝突しないよう、`head_sha` の先頭 7 文字を suffix に付ける）

`/loop` には載せず、対話実行で使う。1回の実行で1件のみ処理する。

## Hermes Agent 自動化 (Phase 0)

Issue #28 の Phase 0 実装として、`hermes/` 配下に pr-codex 専用の Hermes Kanban + cron + 複数 profile 用テンプレートと監視スクリプトを追加しています。

- `hermes/profiles/` — `issue-triager` / `pr-reviewer` / `review-triager` / `developer` / `sheriff` の profile 方針
- `hermes/scripts/pr_codex_watch.py` — GitHub の Issue / PR / review 差分を polling し、冪等に Kanban task 化
- `hermes/scripts/pr_codex_kanban_health.py` — blocked / stale / retry / ready 放置 task の検出
- `hermes/scripts/pr_codex_daily_digest.py` — daily digest 生成
- `hermes/install_phase0.sh` — ローカルの `~/.hermes/` へ profile/script/board/state を導入する補助スクリプト
- `hermes/pr-codex.phase0.json` — board/profile/cron/safety の Phase 0 設定

Phase 0 は read-only observer です。GitHub への自動コメント、label/milestone/assignee 変更、push、approve、merge は行いません。詳細は [`hermes/README.md`](hermes/README.md) を参照してください。

## ファイル構成

```
~/claude-loop-pr-codex/
  ├── $org-$repo-$pr/             # 進行中 / 未投稿のレビュー
  │     ├── status.json           # 実行状態（running / completed / failed）
  │     ├── metadata.json         # PR情報（org, repo, pr_number, head_sha 等）
  │     ├── pr.diff               # PR 差分 (unified diff)
  │     ├── pr.diff.ranges.txt    # GitHub inline comment 可能範囲
  │     ├── clone-claude/         # Claude Code 用 shallow clone
  │     ├── clone-codex/          # Codex CLI 用 shallow clone
  │     ├── claude-review.md      # Claude Code の生レビュー
  │     ├── codex-review.md       # Codex CLI の生レビュー
  │     ├── findings.verified.json # canonical findings (`schemas/findings.v1.json`)
  │     ├── validation-report.json # validation の副成果物（canonical findings とは分離）
  │     ├── review.md             # 統合レビュー（最終成果物）
  │     ├── claude.log
  │     └── codex.log
  └── sent/                       # /pr-codex:send で投稿済み
        └── $org-$repo-$pr-$head_sha_short/ # 投稿後にここへ移動される
              ├── findings.verified.json
              ├── review.md
              ├── review-payload.json   # 投稿した GitHub Reviews API の payload
              ├── review-response.json  # gh api のレスポンス（.html_url 等）
              └── ... (他ファイルも一緒に保管される)
```

### 旧形式の `sent/$org-$repo-$pr/` が残っている場合

SHA suffix なしの旧形式ディレクトリがある場合は、`metadata.json` または `status.json` の `head_sha` を確認し、先頭 7 文字を付けて手動でリネームする。

```bash
# 旧形式: sent/yuki777-pr-codex-24/
# head_sha は status.json または metadata.json から確認
mv ~/claude-loop-pr-codex/sent/yuki777-pr-codex-24 \
   ~/claude-loop-pr-codex/sent/yuki777-pr-codex-24-d8e4ae5
```

## Schema

- canonical runtime artifact は `schemas/findings.v1.json` (JSON Schema Draft 2020-12) で定義する
- `findings.verified.json` は top-level `generated_at` を持ち、per-finding `created_at` は持たない
- `findings.verified.json.pr.repository` は **投稿先の base repo** (`owner/repo`) に固定する。fork PR でも head repo ではなく、`metadata.json.repository_full_name` および `/pr-codex:send` の投稿先 `$org/$repository` と一致させる
- M1 の `finding.id` は **`fingerprint` と同値**に固定する（retry / send の `source_finding_id` / eval harness 比較で決定論的に追跡するため）
- `category` は schema enum（`bug` / `security` / `performance` / `tests` / `design` / `code_quality` / `consistency` / `runtime_error`）に固定し、自由文字列の揺れを `fingerprint` に入れない
- `fingerprint` の入力は `path` / `category` / `normalized_title` / `primary_symbol` に固定し、`line` は含めない
- JSON Schema Draft 2020-12 単体では sibling equality (`id == fingerprint`) を標準機能だけで強制しにくいため、この等値は **review/send workflow の必須 runtime gate** として扱う
- review 側は `findings.verified.json` を completed 前に同梱 validator `tasks/validate_findings.py` で `schemas/findings.v1.json` へ検証し、send 側 primary path も同じ validator に失敗したら fallback せず中断する
- schema 自体は `location.side` に `LEFT` も残すが、M1 の send workflow は `RIGHT` のみ受け付ける
- `tasks/validate_findings.py` は JSON shape / enum / conditional rule / RFC3339 date-time / URI / `end_line >= start_line` / `id == fingerprint` / fingerprint 再計算 / `metadata.json` との PR context 一致を stdlib-only で検証する

### fingerprint 正準アルゴリズム

1. `path`: `location.path` のリポジトリ相対 POSIX path をそのまま使う
2. `category`: schema enum の値をそのまま使う
3. `normalized_title`: `title` に Unicode NFKC 正規化 → Unicode lowercase → 連続空白を ASCII space 1 個へ畳み込み → 前後 trim → 末尾の Unicode punctuation（General Category が P で始まる文字）をなくなるまで除去 → 最後に右 trim、の順で処理する
4. `primary_symbol`: `title` 内で最初に backtick で囲まれた symbol を前後 trim して使う。存在しない場合は空文字列にする
5. `id = fingerprint = lowercase_hex(sha256(path + "\x1f" + category + "\x1f" + normalized_title + "\x1f" + (primary_symbol || "")))`

## バージョンアップ（作者向け）

利用者が `/plugin update pr-codex` で最新化できるようにするには、以下の手順でリリースする。

1. `.claude-plugin/plugin.json` と `.claude-plugin/marketplace.json` の `version` を同じ値に bump する（semver: パッチ `1.0.0` → `1.0.1`、マイナー `1.0.0` → `1.1.0`、メジャー `1.0.0` → `2.0.0`）
2. 変更を commit する
   ```bash
   git commit -am "Bump version to 1.0.1"
   ```
3. リモートへ push する
   ```bash
   git push
   ```

利用者側は `/plugin update pr-codex` で最新版に更新できる。`version` が上がっていないとキャッシュで古い内容が使われる場合があるため、コード変更と同じコミットで必ず `version` を bump すること。

## ライセンス

MIT
