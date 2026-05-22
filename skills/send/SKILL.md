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

- `/pr-codex:review` が先に実行されており、`~/claude-loop-pr-codex/<org>-<repository>-<pr_number>/` 配下に `status.json` (`state:completed`) / `metadata.json` / `findings.verified.json` / `review.md` が揃っている
- `ci-status.json` / `ci-summary.md` が存在する場合は、投稿前判断の read-only CI context として参照する。`failure` / `pending` を理由に GitHub workflow の rerun / cancel / write は行わず、必要ならユーザーへ CI 状態を説明して投稿可否を確認する
- `findings.verified.json` を **必須の一次入力** とする。M1 の F13 以降、`review.md` parser への Markdown fallback は使わない
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
- 次アクション: 存在すれば `findings.verified.json` 存在確認へ。存在しなければスキップし次の候補へ

```bash
test -f ~/claude-loop-pr-codex/$candidate/review.md
```

- いつ使うか: `review.md` が存在する `$candidate` に対して実行する
- 判定条件: `findings.verified.json` が存在する
- 次アクション: 存在すればこの候補を確定し、`$candidate` の値を `$dir_name` として保持して Step 2 へ進む。存在しなければ、F13 の必須入力欠落としてユーザーへ通知し処理を中断する（Markdown fallback へは切り替えない）

```bash
test -f ~/claude-loop-pr-codex/$candidate/findings.verified.json
```

全候補がスキップなら「投稿対象の completed レビューなし」とユーザーに報告して正常終了する。`sent/` への移動も payload 生成も行わない。

### Step 2: メタデータとレビューの読み込み

- いつ使うか: `$dir_name` が確定した直後に実行する
- 判定条件: 標準出力に `org=` / `repository=` / `repository_full_name=` / `pr_number=` / `pr_url=` / `head_sha=` / `head_sha_short=` / `base_sha=` / `title=` の 9 行が返る
- 次アクション: 各値をそれぞれ `$org`, `$repository`, `$repository_full_name`, `$pr_number`, `$pr_url`, `$head_sha`, `$head_sha_short`, `$base_sha`, `$title` として保持し、`review.md` の Read へ進む

```bash
jq -r '"org=\(.org)\nrepository=\(.repository)\nrepository_full_name=\(.repository_full_name)\npr_number=\(.pr_number)\npr_url=\(.pr_url)\nhead_sha=\(.head_sha)\nhead_sha_short=\(.head_sha[0:7])\nbase_sha=\(.base_sha)\ntitle=\(.title)"' ~/claude-loop-pr-codex/$dir_name/metadata.json
```

続いて `review.md` と `findings.verified.json` を Read ツールで取得する。`file_path` は `~` を `$HOME` の実値に展開した絶対パスで渡す（例: `/Users/adachi/claude-loop-pr-codex/$dir_name/review.md` の `$dir_name` と `/Users/adachi` をいずれも実値に置換してから呼び出す）。

- いつ使うか: `review.md` を読み込んだ直後に実行する
- 判定条件: `findings.verified.json` が存在するなら終了コード 0
- 次アクション: 存在するなら `findings.verified.json` を Read ツールで取得して Step 3 へ。存在しなければ F13 必須入力欠落としてユーザーへ通知し中断する（Markdown fallback へは切り替えない）

```bash
test -f ~/claude-loop-pr-codex/$dir_name/findings.verified.json
```

### Step 2.5: plugin root / schema / validator path の解決

Step 3 と Step 4.5 の verifier pipeline で `{SCHEMA_PATH}` / `{VALIDATOR_PATH}` / `{SARIF_SCHEMA_PATH}` / `{SARIF_VALIDATOR_PATH}` / `{SARIF_GENERATOR_PATH}` / `{PREFLIGHT_SCHEMA_PATH}` / `{PREFLIGHT_VALIDATOR_PATH}` を絶対パスへ置換できるよう、ここで各 path を保持する。`$CLAUDE_PLUGIN_ROOT` が未設定・不明な場合は review skill と同じ手順（`echo "$CLAUDE_PLUGIN_ROOT"`、空なら `**/pr-codex/skills/review/REVIEW_CRITERIA.md` の探索結果から plugin root を逆算）で絶対パスを確定する。

保持する値:

- `schema_path = <plugin-root>/schemas/findings.v1.json`
- `validator_path = <plugin-root>/tasks/validate_findings.py`
- `sarif_schema_path = <plugin-root>/schemas/sarif-2.1.0.json`
- `sarif_validator_path = <plugin-root>/tasks/validate_findings_sarif.py`
- `sarif_generator_path = <plugin-root>/tasks/generate_findings_sarif.py`
- `preflight_schema_path = <plugin-root>/schemas/preflight-result.v1.json`
- `preflight_validator_path = <plugin-root>/tasks/validate_preflight_result.py`

### Step 3: `findings.verified.json` の解析 (primary)

`findings.verified.json` を **必須の一次情報源**として payload を組み立てる。`review.md` は `## 総評` / `## 良い点` の本文取得と、Must Fix 件数 gate の確認にだけ使う。まず Step 2.5 で保持した `validator_path` / `schema_path` を使い、`findings.verified.json` がその schema に適合するかを review 側と同じ同梱 validator で外部検証してから抽出へ進む。

#### 同梱 validator コマンド

- いつ使うか: `findings.verified.json` 解析の開始直後、JSON 抽出や payload 生成の前に必ず実行する
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
  - top-level `root_cause_clusters[]` がある場合は同梱 validator 済みの cluster detail を読み、各 cluster の `representative_finding_id` を representative posting 対象として扱う。cluster member は canonical finding としては残し、GitHub inline duplicate は代表コメントに集約する
  - `findings[]` のうち `severity == "should_fix" && posting.post_policy == "body_summary"` の要素を `$should_fix_body_summary_candidates` 配列として抽出する。順序は `findings[]` の登場順を保ち、Step 5 の opt-in がない限り body には含めない
  - `findings[]` のうち `severity == "nit"` の要素を `$nit_findings` 配列として抽出する。`posting.post_policy` の値に関わらず GitHub payload には含めず、primary path でのみ `nits.md` に書き出す
  - M1 の投稿 contract として、`severity != "must_fix"` の finding に `posting.post_policy == "inline"` が含まれないことを確認する
  - `category == "security"` の finding は `security` extension を必須とし、`security.severity == "critical" | "high"` または `security.disclosure_policy != "inline_safe"` の場合は inline 投稿対象から除外する。公開 body に含める場合も `security.public_safe_summary` だけを使い、raw exploit detail / secret / 攻撃手順は載せない

