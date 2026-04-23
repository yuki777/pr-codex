---
user-invocable: true
name: pr-codex-send
description: "/pr-codex:review で生成された統合レビュー(review.md)を GitHub PR にレビューコメントとして投稿し、処理済みディレクトリを sent/ に移動する"
argument-hint: ""
allowed-tools: ["Bash", "Read", "Write", "Glob", "Grep"]
---

# pr-codex-send

`/pr-codex:review` が生成した統合レビュー (`review.md`) を該当 GitHub PR にレビューコメントとして投稿し、処理済みディレクトリを `~/claude-loop-pr-codex/sent/` に移動する。

## 前提

- `/pr-codex:review` が先に実行されており、`~/claude-loop-pr-codex/<org>-<repository>-<pr_number>/` 配下に `status.json` (`state:completed`) / `metadata.json` / `review.md` が揃っている
- GitHub CLI (`gh`) がログイン済みで、対象 PR にレビュー投稿権限がある (`gh auth status` で確認可能)
- `jq` が利用可能

## 使い方

```
/pr-codex:send
```

対話実行を前提とする。Step 5 で投稿 payload のサマリを提示し、ユーザーの明示的な承認を得てから Step 6 で投稿する。`/loop` には載せない。

1 回の実行で対象は 1 件のみ処理する。未投稿の completed レビューが複数ある場合は、`ls` の出力順（名前昇順）で最初の 1 件のみを処理し、残りは次回以降の `/pr-codex:send` 実行に委ねる。

## フロー

各テンプレートはコードブロックの内容をそのまま 1 回のシェル実行単位として使う。変数（`$candidate`, `$dir_name`, `$org`, `$repository`, `$pr_number`, `$pr_url`, `$head_sha`, `$title`, `$review_url` など）の置換以外の改変は不可。

### Step 1: 対象ディレクトリの選定

- いつ使うか: Skill 起動直後に必ず実行する
- 判定条件: 標準出力に `<org>-<repository>-<pr_number>` 形式のディレクトリ名が名前昇順で列挙される（`sent` は除外される）
- 次アクション: 出力を上から順に走査し、各行を `$candidate` として後続の判定テンプレートへ渡す

```bash
ls -1 ~/claude-loop-pr-codex | grep -v '^sent$' | grep -v 'clear.sh'
```

- いつ使うか: 各 `$candidate` に対して実行する
- 判定条件: `status.json` が存在する
- 次アクション: 存在すれば次の `state` 判定へ。存在しなければこの候補はスキップし次の候補へ

```bash
test -f ~/claude-loop-pr-codex/$candidate/status.json
```

- いつ使うか: `status.json` が存在する `$candidate` に対して実行する
- 判定条件: 出力が `completed`
- 次アクション: `completed` なら `review.md` 存在確認へ。それ以外 (`running` / `failed`) はスキップし次の候補へ

```bash
jq -r '.state' ~/claude-loop-pr-codex/$candidate/status.json
```

- いつ使うか: `state == "completed"` の `$candidate` に対して実行する
- 判定条件: `review.md` が存在する
- 次アクション: 存在すればこの候補を確定し、`$candidate` の値を `$dir_name` として保持して Step 2 へ進む。存在しなければスキップし次の候補へ

```bash
test -f ~/claude-loop-pr-codex/$candidate/review.md
```

全候補がスキップなら「投稿対象の completed レビューなし」とユーザーに報告して正常終了する。`sent/` への移動も payload 生成も行わない。

### Step 2: メタデータとレビューの読み込み

- いつ使うか: `$dir_name` が確定した直後に実行する
- 判定条件: 標準出力に `org=` / `repository=` / `pr_number=` / `pr_url=` / `head_sha=` / `title=` の 6 行が返る
- 次アクション: 各値をそれぞれ `$org`, `$repository`, `$pr_number`, `$pr_url`, `$head_sha`, `$title` として保持し、`review.md` の Read へ進む

```bash
jq -r '"org=\(.org)\nrepository=\(.repository)\npr_number=\(.pr_number)\npr_url=\(.pr_url)\nhead_sha=\(.head_sha)\ntitle=\(.title)"' ~/claude-loop-pr-codex/$dir_name/metadata.json
```

続いて `review.md` を Read ツールで取得する。`file_path` は `~` を `$HOME` の実値に展開した絶対パスで渡す（例: `/Users/adachi/claude-loop-pr-codex/$dir_name/review.md` の `$dir_name` と `/Users/adachi` をいずれも実値に置換してから呼び出す）。

### Step 3: `review.md` の解析

Claude 側で本文をメモリ上で以下のセクションに分解する。シェルでのパースは行わない。

- `## 総評` 直下の本文 → `$summary`（後続セクション見出しの直前まで。前後の空行はトリム）
- `## 良い点` 直下の本文 → `$good_points`（同様にトリム）
- `## 重大な問題 (Must Fix)` 配下の各 `### \`path:L行番号\`` ブロック → `$must_fix` 配列
- `## 改善提案 (Should Fix)` 配下の各 `### \`path:L行番号\`` ブロック → `$should_fix` 配列
- `## 軽微な指摘 (Nit)` / `## 議論・判断` は**使わない** (投稿しない)

