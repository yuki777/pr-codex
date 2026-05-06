---
user-invocable: true
name: pr-codex-send
description: "/pr-codex:review で生成された統合レビュー(review.md)を GitHub PR にレビューコメントとして投稿し、処理済みディレクトリを sent/ に移動する"
argument-hint: ""
allowed-tools: ["Bash", "Read", "Write", "Glob", "Grep"]
---

# pr-codex-send

`/pr-codex:review` が生成した canonical findings (`findings.verified.json`) と統合レビュー (`review.md`) を使って GitHub PR にレビューコメントを投稿し、処理済みディレクトリを `~/claude-loop-pr-codex/sent/` に移動する。

## 前提

- `/pr-codex:review` が先に実行されており、`~/claude-loop-pr-codex/<org>-<repository>-<pr_number>/` 配下に `status.json` (`state:completed`) / `metadata.json` / `review.md` が揃っている
- `findings.verified.json` があればそれを **一次入力** とし、`review.md` parser は fallback としてのみ使う
- GitHub CLI (`gh`) がログイン済みで、対象 PR にレビュー投稿権限がある (`gh auth status` で確認可能)
- `jq` が利用可能

## 使い方

```
/pr-codex:send
```

対話実行を前提とする。Step 5 で投稿 payload のサマリを提示し、ユーザーの明示的な承認を得てから Step 6 で投稿する。`/loop` には載せない。

1 回の実行で対象は 1 件のみ処理する。未投稿の completed レビューが複数ある場合は、`ls` の出力順（名前昇順）で最初の 1 件のみを処理し、残りは次回以降の `/pr-codex:send` 実行に委ねる。

## フロー

各テンプレートはコードブロックの内容をそのまま 1 回のシェル実行単位として使う。変数（`$candidate`, `$dir_name`, `$org`, `$repository`, `$pr_number`, `$pr_url`, `$head_sha`, `$head_sha_short`, `$title`, `$review_url` など）の置換以外の改変は不可。

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
- 判定条件: 標準出力に `org=` / `repository=` / `repository_full_name=` / `pr_number=` / `pr_url=` / `head_sha=` / `head_sha_short=` / `base_sha=` / `title=` の 9 行が返る
- 次アクション: 各値をそれぞれ `$org`, `$repository`, `$repository_full_name`, `$pr_number`, `$pr_url`, `$head_sha`, `$head_sha_short`, `$base_sha`, `$title` として保持し、`review.md` の Read へ進む

```bash
jq -r '"org=\(.org)\nrepository=\(.repository)\nrepository_full_name=\(.repository_full_name)\npr_number=\(.pr_number)\npr_url=\(.pr_url)\nhead_sha=\(.head_sha)\nhead_sha_short=\(.head_sha[0:7])\nbase_sha=\(.base_sha)\ntitle=\(.title)"' ~/claude-loop-pr-codex/$dir_name/metadata.json
```

続いて `review.md` を Read ツールで取得する。`file_path` は `~` を `$HOME` の実値に展開した絶対パスで渡す（例: `/Users/adachi/claude-loop-pr-codex/$dir_name/review.md` の `$dir_name` と `/Users/adachi` をいずれも実値に置換してから呼び出す）。

- いつ使うか: `review.md` を読み込んだ直後に実行する
- 判定条件: `findings.verified.json` が存在するなら終了コード 0
- 次アクション: 存在するなら `findings.verified.json` を Read ツールで取得して Step 3 の primary path へ。存在しなければ Step 3b の Markdown fallback へ

```bash
test -f ~/claude-loop-pr-codex/$dir_name/findings.verified.json
```

### Step 2.5: plugin root / schema / validator path の解決

primary / fallback のどちらへ進む場合も、Step 4.5 の Codex セルフレビューで `{SCHEMA_PATH}` / `{VALIDATOR_PATH}` を絶対パスへ置換できるよう、ここで `schema_path` と `validator_path` を保持する。`findings.verified.json` が存在する primary path では同じ値を Step 3 の同梱 validator 実行にも使う。`$CLAUDE_PLUGIN_ROOT` が未設定・不明な場合は review skill と同じ手順（`echo "$CLAUDE_PLUGIN_ROOT"`、空なら `**/pr-codex/skills/review/REVIEW_CRITERIA.md` の探索結果から plugin root を逆算）で絶対パスを確定する。

保持する値:

- `schema_path = <plugin-root>/schemas/findings.v1.json`
- `validator_path = <plugin-root>/tasks/validate_findings.py`

### Step 3: `findings.verified.json` の解析 (primary)

`findings.verified.json` が存在する場合、**これを一次情報源**として payload を組み立てる。`review.md` は `## 総評` / `## 良い点` の本文取得と、Must Fix 件数 gate の確認にだけ使う。まず Step 2.5 で保持した `validator_path` / `schema_path` を使い、`findings.verified.json` がその schema に適合するかを review 側と同じ同梱 validator で外部検証してから抽出へ進む。

#### 同梱 validator コマンド

- いつ使うか: `findings.verified.json` が存在する primary path の開始直後、JSON 抽出や payload 生成の前に必ず実行する
- 判定条件: 終了コード 0
- 次アクション: 成功なら Read ツールで `findings.verified.json` を読み Step 3 の抽出へ進む。失敗ならユーザーに通知して中断し、Markdown fallback へは切り替えない
- `$CLAUDE_PLUGIN_ROOT` が shell 環境で未設定の場合は、Step 2.5 で保持した `validator_path` / `schema_path` の実値へ置換してから Bash ツールへ渡す。Step 4.5 のプロンプトにも同じ絶対パスを埋め込む

```bash
python3 $validator_path --schema $schema_path --data ~/claude-loop-pr-codex/$dir_name/findings.verified.json --metadata ~/claude-loop-pr-codex/$dir_name/metadata.json
```

Claude 側でメモリ上に以下を抽出する:

- `review.md` から:
  - `## 総評` 直下の本文 → `$summary`（後続セクション見出しの直前まで。前後の空行はトリム）
  - `## 良い点` 直下の本文 → `$good_points`（同様にトリム）
  - `## 重大な問題 (Must Fix)` 配下の `### ...` 見出し数 → `$must_fix_markdown_count`