#### `findings.verified.json` から抽出するフィールド

各 Must Fix finding から以下を payload 用に組み立てる。`root_cause_clusters[]` がない finding は従来どおり個別 inline comment にする。cluster member のうち `representative_finding_id` ではない finding は duplicate inline comment としては投稿せず、代表 finding の `body` に affected findings summary として path/line/problem を最大 5 件まで短く含める（超過分は `他 N 件` として数だけ示す）。canonical / review.md / SARIF / preflight の Must Fix count は cluster member を含む full finding count を使い、代表数に減らして数えない:

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

- `findings.verified.json` が存在しない / 空 / JSON parse 失敗 / top-level object でない / `findings[]` 不在または非配列 / 同梱 validator による `schemas/findings.v1.json` validation / fingerprint 再計算 / format / range validation 失敗 / `id != fingerprint` のいずれかなら、ユーザーに通知して **中断** する（Markdown fallback へは切り替えない）
- `findings.verified.json.pr.repository != metadata.json.repository_full_name`、`findings.verified.json.pr.number != metadata.json.pr_number`、`findings.verified.json.pr.head_sha != metadata.json.head_sha`、`findings.verified.json.pr.base_sha != metadata.json.base_sha`、または `metadata.json.repository_full_name != "$org/$repository"` のいずれかなら、canonical artifact が投稿先 PR と一致しないため **中断** する（Markdown fallback へは切り替えない）
- `severity == "must_fix"` の finding は、M1 では原則 **`posting.post_policy == "inline"` かつ `posting.explanation_postable == true`** のものだけを自動 inline 投稿対象として扱う。security high/critical または `disclosure_policy != inline_safe` の Must Fix は例外として inline から除外し、公開可能な `public_safe_summary` を body/local-only 側に分岐する
- `severity != "must_fix"` の finding に `posting.post_policy == "inline"` が 1 件でもあれば、review 側の M1 posting contract 違反として **中断** する（Markdown fallback へは切り替えない）。M1 では `should_fix` / `nit` / `note` は inline 自動投稿対象外であり、`body_summary` / `local_only` / `suppress` のいずれかで表現する
- `severity == "must_fix"` の finding で `location.side != "RIGHT"` が 1 件でもあれば、現 workflow の `pr.diff.ranges.txt` が head/new 側前提のため **中断** する（Markdown fallback へは切り替えない）
- `must_fix` なのに `posting.post_policy` が `body_summary` / `local_only` / `suppress` のもの、または `posting.explanation_postable == false` のものが 1 件でもあれば、GitHub payload へ安全に変換できないため **中断** する（Markdown fallback へは切り替えない）。ただし `category == "security"` かつ `security.severity == "critical" | "high"` または `security.disclosure_policy != "inline_safe"` の場合は例外で、inline 詳細を避けるため `body_summary` / `local_only` 側に分岐させる
- `category == "security"` なのに `security` extension が無い、`security.public_safe_summary` が exploit command / secret / raw payload を含む、または `security.severity == "critical" | "high"` で `posting.post_policy == "inline"` のものが 1 件でもあれば **中断** する。公開 repo では high/critical security finding を inline 詳細として投稿せず、review 再生成で `body_summary_safe` / `local_only` に分岐させる
- `$must_fix` の件数と `$must_fix_markdown_count` が **完全一致** しなければ中断する。人手で `review.md` が編集された、または review 側の派生生成が壊れている可能性があるため、Markdown fallback へは切り替えない

#### `body` のフォーマット

Must Fix:

```
🚨 **Must Fix**

- 問題: <problem>
- 理由: <reason>
- 提案: <suggestion>

同一 root cause の影響箇所:
- `<path:Lline>` <problem>
```

`同一 root cause の影響箇所` は cluster representative の場合のみ追加する。representative 自身はこの箇条書きから除外し、同じ cluster の他 finding を canonical array order で最大 5 件まで列挙する。

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

### Step 3b: `review.md` の解析 (fallback 廃止)

F13 以降、`findings.verified.json` は必須の一次入力であり、`review.md` parser fallback は使わない。`findings.verified.json` が存在しない、壊れている、または `review.md` と Must Fix 件数が一致しない場合は、payload 生成へ進まず処理を中断する。Should Fix body summary の opt-in と `nits.md` 書き出しも primary path のみで行う。

### Step 3.5: 行範囲検証

GitHub Reviews API は PR diff の新ファイル側 hunk 範囲外の `line` を 422 `Line could not be resolved` で拒否するため、payload 構築前に `pr.diff` からコメント可能行範囲を抽出し、Step 3 で得たインラインコメント候補を検証する。

- いつ使うか: Step 3 で `$must_fix` 配列を作成した直後、Step 4 の payload 構築前に必ず実行する
- 判定条件: `pr.diff.ranges.txt` が作成される
- 次アクション: 作成後、`pr.diff.ranges.txt` を Read ツールで取得し、Claude 側で `$must_fix` の各エントリを検証する

```bash
test -f "${CLAUDE_PLUGIN_ROOT}/skills/lib/extract-diff-ranges.awk" && awk -f "${CLAUDE_PLUGIN_ROOT}/skills/lib/extract-diff-ranges.awk" ~/claude-loop-pr-codex/$dir_name/pr.diff > ~/claude-loop-pr-codex/$dir_name/pr.diff.ranges.txt
```