#### 指摘ブロックの構造

各指摘ブロックは以下のフォーマット:

```markdown
### `path/to/file.ext:L<行番号>` (もしくは `path/to/file.ext:L<開始>-L<終了>`)

- 問題: <問題文>
- 理由: <理由文>
- 提案: <提案文>
```

ここから抽出するフィールド:

| 出力キー        | 値                                                                 |
| --------------- | ------------------------------------------------------------------ |
| `path`          | 見出し内のバッククォート直後からコロン `:L` 直前までの文字列       |
| `line`          | 単一行指定なら `L<行番号>` の数値、範囲指定なら `L<終了>` の数値    |
| `start_line`    | 範囲指定時のみ。`L<開始>` の数値                                   |
| `side`          | 常に `"RIGHT"` を付与                                              |
| `start_side`    | 範囲指定時のみ `"RIGHT"`                                           |
| `body`          | 下のフォーマットで組み立てた文字列                                 |

#### `body` のフォーマット

Must Fix:

```
🚨 **Must Fix**

- 問題: <問題文>
- 理由: <理由文>
- 提案: <提案文>
```

Should Fix:

```
⚠️ **Should Fix**

- 問題: <問題文>
- 理由: <理由文>
- 提案: <提案文>
```

#### 行番号ヘッダが壊れているブロックの扱い

