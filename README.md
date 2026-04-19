# pr-codex

GitHub PRを **Claude Code** と **Codex CLI** の2者レビュー方式で自動レビューする Claude Code プラグイン。

## 特徴

- **2者レビュー**: Claude Code と Codex CLI が独立してPRをレビューし、結果を統合
- **自動巡回**: `/loop` と組み合わせて定期的にレビュー依頼PRを検出・処理
- **冪等実行**: status.json による状態管理で、完了済みPRのスキップや失敗時の再実行に対応
- **最小権限**: 各レビューツールは読み取り専用で動作し、PRへのコメント投稿は行わない

## 必要なもの

- Claude Code
- Codex CLI (`codex-cli 0.121.0` 前提)
- GitHub CLI (`gh`)

## セットアップ

### インストール

```
/plugin marketplace add yuki777/pr-codex
/plugin install pr-codex@yuki777/pr-codex
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

- `--permission-mode auto` — `/loop` による定期自動レビューを承認プロンプトなしで回すために必須。読み取り系は分類器が自動承認し、書き込みはスキル側の allowlist で制御する
- `--effort max` — レビュー時の推論深度を最大に引き上げる

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

## ファイル構成

```
~/claude-loop-pr-codex/
  └── $org-$repo-$pr/
        ├── status.json           # 実行状態（running / completed / failed）
        ├── metadata.json         # PR情報（org, repo, pr_number, head_sha 等）
        ├── clone-claude/         # Claude Code 用 shallow clone
        ├── clone-codex/          # Codex CLI 用 shallow clone
        ├── claude-review.md      # Claude Code の生レビュー
        ├── codex-review.md       # Codex CLI の生レビュー
        ├── review.md             # 統合レビュー（最終成果物）
        ├── claude.log
        └── codex.log
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