- `findings.verified.json` から:
  - ファイルが空でないこと、JSON parse に成功すること、top-level が object であること
  - top-level `schema_version` が **`findings.v1`** であること
  - top-level `findings` フィールドが存在し、array であること
  - 上記同梱 validator による `schemas/findings.v1.json` validation を通ること
  - top-level `pr.repository` / `pr.number` / `pr.head_sha` / `pr.base_sha` が `metadata.json.repository_full_name` / `metadata.json.pr_number` / `metadata.json.head_sha` / `metadata.json.base_sha` と一致し、`metadata.json.repository_full_name == "$org/$repository"` で投稿先 repo と一致すること
  - すべての finding で `id == fingerprint` が成り立ち、同梱 validator が正準アルゴリズムで再計算した fingerprint と一致すること
  - `findings[]` のうち `severity == "must_fix"` の要素を `$must_fix` 配列として抽出する
  - `findings[]` のうち `severity == "should_fix" && posting.post_policy == "body_summary"` の要素を `$should_fix_body_summary_candidates` 配列として抽出する。順序は `findings[]` の登場順を保ち、Step 5 の opt-in がない限り body には含めない
  - `findings[]` のうち `severity == "nit"` の要素を `$nit_findings` 配列として抽出する。`posting.post_policy` の値に関わらず GitHub payload には含めず、primary path でのみ `nits.md` に書き出す
  - M1 の投稿 contract として、`severity != "must_fix"` の finding に `posting.post_policy == "inline"` が含まれないことを確認する

#### `findings.verified.json` から抽出するフィールド

各 Must Fix finding から以下を payload 用に組み立てる:

| 出力キー        | 値 |
| --------------- | --- |
| `path`          | `location.path` |
| `line`          | `location.end_line` があればその値、なければ `location.start_line` |
| `start_line`    | `location.end_line` がある場合のみ `location.start_line` |
| `side`          | `location.side` が `"RIGHT"` であることを確認したうえで `"RIGHT"` |
| `start_side`    | `location.end_line` がある場合のみ `"RIGHT"` |
| `body`          | 下の Must Fix body フォーマット |
| `heading_markdown` | ``### `path:L<行番号>` `` または ``### `path:L<開始>-L<終了>` `` |
| `source_finding_id` | finding の `id` |

各 Should Fix body summary 候補から以下をメモリ上に保持する:

| 出力キー        | 値 |
| --------------- | --- |
| `path`          | `location.path` |
| `line`          | `location.end_line` があればその値、なければ `location.start_line` |
| `heading_markdown` | ``### `path:L<行番号>` `` または ``### `path:L<開始>-L<終了>` `` |
| `summary_line`  | `problem` を 1 行に畳み込んだ改善内容 |
| `suggestion_line` | `suggestion` を 1 行に畳み込んだ提案 |
| `source_finding_id` | finding の `id` |

`$should_fix_body_summary_candidates` の上位判定は `findings[]` の配列順に固定し、send 側で severity / category / path などによる再ソートは行わない。Step 5 で opt-in された場合だけ、先頭から最大 3 件を `$included_should_fix_body_summary` として Step 4 の body に使う。

各 Nit finding から以下を `nits.md` 用に保持する:

| 出力キー        | 値 |
| --------------- | --- |
| `path`          | `location.path` |
| `line`          | `location.end_line` があればその値、なければ `location.start_line` |
| `heading_markdown` | ``### `path:L<行番号>` `` または ``### `path:L<開始>-L<終了>` `` |
| `problem`       | finding の `problem` |
| `suggestion`    | finding の `suggestion` |
| `source_finding_id` | finding の `id` |

#### primary path の必須ガード

- `findings.verified.json` が空 / JSON parse 失敗 / top-level object でない / `findings[]` 不在または非配列 / 同梱 validator による `schemas/findings.v1.json` validation / fingerprint 再計算 / format / range validation 失敗 / `id != fingerprint` のいずれかなら、ユーザーに通知して **中断** する（fallback へは切り替えない）
- `findings.verified.json.pr.repository != metadata.json.repository_full_name`、`findings.verified.json.pr.number != metadata.json.pr_number`、`findings.verified.json.pr.head_sha != metadata.json.head_sha`、`findings.verified.json.pr.base_sha != metadata.json.base_sha`、または `metadata.json.repository_full_name != "$org/$repository"` のいずれかなら、canonical artifact が投稿先 PR と一致しないため **中断** する（fallback へは切り替えない）
- `severity == "must_fix"` の finding は、M1 では **`posting.post_policy == "inline"` かつ `posting.explanation_postable == true`** のものだけを自動投稿対象として扱う
- `severity != "must_fix"` の finding に `posting.post_policy == "inline"` が 1 件でもあれば、review 側の M1 posting contract 違反として **中断** する（fallback へは切り替えない）。M1 では `should_fix` / `nit` / `note` は inline 自動投稿対象外であり、`body_summary` / `local_only` / `suppress` のいずれかで表現する
- `severity == "must_fix"` の finding で `location.side != "RIGHT"` が 1 件でもあれば、現 workflow の `pr.diff.ranges.txt` が head/new 側前提のため **中断** する（fallback へは切り替えない）
- `must_fix` なのに `posting.post_policy` が `body_summary` / `local_only` / `suppress` のもの、または `posting.explanation_postable == false` のものが 1 件でもあれば、GitHub payload へ安全に変換できないため **中断** する（fallback へは切り替えない）
- `findings.verified.json` が存在する場合、`$must_fix` の件数と `$must_fix_markdown_count` が **完全一致** しなければ中断する。人手で `review.md` が編集された、または review 側の派生生成が壊れている可能性があるため、fallback へは切り替えない

#### `body` のフォーマット

Must Fix:

```
🚨 **Must Fix**

- 問題: <problem>
- 理由: <reason>
- 提案: <suggestion>
```

#### 空セクションの扱い