見出しに `:L<番号>` が欠落している、もしくは空のコードブロック (`` ### `` 以降が空) のブロックは**除外**する。GitHub API はこれらを 422 で拒否するため、payload に含めない。

#### 空セクションの扱い

- `$must_fix` / `$should_fix` のどちらかが空配列になっても構わない
- `$good_points` が空文字列なら body から `## 良い点` セクションを省略する
- `$summary` が空になることは想定しない（`/pr-codex:review` のテンプレートで必ず出力されるため）。万一空ならユーザーに通知して処理を中断する

### Step 4: payload の構築

以下のルールで GitHub Reviews API の payload JSON を組み立てる（`POST /repos/{owner}/{repo}/pulls/{pull_number}/reviews` の request body 仕様に従う）:

- `commit_id`: `$head_sha`（レビュー時点の head に明示的に紐付ける）
- `event`:
  - `$must_fix` が 1 件以上あれば `"REQUEST_CHANGES"`
  - 0 件なら `"COMMENT"`
  - `"APPROVE"` は自動では発行しない
- `body`:
  - `$good_points` が非空の場合:
    ```
    <$summary>

    ## 良い点

    <$good_points>
    ```
  - `$good_points` が空の場合:
    ```
    <$summary>
    ```
- `comments`: `$must_fix` 配列 + `$should_fix` 配列を結合した配列（元の登場順を保つ）。各要素は以下のキーを含む:
  - `path` (必須)
  - `line` (必須)
  - `side` (`"RIGHT"`)
  - `body` (Step 3 の body フォーマット)
  - `start_line` / `start_side` は範囲指定の場合のみ含める

`$must_fix` と `$should_fix` がいずれも空だった場合でも、`event: "COMMENT"` + body (総評 + 良い点) のみで投稿する。

payload は Write ツールで `~/claude-loop-pr-codex/$dir_name/review-payload.json` に書き出す。`file_path` には `~` を実値に展開した絶対パスを渡し、`$dir_name` も実値に置換する。整形された JSON（インデント 2）で書き出して後から人間が読めるようにする。

### Step 5: 承認プロンプト

投稿前にユーザーに以下のサマリをテキストで提示し、明示的な承認を求める:

```
対象 PR: <$pr_url> (<$title>)
event: <REQUEST_CHANGES | COMMENT>
review file: ~/claude-loop-pr-codex/<$dir_name>/review.md
body プレビュー:
  <$summary の先頭 200 文字。長ければ "..." で省略>
インラインコメント: Must Fix N 件 / Should Fix M 件
payload: ~/claude-loop-pr-codex/<$dir_name>/review-payload.json

この内容で投稿してよろしいですか？ (yes/no)
```

ユーザーの応答が `yes` / `y` / `はい` 等の明示的な承認である場合のみ Step 6 に進む。それ以外（`no` / `n` / `いいえ` / 曖昧・無回答）の場合は処理を中断し、以下を報告して終了する:

- 投稿はスキップした旨
- payload ファイルは保持されている旨 (`~/claude-loop-pr-codex/$dir_name/review-payload.json`)
- 再実行したい場合は再度 `/pr-codex:send` を叩くか、payload を手動編集してから `gh api --method POST ... --input <payload>` で直接投稿できる旨

承認拒否時は `sent/` への移動は行わない。

### Step 6: `gh api` で投稿

- いつ使うか: Step 5 でユーザーから明示的な承認を得た直後に実行する
- 判定条件: 終了コードが 0、かつ出力された `review-response.json` に `.html_url` が含まれる
- 次アクション: 成功なら Step 7 へ進む。非ゼロ終了なら Step 8 の失敗報告へ遷移し、`sent/` への移動は行わない

```bash
gh api --method POST "/repos/$org/$repository/pulls/$pr_number/reviews" --input ~/claude-loop-pr-codex/$dir_name/review-payload.json > ~/claude-loop-pr-codex/$dir_name/review-response.json
```

- いつ使うか: 上の投稿が成功した直後に実行する
- 判定条件: 標準出力が `https://github.com/...` 形式の URL
- 次アクション: 出力を `$review_url` として保持し Step 7 へ進む

```bash
jq -r '.html_url' ~/claude-loop-pr-codex/$dir_name/review-response.json
```

### Step 7: `sent/` への移動

- いつ使うか: Step 6 で投稿に成功した直後に実行する
- 判定条件: `sent/` ディレクトリが存在する
- 次アクション: `mv` テンプレートへ進む

```bash
install -d ~/claude-loop-pr-codex/sent
```

- いつ使うか: `sent/` を作成した直後に実行する
- 判定条件: `mv` が成功し、元ディレクトリが消え `sent/$dir_name` に移動している
- 次アクション: Step 8 の結果報告へ進む

```bash
mv ~/claude-loop-pr-codex/$dir_name ~/claude-loop-pr-codex/sent/$dir_name
```

移動後、`review-payload.json` / `review-response.json` も一緒に保管され、投稿履歴として残る。

### Step 8: 結果報告

ユーザーに以下をテキストで報告して終了する:

成功時:

- 対象 PR: `$pr_url` (`$title`)
- 投稿した review の URL: `$review_url`
- 選択した `event`
- インラインコメント件数 (Must Fix / Should Fix)
- 移動先: `~/claude-loop-pr-codex/sent/$dir_name`

失敗時（Step 6 が非ゼロ終了した場合）:

- エラー内容 (`gh api` の stderr)
- 推定原因:
  - 422 → インラインコメントの行番号が PR の diff 範囲外。`$must_fix` / `$should_fix` 配列のどのエントリが範囲外か `pr.diff` と照合するようユーザーに案内
  - 403 → 権限不足。`gh auth status` の確認と、PR リポジトリへのコメント権限を案内
  - 404 → PR が見つからない。`$org` / `$repository` / `$pr_number` の値確認を案内
- `~/claude-loop-pr-codex/$dir_name/` は**移動しない**。payload と response を残した状態で終了するので、ユーザーは payload 修正後に再度 `/pr-codex:send` を叩くか、手動で `gh api` を実行できる

## エラーハンドリング

- 対象ディレクトリなし → 「投稿対象の completed レビューなし」と報告して正常終了（非エラー）
- `review.md` に Must Fix / Should Fix が一件も無い → それでも `event: COMMENT` + body (総評 + 良い点) のみで投稿する（コメントが無ければインラインコメント配列は空）
- `review.md` の `## 総評` セクションが空 or 見つからない → ユーザーに通知して処理中断。`sent/` 移動は行わない
- `gh api` 422/403/404 → Step 8 の失敗報告で分岐し、`sent/` 移動は行わない
- ユーザーが Step 5 で承認を拒否 → 何もせず終了。payload ファイルは残す

## 実装上の制約

本スキルは対話実行を前提とし、通常の permission mode で使うことを想定する（`/loop` には載せない）。ただし既存 `/pr-codex:review` と統一感を持たせるため、以下の原則を踏襲する:

1. 各テンプレートは 1 テンプレート = 1 シェル実行単位として扱う
2. テンプレートの改変は変数置換のみ許可する。フラグ、引数順、引用符、リダイレクトはテンプレート記載どおりに使う
3. シェル演算子はテンプレート中に明示された `|` `>` のみ許可する
4. payload JSON の生成は Write ツールで行う（`jq -n` によるインラインでの複雑な配列組み立ては使わない）
5. `$()` / `for` / `while` / `xargs` / ヒアドキュメントは使わない
6. `mv` は `sent/` への移動以外では使わない
7. `gh` の write 系操作は `gh api --method POST .../reviews` のみとし、`gh pr review` / `gh pr comment` / `gh pr merge` などは使わない
8. 1 回の実行で処理する対象ディレクトリは 1 件のみとする
9. 投稿前の Step 5 承認プロンプトは必須。自動投稿はしない

## ファイル構成

スキル本体:

```
$CLAUDE_PLUGIN_ROOT/skills/send/
  └── SKILL.md                ← 本ファイル
```

実行時の作業ディレクトリ (投稿前):

```
~/claude-loop-pr-codex/
  └── $org-$repository-$pr_number/
        ├── status.json            ← state:completed
        ├── metadata.json
        ├── review.md              ← 投稿元
        ├── pr.diff
        ├── claude-review.md
        ├── codex-review.md
        ├── claude.log
        └── codex.log
```

投稿後:

```
~/claude-loop-pr-codex/
  └── sent/
        └── $org-$repository-$pr_number/
              ├── status.json
              ├── metadata.json
              ├── review.md
              ├── review-payload.json    ← 追加: 投稿した payload
              ├── review-response.json   ← 追加: gh api のレスポンス (.html_url 等を含む)
              ├── pr.diff
              ├── claude-review.md
              ├── codex-review.md
              ├── claude.log
              └── codex.log
```