`${CLAUDE_PLUGIN_ROOT}` が Bash subprocess に渡らず `test -f` が失敗した場合は、`echo "$CLAUDE_PLUGIN_ROOT"` または plugin cache の絶対パス確認で root を確定し、`${CLAUDE_PLUGIN_ROOT}` の位置を実値に置換した同じ `test -f ... && awk -f ...` コマンドを再実行する。root を確定できない場合は silent な空ファイル生成を避けるため中断する。

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
- 除外後の `$must_fix` の相対順は、`findings.verified.json` の配列順を保つ

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
- `comments`: `$must_fix` 配列のみ（`findings.verified.json` の順序を保つ）。各要素は以下のキーを含む:
  - `path` (必須)
  - `line` (必須)
  - `side` (`"RIGHT"`)
  - `body` (Step 3 の body フォーマット)
  - `start_line` / `start_side` は範囲指定の場合のみ含める

範囲検証後の `$must_fix` が空だった場合でも、`event: "COMMENT"` + body (総評 + 良い点 + opt-in された Should Fix body summary + 必要なら行コメント不可セクション) のみで投稿する。ただし `$out_of_range_comments` に `Must Fix` が含まれる場合の `event` は上記ルールどおり `"REQUEST_CHANGES"` とする。

body のセクション順は必ず `総評` → `## 良い点`（存在する場合）→ `## 非ブロッキング改善 (Should Fix)`（opt-in された場合）→ `## 行コメント不可 (diff 範囲外)`（存在する場合）とする。Should Fix セクションを含めても `comments` 配列は Must Fix のみで、GitHub inline comment に Should Fix / Nit / Note を混ぜてはならない。

payload は Write ツールで `~/claude-loop-pr-codex/$dir_name/review-payload.json` に書き出す。`file_path` には `~` を実値に展開した絶対パスを渡し、`$dir_name` も実値に置換する。整形された JSON（インデント 2）で書き出して後から人間が読めるようにする。

### Step 4.5: 投稿前 verifier pipeline (Codex セルフレビュー)

Claude が生成した `review-payload.json` と review 側が生成した local-only `findings.sarif` を Codex CLI に独立検証させ、投稿直前の検証を **4 stage verifier pipeline** として実行する。Step 5 第2ステップ（最終承認プロンプト）の直前で必ず実行する。`findings.verified.json` は必須入力であり、Markdown fallback は使わない。検証では `comments[]` への Must Fix 以外の混入、Should Fix body summary の対応関係、Nit の payload 混入、SARIF schema/side/post_policy 違反、行範囲外コメント、event/body 不整合を検出する。従来互換の `preflight-codex.md` / `preflight-codex.log` は維持し、新たに構造化結果 `preflight-result.json` を出力する。検証プロンプトには `$CLAUDE_PLUGIN_ROOT/schemas/findings.v1.json`、`$CLAUDE_PLUGIN_ROOT/schemas/sarif-2.1.0.json`、`$CLAUDE_PLUGIN_ROOT/schemas/preflight-result.v1.json`、および各 validator/generator の絶対パスを埋め込み、`--cd ~/claude-loop-pr-codex/$dir_name` 配下の相対 `schemas/` には依存しない。

`findings.verified.json` 検証プロンプトには `$CLAUDE_PLUGIN_ROOT/schemas/findings.v1.json` の**絶対パス**（Step 2.5 で解決した `schema_path`）と同梱 validator の絶対パス（`validator_path`）を埋め込む。SARIF 検証には `sarif_schema_path` / `sarif_validator_path` / `sarif_generator_path` を使う。`preflight-result.json` 抽出/検証には `preflight_schema_path` と `preflight_validator_path` を使い、Codex の出力崩れを `PASS` と誤判定しない。

#### 4 stage と既存観点の対応

Codex は以下の 4 stage を順に判定する。各 stage は前段の結論に依存せず、毎回 `findings.verified.json` / `findings.sarif` / `review-payload.json` / `review.md` / `pr.diff` / `pr.diff.ranges.txt` / `metadata.json` / 当該 finding 抜粋を根拠として再検証する。既存観点として、Must Fix 対応、Should Fix body summary の 1:1 対応と 3 件上限、Nit の payload 混入禁止、body セクション順序も stage 内で検証する。


| Stage | 検証観点 |
| --- | --- |
| 1. `schema_validation` | `findings.verified.json` の `schema_version == "findings.v1"`、同梱 validator validation、top-level `pr.*` と `metadata.json` の一致、全 finding の `id == fingerprint` と正準 fingerprint 再計算一致、`findings.sarif` の schema validation、`canonical_must_fix == markdown_must_fix == sarif_must_fix`、および payload 側は cluster representative 集約後の Must Fix posting count と一致 |
| 2. `range_validation` | `payload.comments[]` の `path` が `metadata.json.files[]` に含まれること、`line` / `start_line` が `pr.diff.ranges.txt` の同一 hunk 範囲内にあること |
| 3. `semantic_preflight` | `payload.comments[]` が `severity == "must_fix"` の finding だけに対応すること、Should Fix / Nit / Note の inline 混入がないこと、Nit が body に混入していないこと、4 軸 + `evidence_level` gate、反証 prompt |
| 4. `payload_consistency` | `event` 判定、`body` 冒頭の `## 総評` 一致、`## 良い点` 一致、Must Fix count 整合性（cluster なし: `findings.verified.json` ↔ `review.md` ↔ `review-payload.json` ↔ `findings.sarif` が完全一致。cluster あり: canonical / review.md / SARIF は full count、`review-payload.json` と out-of-range Must Fix payload は representative expected payload count と一致）、Should Fix body summary の 1:1 対応・3 件上限・セクション順序 |

semantic preflight の反証 prompt は Must Fix finding のみに適用する。Codex は各 Must Fix finding について「この指摘が誤りである可能性」を 1 つだけ、1〜2 文で探索する。`pr.diff` / `pr.diff.ranges.txt` / `metadata.json` / 当該 finding 抜粋だけを根拠にし、反証を挙げられない場合のみ採用する。反証を挙げられた場合は `counterargument_succeeded` violation として `requires_review_regeneration=true` で報告する（反証成功 = 不採用 / FAIL）。

#### violation 分類ルール