- `$must_fix` が空配列になっても構わない
- `$should_fix_body_summary_candidates` が空配列なら Should Fix body inclusion の opt-in prompt は表示しない
- `$nit_findings` が空配列なら `nits.md` は作成しない
- `$good_points` が空文字列なら body から `## 良い点` セクションを省略する
- `$summary` が空になることは想定しない（`/pr-codex:review` のテンプレートで必ず出力されるため）。万一空ならユーザーに通知して処理を中断する

Step 3.5 で範囲外コメントをレビュー body 末尾へ退避するため、各 finding について `heading_markdown` と `body`（GitHub API 用に整形する前の元情報）も Claude 側のメモリ上に保持する。

#### `nits.md` の書き出し (primary path のみ)

`$nit_findings` が 1 件以上ある場合、Step 4 の payload 構築前に Write ツールで `~/claude-loop-pr-codex/$dir_name/nits.md` へ Markdown を書き出す。`file_path` には `~` を実値に展開した絶対パスを渡し、`$dir_name` も実値に置換する。0 件の場合は `nits.md` を作成しない。

形式:

```markdown
PR には投稿しない軽微な指摘の控えです。

### `path/to/file.ext:L<行番号>`

- 内容: <problem>
- 提案: <suggestion>
```

複数件ある場合は finding ごとに同じ `###` ブロックを繰り返す。`nits.md` は投稿 payload には含めず、Step 7 の `mv` で他 artifact と一緒に `sent/` 配下へ移動される。

### Step 3b: `review.md` の解析 (fallback)

`findings.verified.json` が存在しない場合のみ、移行期間の fallback として従来どおり `review.md` をパースする。シェルでのパースは行わない。

- `## 総評` 直下の本文 → `$summary`
- `## 良い点` 直下の本文 → `$good_points`
- `## 重大な問題 (Must Fix)` 配下の各 `### \`path:L行番号\`` ブロック → `$must_fix` 配列
- それ以外のセクション（`## 改善提案 (Should Fix)` / `## 軽微な指摘 (Nit)` / `## 補足`）はすべて**投稿対象外**
- fallback path では Should Fix body summary の opt-in prompt と `nits.md` 書き出しは行わない

各指摘ブロックの構造と抽出ルールは従来どおり以下とする:

```markdown
### `path/to/file.ext:L<行番号>` (もしくは `path/to/file.ext:L<開始>-L<終了>`)

- 問題: <問題文>
- 理由: <理由文>
- 提案: <提案文>
```

| 出力キー        | 値                                                                 |
| --------------- | ------------------------------------------------------------------ |
| `path`          | 見出し内のバッククォート直後からコロン `:L` 直前までの文字列       |
| `line`          | 単一行指定なら `L<行番号>` の数値、範囲指定なら `L<終了>` の数値    |
| `start_line`    | 範囲指定時のみ。`L<開始>` の数値                                   |
| `side`          | 常に `"RIGHT"` を付与 (review.md の行番号は head 基準であるため)   |
| `start_side`    | 範囲指定時のみ `"RIGHT"`                                           |
| `body`          | 上の Must Fix body フォーマット                                    |

