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
- Codex CLI (`codex-cli 0.121.0` 前提)
- GitHub CLI (`gh`)
- `jq`（SKILL.md 内の全テンプレートで利用する。macOS 標準では未インストール）

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

10分間隔で自動レビューを開始:

```
/loop 10m /pr-codex:review
```

## レビューフロー

1. **PR候補の取得** — GitHub Search API で `review-requested` のPRを一覧取得
2. **候補の選定** — 未レビュー・失敗・追加コミットありの最初の1件を選定
3. **作業ディレクトリの準備** — PRブランチを各ツール用に個別に shallow clone
4. **2者レビュー実行** — Claude Code と Codex CLI が並行してレビュー
5. **結果の統合** — 両者の指摘を比較・議論し、統合レビューを作成
6. **結果報告** — レビュー結果の要約をユーザーに報告

## レビューの投稿

`/pr-codex:review` は `review.md` をローカル生成するのみで、PRへの投稿は行わない。投稿は別スキル `/pr-codex:send` を手動で実行する。

```
/pr-codex:send
```

`/pr-codex:send` の挙動:

1. `~/claude-loop-pr-codex/` 配下から `status.json` が `state:completed` でかつ `review.md` が存在するディレクトリを1件選定する（名前昇順の先頭1件）
2. `review.md` をパースし、`## 総評` / `## 良い点` を body に、`## 重大な問題 (Must Fix)` / `## 改善提案 (Should Fix)` をインラインコメントに分解（`## 軽微な指摘` と `## 議論・判断` は投稿しない）
3. GitHub Reviews API への payload サマリをユーザーに提示し、明示的な承認を得る
4. 承認後、`gh api --method POST .../reviews` で投稿（`event` は Must Fix ありなら `REQUEST_CHANGES`、なければ `COMMENT`。`APPROVE` は自動では出さない）
5. 投稿成功後、対象ディレクトリを `~/claude-loop-pr-codex/sent/` に移動する

`/loop` には載せず、対話実行で使う。1回の実行で1件のみ処理する。

## ファイル構成

```
~/claude-loop-pr-codex/
  ├── $org-$repo-$pr/             # 進行中 / 未投稿のレビュー
  │     ├── status.json           # 実行状態（running / completed / failed）
  │     ├── metadata.json         # PR情報（org, repo, pr_number, head_sha 等）
  │     ├── clone-claude/         # Claude Code 用 shallow clone
  │     ├── clone-codex/          # Codex CLI 用 shallow clone
  │     ├── claude-review.md      # Claude Code の生レビュー
  │     ├── codex-review.md       # Codex CLI の生レビュー
  │     ├── review.md             # 統合レビュー（最終成果物）
  │     ├── claude.log
  │     └── codex.log
  └── sent/                       # /pr-codex:send で投稿済み
        └── $org-$repo-$pr/       # 投稿後にここへ移動される
              ├── review.md
              ├── review-payload.json   # 投稿した GitHub Reviews API の payload
              ├── review-response.json  # gh api のレスポンス（.html_url 等）
              └── ... (他ファイルも一緒に保管される)
```

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