Codex は `preflight-result.json` の `violations[]` を以下の安定 `rule` と分類で出力する。`severity == "warning"` は将来拡張用であり、M1 では top-level `verdict` / `auto_fixable_count` / `requires_human_count` にカウントしない。中間 verdict `PASS_WITH_WARNINGS` は使わず、top-level `verdict` は `PASS` / `FAIL` のみとする。

| Stage | rule | auto_fixable | requires_review_regeneration |
| --- | --- | --- | --- |
| `schema_validation` | `schema_version_mismatch` | false | true |
| `schema_validation` | `findings_validator_failed` | false | true |
| `schema_validation` | `sarif_schema_invalid` | false | true |
| `schema_validation` | `must_fix_count_mismatch` | false | true |
| `schema_validation` | `id_fingerprint_mismatch` | false | true |
| `schema_validation` | `pr_context_mismatch` | false | true |
| `range_validation` | `path_not_in_files` | true | false |
| `range_validation` | `line_out_of_hunk` | true | false |
| `range_validation` | `multi_hunk_span` | true | false |
| `semantic_preflight` | `severity_misclassification` | true | false |
| `semantic_preflight` | `non_must_fix_inline_inclusion` | true | false |
| `semantic_preflight` | `axes_gate_violation` | false | true |
| `semantic_preflight` | `evidence_level_violation` | false | true |
| `semantic_preflight` | `counterargument_succeeded` | false | true |
| `payload_consistency` | `event_mismatch` | true | false |
| `payload_consistency` | `summary_body_mismatch` | true | false |
| `payload_consistency` | `good_points_body_mismatch` | true | false |
| `payload_consistency` | `must_fix_count_mismatch_findings_vs_md` | false | true |

#### `preflight-result.json` 構造

Codex は `preflight-codex.md` の末尾付近に `### RESULT_JSON` 見出しと fenced JSON を 1 つ出力する。Claude は次の抽出コマンドで最後の `RESULT_JSON` JSON block を検証し、`~/claude-loop-pr-codex/$dir_name/preflight-result.json` として保存する。

```json
{
  "schema_version": "preflight-result.v1",
  "verdict": "PASS",
  "stages": {
    "schema_validation": {"status": "PASS", "note": "..."},
    "range_validation": {"status": "PASS", "note": "..."},
    "semantic_preflight": {"status": "PASS", "note": "..."},
    "payload_consistency": {"status": "PASS", "note": "..."}
  },
  "violations": [],
  "auto_fixable_count": 0,
  "requires_human_count": 0,
  "generated_at": "2026-05-06T00:00:00Z"
}
```

#### Codex verifier prompt file

- いつ使うか: Step 4 で `review-payload.json` を生成した直後、Codex verifier コマンドの直前に必ず作成する
- 作成方法: Write ツールで `~/claude-loop-pr-codex/$dir_name/preflight-prompt.md` に以下の prompt 本文を書き出す。`file_path` は `~` と `$dir_name` を実値へ展開した絶対パスで渡す
- 置換ルール: `{SCHEMA_PATH}` / `{VALIDATOR_PATH}` / `{SARIF_SCHEMA_PATH}` / `{SARIF_VALIDATOR_PATH}` / `{SARIF_GENERATOR_PATH}` / `{PREFLIGHT_SCHEMA_PATH}` / `{PREFLIGHT_VALIDATOR_PATH}` は Step 2.5 で保持した絶対パスへ Claude 側で置換してから書き出す。shell で prompt 本文を展開してはならない
- 理由: prompt 本文には Markdown backtick や JSON double quote が含まれるため、shell の double-quoted argument として渡すと command substitution / quote 分割で壊れる。prompt file + stdin 経由に固定し、shell は本文を解釈しない