見出しに `:L<番号>` が欠落している、もしくは空のコードブロック (`` ### `` 以降が空) のブロックは**除外**する。GitHub API はこれらを 422 で拒否するため、payload に含めない。

fallback path でも Step 3.5 用に、各指摘ブロックの元の見出し行と本文（GitHub API 用に整形する前の Markdown）を Claude 側のメモリ上に保持する。

### Step 3.5: 行範囲検証

GitHub Reviews API は PR diff の新ファイル側 hunk 範囲外の `line` を 422 `Line could not be resolved` で拒否するため、payload 構築前に `pr.diff` からコメント可能行範囲を抽出し、Step 3/3b で得たインラインコメント候補を検証する。

- いつ使うか: Step 3 で `$must_fix` 配列を作成した直後、Step 4 の payload 構築前に必ず実行する
- 判定条件: `pr.diff.ranges.txt` が作成される
- 次アクション: 作成後、`pr.diff.ranges.txt` を Read ツールで取得し、Claude 側で `$must_fix` の各エントリを検証する

```bash
awk '
  /^diff --git/ { match($0, /b\/[^ ]+/); path = substr($0, RSTART+2, RLENGTH-2); next }
  /^@@/ {
    match($0, /\+[0-9]+,?[0-9]*/);
    spec = substr($0, RSTART+1, RLENGTH-1);
    n = split(spec, a, ",");
    start = a[1]; len = (n == 2 ? a[2] : 1);
    if (len > 0) printf "%s\tL%d-L%d\n", path, start, start+len-1;
  }
' ~/claude-loop-pr-codex/$dir_name/pr.diff > ~/claude-loop-pr-codex/$dir_name/pr.diff.ranges.txt
```

続いて `pr.diff.ranges.txt` を Read ツールで取得する。`file_path` は `~` を `$HOME` の実値に展開した絶対パスで渡す。

#### 範囲判定ルール

- `pr.diff.ranges.txt` の各行は `<path>\tL<開始>-L<終了>` として扱う
- 単一行コメントは `line` が同一 `path` のいずれかの範囲内に含まれる場合のみ有効
- 複数行コメントは `[start_line, line]` の両端が同一 `path` の同じ hunk 範囲内に含まれる場合のみ有効。複数 hunk をまたぐ範囲は無効
- `path` が `pr.diff.ranges.txt` に存在しない場合は無効
- `pr.diff.ranges.txt` が空、または `pr.diff` が存在しない場合は、行範囲を確定できないためすべてのインラインコメント候補を無効として扱う

#### 範囲外エントリの扱い

範囲外と判定した `$must_fix` のエントリは、以下のように扱う。

- `$must_fix` から除外し、`comments` 配列には含めない
- 除外したエントリを `$out_of_range_comments` 配列として保持する
- `$out_of_range_comments` には、元の見出し行、元の本文、種別 (`Must Fix`) を保持する
- Step 4 のレビュー body 末尾に `## 行コメント不可 (diff 範囲外)` セクションを追加し、除外した各エントリの元の見出し行と本文を転記する
- 除外後の `$must_fix` の相対順は、`findings.verified.json` がある場合はその配列順、fallback 時は元の `review.md` の登場順を保つ

既存の正常系 PR で全指摘が範囲内の場合、`$out_of_range_comments` は空配列となり、Step 4 以降の payload は従来と同じ内容になる。

### Step 3.75: Should Fix body inclusion opt-in (Step 5 第1ステップ)

`findings.verified.json` primary path で `$should_fix_body_summary_candidates` が 1 件以上ある場合のみ、payload 構築前に以下をユーザーへ提示する。fallback path、または候補 0 件の場合はこのステップを表示せず、`$include_should_fix_body_summary=false` / `$included_should_fix_body_summary=[]` として Step 4 へ進む。

```
非ブロッキング改善 (Should Fix) の上位 3 件を投稿 body に含めますか? (default: no)
候補:
- <path>:L<line> — <summary_line>
- ...

含める場合のみ yes と入力してください。 (yes/no)
```

候補は `findings.verified.json` の `findings[]` 配列順の先頭 3 件までを表示する。ユーザーの応答が `yes` / `y` / `はい` 等の明示的な承認である場合のみ `$include_should_fix_body_summary=true` とし、候補先頭から最大 3 件を `$included_should_fix_body_summary` として保持する。それ以外（`no` / `n` / `いいえ` / 曖昧・無回答）は default の `$include_should_fix_body_summary=false` として扱い、Should Fix は body に含めない。

この opt-in は投稿そのものの承認ではない。Step 4.5 の Codex セルフレビューで最終 payload を検証した後、Step 5 第2ステップで従来どおり投稿可否を確認する。

### Step 4: payload の構築

以下のルールで GitHub Reviews API の payload JSON を組み立てる（`POST /repos/{owner}/{repo}/pulls/{pull_number}/reviews` の request body 仕様に従う）:

- `commit_id`: `$head_sha`（レビュー時点の head に明示的に紐付ける）
- `event`:
  - 範囲検証後の `$must_fix` が 1 件以上、または `$out_of_range_comments` に `Must Fix` が 1 件以上あれば `"REQUEST_CHANGES"`
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
  - `$include_should_fix_body_summary == true` かつ `$included_should_fix_body_summary` が非空の場合は、`## 良い点` セクションの後ろ（`$good_points` が空の場合は `$summary` の直後）に以下を追加する:
    ```
    ## 非ブロッキング改善 (Should Fix)

    - `path/to/file.ext:L<行番号>`
      改善内容: <summary_line>
      提案: <suggestion_line>
    ```
    - 最大 3 件まで。`findings.verified.json` の `findings[]` 配列順を保つ
    - 1 件あたり 3 行以内（path 行 + 改善内容 1 行 + 提案 1 行）
    - Nit / Note / Must Fix はこのセクションに混ぜない
  - `$out_of_range_comments` が非空の場合は body 末尾に以下を追加する:
    ```
    ## 行コメント不可 (diff 範囲外)

    ### `path/to/file.ext:L<行番号>` (元の見出し)

    - 問題: <問題文>
    - 理由: <理由文>
    - 提案: <提案文>
    ```
- `comments`: `$must_fix` 配列のみ（`findings.verified.json` がある場合はその順序、fallback 時は元の登場順を保つ）。各要素は以下のキーを含む:
  - `path` (必須)
  - `line` (必須)
  - `side` (`"RIGHT"`)
  - `body` (Step 3 の body フォーマット)
  - `start_line` / `start_side` は範囲指定の場合のみ含める

範囲検証後の `$must_fix` が空だった場合でも、`event: "COMMENT"` + body (総評 + 良い点 + opt-in された Should Fix body summary + 必要なら行コメント不可セクション) のみで投稿する。ただし `$out_of_range_comments` に `Must Fix` が含まれる場合の `event` は上記ルールどおり `"REQUEST_CHANGES"` とする。

body のセクション順は必ず `総評` → `## 良い点`（存在する場合）→ `## 非ブロッキング改善 (Should Fix)`（opt-in された場合）→ `## 行コメント不可 (diff 範囲外)`（存在する場合）とする。Should Fix セクションを含めても `comments` 配列は Must Fix のみで、GitHub inline comment に Should Fix / Nit / Note を混ぜてはならない。

payload は Write ツールで `~/claude-loop-pr-codex/$dir_name/review-payload.json` に書き出す。`file_path` には `~` を実値に展開した絶対パスを渡し、`$dir_name` も実値に置換する。整形された JSON（インデント 2）で書き出して後から人間が読めるようにする。

### Step 4.5: 投稿前 Codex セルフレビュー

Claude が生成した `review-payload.json` を Codex CLI に独立検証させ、`findings.verified.json`（存在する場合）または `review.md` fallback との不整合、`comments[]` への Must Fix 以外の混入、Should Fix body summary の対応関係、Nit の payload 混入、schema/side 違反、行範囲外コメントを検出する。Step 5 第2ステップ（最終承認プロンプト）の直前で必ず実行する。`findings.verified.json` が存在する場合は、検証プロンプトに `$CLAUDE_PLUGIN_ROOT/schemas/findings.v1.json` の**絶対パス**（Step 3 で解決した `schema_path`）と同梱 validator の絶対パス（`validator_path`）を埋め込み、`--cd ~/claude-loop-pr-codex/$dir_name` 配下に `schemas/` が無くても Codex が schema 実体を読めるようにする。

#### 検証観点

Codex は以下の観点で payload を確認する:

1. `findings.verified.json` が存在する場合は `payload.comments[]` の各要素が `findings[].severity == "must_fix"` の finding に対応し、存在しない場合は `review.md` の `## 重大な問題 (Must Fix)` セクション内の `### path:L<行番号>` 見出しに対応するか
2. `findings.verified.json` が存在する場合は `comments[]` に `should_fix` / `nit` / `note` finding が、fallback 時は `comments[]` に `## 改善提案 (Should Fix)` / `## 軽微な指摘 (Nit)` / `## 補足` セクション由来のエントリが混入していないか
3. 各 `comments[]` の `path` が `metadata.json.files[]` に含まれるか
4. 各 `comments[]` の `path` と `line`（および `start_line`）が `pr.diff.ranges.txt` の同一 path の hunk 範囲内に収まるか
5. `event` が「Must Fix が1件以上→REQUEST_CHANGES / 0件→COMMENT」ルールに従っているか
6. `body` の冒頭が `review.md` の `## 総評` セクション本文と一致するか
7. `body` 中に `## 良い点` セクションがあれば、`review.md` の `## 良い点` 本文と一致するか
8. `findings.verified.json` が存在する場合、そこにある Must Fix 件数と `review.md` の Must Fix 見出し件数が一致するか
9. `findings.verified.json` が存在する場合、`schema_path` / `validator_path` の実体を読んで同梱 validator validation を通っており、Must Fix に `location.side != RIGHT` が混入していないか
10. `findings.verified.json` が存在する場合、全 finding で `id == fingerprint` が成り立ち、正準 fingerprint 再計算値とも一致するか
11. `findings.verified.json` が存在する場合、`severity == "must_fix"` の各 finding が 4 軸 gate（`axes.real == "yes"` / `axes.triggerable == "yes"` / `axes.impactful == "yes"` / (`axes.general == "yes"` または `evidence_level in {"impact_explained", "verified"}`) / `evidence_level != "suspicion"`）を満たしているか
12. payload.body に `## 非ブロッキング改善 (Should Fix)` セクションが含まれている場合、各エントリの `path:L<line>` が `findings[].severity == "should_fix" && posting.post_policy == "body_summary"` の finding と 1:1 対応し、Must Fix / Nit / Note 由来の混入がなく、件数が 3 件以下か
13. payload.body のセクション順序が `総評` → `## 良い点` → `## 非ブロッキング改善 (Should Fix)` → `## 行コメント不可 (diff 範囲外)` の順を守っているか（存在しない任意セクションはスキップして順序だけ検証する）
14. `findings[].severity == "nit"` の finding が `payload.comments[]` と `payload.body` のどこにも出現していないか

#### コマンド

- いつ使うか: Step 4 で `review-payload.json` を生成した直後、Step 5 第2ステップ（最終承認プロンプト）前に必ず実行する
- 判定条件: 標準出力に VERDICT: PASS または VERDICT: FAIL の行が含まれる
- 次アクション: PASS なら Step 5 へ進む。FAIL なら標準出力の指摘内容を読み取り、payload を再生成して再度本ステップを実行する（最大 3 回まで）。3 回連続 FAIL なら処理中断してユーザーへ通知
- `{SCHEMA_PATH}` は Step 2.5 で保持した `schema_path`、`{VALIDATOR_PATH}` は `validator_path` の絶対パスに置換される。Bash ツールに渡す前に Claude 側で prompt 内の両プレースホルダを絶対パス文字列へ置換する

```bash
codex \
  --ask-for-approval never \
  -m gpt-5.5 \
  -c sandbox_mode=read-only \
  exec \
  --ignore-user-config \
  --skip-git-repo-check \
  --cd ~/claude-loop-pr-codex/$dir_name \
  "
あなたは GitHub PR レビュー投稿前の独立検証エージェントです。Claude が生成した review-payload.json を読み、以下の観点で検証してください。判定が完了したら PASS / FAIL のいずれかを最終行に明記してください。

目的は、GitHub Reviews API に投稿する直前の payload から、comments[] への Must Fix 以外の混入・Should Fix body summary の対応ミス・Nit の payload 混入・範囲外コメント・event 判定ミスを検出して誤投稿を防ぐことです。
完了条件は、検証対象ファイルをすべて読み、各観点の PASS / FAIL 理由を示し、最終行に VERDICT: PASS または VERDICT: FAIL を単独で出力することです。

## 検証対象ファイル
- review-payload.json: 投稿予定の GitHub Reviews API payload
- findings.verified.json: canonical findings（存在する場合のみ source of truth）
- {SCHEMA_PATH}: canonical findings schema（絶対パス。存在する場合のみ findings validation に使う）
- {VALIDATOR_PATH}: 同梱 validator（絶対パス。存在する場合のみ findings validation に使う）
- review.md: 統合レビューの全文
- pr.diff.ranges.txt: コメント可能な hunk 範囲一覧
- metadata.json: 対象 PR のメタデータ（files 配列を含む）

## 検証観点
1. findings.verified.json が存在する場合は payload.comments[] の各要素が findings[].severity == 'must_fix' の finding に対応すること。存在しない場合は review.md の '## 重大な問題 (Must Fix)' セクション内の '### path:L<行番号>' 見出しに対応すること。comments[] に Must Fix 以外（findings の should_fix / nit / note、または review.md の '## 改善提案 (Should Fix)' / '## 軽微な指摘 (Nit)' / '## 補足'）由来のエントリが含まれていないこと。findings.verified.json が存在する場合は、M1 posting contract として severity != 'must_fix' の finding に posting.post_policy == 'inline' が含まれていないことも確認する
2. payload.comments[] の各 path が metadata.json.files[] に含まれること
3. payload.comments[] の各エントリで、path と line（および start_line）が pr.diff.ranges.txt の同一 path の hunk 範囲内に収まること（複数行は両端が同一 hunk）
4. payload.event が 'Must Fix が1件以上 → REQUEST_CHANGES / 0件 → COMMENT' のルールに従うこと
5. payload.body の冒頭が review.md の '## 総評' セクション本文と一致すること（先頭・末尾の空白を除く）
6. payload.body 中の '## 良い点' セクションがある場合、review.md の '## 良い点' 本文と一致すること
7. findings.verified.json が存在する場合、そこにある Must Fix 件数と review.md の Must Fix 見出し件数が一致すること
8. findings.verified.json が存在する場合、top-level pr.repository / pr.number / pr.head_sha / pr.base_sha が metadata.json の repository_full_name / pr_number / head_sha / base_sha と一致し、metadata.json.repository_full_name が投稿先 org/repository と一致すること
9. findings.verified.json が存在する場合、絶対パス {SCHEMA_PATH} の schema 実体と {VALIDATOR_PATH} の validator 実体を読み、可能なら python3 {VALIDATOR_PATH} --schema {SCHEMA_PATH} --data findings.verified.json --metadata metadata.json を実行して適合していることを確認する。実行できない場合も同梱 validator と同じ条件（required / enum / additionalProperties / allOf / if/then / format / range / fingerprint 再計算 / metadata.json との PR context 一致 / 4 軸 gate）で手動検証し、Must Fix finding の location.side がすべて RIGHT であることを確認する。schema または validator 実体を読めない場合は PASS ではなく FAIL とする
10. findings.verified.json が存在する場合、全 finding で id == fingerprint が成り立ち、同梱 validator と同じ正準アルゴリズムで再計算した fingerprint と一致すること
11. findings.verified.json が存在する場合、severity == 'must_fix' の各 finding が以下を全部満たすことを確認する: axes.real == 'yes' / axes.triggerable == 'yes' / axes.impactful == 'yes' / (axes.general == 'yes' または evidence_level in {'impact_explained', 'verified'}) / evidence_level != 'suspicion'。1 件でも違反していれば FAIL とする。python3 {VALIDATOR_PATH} の再実行に成功している場合も、この観点を明示的に PASS / FAIL として報告する
12. payload.body に '## 非ブロッキング改善 (Should Fix)' セクションが含まれている場合、各エントリの path:L<line> が findings[].severity == 'should_fix' && posting.post_policy == 'body_summary' の finding と 1:1 対応すること。Must Fix / Nit / Note 由来の混入がなく、件数が 3 件以下であること
13. payload.body のセクション順序が '総評' → '## 良い点' → '## 非ブロッキング改善 (Should Fix)' → '## 行コメント不可 (diff 範囲外)' の順を守っていること。存在しない任意セクションはスキップしてよいが、出現したセクションの相対順が逆転していれば FAIL とする
14. findings[].severity == 'nit' の finding が payload.comments[] と payload.body のどこにも出現していないこと。nits.md が存在する場合でも、その内容は payload に転記されていないこと

## 出力フォーマット
最初に各観点の検証結果を箇条書きで列挙し、最終行に必ず以下のいずれかを単独で出力してください:
- VERDICT: PASS  （全観点クリア）
- VERDICT: FAIL  （1件以上の違反あり）

FAIL の場合は VERDICT: FAIL の直前に '違反一覧' セクションを設け、対象 comment の index・違反観点番号・理由・推奨アクション（除外 / 行範囲補正 / body 修正 など）を列挙してください。
" \
  <  /dev/null \
  >  ~/claude-loop-pr-codex/$dir_name/preflight-codex.md \
  2> ~/claude-loop-pr-codex/$dir_name/preflight-codex.log
```

フラグの説明:

- `--ask-for-approval never` / `-m gpt-5.5` / `-c ...` は global flag のため、すべて `exec` の前に置く
- `-c sandbox_mode=read-only` — シェル実行を read-only サンドボックスに固定する。`--sandbox read-only` と等価だが、config override として明示するため `-c` に統一する
- `--ignore-user-config` — 投稿前検証中のみ `$CODEX_HOME/config.toml` / `~/.codex/config.toml` を読み込まない。auth は引き続き `CODEX_HOME` を使うため、古い MCP 設定や無効な `model_reasoning_effort` による config 検証エラーから Step 4.5 preflight を切り離せる
- `--skip-git-repo-check` / `-C, --cd` は `exec` サブコマンド側の option のため、`exec` の後ろ、かつ prompt の前に置く
- `--color never` / `--ephemeral` はテンプレートを簡素化するため使わない。カラーは TTY 自動判定に任せ、セッション保存挙動は config 側に委ねる

#### 失敗時の Claude 側の処理

- いつ使うか: 上の codex 実行で `VERDICT: FAIL` が出力された場合
- 判定条件: `preflight-codex.md` に `VERDICT: FAIL` 行が含まれる
- 次アクション: 違反一覧を読み、payload.comments[] から該当エントリを除外、または body / event を修正して `review-payload.json` を再生成し、再度本 Step 4.5 を実行する。再試行は最大 3 回まで

#### 3 回連続 FAIL 時の処理

- 自動再投稿は中止し、ユーザーに以下を報告して終了する:
  - `preflight-codex.md` のパス
  - 最新の `review-payload.json` のパス
  - 「Codex セルフレビューが 3 回連続で FAIL したため自動投稿を中止」という旨

### Step 5: 承認プロンプト

Step 3.75 の Should Fix body inclusion opt-in（候補がある場合のみ表示）と Step 4.5 の Codex セルフレビューを終えた後、投稿前の最終確認として以下のサマリをテキストで提示し、明示的な承認を求める:

```
対象 PR: <$pr_url> (<$title>)
event: <REQUEST_CHANGES | COMMENT>
findings source: ~/claude-loop-pr-codex/<$dir_name>/findings.verified.json (fallback 時のみ review.md parser)
review file: ~/claude-loop-pr-codex/<$dir_name>/review.md
body プレビュー:
  <$summary の先頭 200 文字。長ければ "..." で省略>
インラインコメント: Must Fix N 件
Should Fix body summary: included <yes|no> (<included_count>/<candidate_count> 件、default: no)
Nit artifact: <~/claude-loop-pr-codex/<$dir_name>/nits.md | nit: 0 件>
（Should Fix は opt-in された上位 3 件のみ body に含めます。Nit は PR には載せず nits.md にのみ残します）
行範囲外で除外したインラインコメント (Must Fix のみ): K 件
  - <path>:L<line> (本文末尾の「行コメント不可」セクションに移動)
payload: ~/claude-loop-pr-codex/<$dir_name>/review-payload.json
移動先 (投稿後): ~/claude-loop-pr-codex/sent/<$dir_name>-<$head_sha_short>

この内容で投稿してよろしいですか？ (yes/no)
```

`$out_of_range_comments` が空の場合も、サマリ行は `行範囲外で除外したインラインコメント: 0 件` として表示する。除外したエントリの箇条書きは 1 件以上ある場合のみ表示する。
fallback path では `Should Fix body summary: included no (0/0 件、default: no)`、`Nit artifact: nit: 0 件` と表示する。primary path で `$nit_findings` が 1 件以上ある場合は `nits.md` のパスを表示し、0 件なら `nit: 0 件` と表示する。

ユーザーの応答が `yes` / `y` / `はい` 等の明示的な承認である場合のみ Step 6 に進む。それ以外（`no` / `n` / `いいえ` / 曖昧・無回答）の場合は処理を中断し、以下を報告して終了する:

- 投稿はスキップした旨
- payload ファイルは保持されている旨 (`~/claude-loop-pr-codex/$dir_name/review-payload.json`)
- Nit 件数。`nits.md` を生成した場合は `~/claude-loop-pr-codex/$dir_name/nits.md`、0 件なら `nit: 0 件`
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
- 次アクション: 移動先の事前衝突チェックへ進む

```bash
install -d ~/claude-loop-pr-codex/sent
```

- いつ使うか: `sent/` を作成した直後、`mv` の直前に実行する
- 判定条件: 終了コード 0（移動先の `sent/$dir_name-$head_sha_short` がまだ存在しない）
- 次アクション: 0 なら `mv` テンプレートへ進む。非 0 なら同一 `head_sha` に対する再投稿などによる degenerate 衝突として Step 8 の失敗報告へ進み、`mv` は実行しない

```bash
test ! -e ~/claude-loop-pr-codex/sent/$dir_name-$head_sha_short
```

- いつ使うか: 移動先が存在しないことを確認した直後に実行する
- 判定条件: 終了コード 0
- 次アクション: 終了コード 0 でも `mv -n` は silent skip し得るため、続けて移動完了検証テンプレートへ進む。非 0 なら Step 8 の失敗報告へ進む

```bash
mv -n ~/claude-loop-pr-codex/$dir_name ~/claude-loop-pr-codex/sent/$dir_name-$head_sha_short
```

- いつ使うか: `mv -n` の直後に必ず実行する
- 判定条件: 終了コード 0（元ディレクトリが消え、かつ `sent/$dir_name-$head_sha_short` が存在する）
- 次アクション: 0 なら Step 8 の成功報告へ進む。非 0 なら `mv` が silent に失敗した可能性として Step 8 の失敗報告へ進む

```bash
test ! -d ~/claude-loop-pr-codex/$dir_name && test -d ~/claude-loop-pr-codex/sent/$dir_name-$head_sha_short
```

移動後、`review-payload.json` / `review-response.json` も一緒に保管され、投稿履歴として残る。
同一 `head_sha` に対する再投稿などで移動先が既に存在する場合は、事前の存在確認と `mv -n` で衝突として扱い、ユーザーに `sent/$dir_name-$head_sha_short` の調査を促す。`mv -n` は TOCTOU 競合でも誤上書きを防ぐ防衛線として残す。

### Step 8: 結果報告

ユーザーに以下をテキストで報告して終了する:

成功時:

- 対象 PR: `$pr_url` (`$title`)
- 投稿した review の URL: `$review_url`
- 選択した `event`
- インラインコメント件数 (Must Fix のみ)
- Should Fix body summary 同梱結果 (`included yes/no` と件数)
- Nit 件数。`nits.md` を生成した場合は移動後の path `~/claude-loop-pr-codex/sent/$dir_name-$head_sha_short/nits.md`、0 件なら `nit: 0 件`
- 行範囲外で除外したインラインコメント件数
- 移動先: `~/claude-loop-pr-codex/sent/$dir_name-$head_sha_short`

失敗時（Step 6 が非ゼロ終了、Step 7 の移動先衝突、または Step 7 の移動完了検証が失敗した場合）:

- エラー内容または状況 (`gh api` の stderr、Step 7 の移動先衝突、または Step 7 の移動完了検証失敗)
- Nit 件数。`nits.md` を生成した場合は未移動の path `~/claude-loop-pr-codex/$dir_name/nits.md`、0 件なら `nit: 0 件`
- 推定原因:
  - 422 → Step 3.5 で PR diff 範囲外のインラインコメントは除外済みのため、残ったコメントの `path` / `line` / `start_line` が GitHub 側で解決不能になっている可能性がある。`review-payload.json` の `comments` と `pr.diff.ranges.txt` / `pr.diff` を照合し、必要なら payload から該当コメントを除外するようユーザーに案内
  - 403 → 権限不足。`gh auth status` の確認と、PR リポジトリへのコメント権限を案内
  - 404 → PR が見つからない。`$org` / `$repository` / `$pr_number` の値確認を案内
  - Step 7 の移動先衝突 → `~/claude-loop-pr-codex/sent/$dir_name-$head_sha_short/` がすでに存在する。同一 `head_sha` (`$head_sha_short`) への重複投稿の可能性があるため、既存の投稿履歴 (`metadata.json` / `review-response.json`) を確認するようユーザーに案内。本当に再投稿が必要なら、既存の `sent/` ディレクトリを手動でリネームまたは退避してから再実行する
  - Step 7 の移動完了検証失敗 → `mv` が silent に失敗した可能性がある。`~/claude-loop-pr-codex/$dir_name/` と `~/claude-loop-pr-codex/sent/$dir_name-$head_sha_short/` の両方を手動確認するようユーザーに案内
- Step 6 失敗時は `~/claude-loop-pr-codex/$dir_name/` を**移動しない**。payload と response を残した状態で終了するので、ユーザーは payload 修正後に再度 `/pr-codex:send` を叩くか、手動で `gh api` を実行できる
- Step 7 失敗時は投稿自体は完了している点に注意する。`~/claude-loop-pr-codex/$dir_name/` は移動せず、`review-response.json` も残す

## エラーハンドリング

- 対象ディレクトリなし → 「投稿対象の completed レビューなし」と報告して正常終了（非エラー）
- `findings.verified.json` が空 / JSON parse 失敗 / top-level object でない / `findings[]` 不在または非配列 → ユーザーに通知して処理中断（fallback へは切り替えない、`sent/` 移動もしない）
- `findings.verified.json` が存在するのに `schema_version != findings.v1` → ユーザーに通知して処理中断
- `findings.verified.json` の schema / fingerprint validation が同梱 validator + `schemas/findings.v1.json` で失敗 → ユーザーに通知して処理中断（fallback へは切り替えない）
- `findings.verified.json.pr.*` が `metadata.json` の投稿先 repo / PR number / head/base SHA と一致しない → ユーザーに通知して処理中断（fallback へは切り替えない）
- `findings.verified.json` の Must Fix 件数と `review.md` の Must Fix 見出し件数が不一致 → ユーザーに通知して処理中断（fallback へは切り替えない）
- `findings.verified.json` の Must Fix に `location.side != RIGHT` が含まれる → ユーザーに通知して処理中断（M1 では old-side 投稿を扱わない）
- `findings.verified.json` の Must Fix に `posting.post_policy != inline` または `explanation_postable != true` が含まれる → ユーザーに通知して処理中断（M1 では安全に自動投稿しない）
- `review.md` に Must Fix が一件も無い → それでも `event: COMMENT` + body (総評 + 良い点 + opt-in された Should Fix body summary) のみで投稿する（インラインコメント配列は空）
- `review.md` の `## 総評` セクションが空 or 見つからない → ユーザーに通知して処理中断。`sent/` 移動は行わない
- Step 3.5 で `pr.diff.ranges.txt` が空 → インラインコメント候補はすべて body 末尾の `## 行コメント不可 (diff 範囲外)` に移動し、`comments` 配列には含めない
- `gh api` 422/403/404 → Step 8 の失敗報告で分岐し、`sent/` 移動は行わない
- Step 7 で `sent/$dir_name-$head_sha_short/` がすでに存在 → ユーザーに通知して処理中断（投稿はすでに完了している点に注意）。`sent/` 移動は行わず、`review-response.json` を残した状態で終了する
- Step 7 の移動完了検証が失敗 → `mv` が silent に失敗した可能性があるため Step 8 の失敗報告で手動確認を促し、`review-response.json` を残した状態で終了する
- ユーザーが Step 5 で承認を拒否 → 何もせず終了。payload ファイルは残す

## 実装上の制約

本スキルは対話実行を前提とし、通常の permission mode で使うことを想定する（`/loop` には載せない）。ただし既存 `/pr-codex:review` と統一感を持たせるため、以下の原則を踏襲する:

1. 各テンプレートは 1 テンプレート = 1 シェル実行単位として扱う
2. テンプレートの改変は変数置換のみ許可する。フラグ、引数順、引用符、リダイレクトはテンプレート記載どおりに使う
3. シェル演算子はテンプレート中に明示された `|` `<` `>` `2>` `&&` のみ許可する
4. `findings.verified.json` が存在する場合はそれを payload の一次入力とし、`review.md` parser は fallback に限定する。parse failure / shape failure / validator failure / `location.side != RIGHT` / 件数不一致 / posting policy 不整合時に fallback へ自動切替してはならない
5. payload JSON と `nits.md` の生成は Write ツールで行う（`jq -n` によるインラインでの複雑な配列組み立ては使わない）
6. `$()` / `for` / `while` / `xargs` / ヒアドキュメントは使わない
7. `mv` は `sent/` への移動以外では使わない
8. `gh` の write 系操作は `gh api --method POST .../reviews` のみとし、`gh pr review` / `gh pr comment` / `gh pr merge` などは使わない
9. 1 回の実行で処理する対象ディレクトリは 1 件のみとする
10. 投稿前の Step 5 承認プロンプトは必須。自動投稿はしない。Should Fix body summary は default no とし、Step 3.75 で明示 opt-in された場合だけ上位 3 件を body に含める
11. `findings.verified.json` が存在する場合は Step 3 の `python3 $CLAUDE_PLUGIN_ROOT/tasks/validate_findings.py ...` を **必ず**実行する。validator 失敗時に payload 生成や Markdown fallback へ進んではならない
12. Step 4.5 の Codex セルフレビューは **必須**。スキップしてはならない。`VERDICT: PASS` を確認するまで Step 5 第2ステップ（最終承認）に進まない。schema 検証観点では `$CLAUDE_PLUGIN_ROOT/schemas/findings.v1.json` の絶対パスを prompt に埋め込み、`--cd` 配下の相対 `schemas/` には依存しない

## ファイル構成

スキル本体:

```
$CLAUDE_PLUGIN_ROOT/skills/send/
  └── SKILL.md                ← 本ファイル
$CLAUDE_PLUGIN_ROOT/tasks/
  └── validate_findings.py    ← primary path の schema / fingerprint / format / range validator
```

実行時の作業ディレクトリ (投稿前):

```
~/claude-loop-pr-codex/
  └── $org-$repository-$pr_number/
        ├── status.json            ← state:completed
        ├── metadata.json
        ├── findings.verified.json  ← primary input (`schemas/findings.v1.json`)
        ├── validation-report.json  ← review 側の副成果物（あれば保持）
        ├── review.md              ← 投稿元
        ├── pr.diff
        ├── pr.diff.ranges.txt     ← Step 3.5 で生成するコメント可能行範囲
        ├── claude-review.md
        ├── codex-review.md
        ├── claude.log
        ├── preflight-codex.md      ← Step 4.5 の Codex セルフレビュー結果（VERDICT: PASS/FAIL）
        ├── preflight-codex.log     ← Codex 実行時の stderr
        ├── nits.md                 ← primary path で Nit がある場合のみ生成（PR には投稿しない）
        └── codex.log
```

投稿後:

```
~/claude-loop-pr-codex/
  └── sent/
        └── $org-$repository-$pr_number-$head_sha_short/
              ├── status.json
              ├── metadata.json
              ├── findings.verified.json
              ├── validation-report.json
              ├── review.md
              ├── review-payload.json    ← 追加: 投稿した payload
              ├── review-response.json   ← 追加: gh api のレスポンス (.html_url 等を含む)
              ├── pr.diff
              ├── pr.diff.ranges.txt
              ├── claude-review.md
              ├── codex-review.md
              ├── claude.log
              ├── preflight-codex.md
              ├── preflight-codex.log
              ├── nits.md                ← Nit があった場合のみ。他 artifact と一緒に移動される
              └── codex.log
```