```markdown
あなたは GitHub PR レビュー投稿前の独立検証エージェントです。Claude が生成した review-payload.json を読み、4 stage verifier pipeline として検証してください。最後に人間可読の違反一覧、RESULT_JSON の fenced JSON、そして最終行の VERDICT: PASS または VERDICT: FAIL を必ず出力してください。

目的は、GitHub Reviews API に投稿する直前の payload から、schema 不整合・範囲外コメント・semantic false positive・event/body 不整合を検出して誤投稿を防ぐことです。top-level verdict は PASS / FAIL のみで、PASS_WITH_WARNINGS は使いません。

## 入力ファイル
- review-payload.json: 投稿予定の GitHub Reviews API payload
- findings.verified.json: canonical findings（必須の source of truth）
- findings.sarif: review 側で canonical から派生した local-only SARIF v2.1.0 artifact（GitHub Code Scanning upload はしない）
- {SCHEMA_PATH}: canonical findings schema（絶対パス）
- {VALIDATOR_PATH}: 同梱 findings validator（絶対パス）
- {SARIF_SCHEMA_PATH}: OASIS SARIF v2.1.0 schema（絶対パス）
- {SARIF_VALIDATOR_PATH}: 同梱 SARIF validator（絶対パス）
- {SARIF_GENERATOR_PATH}: 同梱 SARIF generator（絶対パス）
- {PREFLIGHT_SCHEMA_PATH}: preflight-result schema（絶対パス）
- {PREFLIGHT_VALIDATOR_PATH}: preflight-result validator（絶対パス）
- review.md: 統合レビューの全文
- pr.diff: PR diff 本文。semantic preflight で finding 実在性と反証探索に使う
- pr.diff.ranges.txt: コメント可能な hunk 範囲一覧
- metadata.json: 対象 PR のメタデータ（files 配列を含む）

## 共通ルール
- findings.verified.json が存在しない場合は schema_validation の findings_validator_failed として FAIL。review.md fallback は使わない
- 各 stage は前段の結論に依存せず、上記入力ファイルだけを根拠に検証する
- finding に対する判断は、当該 finding 抜粋・pr.diff・pr.diff.ranges.txt・metadata.json のみを参照する。前ラウンドの結論や他 finding の結論だけに依存しない
- violation の rule / auto_fixable / requires_review_regeneration は prompt 内の分類表に必ず従う
- 既知 rule は severity=error とする。severity == 'warning' は分類表に無い将来拡張用 rule のみで使い、M1 では warning だけなら verdict は PASS、auto_fixable_count / requires_human_count に数えない

## STAGE 1: schema_validation
以下を確認し、STAGE 1: PASS または STAGE 1: FAIL を出力してください。
1. findings.verified.json の top-level schema_version が 'findings.v1' であること
2. 絶対パス {SCHEMA_PATH} と {VALIDATOR_PATH} を読み、可能なら `python3 {VALIDATOR_PATH} --schema {SCHEMA_PATH} --data findings.verified.json --metadata metadata.json` を実行して適合していること。実行できない場合も同梱 validator と同じ条件（required / enum / additionalProperties / allOf / if/then / format / range / fingerprint 再計算 / metadata.json との PR context 一致 / 4 軸 gate）で手動検証する。schema または validator 実体を読めない場合は FAIL
3. findings.verified.json の top-level pr.repository / pr.number / pr.head_sha / pr.base_sha が metadata.json の repository_full_name / pr_number / head_sha / base_sha と一致し、metadata.json.repository_full_name が投稿先 org/repository と一致すること
4. 全 finding で id == fingerprint が成り立ち、同梱 validator と同じ正準アルゴリズムで再計算した fingerprint と一致すること
5. findings.sarif が存在し、絶対パス {SARIF_SCHEMA_PATH} / {SARIF_VALIDATOR_PATH} で `python3 {SARIF_VALIDATOR_PATH} --schema {SARIF_SCHEMA_PATH} --data findings.sarif --findings findings.verified.json --ranges pr.diff.ranges.txt --markdown review.md --payload review-payload.json` を実行して PASS すること。存在しない場合は `python3 {SARIF_GENERATOR_PATH} --findings findings.verified.json --metadata metadata.json --ranges pr.diff.ranges.txt --output findings.sarif` で再生成してから検証してよいが、GitHub へ upload してはならない。空の `pr.diff.ranges.txt` は「コメント可能範囲なし」として扱い、非空 findings / SARIF results がある場合は生成・検証ともに FAIL とする（`--ranges` 未指定時だけ range gate 無効）
6. Must Fix 件数は full canonical count として `canonical_must_fix == markdown_must_fix == sarif_must_fix` を保つこと。payload 側は cluster representative 集約後の posting count（unclustered Must Fix + Must Fix cluster representatives）と一致すること。SARIF 側は `level == "error"` の result 件数で数える。不一致は rule=must_fix_count_mismatch, stage=schema_validation, auto_fixable=false, requires_review_regeneration=true とする

## STAGE 2: range_validation
以下を確認し、STAGE 2: PASS または STAGE 2: FAIL を出力してください。
1. payload.comments[] の各 path が metadata.json.files[] に含まれること
2. payload.comments[] の各エントリで、path と line（および start_line）が pr.diff.ranges.txt の同一 path の hunk 範囲内に収まること。複数行コメントは両端が同一 hunk に含まれる場合だけ PASS。複数 hunk をまたぐ場合は multi_hunk_span

## STAGE 3: semantic_preflight
以下を確認し、STAGE 3: PASS または STAGE 3: FAIL を出力してください。
1. payload.comments[] の各要素が findings[].severity == 'must_fix' の finding に対応すること
2. should_fix / nit / note finding が inline payload に混入していないこと。M1 posting contract として severity != 'must_fix' の finding に posting.post_policy == 'inline' が含まれていないこと
3. severity == 'must_fix' の各 finding が以下を全部満たすこと: axes.real == 'yes' / axes.triggerable == 'yes' / axes.impactful == 'yes' / (axes.general == 'yes' または evidence_level in {'impact_explained', 'verified'}) / evidence_level != 'suspicion'。python3 {VALIDATOR_PATH} の再実行に成功している場合も、この観点を明示的に PASS / FAIL として報告する
4. 反証 prompt: 各 Must Fix finding について、この指摘が誤りである可能性を 1 つだけ 1〜2 文で挙げてください。根拠は当該 finding 抜粋 / pr.diff / pr.diff.ranges.txt / metadata.json のみです。反証を挙げられない場合のみ採用し、挙げられた場合は rule=counterargument_succeeded、auto_fixable=false、requires_review_regeneration=true の violation にしてください
   - 正例: diff 上でも削除後の値が未定義になり得る経路を確認でき、反証を挙げられない → 採用 / PASS
   - 負例: metadata.json.files[] 外の既存コード前提に依存しており、この PR の diff だけでは問題が実在すると言えない → 反証成功 / FAIL

## STAGE 4: payload_consistency
以下を確認し、STAGE 4: PASS または STAGE 4: FAIL を出力してください。
1. payload.event が 'Must Fix が1件以上（body 末尾へ退避した範囲外 Must Fix も含む）→ REQUEST_CHANGES / 0件 → COMMENT' のルールに従うこと
2. payload.body の冒頭が review.md の '## 総評' セクション本文と一致すること（先頭・末尾の空白を除く）
3. payload.body 中の '## 良い点' セクションがある場合、review.md の '## 良い点' 本文と一致すること
4. findings.verified.json にある Must Fix 件数、review.md の Must Fix 見出し件数、payload.comments[] と body 末尾へ退避した Must Fix の合計件数、findings.sarif の `level=error` result 件数がすべて整合すること

## violation 分類表
- schema_version_mismatch: stage=schema_validation, auto_fixable=false, requires_review_regeneration=true
- findings_validator_failed: stage=schema_validation, auto_fixable=false, requires_review_regeneration=true
- sarif_schema_invalid: stage=schema_validation, auto_fixable=false, requires_review_regeneration=true
- must_fix_count_mismatch: stage=schema_validation, auto_fixable=false, requires_review_regeneration=true
- id_fingerprint_mismatch: stage=schema_validation, auto_fixable=false, requires_review_regeneration=true
- pr_context_mismatch: stage=schema_validation, auto_fixable=false, requires_review_regeneration=true
- path_not_in_files: stage=range_validation, auto_fixable=true, requires_review_regeneration=false
- line_out_of_hunk: stage=range_validation, auto_fixable=true, requires_review_regeneration=false
- multi_hunk_span: stage=range_validation, auto_fixable=true, requires_review_regeneration=false
- severity_misclassification: stage=semantic_preflight, auto_fixable=true, requires_review_regeneration=false
- non_must_fix_inline_inclusion: stage=semantic_preflight, auto_fixable=true, requires_review_regeneration=false
- axes_gate_violation: stage=semantic_preflight, auto_fixable=false, requires_review_regeneration=true
- evidence_level_violation: stage=semantic_preflight, auto_fixable=false, requires_review_regeneration=true
- counterargument_succeeded: stage=semantic_preflight, auto_fixable=false, requires_review_regeneration=true
- event_mismatch: stage=payload_consistency, auto_fixable=true, requires_review_regeneration=false
- summary_body_mismatch: stage=payload_consistency, auto_fixable=true, requires_review_regeneration=false
- good_points_body_mismatch: stage=payload_consistency, auto_fixable=true, requires_review_regeneration=false
- must_fix_count_mismatch_findings_vs_md: stage=payload_consistency, auto_fixable=false, requires_review_regeneration=true

## 出力フォーマット
1. Stage ごとに検証内容と `STAGE 1: PASS` / `STAGE 1: FAIL` のような判定を書く
2. `### 違反一覧` に、violation ごとの stage / rule / finding_id または comment_index / detail / auto_fixable / requires_review_regeneration を列挙する。違反がなければ「なし」
3. `### RESULT_JSON` の直後に fenced JSON を 1 個だけ出力する。JSON fence の後は最終 `VERDICT:` line 以外の本文や追加 JSON fence を出力しない。JSON は {PREFLIGHT_SCHEMA_PATH} の schema に従い、可能なら `python3 {PREFLIGHT_VALIDATOR_PATH} --schema {PREFLIGHT_SCHEMA_PATH} --from-markdown preflight-codex.md` と同じ契約を満たす形にする
4. 最終行に必ず `VERDICT: PASS` または `VERDICT: FAIL` を単独で出力する

RESULT_JSON の必須形（実際の出力ではこの object を fenced JSON として出力する）:
{
  "schema_version": "preflight-result.v1",
  "verdict": "PASS",
  "stages": {
    "schema_validation": {"status": "PASS", "note": "..."},
    "range_validation": {"status": "PASS", "note": "..."},
    "semantic_preflight": {"status": "PASS", "note": "..."},
    "payload_consistency": {"status": "PASS", "note": "..."}
  },
  "violations": [],
  "auto_fixable_count": 0,
  "requires_human_count": 0,
  "generated_at": "2026-05-06T00:00:00Z"
}
```

#### Codex verifier コマンド

- いつ使うか: `preflight-prompt.md` を Write ツールで作成した直後、Step 5 の承認プロンプト前に必ず実行する
- 判定条件: `preflight-codex.md` の最後の非空行に `VERDICT: PASS` または `VERDICT: FAIL` があり、最後の `### RESULT_JSON` 直後に fenced JSON が 1 個だけあり、その `verdict` と一致し、次の `preflight-result.json` 抽出/検証コマンドが終了コード 0 で成功する
- 次アクション: `preflight-result.json.verdict == "PASS"` なら Step 5 へ進む。`FAIL` なら下記の失敗時処理へ進む。Codex 実行または JSON 抽出/検証が失敗した場合は FAIL と同等に扱い、payload 修正では直せない出力崩れとして再試行対象にする（最大 3 回）
- prompt は `exec` の `-` 引数と stdin redirection で渡す。Bash ツールへ渡すコマンド文字列に prompt 本文を直接埋め込んではならない

```bash
codex \
  --ask-for-approval never \
  -m gpt-5.5 \
  -c sandbox_mode=read-only \
  exec \
  --ignore-user-config \
  --skip-git-repo-check \
  --cd ~/claude-loop-pr-codex/$dir_name \
  - \
  <  ~/claude-loop-pr-codex/$dir_name/preflight-prompt.md \
  >  ~/claude-loop-pr-codex/$dir_name/preflight-codex.md \
  2> ~/claude-loop-pr-codex/$dir_name/preflight-codex.log
```

フラグの説明:

- `--ask-for-approval never` / `-m gpt-5.5` / `-c ...` は global flag のため、すべて `exec` の前に置く
- `-c sandbox_mode=read-only` — シェル実行を read-only サンドボックスに固定する。`--sandbox read-only` と等価だが、config override として明示するため `-c` に統一する
- `--ignore-user-config` — 投稿前検証中のみ `$CODEX_HOME/config.toml` / `~/.codex/config.toml` を読み込まない。auth は引き続き `CODEX_HOME` を使うため、古い MCP 設定や無効な `model_reasoning_effort` による config 検証エラーから Step 4.5 preflight を切り離せる
- `--skip-git-repo-check` / `-C, --cd` は `exec` サブコマンド側の option のため、`exec` の後ろ、かつ prompt の前に置く
- `--color never` / `--ephemeral` はテンプレートを簡素化するため使わない。カラーは TTY 自動判定に任せ、セッション保存挙動は config 側に委ねる

#### RESULT_JSON 抽出・検証コマンド

- いつ使うか: 上の Codex verifier コマンドが終了した直後に必ず実行する
- 判定条件: 終了コード 0 かつ `preflight-result.json` が非空で、final `VERDICT:` line と `preflight-result.json.verdict` が一致し（一致しなければ失敗）、`schema_version == "preflight-result.v1"` / `verdict in {"PASS","FAIL"}` / 4 stage / violation count の cross-field validation を満たす
- 次アクション: 成功なら `preflight-result.json` を Read ツールで取得し、`verdict` と count を確認する。`RESULT_JSON` 見出し欠落、最後の `RESULT_JSON` 見出し後の JSON fence 欠落、追加 JSON fence / 余分な本文、final `VERDICT:` line との不一致、または schema/cross-field validation 失敗時は Codex 出力が構造化契約に違反したものとして Step 4.5 FAIL と扱い、最大 3 回まで再試行する

```bash
python3 $preflight_validator_path --schema $preflight_schema_path --from-markdown ~/claude-loop-pr-codex/$dir_name/preflight-codex.md --emit-json > ~/claude-loop-pr-codex/$dir_name/preflight-result.json && test -s ~/claude-loop-pr-codex/$dir_name/preflight-result.json
```

#### 失敗時の Claude 側の処理

`preflight-result.json` の `verdict == "FAIL"` の場合、Claude は `violations[]` を読み、以下の優先順で分岐する。

1. `requires_human_count > 0` の場合は即中断し、ユーザーに以下を報告する。自動で該当 finding を握りつぶして投稿してはならない
   - `preflight-result.json` と `preflight-codex.md` のパス
   - `requires_review_regeneration == true` の違反一覧（`rule` / `finding_id` / `detail`）
   - 「review 側の `findings.verified.json` / `review.md` 再生成が必要」という旨
2. `requires_human_count == 0` かつ `auto_fixable_count > 0` かつ試行回数 < 3 の場合だけ、`auto_fixable == true` の violation を rule 別に適用し、`review-payload.json` を再生成して Step 4.5 を再実行する
   - `path_not_in_files` / `line_out_of_hunk` / `multi_hunk_span`: 該当 comment を `comments[]` から除外し、Must Fix は body 末尾の `## 行コメント不可 (diff 範囲外)` へ退避する
   - `severity_misclassification` / `non_must_fix_inline_inclusion`: 該当 comment を `comments[]` から除外する
   - `event_mismatch`: Step 4 の event ルールで再計算する
   - `summary_body_mismatch` / `good_points_body_mismatch`: `review.md` の `## 総評` / `## 良い点` を再 parse し body を再生成する
3. 3 回連続 FAIL、または auto-fix 後も FAIL が解消しない場合は自動投稿を中止する

#### 3 回連続 FAIL 時の処理

- 自動再投稿は中止し、ユーザーに以下を報告して終了する:
  - `preflight-result.json` のパス
  - `preflight-codex.md` のパス
  - 最新の `review-payload.json` のパス
  - 「verifier pipeline が 3 回連続で FAIL したため自動投稿を中止」という旨

### Step 5: 承認プロンプト

Step 3.75 の Should Fix body inclusion opt-in（候補がある場合のみ表示）と Step 4.5 の Codex セルフレビューを終えた後、投稿前の最終確認として以下のサマリをテキストで提示し、明示的な承認を求める:

```
対象 PR: <$pr_url> (<$title>)
event: <REQUEST_CHANGES | COMMENT>
findings source: ~/claude-loop-pr-codex/<$dir_name>/findings.verified.json
review file: ~/claude-loop-pr-codex/<$dir_name>/review.md
SARIF artifact: ~/claude-loop-pr-codex/<$dir_name>/findings.sarif (local-only, Code Scanning upload なし)
body プレビュー:
  <$summary の先頭 200 文字。長ければ "..." で省略>
インラインコメント: Must Fix N 件
Should Fix body summary: included <yes|no> (<included_count>/<candidate_count> 件、default: no)
Nit artifact: <~/claude-loop-pr-codex/<$dir_name>/nits.md | nit: 0 件>
（Should Fix は opt-in された上位 3 件のみ body に含めます。Nit は PR には載せず nits.md にのみ残します）
行範囲外で除外したインラインコメント (Must Fix のみ): K 件
  - <path>:L<line> (本文末尾の「行コメント不可」セクションに移動)
payload: ~/claude-loop-pr-codex/<$dir_name>/review-payload.json
preflight result: ~/claude-loop-pr-codex/<$dir_name>/preflight-result.json
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
- preflight result: `~/claude-loop-pr-codex/sent/$dir_name-$head_sha_short/preflight-result.json`
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
- `findings.verified.json` が空 / JSON parse 失敗 / top-level object でない / `findings[]` 不在または非配列 → ユーザーに通知して処理中断（Markdown fallback へは切り替えない、`sent/` 移動もしない）
- `findings.verified.json` が存在するのに `schema_version != findings.v1` → ユーザーに通知して処理中断
- `findings.verified.json` の schema / fingerprint validation が同梱 validator + `schemas/findings.v1.json` で失敗 → ユーザーに通知して処理中断（Markdown fallback へは切り替えない）
- `findings.verified.json.pr.*` が `metadata.json` の投稿先 repo / PR number / head/base SHA と一致しない → ユーザーに通知して処理中断（Markdown fallback へは切り替えない）
- `findings.verified.json` の Must Fix 件数と `review.md` の Must Fix 見出し件数が不一致 → ユーザーに通知して処理中断（Markdown fallback へは切り替えない）
- `findings.sarif` が存在しない、または `tasks/validate_findings_sarif.py --schema $sarif_schema_path --data findings.sarif --findings findings.verified.json --ranges pr.diff.ranges.txt --markdown review.md --payload review-payload.json` に失敗 → schema_validation FAIL として投稿を中断する。`findings.sarif` は local-only artifact であり、M2 では upload しない
- `findings.verified.json` の Must Fix に `location.side != RIGHT` が含まれる → ユーザーに通知して処理中断（M1 では old-side 投稿を扱わない）
- `findings.verified.json` の Must Fix に `posting.post_policy != inline` または `explanation_postable != true` が含まれる → ユーザーに通知して処理中断（M1 では安全に自動投稿しない）
- `review.md` に Must Fix が一件も無い → それでも `event: COMMENT` + body (総評 + 良い点 + opt-in された Should Fix body summary) のみで投稿する（インラインコメント配列は空）
- `review.md` の `## 総評` セクションが空 or 見つからない → ユーザーに通知して処理中断。`sent/` 移動は行わない
- Step 3.5 で `pr.diff.ranges.txt` が空 → インラインコメント候補はすべて body 末尾の `## 行コメント不可 (diff 範囲外)` に移動し、`comments` 配列には含めない
- Step 4.5 の Codex verifier が `RESULT_JSON` を出力しない、最後の `RESULT_JSON` 見出しが dangling、`RESULT_JSON` 後に追加 JSON fence / 余分な本文を出す、final `VERDICT:` line と JSON verdict が一致しない、または `tasks/validate_preflight_result.py` が `preflight-result.json` validation に失敗 → 構造化 preflight 失敗として最大 3 回まで再試行し、解消しなければ投稿を中止
- Step 4.5 の `preflight-result.json.verdict == "FAIL"` かつ `requires_human_count > 0` → review 側の再生成が必要として即中断し、`preflight-result.json` / `preflight-codex.md` のパスと違反一覧を提示
- `gh api` 422/403/404 → Step 8 の失敗報告で分岐し、`sent/` 移動は行わない
- Step 7 で `sent/$dir_name-$head_sha_short/` がすでに存在 → ユーザーに通知して処理中断（投稿はすでに完了している点に注意）。`sent/` 移動は行わず、`review-response.json` を残した状態で終了する
- Step 7 の移動完了検証が失敗 → `mv` が silent に失敗した可能性があるため Step 8 の失敗報告で手動確認を促し、`review-response.json` を残した状態で終了する
- ユーザーが Step 5 で承認を拒否 → 何もせず終了。payload ファイルは残す

## 実装上の制約

本スキルは対話実行を前提とし、通常の permission mode で使うことを想定する（`/loop` には載せない）。ただし既存 `/pr-codex:review` と統一感を持たせるため、以下の原則を踏襲する:

1. 各テンプレートは 1 テンプレート = 1 シェル実行単位として扱う
2. テンプレートの改変は変数置換のみ許可する。フラグ、引数順、引用符、リダイレクトはテンプレート記載どおりに使う
3. シェル演算子はテンプレート中に明示された `|` `<` `>` `2>` `&&` のみ許可する
4. `findings.verified.json` は必須の一次入力とし、`review.md` parser fallback は使わない。parse failure / shape failure / validator failure / `location.side != RIGHT` / 件数不一致 / posting policy 不整合時に Markdown fallback へ自動切替してはならない
5. payload JSON、`preflight-prompt.md`、`nits.md` の生成は Write ツールで行う（`jq -n` によるインラインでの複雑な配列組み立てや shell 文字列内 prompt 埋め込みは使わない）
6. `$()` / `for` / `while` / `xargs` / ヒアドキュメントは使わない
7. `mv` は `sent/` への移動以外では使わない
8. `gh` の write 系操作は `gh api --method POST .../reviews` のみとし、`gh pr review` / `gh pr comment` / `gh pr merge` などは使わない
9. 1 回の実行で処理する対象ディレクトリは 1 件のみとする
10. 投稿前の Step 5 承認プロンプトは必須。自動投稿はしない。Should Fix body summary は default no とし、Step 3.75 で明示 opt-in された場合だけ上位 3 件を body に含める
11. Step 3 の `python3 $CLAUDE_PLUGIN_ROOT/tasks/validate_findings.py ...` を **必ず**実行する。`findings.verified.json` 欠落または validator 失敗時に payload 生成や Markdown fallback へ進んではならない
12. Step 4.5 の verifier pipeline は **必須**。スキップしてはならない。`preflight-result.json.verdict == "PASS"` と `preflight-codex.md` の `VERDICT: PASS` を確認するまで Step 5 に進まない。schema 検証観点では `$CLAUDE_PLUGIN_ROOT/schemas/findings.v1.json`、`$CLAUDE_PLUGIN_ROOT/schemas/sarif-2.1.0.json`、`$CLAUDE_PLUGIN_ROOT/schemas/preflight-result.v1.json` の絶対パスを prompt / 抽出コマンドに埋め込み、`--cd` 配下の相対 `schemas/` には依存しない

## ファイル構成

スキル本体:

```
$CLAUDE_PLUGIN_ROOT/skills/send/
  └── SKILL.md                ← 本ファイル
$CLAUDE_PLUGIN_ROOT/tasks/
  ├── validate_findings.py        ← findings.verified.json の schema / fingerprint / format / range validator
  ├── generate_findings_sarif.py  ← findings.verified.json から local-only SARIF を生成
  ├── validate_findings_sarif.py  ← findings.sarif の schema / count consistency validator
  └── validate_preflight_result.py ← preflight-result.json の抽出 / schema / cross-field validator
$CLAUDE_PLUGIN_ROOT/schemas/
  ├── findings.v1.json
  ├── sarif-2.1.0.json
  └── preflight-result.v1.json
```

実行時の作業ディレクトリ (投稿前):

```
~/claude-loop-pr-codex/
  └── $org-$repository-$pr_number/
        ├── status.json            ← state:completed
        ├── metadata.json
        ├── findings.verified.json  ← primary input (`schemas/findings.v1.json`)
        ├── findings.sarif          ← local-only SARIF v2.1.0 artifact（upload しない）
        ├── validation-report.json  ← review 側の副成果物（あれば保持）
        ├── review.md              ← 投稿元
        ├── pr.diff
        ├── pr.diff.ranges.txt     ← Step 3.5 で生成するコメント可能行範囲
        ├── claude-review.md
        ├── codex-review.md
        ├── claude.log
        ├── preflight-prompt.md     ← Step 4.5 の Codex verifier prompt（Write ツールで生成）
        ├── preflight-codex.md      ← Step 4.5 の人間可読 verifier 結果（VERDICT: PASS/FAIL）
        ├── preflight-result.json   ← Step 4.5 の構造化 verifier 結果 (`schemas/preflight-result.v1.json`)
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
              ├── findings.sarif          ← local-only SARIF。GitHub Code Scanning upload は自動化しない
              ├── validation-report.json
              ├── review.md
              ├── review-payload.json    ← 追加: 投稿した payload
              ├── review-response.json   ← 追加: gh api のレスポンス (.html_url 等を含む)
              ├── pr.diff
              ├── pr.diff.ranges.txt
              ├── claude-review.md
              ├── codex-review.md
              ├── claude.log
              ├── preflight-prompt.md
              ├── preflight-codex.md
              ├── preflight-result.json
              ├── preflight-codex.log
              ├── nits.md                ← Nit があった場合のみ。他 artifact と一緒に移動される
              └── codex.log
```
