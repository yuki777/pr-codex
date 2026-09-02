---
user-invocable: true
name: pr-codex-send
description: "/pr-codex:review で生成された統合レビュー(review.md)を GitHub PR にレビューコメントとして投稿し、処理済みディレクトリを sent/ に移動する"
argument-hint: "[<PR URL|PR number>] [--auto-send] [--include-should-fix] [--include-nit]"
allowed-tools: ["Bash", "Read", "Write", "Glob", "Grep"]
---

# pr-codex-send

`/pr-codex:review` が生成した canonical findings (`findings.verified.json`) と統合レビュー (`review.md`) を使って GitHub PR にレビューコメントを投稿し、処理済みディレクトリを `~/claude-loop-pr-codex/sent/` に移動する。

## 前提

- `/pr-codex:review` が先に実行されており、`~/claude-loop-pr-codex/<org>-<repository>-<pr_number>/` 配下に `status.json` (`state:completed`) / `metadata.json` / `findings.verified.json` / `review.md` が揃っている
- `ci-status.json` / `ci-summary.md` が存在する場合は、投稿前判断の read-only CI context として参照する。`failure` / `pending` を理由に GitHub workflow の rerun / cancel / write は行わず、Must Fix 0 件の自動 `APPROVE` は `COMMENT` に抑止してユーザーへ CI 状態を説明する
- 投稿アカウントが PR 作者と同一（self-PR）の場合、GitHub Reviews API は `APPROVE` / `REQUEST_CHANGES` を 422 で拒否するため、Step 2b で self-PR を検知し event を `COMMENT` に抑止する。投稿者 identity を判定できない場合は投稿前に中断する（fail-closed）
- `findings.verified.json` を **必須の一次入力** とする。M1 の F13 以降、`review.md` parser への Markdown fallback は使わない
- GitHub CLI (`gh`) がログイン済みで、対象 PR にレビュー投稿権限がある (`gh auth status` で確認可能)
- `jq` が利用可能

## 使い方

```
# Default（自動抽出）: 承認ストップありで、Must Fixのみを inline comment する
/pr-codex:send

# 承認ストップ無しで、Must Fixのみを inline comment する
/pr-codex:send --auto-send

# 承認ストップありで、Must FixとShould Fixを inline comment する
/pr-codex:send --include-should-fix

# 承認ストップ無しで、Must FixとShould FixとNitを inline comment する
/pr-codex:send --auto-send --include-should-fix --include-nit

# 直前にレビューした特定 PR を、Must Fix のみ承認なしで投稿する
/pr-codex:send https://github.com/org/repo/pull/123 --auto-send

# 特定 PR の Must Fix + Should Fix を承認なしで投稿する
/pr-codex:send https://github.com/org/repo/pull/123 --auto-send --include-should-fix

# PR 番号のみ指定（~/claude-loop-pr-codex に同番号の dir が1件だけのとき有効）
/pr-codex:send 123 --auto-send
```

引数なしは従来どおり `~/claude-loop-pr-codex` 直下から名前昇順の先頭 completed レビューを自動抽出し、対話実行を前提として Step 5 で投稿 payload のサマリを提示してユーザーの明示的な承認を得てから Step 6 で投稿する。PR URL または PR 番号を位置引数で 1 つ指定した場合は、その completed レビューだけを対象にする。`--auto-send` は Step 5 の最終投稿承認だけをスキップし、すべての validator / Step 4.5 preflight / Step 5.5 投稿直前 safety gate が成功した場合のみ Step 6 へ進む。旧 `--auto-submit` は互換エイリアスとして同じ mode に正規化するが、表示と案内では `--auto-send` を使う。`--include-should-fix` は投稿可能な Should Fix を inline comment に含め、`--include-nit` は投稿可能な Nit も inline comment に含める（`--include-nit` は `--include-should-fix` との併用必須）。diff 範囲外のものは body の `## 行コメント不可 (diff 範囲外)` へ退避する。unknown option、解釈できない位置引数、位置引数が2つ以上、重複オプション、または無効な組み合わせは unsupported argument として中断する。

1 回の実行で対象は 1 件のみ処理する。位置引数なしの場合、未投稿の completed レビューが複数あっても `ls` の出力順（名前昇順）で最初の 1 件のみを処理し、残りは次回以降の `/pr-codex:send` 実行に委ねる。位置引数ありの場合、auto 選定は行わず、指定 PR に対応する review directory だけを検証する。

## フロー

各テンプレートはコードブロックの内容をそのまま 1 回のシェル実行単位として使う。変数（`$candidate`, `$dir_name`, `$org`, `$repository`, `$pr_number`, `$pr_url`, `$head_sha`, `$head_sha_short`, `$title`, `$review_url`, `$plugin_root` など）の置換以外の改変は不可。`$plugin_root` には Step 1 common で解決した絶対パスの実値を使う。

### Step 0: 引数解析

Skill 起動直後に `$ARGUMENTS` を shell 風に空白分割して解釈し、`$send_mode = interactive | auto_send`、`$include_should_fix = true | false`、`$include_nit = true | false`、`$target_mode = auto | direct` に正規化する。フラグと位置引数は順不同で指定できる。

- `$ARGUMENTS` が空文字列または空白のみ: `$send_mode=interactive` / `$include_should_fix=false` / `$include_nit=false` / `$target_mode=auto`
- `--auto-send` が含まれる: `$send_mode=auto_send`
- 旧 `--auto-submit` が含まれる: 互換エイリアスとして `$send_mode=auto_send` に正規化する
- `--auto-send` と `--auto-submit` のどちらも含まれない: `$send_mode=interactive`
- `--include-should-fix` が含まれる: `$include_should_fix=true` とし、投稿可能な Should Fix 候補を inline comment 対象にする
- `--include-nit` が含まれる: `$include_nit=true` とし、投稿可能な Nit 候補を inline comment 対象にする。ただし `--include-nit` は `--include-should-fix` なしでは unsupported argument として中断する（--include-nit は --include-should-fix なしでは unsupported argument）
- 位置引数なし: `$target_mode=auto`。従来どおり Step 1 で `ls` 名前昇順の先頭 completed レビューを選定する
- `https://github.com/<org>/<repo>/pull/<number>` 形式の位置引数 1 つ: `$target_mode=direct` とし、URL から `$org` / `$repository` / `$pr_number` を取り出す。`$target_dir_name = "<org>-<repository>-<pr_number>"` として Step 1 の direct 分岐へ進む
- `<number>`（数字のみ）の位置引数 1 つ: `$target_mode=direct` とし、`$target_pr_number=<number>` を保持する。Step 1 で `~/claude-loop-pr-codex` 直下（`sent` 除く）の末尾セグメントが `-<number>` に一致する directory を解決する
- 未知オプション、解釈できない位置引数、位置引数が2つ以上、同じオプションの重複、`--auto-send` と `--auto-submit` の併用、または `--include-nit` 単独のような無効な組み合わせ: `unsupported argument` として中断し、Step 1 以降の payload 生成や GitHub write は行わない

`--auto-send`（および互換エイリアス `--auto-submit`）は Step 5 の最終投稿承認だけを省略するモードであり、severity inclusion (`--include-should-fix` / `--include-nit`)、canonical artifact validation、SARIF validation、Step 4.5 verifier pipeline、head SHA 再確認、二重投稿防止 gate は省略しない。

### Step 1: 対象ディレクトリの選定

#### common: plugin root / validator path の早期解決

direct mode / auto mode のどちらでも Step 2.5 と Step 3 の validator path 解決に使うため、対象 directory の選定前に plugin root を解決する。fallback block はこの 1 箇所にだけ置き、末尾の `printf` が出力する解決済み plugin root の絶対パス 1 行を保持する。各テンプレートは 1 シェル実行単位でありシェル変数は持ち越されないため、以降のフロー内テンプレートに現れる `$plugin_root` は置換対象変数として扱い、Bash ツールへ渡す前に解決済みの絶対パス実値へ置換する。値を確定できない場合は、この fallback block を単独で再実行してから当該テンプレートをやり直す。

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
test -d "$plugin_root/tasks" && test -d "$plugin_root/schemas" && printf '%s\n' "$plugin_root"
```

#### direct mode（PR URL / PR 番号指定）

`$target_mode=direct` の場合、`ls` 走査による自動選定はスキップし、指定 PR に対応する directory だけを対象にする。`$target_dir_name` が URL 指定で既に確定している場合は存在確認へ進む。PR 番号のみ指定の場合は、`~/claude-loop-pr-codex` 直下（`sent` 除く）の directory 名の末尾セグメントが `-<number>` に一致するものを解決する。repo 名にハイフンを含んでも、PR 番号は directory 名の最後の `-` 区切りセグメントとして扱う。

- いつ使うか: `$target_mode=direct` かつ PR 番号のみ指定の場合
- 判定条件: 標準出力に末尾 `-<number>` に一致する active directory が列挙される
- 次アクション:
  - ちょうど 1 件なら、その行を `$target_dir_name` として保持し存在確認へ進む
  - 複数件なら曖昧として中断し、PR URL 指定を案内する
  - 0 件なら、後続の sent 済み / 未レビュー判定へ進む

```bash
ls -1 ~/claude-loop-pr-codex | grep -v '^sent$' | grep -v 'clear.sh' | awk -F- -v pr="$target_pr_number" 'NF >= 2 && $NF == pr {print}'
```

- いつ使うか: `$target_mode=direct` で `$target_dir_name` が確定した直後
- 判定条件: 対象 directory が存在する
- 次アクション: 存在すれば `status.json` 確認へ進む。存在しなければ sent 済み / 未レビュー判定へ進む

```bash
test -d ~/claude-loop-pr-codex/$target_dir_name
```

- いつ使うか: URL 指定または PR 番号指定で active directory が存在しない場合
- 判定条件: URL 指定なら `sent/$target_dir_name-*` が存在する。PR 番号指定なら `sent/*-<number>-*` が存在する
- 次アクション: 一致があれば「指定 PR は既に send 済み（`sent/` にある）」と報告して中断する。なければ「指定 PR の completed レビューが無い。先に `/pr-codex:review <PR URL>` を実行」と案内して中断する

```bash
test -d ~/claude-loop-pr-codex/sent && ls -1 ~/claude-loop-pr-codex/sent | awk -v prefix="$target_dir_name-" 'index($0, prefix) == 1 {print}'
```

```bash
test -d ~/claude-loop-pr-codex/sent && ls -1 ~/claude-loop-pr-codex/sent | awk -F- -v pr="$target_pr_number" 'NF >= 3 && $(NF - 1) == pr {print}'
```

- いつ使うか: `$target_mode=direct` で対象 directory が存在する場合
- 判定条件: `status.json` が存在する
- 次アクション: 存在すれば state 判定へ。存在しなければ「指定 PR の completed レビューが無い。先に `/pr-codex:review <PR URL>` を実行」と案内して中断する

```bash
test -f ~/claude-loop-pr-codex/$target_dir_name/status.json
```

- いつ使うか: direct mode の `status.json` が存在する場合
- 判定条件: 出力が `completed`
- 次アクション: `completed` なら `review.md` 存在確認へ。それ以外 (`running` / `failed`) は理由を添えて中断し、auto 選定や `review.md` parser fallback には切り替えない

```bash
jq -r '.state' ~/claude-loop-pr-codex/$target_dir_name/status.json
```

- いつ使うか: direct mode で `state == "completed"` の場合
- 判定条件: `review.md` が存在する
- 次アクション: 存在すれば `findings.verified.json` 存在確認へ。存在しなければ必須成果物欠落として中断する

```bash
test -f ~/claude-loop-pr-codex/$target_dir_name/review.md
```

- いつ使うか: direct mode で `review.md` が存在する場合
- 判定条件: `findings.verified.json` が存在する
- 次アクション: 存在すれば `$dir_name = $target_dir_name` として Step 2 へ進む。存在しなければ F13 の必須入力欠落として中断し、Markdown fallback へは切り替えない

```bash
test -f ~/claude-loop-pr-codex/$target_dir_name/findings.verified.json
```

#### auto mode（位置引数なし）

`$target_mode=auto` の場合は、従来どおり `~/claude-loop-pr-codex` 直下を `ls` 名前昇順で走査し、最初の completed レビュー 1 件だけを対象にする。

- いつ使うか: `$target_mode=auto` の場合のみ実行する
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
- 次アクション: 存在するなら `findings.verified.json` を Read ツールで取得して Step 2b へ。存在しなければ F13 必須入力欠落としてユーザーへ通知し中断する（Markdown fallback へは切り替えない）

```bash
test -f ~/claude-loop-pr-codex/$dir_name/findings.verified.json
```

### Step 2b: self-PR 検知（read-only）

投稿 event の決定は投稿者 identity に依存するため、Step 2 の値取得後・Step 2.5 の前に、投稿アカウントと PR 作者を read-only で照合する。GitHub Reviews API はレビュー投稿者自身が作成した PR への `APPROVE` / `REQUEST_CHANGES` を 422 で拒否するため、self-PR では Step 4 の builder が event を常に `COMMENT` に抑止する（inline の Must Fix コメントは `COMMENT` イベントでも投稿できるため維持する）。判定不能のまま続行してはならない（fail-closed）。

- いつ使うか: Step 2 で `$org` / `$repository` / `$pr_number` を保持し `findings.verified.json` を読み込んだ直後、Step 2.5 の前に必ず実行する。Step 2b をスキップして Step 2.5 / Step 3 へ進んではならない
- 判定条件: 1 つのテンプレート内にある 2 つの read-only API 呼び出しがともに終了コード 0 で非空のログイン名 1 行を返し、テンプレート全体が `true` または `false` の 1 行を返す
- 次アクション: 終了コード 0 の標準出力を `$self_review=true|false` として保持し、Step 2.5 へ進む。どちらか一方でも失敗（非ゼロ終了、空出力、または複数行出力）した場合はテンプレート自身が失敗した API と `gh auth status` の確認・再実行手順を報告して非ゼロ終了するため、**投稿前に中断** する。builder / Step 4.5 preflight は実行せず、`sent/` 移動も行わない

```bash
poster_login="$(gh api user --jq '.login')" || {
  printf '%s\n' 'self-PR identity 取得失敗: gh api user。gh auth status を確認し、/pr-codex:send を再実行してください。' >&2
  exit 1
}
if [ -z "$poster_login" ] || [ "$(printf '%s\n' "$poster_login" | wc -l | tr -d '[:space:]')" != "1" ]
then
  printf '%s\n' 'self-PR identity 取得失敗: gh api user。gh auth status を確認し、/pr-codex:send を再実行してください。' >&2
  exit 1
fi
pr_author_login="$(gh api "repos/$org/$repository/pulls/$pr_number" --jq '.user.login')" || {
  printf 'self-PR identity 取得失敗: gh api repos/%s/%s/pulls/%s。gh auth status を確認し、/pr-codex:send を再実行してください。\n' "$org" "$repository" "$pr_number" >&2
  exit 1
}
if [ -z "$pr_author_login" ] || [ "$(printf '%s\n' "$pr_author_login" | wc -l | tr -d '[:space:]')" != "1" ]
then
  printf 'self-PR identity 取得失敗: gh api repos/%s/%s/pulls/%s。gh auth status を確認し、/pr-codex:send を再実行してください。\n' "$org" "$repository" "$pr_number" >&2
  exit 1
fi
if [ "$poster_login" = "$pr_author_login" ]
then
  self_review=true
else
  self_review=false
fi
printf '%s\n' "$self_review"
```

### Step 2.5: plugin root / schema / validator path の解決

Step 3 と Step 4.5 の verifier pipeline で `{SCHEMA_PATH}` / `{VALIDATOR_PATH}` / `{SARIF_SCHEMA_PATH}` / `{SARIF_VALIDATOR_PATH}` / `{SARIF_GENERATOR_PATH}` / `{PREFLIGHT_SCHEMA_PATH}` / `{PREFLIGHT_VALIDATOR_PATH}` / `{SEMANTIC_SCHEMA_PATH}` に埋め込むため、ここで各 path を保持する。`CLAUDE_PLUGIN_ROOT` が未設定・不明な場合も、冒頭の `plugin_root` fallback block で plugin root を自己解決する。

保持する値:

- `schema_path = <plugin-root>/schemas/findings.v1.json`
- `validator_path = <plugin-root>/tasks/validate_findings.py`
- `sarif_schema_path = <plugin-root>/schemas/sarif-2.1.0.json`
- `sarif_validator_path = <plugin-root>/tasks/validate_findings_sarif.py`
- `sarif_generator_path = <plugin-root>/tasks/generate_findings_sarif.py`
- `preflight_schema_path = <plugin-root>/schemas/preflight-result.v1.json`
- `preflight_validator_path = <plugin-root>/tasks/validate_preflight_result.py`
- `semantic_schema_path = <plugin-root>/schemas/preflight-semantic.v1.json`
- `payload_builder_path = <plugin-root>/tasks/build_review_payload.py`

### Step 3: `findings.verified.json` の解析 (primary)

`findings.verified.json` を **必須の一次情報源**として payload を組み立てる。`review.md` は `## 良い点` の本文取得と、`## 総評` の非空 gate、Must Fix 件数 gate の確認にだけ使う。投稿 body 先頭の総評（`$posted_summary`）は builder が投稿対象の finding だけから決定論的に生成し、`review.md` の自由文総評は GitHub へ投稿しない（投稿対象外の Should Fix / Nit への言及や非公開情報の混入を防ぐ。#120）。payload の抽出・整形・振り分けは Step 4 の `build_review_payload.py` が決定論的に行い、Claude がメモリ上で payload を組み立てることはしない。まず Step 2.5 で保持した `validator_path` / `schema_path` を使い、`findings.verified.json` がその schema に適合するかを review 側と同じ同梱 validator で外部検証してから進む。

#### 同梱 validator コマンド

- いつ使うか: `findings.verified.json` 解析の開始直後、JSON 抽出や payload 生成の前に必ず実行する
- 判定条件: 終了コード 0
- 次アクション: 成功なら Read ツールで `findings.verified.json` を読み Step 3 の抽出へ進む。失敗ならユーザーに通知して中断し、Markdown fallback へは切り替えない
- `validator_path` / `schema_path` は Step 2.5 で `plugin_root` から組み立てた値を使う。Step 4.5 のプロンプトにも同じ絶対パスを埋め込む

```bash
python3 $validator_path --schema $schema_path --data ~/claude-loop-pr-codex/$dir_name/findings.verified.json --metadata ~/claude-loop-pr-codex/$dir_name/metadata.json
```

この Step で Claude が保持するのは以下だけとする（payload 用の抽出は行わない）:

- `findings.verified.json` を Read し、`findings[]` のうち `severity == "nit"` の要素を `$nit_findings` 配列として `nits.md` 用に抽出する
- `findings.verified.json` の `severity == "must_fix"` 件数を `$count_must` として保持する（Step 4 の builder 出力サマリとの突き合わせ、および Step 5 の承認サマリ用）

以下の抽出・検証は `build_review_payload.py` の責務であり、builder が fail-closed で実施する:

- `review.md` から: `## 総評` 直下の本文（後続セクション見出しの直前まで。前後の空行はトリム）は非空 gate にのみ使う（空なら中断。投稿 body へは転記しない）、`## 良い点` 直下の本文 → `$good_points`、`## 重大な問題 (Must Fix)` 配下の `### ...` 見出し数 → `$must_fix_markdown_count`
- `findings.verified.json` から:
  - top-level `schema_version` が **`findings.v1`** であり、`findings[]` が array であること
  - top-level `pr.repository` / `pr.number` / `pr.head_sha` / `pr.base_sha` が `metadata.json.repository_full_name` / `metadata.json.pr_number` / `metadata.json.head_sha` / `metadata.json.base_sha` と一致し、`metadata.json.repository_full_name` が `$org/$repository` と一致すること
  - `findings[]` のうち `severity == "must_fix"` の要素を `$must_fix` 配列として抽出する
  - top-level `root_cause_clusters[]` がある場合は各 cluster の `representative_finding_id` を representative posting 対象として扱う。cluster member は canonical finding としては残し、GitHub inline comment としては代表 finding の body に集約する
  - `findings[]` のうち `severity == "should_fix" && posting.post_policy == "body_summary" && posting.explanation_postable == true` の要素を `$should_fix_candidates` 配列として抽出する。順序は `findings[]` の登場順を保ち、`$include_should_fix == true` のときだけ inline 候補として使う
  - `severity == "nit"` の inline/fallback 候補は `posting.post_policy == "body_summary" && posting.explanation_postable == true` の要素だけを `$nit_inline_candidates` として扱う
  - M1 の投稿 contract として、`severity != "must_fix"` の finding は canonical 側の `posting.post_policy` を変更せず、明示オプション指定時だけ send 側で `body_summary` かつ postable な finding を inline comment に昇格できる
  - `category == "security"` の finding は `security` extension を必須とし、`security.severity == "critical" | "high"` または `security.disclosure_policy != "inline_safe"` の場合は inline 投稿対象から除外する。公開 body に含める場合も `public_safe_summary` レベルの安全な説明だけを使う

#### `findings.verified.json` から抽出するフィールド

builder は各 Must Fix finding から以下を payload 用に組み立てる。`root_cause_clusters[]` がない finding は従来どおり個別 inline comment にする。cluster member のうち `representative_finding_id` ではない finding は duplicate inline comment としては投稿せず、代表 finding の `body` に affected findings summary として path/line/problem を最大 5 件まで短く含める（超過分は `他 N 件` として数だけ示す）。掲載する member は `posting.post_policy == "body_summary" | "inline"` かつ `posting.explanation_postable == true` かつ severity が active severity flags で許可されたもの（must_fix は常時、should_fix / nit はオプション指定時のみ、note は掲載しない）に限り、`local_only` / `suppress` / `explanation_postable == false` / disclosure_policy が `local_only` の member は内容を掲載せず `他 N 件` の数にだけ含める。canonical / review.md / SARIF / preflight の Must Fix count は cluster member を含む full finding count を使い、代表数に減らして数えない:

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

builder は各 Should Fix inline 候補から以下を保持する:

| 出力キー        | 値 |
| --------------- | --- |
| `path`          | `location.path` |
| `line`          | `location.end_line` があればその値、なければ `location.start_line` |
| `heading_markdown` | ``### `path:L<行番号>` `` または ``### `path:L<開始>-L<終了>` `` |
| `summary_line`  | `problem` を 1 行に畳み込んだ改善内容 |
| `suggestion_line` | `suggestion` を 1 行に畳み込んだ提案 |
| `source_finding_id` | finding の `id` |

`$should_fix_candidates` は `location.side` にかかわらず保持し、LEFT-side finding は Step 3.5 で GitHub inline comment に変換せず body 退避対象にする。基礎条件は `severity == "should_fix" && posting.post_policy == "body_summary" && posting.explanation_postable == true` であり、RIGHT-side guard は抽出時ではなく Step 3.5 の inline 可否判定で適用する。`$should_fix_candidates` の上位判定は `findings[]` の配列順に固定し、send 側で severity / category / path などによる再ソートは行わない。`$include_should_fix == true` の場合は範囲検証を通った全件のうち、`location.side == "RIGHT"` のものだけを `$inline_should_fix` として Step 4 の `comments[]` に使い、LEFT-side は body 退避する。false の場合は `$inline_should_fix=[]` とする。
builder は各 Nit inline/fallback 候補から以下を保持する。`$include_nit == true` 時の inline/fallback 候補は `$nit_findings` 全件ではなく、`posting.post_policy == "body_summary" && posting.explanation_postable == true` の `$nit_inline_candidates` だけに限定する。RIGHT-side guard は抽出時ではなく Step 3.5 の inline 可否判定で適用する:

| 出力キー        | 値 |
| --------------- | --- |
| `path`          | `location.path` |
| `line`          | `location.end_line` があればその値、なければ `location.start_line` |
| `heading_markdown` | ``### `path:L<行番号>` `` または ``### `path:L<開始>-L<終了>` `` |
| `problem`       | finding の `problem` |
| `suggestion`    | finding の `suggestion` |
| `source_finding_id` | finding の `id` |

`$nit_inline_candidates` も `location.side` にかかわらず保持し、LEFT-side Nit は Step 3.5 で inline comment にせず body 退避対象にする。`local_only` / `suppress` / `explanation_postable == false` の Nit は `--include-nit` 指定時でも inline comment に昇格せず、fallback 対象にもならない。diff 範囲外の Should Fix / Nit は body の `## 行コメント不可 (diff 範囲外)` へ退避する。diff 範囲外または `location.side != "RIGHT"` の Should Fix / Nit は inline comment へ昇格せず、同じく body へ退避する。

#### primary path の必須ガード

以下のガードは `build_review_payload.py` が fail-closed で enforcement し、違反があれば builder が非ゼロ終了する。Claude はその stderr をユーザーに提示して **中断** する（Markdown fallback へは切り替えない）。同梱 validator 由来のガード（1〜2 行目）は Step 3 の validator コマンドでも先に検証される。

- `findings.verified.json` が存在しない / 空 / JSON parse 失敗 / top-level object でない / `findings[]` 不在または非配列 / 同梱 validator による `schemas/findings.v1.json` validation / fingerprint 再計算 / format / range validation 失敗 / `id != fingerprint` のいずれかなら、ユーザーに通知して **中断** する（Markdown fallback へは切り替えない）
- `findings.verified.json.pr.repository != metadata.json.repository_full_name`、`findings.verified.json.pr.number != metadata.json.pr_number`、`findings.verified.json.pr.head_sha != metadata.json.head_sha`、`findings.verified.json.pr.base_sha != metadata.json.base_sha`、または `metadata.json.repository_full_name != "$org/$repository"` のいずれかなら、canonical artifact が投稿先 PR と一致しないため **中断** する（Markdown fallback へは切り替えない）
- `severity == "must_fix"` の finding は、M1 では原則 **`posting.post_policy == "inline"` かつ `posting.explanation_postable == true`** のものだけを自動 inline 投稿対象として扱う。`category == "security"` の Must Fix だけは例外として二層に分岐する: `security.disclosure_policy == "body_summary_safe"`（または security high/critical で `inline_safe` でないもの）は inline から除外して body 退避し、公開可能な `public_safe_summary` だけを body に使う。security かつ `posting.post_policy` が `local_only` / `suppress`、または `security.disclosure_policy == "local_only"` かつ post_policy が `inline` 以外のものは **公開 payload のどこにも載せず**、`payload-manifest.json` の `withheld` に記録だけ残す（event 判定と semantic 対象には含める）。`security.disclosure_policy == "local_only"` なのに `posting.post_policy == "inline"` のものは canonical の内部矛盾として **中断** する（builder が黙って振り替えない）。非 security の Must Fix に `local_only` / `suppress` はこの例外の対象外であり、次項の必須ガードどおり中断する
- `severity != "must_fix"` の finding に `posting.post_policy == "inline"` が 1 件でもあれば、review 側の M1 posting contract 違反として **中断** する（Markdown fallback へは切り替えない）。M1 では `should_fix` / `nit` / `note` は inline 自動投稿対象外であり、`body_summary` / `local_only` / `suppress` のいずれかで表現する
- `severity == "must_fix"` の finding で `location.side != "RIGHT"` が 1 件でもあれば、現 workflow の `pr.diff.ranges.txt` が head/new 側前提のため **中断** する（Markdown fallback へは切り替えない）
- `must_fix` なのに `posting.post_policy` が `body_summary` / `local_only` / `suppress` のもの、または `posting.explanation_postable == false` のものが 1 件でもあれば、GitHub payload へ安全に変換できないため **中断** する（Markdown fallback へは切り替えない）。ただし `category == "security"` かつ `security.severity == "critical" | "high"` または `security.disclosure_policy != "inline_safe"` の場合は例外で、inline 詳細を避けるため `body_summary_safe` は body 退避、`local_only` / `suppress` は `withheld`（非公開）側に分岐させる
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

`同一 root cause の影響箇所` は cluster representative の場合のみ追加する。representative 自身はこの箇条書きから除外し、同じ cluster の他 finding のうち上記の掲載条件（posting policy / explanation_postable / active severity flags / security disclosure）を満たすものだけを canonical array order で最大 5 件まで列挙し、条件を満たさない member と 6 件目以降は `他 N 件` の数にだけ含める。

#### 空セクションの扱い

- `$must_fix` が空配列になっても構わない
- `$should_fix_candidates` が空配列なら `$inline_should_fix=[]` とする
- `$nit_findings` が空配列なら `nits.md` は作成しない
- `$good_points` が空文字列なら body から `## 良い点` セクションを省略する
- `review.md` の `## 総評` が空になることは想定しない（`/pr-codex:review` のテンプレートで必ず出力されるため）。万一空ならユーザーに通知して処理を中断する。`$posted_summary` は builder が生成するため空にならない

行範囲検証で範囲外コメントをレビュー body 末尾へ退避するため、builder は各 finding について `heading_markdown` と `body`（GitHub API 用に整形する前の元情報）も内部で保持する。

#### `nits.md` の書き出し (primary path のみ)

`$nit_findings` が 1 件以上ある場合、Step 4 の payload 構築前に Write ツールで `~/claude-loop-pr-codex/$dir_name/nits.md` へ Markdown を書き出す。`file_path` には `~` を実値に展開した絶対パスを渡し、`$dir_name` も実値に置換する。0 件の場合は `nits.md` を作成しない。`$include_nit == true` の場合も local artifact として `nits.md` は残しつつ、`$nit_inline_candidates` のうち範囲検証を通ったものだけを `$inline_nit` として inline comment に含める。`local_only` / `suppress` / `explanation_postable == false` の Nit は PR には投稿しない。

形式:

```markdown
PR には投稿しない軽微な指摘の控えです。

### `path/to/file.ext:L<行番号>`

- 内容: <problem>
- 提案: <suggestion>
```

複数件ある場合は finding ごとに同じ `###` ブロックを繰り返す。`nits.md` は投稿 payload には含めず、Step 7 の `mv` で他 artifact と一緒に `sent/` 配下へ移動される。

### Step 3b: `review.md` の解析 (fallback 廃止)

F13 以降、`findings.verified.json` は必須の一次入力であり、`review.md` parser fallback は使わない。`findings.verified.json` が存在しない、壊れている、または `review.md` と Must Fix 件数が一致しない場合は、payload 生成へ進まず処理を中断する。Should Fix / Nit の inline inclusion と `nits.md` 書き出しも primary path のみで行う。

### Step 3.5: 行範囲検証

GitHub Reviews API は PR diff の新ファイル側 hunk 範囲外の `line` を 422 `Line could not be resolved` で拒否するため、payload 構築前に `pr.diff` からコメント可能行範囲を抽出する。インラインコメント候補の範囲検証そのものは Step 4 の `build_review_payload.py` が以下のルールで決定論的に行い、Claude 側では検証しない。

- いつ使うか: Step 3 の validator 通過直後、Step 4 の builder 実行前に必ず実行する
- 判定条件: `pr.diff.ranges.txt` が作成される
- 次アクション: 作成後、Step 3.75 のフラグ確認を経て Step 4 の builder テンプレートへ進む。builder は `$must_fix` / `$should_fix_candidates` / `$nit_inline_candidates` の各エントリを下記の範囲判定ルールで検証する

```bash
test -f "$plugin_root/skills/lib/extract-diff-ranges.awk" && awk -f "$plugin_root/skills/lib/extract-diff-ranges.awk" ~/claude-loop-pr-codex/$dir_name/pr.diff > ~/claude-loop-pr-codex/$dir_name/pr.diff.ranges.txt
```

テンプレート中の `$plugin_root` は Step 1 common で解決済みの絶対パスの実値に置換して使う。`test -f` が失敗した場合は、Step 1 common の fallback block を再実行して plugin root を再解決し、まだ root を確定できない場合は silent な空ファイル生成を避けるため中断する。

`pr.diff.ranges.txt` は builder が `--ranges` 入力として読む。Claude 側で Read して手動検証する必要はない。

#### 範囲判定ルール（builder が適用する）

- `pr.diff.ranges.txt` の各行は `<path>\tL<開始>-L<終了>` として扱う
- 単一行コメントは `line` が同一 `path` のいずれかの範囲内に含まれる場合のみ有効
- 複数行コメントは `[start_line, line]` の両端が同一 `path` の同じ hunk 範囲内に含まれる場合のみ有効。複数 hunk をまたぐ範囲は無効
- `path` が `pr.diff.ranges.txt` に存在しない場合は無効
- `path` が `metadata.json.files[]` に含まれない場合は無効（`pr.diff.ranges.txt` が stale で PR 外の path を含んでいても inline comment にしない）。`metadata.json.files[]` が欠落または配列でない場合は、範囲を確定できないためすべてのインラインコメント候補を無効として扱う
- `pr.diff.ranges.txt` が空、または `pr.diff` が存在しない場合は、行範囲を確定できないためすべてのインラインコメント候補を無効として扱う
- `location.side != "RIGHT"` の Should Fix / Nit は、現 M1 workflow では GitHub inline comment に変換できないため無効として扱う

#### 範囲外エントリの扱い（builder が適用する）

範囲検証は `$must_fix`、`$include_should_fix == true` の `$should_fix_candidates`、`$include_nit == true` の `$nit_inline_candidates` に対して同じルールで適用する。範囲外または `location.side != "RIGHT"` と判定したエントリは、以下のように扱う。

- 元の inline 配列（`$must_fix` / `$inline_should_fix` / `$inline_nit`）から除外し、`comments` 配列には含めない
- 除外したエントリを `$out_of_range_comments` 配列として保持する
- `$out_of_range_comments` には、元の見出し行、元の本文、種別 (`Must Fix` / `Should Fix` / `Nit`)、退避理由 (`diff 範囲外` / `LEFT-side 非対応` / `security disclosure policy`) を保持する。security 退避分の本文は `security.public_safe_summary` だけで構成する
- レビュー body 末尾に `## 行コメント不可 (diff 範囲外)` セクションを追加し、除外した各エントリの元の見出し行と本文を転記する
- 除外後の `$must_fix` / `$inline_should_fix` / `$inline_nit` の相対順は、`findings.verified.json` の配列順を保つ

既存の正常系 PR で全指摘が範囲内の場合、`$out_of_range_comments` は空配列となり、payload は従来と同じ内容になる。

### Step 3.75: severity inclusion option の適用

Step 0 で正規化した `$include_should_fix` / `$include_nit` は、Step 4 の builder へ `--include-should-fix` / `--include-nit` フラグとしてそのまま渡す。ここではユーザーへの追加 opt-in prompt は表示しない。投稿可否の承認は interactive mode の Step 5 だけで行う。builder は以下のとおり適用する:

- `$include_should_fix == true`: `$should_fix_candidates` のうち範囲検証を通った全件を `$inline_should_fix` に設定し、範囲外のものは `$out_of_range_comments` に保持する
- `$include_should_fix == false`: `$inline_should_fix=[]`
- `$include_nit == true`: `$nit_inline_candidates` のうち範囲検証を通った全件を `$inline_nit` に設定する（Step 0 により `--include-should-fix` との併用済み）。`$nit_findings` のうち `local_only` / `suppress` / `explanation_postable == false` のものは inline に昇格しない
- `$include_nit == false`: `$inline_nit=[]`
- 候補 0 件の場合も prompt は表示せず、該当する included 配列を空にする

`--auto-send` は承認 stop だけを制御し、severity inclusion には影響しない。`--include-should-fix` / `--include-nit` を指定した場合、interactive mode でも auto_send mode でも同じ範囲を inline comment に含める。

### Step 4: payload の構築

Step 2.5 で保持した `payload_builder_path` を使い、以下のテンプレートで `build_review_payload.py` を実行して GitHub Reviews API の payload JSON（`POST /repos/{owner}/{repo}/pulls/{pull_number}/reviews` の request body 仕様）を決定論的に組み立てる。Claude がメモリ上で payload を組み立てることはしない。

- いつ使うか: Step 3.5 の `pr.diff.ranges.txt` 生成後、Step 4.5 の verifier pipeline 前に必ず実行する
- 判定条件: 終了コード 0、`review-payload.json` と `payload-manifest.json` が生成され、`payload-manifest.json` の `counts.must_fix_total`（cluster member 込みの canonical Must Fix 件数）が Step 3 で保持した `$count_must` と一致する
- 次アクション: 成功なら Step 4.5 へ。非ゼロ終了（deterministic payload failure）の場合は同じテンプレートを **1 回だけ** 再実行し、それでも失敗するなら builder の stderr を提示して中断する
- `--include-should-fix` / `--include-nit` は Step 0 の `$include_should_fix` / `$include_nit` が true の場合だけコマンドに含める。この 2 フラグの有無と `--self-review` の値だけがこのテンプレートで許可された可変部分であり、他の引数は変数置換以外で改変しない
- `--self-review` には Step 2b で保持した `$self_review` の実値（`true` / `false`）を必ず渡す。未指定・不正値は builder が非ゼロ終了する（fail-closed）。`unknown` は受け付けない。identity 判定不能の場合は Step 2b で builder 到達前に中断済みのため、この値が必要になることはない
- `--ci-status` / `--run-plan` / `--ci-summary` / `--sarif` / `--diff` は対象ファイルが存在しない場合も指定してよく、builder が未取得として扱う

```bash
python3 $payload_builder_path --findings ~/claude-loop-pr-codex/$dir_name/findings.verified.json --review ~/claude-loop-pr-codex/$dir_name/review.md --metadata ~/claude-loop-pr-codex/$dir_name/metadata.json --ranges ~/claude-loop-pr-codex/$dir_name/pr.diff.ranges.txt --ci-status ~/claude-loop-pr-codex/$dir_name/ci-status.json --run-plan ~/claude-loop-pr-codex/$dir_name/run-plan.json --ci-summary ~/claude-loop-pr-codex/$dir_name/ci-summary.md --sarif ~/claude-loop-pr-codex/$dir_name/findings.sarif --diff ~/claude-loop-pr-codex/$dir_name/pr.diff --self-review $self_review --output ~/claude-loop-pr-codex/$dir_name/review-payload.json --manifest ~/claude-loop-pr-codex/$dir_name/payload-manifest.json
```

builder は以下のルールを実装している:

- `commit_id`: `$head_sha`（レビュー時点の head に明示的に紐付ける）
- `event`:
  - `$self_review == true` なら、Must Fix 件数と CI 状態にかかわらず `"COMMENT"` に抑止する（自分の PR には `APPROVE` / `REQUEST_CHANGES` を投稿できないため。inline の Must Fix コメントは `COMMENT` イベントでも投稿できるので `comments[]` は維持する）。以下の 3 分岐は `$self_review == false` の場合に適用する:
  - canonical（`findings.verified.json`）の Must Fix が 1 件以上あれば `"REQUEST_CHANGES"`（`counts.must_fix_total`。範囲検証後の `$must_fix` と `$out_of_range_comments` の `Must Fix` に加え、cluster 非代表 member / `withheld` を含む）
  - Must Fix が 0 件、かつ `ci-status.json.state` が `failure` / `pending` ではない場合は `"APPROVE"`。`ci-status.json` が存在しない、または `success` / `skipped` の場合もこの分岐に含める
  - Must Fix が 0 件、かつ `ci-status.json.state` が `failure` / `pending` の場合は `"COMMENT"` に抑止する
- `body`:
  - `$posted_summary`（投稿用総評）: builder が投稿対象の finding と event だけから決定論的に生成する。`review.md` の `## 総評` は転記しない。件数は表示せず、投稿しない severity と `withheld` には一切言及しない（#120）。生成ルール:
    - event 行: `REQUEST_CHANGES` は「Must Fix を検出しました。マージ前に修正が必要です。」とし、件数は表示しない（cluster 代表への集約により canonical 件数と投稿コメント数が一致しないため）。本文退避がある場合は「（一部は）行コメント不可のため本文末尾に記載しています。」を続ける。公開可能な Must Fix が 0 件の場合（withheld のみ等）は event の言い換えだけの汎用文「このレビューは変更をリクエストします。」とし、withheld の存在・件数・カテゴリを新たに公開しない。`APPROVE` は「Must Fix はありません。承認します。」、CI 抑止の `COMMENT` は「Must Fix はありませんが、CI が {state} のため承認を保留します。」。self-PR 抑止の `COMMENT` は、Must Fix 0 件なら「Must Fix はありません。」に「このレビューは PR 作成者自身のアカウントから投稿されているため、承認（APPROVE）ではなくコメントとして投稿します。」を続け、Must Fix 1 件以上なら上記 `REQUEST_CHANGES` と同じ Must Fix 文言を維持したうえで「このレビューは PR 作成者自身のアカウントから投稿されているため、変更リクエスト（REQUEST_CHANGES）ではなくコメントとして投稿します。」を続ける
    - severity 行: `--include-should-fix` / `--include-nit` で実際に投稿した Should Fix / Nit がある場合のみ、severity ごとに投稿先（inline / 行コメント不可による本文末尾）を 1 行で付加する。件数は表示せず、投稿 0 件の severity には言及しない
  - `$good_points` が非空の場合:
    ```
    <$posted_summary>

    ## 良い点

    <$good_points>
    ```
  - `$good_points` が空の場合:
    ```
    <$posted_summary>
    ```
  - `event == "APPROVE"` の場合は、承認根拠として body 末尾に以下を追加する。`$reviewed_files` は `metadata.json.files[]` から、`$reviewed_scope` は `review.md` / `run-plan.json` / `ci-summary.md` から確認済みの観点だけを抽出し、推測した観点を混ぜない:
    ```
    ## 確認した範囲

    - 変更ファイル: <$reviewed_files>
    - 検証観点: <$reviewed_scope>
    - CI 状態: <$ci_status_state または "未取得">
    ```
  - `event == "COMMENT"` が CI 抑止によるものの場合は、body 末尾に `## CI 状態` を追加し、`ci-status.json.state` と `ci-summary.md` の要約を短く記載する。self-PR 抑止による `COMMENT` の場合は `## CI 状態` を追加せず、抑止理由の 1 行は総評（`$posted_summary`）内に含める
  - Nit は通常 body section には追加しない。`--include-should-fix` 未指定時、postable な Should Fix（cluster 代表で `post_policy: body_summary` かつ `explanation_postable: true`。`security.disclosure_policy == "local_only"` は除外）は builder が body の `## 改善提案` セクションへ `<details><summary>詳細はこちら</summary><div>` の折りたたみ付き箇条書きとして含める（#140 の検証運用。security 由来の項目は `public_safe_summary` だけで構成する）。`--include-should-fix` 指定時の Should Fix は従来どおり inline comment とし、`## 改善提案` セクションは追加しない。diff 範囲外の Must Fix / Should Fix / Nit（opted-in のもの）がある場合だけ、`$out_of_range_comments` に退避して body 末尾の `## 行コメント不可 (diff 範囲外)` に含める
  - `$out_of_range_comments` が非空の場合は body 末尾に以下を追加する:
    ```
    ## 行コメント不可 (diff 範囲外)

    ### `path/to/file.ext:L<行番号>` (元の見出し)

    - 問題: <問題文>
    - 理由: <理由文>
    - 提案: <提案文>
    ```
  - body 末尾に必ず自動レビューのフッターを追加する（#124）。builder が `findings.verified.json` の `producer.version` と `metadata.json.review_engines[]`（`{name, model, effort}` の配列。review 側 Step 3 が記録。`effort` は記録のみでフッターには表示しない #128）から決定論的に生成する:
    ```
    ---
    これは [pr-codex](https://github.com/yuki777/pr-codex):v<producer.version> による自動レビューです。
    レビューは <name> <model> と <name> <model> により行われました。
    投稿前検証 (semantic preflight) は Codex gpt-5.6-sol により行われました。
    ```
    builder は `producer.version` と `review_engines[]` を必須入力として検証する。`review_engines[]` は実行順の `Claude Code`、`Codex` の2件ちょうどで、各要素の `name` / `model` / `effort` がすべて非空文字列でなければならない。欠落・不正なら deterministic failure として非ゼロ終了する（フッターを省略した投稿は行わない fail-closed。#124）。`review_engines` 記録前の旧バージョン review artifact を send する場合は、`/pr-codex:review` を再実行して metadata を再生成する。
    3 行目（投稿前検証）は `counts.must_fix_total` が 1 件以上の場合のみ builder が追加する。Step 4.5 の semantic preflight は Must Fix があるときだけ実行され、失敗時は投稿自体が中止されるため、投稿された body の表示は常に実行事実と一致する。Must Fix 0 件の skip 時は表示しない。文言に Must Fix を含めず、行の有無は Must Fix 1 件以上の情報のみで（非 self-PR では公開 event `REQUEST_CHANGES` と等価。self-PR 抑止の `COMMENT` でも総評と inline コメントが同じ情報を先に公開している）、`withheld` の存在・件数・カテゴリを新たに公開しない（#120 と整合）。verifier は send 実行時の構成のため `metadata.json` には記録せず、builder 同梱の固定値 `SEMANTIC_VERIFIER_ENGINE` を使う。Step 4.5 の Codex テンプレートのモデルを変更する場合は builder の固定値も併せて更新する（`tasks/test_issue124_docs.py` が一致を検証する）。effort はどのフッター行にも表示しない。実行時の実効 effort は投稿時点で確定できないため、`review_engines[].effort` は記録の検証にだけ使い、表示は name と model に限定する（#128）
- `comments`: `$must_fix` + `$inline_should_fix` + `$inline_nit`（それぞれ `findings.verified.json` の順序を保つ）。Should Fix / Nit も `path` / `line` / `side` / `body` を持つ inline comment として投稿する。各要素は以下のキーを含む:
  - `path` (必須)
  - `line` (必須)
  - `side` (`"RIGHT"`)
  - `body` (Step 3 の body フォーマット)
  - `start_line` / `start_side` は範囲指定の場合のみ含める

範囲検証後の `$must_fix` / `$inline_should_fix` / `$inline_nit` が空でも、`event` は canonical（`findings.verified.json`）の Must Fix 件数（cluster 非代表 member / `withheld` を含む）で決める。canonical の Must Fix が 0 件なら、Must Fix 0 件の結論として `event: "APPROVE"` + body (総評 + 良い点 + 確認した範囲) で投稿する。ただし canonical に Must Fix が 1 件以上ある場合（`$out_of_range_comments` の `Must Fix` や `withheld` のみの場合を含む）の `event` は上記ルールどおり `"REQUEST_CHANGES"` とし、Must Fix 0 件で `ci-status.json.state` が `failure` / `pending` の場合は `event: "COMMENT"` に抑止して CI 状態を body と Step 5 に表示する。これらの event ルールはすべて `$self_review == false` の場合であり、`$self_review == true` なら Must Fix 件数と CI 状態にかかわらず `event: "COMMENT"` に抑止して、抑止理由を総評と Step 5 に表示する。

body のセクション順は必ず `総評`（`$posted_summary`） → `## 良い点`（存在する場合）→ `## 確認した範囲`（`APPROVE` の場合）→ `## CI 状態`（CI 抑止 `COMMENT` の場合）→ `## 改善提案`（`--include-should-fix` 未指定で postable な Should Fix がある場合。#140）→ `## 行コメント不可 (diff 範囲外)`（存在する場合）→ 自動レビューフッター（常に最終セクション。#124）とする。diff 範囲内の opted-in Should Fix / Nit は body section ではなく `comments[]` の inline comment とする。diff 範囲外の Must Fix / opted-in Should Fix / Nit は body の `## 行コメント不可 (diff 範囲外)` へ退避する。

payload は builder が `--output` で `~/claude-loop-pr-codex/$dir_name/review-payload.json` に整形 JSON（インデント 2）として書き出す。同時に `--manifest` で `payload-manifest.json`（`payload-manifest.v1`）を書き出し、`comment_index → finding_id` の対応表（`comment_map`）、body 退避一覧（`out_of_range`）、非公開一覧（`withheld`。local_only / suppress の Must Fix で、body にも載せない）、body `## 改善提案` 掲載一覧（`should_fix_summary`。#140）、semantic 対象の全 Must Fix finding id（`semantic_targets`。cluster 非代表 member を含む）、active severity flags（`flags`）、self-PR 判定（`self_review`。`true|false` の必須 boolean。event 決定に使った入力として必ず記録する）、件数（`counts`）、event、および role 付きの sha256 digest（`files`。required: findings / review / metadata / ranges / payload、optional: sarif / diff / ci_status / run_plan / ci_summary）を記録する。以降の工程は location 文字列による曖昧照合ではなく `comment_map` を使って finding と comment を対応付ける。`--verify` は manifest 構造・required role の存在・全 role の digest 照合に加えて、digest 検証済みの入力と `flags` / `self_review` から payload / manifest をドライラン再生成して現物と完全一致比較し（`generated_at` を除く）、payload と manifest の協調改竄も検出する。`self_review` を記録しない旧形式 manifest は `--verify` が旧形式として明示的に非ゼロ終了する（黙って `false` 扱いにしない）。

#### 許可 severity (active severity flags)

builder は `--include-should-fix` / `--include-nit` の active severity flags で許可された severity の finding だけを `comments[]` に含める。許可される severity は `include_should_fix=false` では `must_fix` のみ、`include_should_fix=true && include_nit=false` では `must_fix` / `should_fix`、`include_should_fix=true && include_nit=true` では `must_fix` / `should_fix` / `nit` とする。未指定の should_fix / nit / note finding は inline payload に混入しない。`include_should_fix=false` の postable な should_fix は inline ではなく body の `## 改善提案` セクションに掲載され、`payload-manifest.json` の `should_fix_summary` / `counts.should_fix_summary` に記録される（#140）。diff 範囲外または `location.side != 'RIGHT'` のため `## 行コメント不可 (diff 範囲外)` へ退避された opted-in should_fix / nit は、inline payload から欠けていても valid exclusion として扱う。`severity != "must_fix"` の finding は canonical `posting.post_policy` を変えず、send 側の明示オプションだけで inline comment に昇格する。この振り分けは `payload-manifest.json` の `comment_map` / `out_of_range` / `should_fix_summary` に記録され、static stage の `--verify` が改竄を検出する。

### Step 4.5: 投稿前 verifier pipeline (static Python + Codex semantic)

投稿直前の検証を **4 stage verifier pipeline** として実行する。`--auto-send` でもスキップしない。Step 5 第2ステップ（interactive の最終承認プロンプト、または auto_send の自動続行判断）の直前で必ず実行する。`findings.verified.json` は必須入力であり、Markdown fallback は使わない。担当は二層に分かれる: `schema_validation` / `range_validation` / `payload_consistency` の 3 つの static stage は決定論的な Python validator / builder が担い、意味判断が必要な `semantic_preflight` だけを Codex CLI (GPT-5.6) が担う。**static stage が 1 つでも FAIL の場合は Codex を呼ばず fail-closed で中断する。** semantic 判定は Codex の structured output（`--output-schema` / `--output-last-message`、`schemas/preflight-semantic.v1.json`）で per-finding decisions として直接受け、`validate_preflight_result.py --from-semantic` が static 結果と合成して `preflight-result.json` を生成する（top-level verdict / stage status / counts は host 算出）。人間可読の `preflight-codex.md` は validated JSON から `--emit-markdown` で派生生成する。Markdown からの `RESULT_JSON` 抽出や final `VERDICT:` line との整合検証は行わない。

#### 4 stage と担当

| Stage | 担当 | 検証観点 |
| --- | --- | --- |
| 1. `schema_validation` | Python (`validate_findings.py` / `validate_findings_sarif.py`) | `findings.verified.json` の schema / fingerprint 再計算 / `metadata.json` との PR context 一致（Step 3 で実行済み）、`findings.sarif` の schema validation、canonical ↔ `review.md` ↔ `review-payload.json` ↔ SARIF の Must Fix count 整合 |
| 2. `range_validation` | Python (`build_review_payload.py`) | `payload.comments[]` の `path` / `line` が `metadata.json.files[]` と `pr.diff.ranges.txt` の hunk 範囲内にあること。builder が構築時に enforcement し、`--verify` の manifest digest 再確認で構築後の改竄・手編集を検出する |
| 3. `semantic_preflight` | Codex (GPT-5.6) | payload 投稿対象の各 Must Fix finding の実在性判定（confirmed / refuted / insufficient_evidence の 3 値） |
| 4. `payload_consistency` | Python (`build_review_payload.py`) | event 判定（'self_review が true → COMMENT（Must Fix 件数・CI 状態にかかわらず抑止）/ self_review が false の場合: canonical Must Fix が1件以上（cluster 非代表 member / withheld / body 末尾へ退避した範囲外 Must Fix を含む）→ REQUEST_CHANGES / Must Fix 0件かつ ci-status.json.state が failure または pending → COMMENT / Must Fix 0件かつ ci-status.json.state が success・skipped・未取得 → APPROVE'。不一致は rule `event_mismatch`）、body セクション順（payload.event が APPROVE の場合は payload.body に '## 確認した範囲' を含む）、counts。builder が生成時に決定論的に保証し、`--verify` で再確認する |

semantic preflight は canonical の `severity == "must_fix"` 全 finding（cluster 非代表 member と `withheld` を含む。`payload-manifest.json` の `semantic_targets`）に適用する。cluster member の要約も代表 comment の body に掲載されるため、代表だけに縮めない。Codex は各 Must Fix finding について「この指摘が誤りである可能性」を 1 つだけ探索し、3 値で判定する。「反証あり」と「調査不足」を区別する:

- `confirmed`: 反証を挙げられない。`counterargument` には検討した最有力の反証仮説と棄却理由を 1〜2 文で書く
- `refuted`: 反証が成立した。反証成功 = 不採用 / FAIL
- `insufficient_evidence`: 反証は成立しないが、`pr.diff` と当該 finding 抜粋だけでは問題の実在を確認できない

#### static stage の実行 (Python)

- いつ使うか: Step 4 の builder 成功直後に必ず実行する
- 判定条件: 2 つのテンプレートがともに終了コード 0。1 つ目が SARIF schema と Must Fix count 多層整合（full canonical count として `canonical_must_fix == markdown_must_fix == sarif_must_fix`。payload 側は cluster representative 集約後の posting count として `--payload` で照合。空の `pr.diff.ranges.txt` は「コメント可能範囲なし」として扱い、非空 finding / SARIF result を PASS させない）、2 つ目が manifest digest 再確認
- 次アクション: 成功なら semantic preflight の skip 判定へ。失敗した場合は **Codex を呼ばず**、Step 4 の builder テンプレートを 1 回だけ再実行してから本テンプレートを再実行し、それでも失敗するなら fail-closed で中断して stderr をユーザーに報告する

```bash
python3 $sarif_validator_path --schema $sarif_schema_path --data ~/claude-loop-pr-codex/$dir_name/findings.sarif --findings ~/claude-loop-pr-codex/$dir_name/findings.verified.json --ranges ~/claude-loop-pr-codex/$dir_name/pr.diff.ranges.txt --markdown ~/claude-loop-pr-codex/$dir_name/review.md --payload ~/claude-loop-pr-codex/$dir_name/review-payload.json
```

```bash
python3 $payload_builder_path --verify --manifest ~/claude-loop-pr-codex/$dir_name/payload-manifest.json
```

#### semantic preflight の skip 判定

`payload-manifest.json` の `counts.must_fix_total` が 0 の場合、semantic 対象の Must Fix が存在しないため Codex preflight を実行しない。以下の合成テンプレートで `preflight-result.json` を直接生成し（`semantic_preflight` は `PASS` + skip note になる）、「preflight-result 検証と人間可読化コマンド」の 2 つ目（`--emit-markdown`）へ進む。`counts.must_fix_total` が 1 以上なら Codex semantic prompt file の作成へ進む。

```bash
python3 $preflight_validator_path --schema $preflight_schema_path --semantic-skipped --manifest ~/claude-loop-pr-codex/$dir_name/payload-manifest.json --emit-json > ~/claude-loop-pr-codex/$dir_name/preflight-result.json
```

#### violation 分類ルール

`validate_preflight_result.py --from-semantic` は Codex の decisions を以下の安定 `rule` に写像して violation を合成する。既知 rule は severity=error とする。`severity == "warning"` は将来拡張用であり、top-level `verdict` / `auto_fixable_count` / `requires_human_count` にカウントしない。中間 verdict `PASS_WITH_WARNINGS` は使わず、top-level `verdict` は `PASS` / `FAIL` のみとする。

| decision | rule | stage | auto_fixable | requires_review_regeneration |
| --- | --- | --- | --- | --- |
| `refuted` | `counterargument_succeeded` | `semantic_preflight` | false | true |
| `insufficient_evidence` | `insufficient_evidence` | `semantic_preflight` | false | true |

static stage の rule 語彙（`schema_validation`: `schema_version_mismatch` / `findings_validator_failed` / `sarif_schema_invalid` / `must_fix_count_mismatch` / `id_fingerprint_mismatch` / `pr_context_mismatch`、`range_validation`: `path_not_in_files` / `line_out_of_hunk` / `multi_hunk_span`、`semantic_preflight`: `severity_misclassification` / `non_must_fix_inline_inclusion` / `axes_gate_violation` / `evidence_level_violation`、`payload_consistency`: `event_mismatch` / `summary_body_mismatch` / `good_points_body_mismatch` / `confirmation_scope_body_mismatch` / `must_fix_count_mismatch_findings_vs_md`）は `preflight-result.v1` の分類として維持するが、static stage は Python が enforcement するため、正常フローでこれらの violation が `preflight-result.json` に現れることはない（現れた場合は builder / validator のバグとして中断する）。

#### `preflight-result.json` 構造

`preflight-result.json` は Codex が直接出力するのではなく、`validate_preflight_result.py` が Codex の semantic decisions（`preflight-semantic.json`）と static stage の通過事実から合成する。static 3 stage は Python validator 通過を前提に `PASS` + 固定 note、`semantic_preflight` は decisions から status / violations を導出し、top-level `verdict` / `auto_fixable_count` / `requires_human_count` は host が error violation から再計算する。violation が特定の finding / comment に紐づかない場合、`finding_id` / `comment_index` は `null` にする。

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

#### Codex semantic prompt file

- いつ使うか: static stage 通過後、`counts.must_fix_total` が 1 以上の場合に Codex semantic コマンドの直前に必ず作成する
- 作成方法: Write ツールで `~/claude-loop-pr-codex/$dir_name/preflight-prompt.md` に以下の prompt 本文を書き出す。`file_path` は `~` と `$dir_name` を実値へ展開した絶対パスで渡す
- 置換ルール: `{SEMANTIC_SCHEMA_PATH}` は Step 2.5 で保持した `semantic_schema_path` へ、`{MUST_FIX_FINDINGS}` は `payload-manifest.json` の `semantic_targets` にある全 Must Fix finding（cluster 非代表 member・withheld を含む）を `findings.verified.json` から引き、finding ごとに `- finding_id / path:L<行> / title / problem / reason / suggestion` を列挙した平文へ、Claude 側で置換してから書き出す。shell で prompt 本文を展開してはならない
- 理由: prompt 本文には Markdown backtick や JSON double quote が含まれるため、shell の double-quoted argument として渡すと command substitution / quote 分割で壊れる。prompt file + stdin 経由に固定し、shell は本文を解釈しない

```markdown
あなたは GitHub PR レビュー投稿前の独立検証エージェントです。以下の各 Must Fix finding について、この指摘が誤りである可能性を 1 つだけ探索し、semantic_preflight の判定を返してください。最終メッセージは preflight-semantic.v1 schema に従う JSON オブジェクト 1 個だけを出力してください。前置き・後置きの散文、Markdown 見出し、コードフェンスを最終メッセージに含めてはいけません。

## 入力ファイル
- pr.diff: PR diff 本文。finding 実在性と反証探索の一次根拠
- pr.diff.ranges.txt: コメント可能な hunk 範囲一覧
- metadata.json: 対象 PR のメタデータ（files 配列を含む）
- findings.verified.json: canonical findings（当該 finding 抜粋の参照用）

## 判定ルール
- 各 finding について decision を confirmed / refuted / insufficient_evidence の 3 値から 1 つだけ選ぶ
- 根拠は当該 finding 抜粋・pr.diff・pr.diff.ranges.txt・metadata.json のみを参照する。他 finding の結論だけに依存しない
- 反証を挙げられない場合のみ confirmed とし、counterargument には検討した最有力の反証仮説と棄却理由を 1〜2 文で書く
- 反証が成立した場合は refuted とし、counterargument に反証を 1〜2 文で書く。反証成功 = 不採用 / FAIL
- 反証は成立しないが、pr.diff と当該 finding 抜粋だけでは問題の実在を確認できない場合は insufficient_evidence とする
  - 正例: diff 上でも削除後の値が未定義になり得る経路を確認でき、反証を挙げられない → confirmed
  - 負例: metadata.json.files[] 外の既存コード前提に依存しており、この PR の diff だけでは問題が実在すると言えない → refuted
- decisions には下記の対象 Must Fix finding を、finding_id の過不足なくすべて含める

## 対象 Must Fix findings
{MUST_FIX_FINDINGS}

## 出力
最終メッセージは {SEMANTIC_SCHEMA_PATH} の schema (preflight-semantic.v1) に従う JSON オブジェクト 1 個だけとする。decisions[] の各要素に finding_id / decision / counterargument / note を埋める。
```

#### Codex semantic コマンド

- いつ使うか: `preflight-prompt.md` を Write ツールで作成した直後、Step 5 の承認プロンプト前に必ず実行する
- 判定条件: 終了コードが 0、`preflight-semantic.json` が生成されて非空、かつ次の合成・検証コマンドが終了コード 0 で成功する
- 次アクション: 成功なら preflight-result 合成へ。Codex 実行の失敗、`preflight-semantic.json` の欠落、または合成コマンドの失敗（malformed structured output。decisions の finding_id 過不足を含む）は、同じ prompt で Codex semantic コマンドを **1 回だけ** 再実行して回復を試みる。再実行でも解消しない場合は自動投稿を中止して検証失敗の内容をユーザーに報告する（同一 prompt の 3 回リトライはしない）
- prompt は `exec` の `-` 引数と stdin redirection で渡す。Bash ツールへ渡すコマンド文字列に prompt 本文を直接埋め込んではならない

```bash
codex \
  --ask-for-approval never \
  -m gpt-5.6-sol \
  -c 'model_reasoning_effort="high"' \
  -c sandbox_mode=read-only \
  exec \
  --ignore-user-config \
  --skip-git-repo-check \
  --cd ~/claude-loop-pr-codex/$dir_name \
  --output-schema $semantic_schema_path \
  --output-last-message ~/claude-loop-pr-codex/$dir_name/preflight-semantic.json \
  - \
  <  ~/claude-loop-pr-codex/$dir_name/preflight-prompt.md \
  >  ~/claude-loop-pr-codex/$dir_name/preflight-codex.log \
  2>&1
```

フラグの説明:

- `--ask-for-approval never` / `-m gpt-5.6-sol` / `-c ...` は global flag のため、すべて `exec` の前に置く
- `-m gpt-5.6-sol` — semantic preflight の実行モデルを GPT-5.6 Sol に固定する（#110 の担当替え）。hunter と同じモデルだが、投稿直前の反証確認という別用途のため effort は実測に基づく `high` を維持する。素の `gpt-5.6` slug は ChatGPT アカウントの Codex では 400 で拒否されるため、動作確認済みの `gpt-5.6-sol` を使う
- `-c 'model_reasoning_effort="high"'` — 7,301-byte の prompt と upstream findings 入力を high / xhigh 間で byte-identical に揃えて再実測し、両方が同じ Must Fix 2件を confirmed、exact / acceptable / false-positive / recall も同値だった。保存 run では high が 14,890 ms / 23,003 tokens、xhigh が 34,217 ms / 23,326 tokens だったため、semantic preflight は high に固定する
- `-c sandbox_mode=read-only` — シェル実行を read-only サンドボックスに固定する。`--sandbox read-only` と等価だが、config override として明示するため `-c` に統一する
- `--ignore-user-config` — 投稿前検証中のみ `$CODEX_HOME/config.toml` / `~/.codex/config.toml` を読み込まない。auth は引き続き `CODEX_HOME` を使うため、古い MCP 設定や無効な `model_reasoning_effort` による config 検証エラーから Step 4.5 preflight を切り離せる
- `--skip-git-repo-check` / `-C, --cd` は `exec` サブコマンド側の option のため、`exec` の後ろ、かつ prompt の前に置く
- `--output-schema` — `schemas/preflight-semantic.v1.json`（Step 2.5 の `semantic_schema_path`）を structured output として強制する。`exec` サブコマンド側の option のため `exec` の後ろに置く
- `--output-last-message` — 最終メッセージ（schema 準拠 JSON）を `preflight-semantic.json` へ直接保存する。標準出力の実行ログは `preflight-codex.log` にまとめる（`2>&1`）
- `--color never` / `--ephemeral` はテンプレートを簡素化するため使わない。カラーは TTY 自動判定に任せ、セッション保存挙動は config 側に委ねる

#### preflight-result 検証と人間可読化コマンド

- いつ使うか: 上の Codex semantic コマンドが終了した直後（skip 判定で合成済みの場合は 2 つ目のみ）に必ず実行する
- 判定条件: 2 つのテンプレートの終了コードがともに 0。1 つ目で decisions と `payload-manifest.json` の Must Fix finding_id 集合の一致を検証したうえで static 結果と合成し、`schema_version == "preflight-result.v1"` / `verdict in {"PASS","FAIL"}` / 4 stage / violation 分類 / count 再計算の cross-field validation を満たす `preflight-result.json` を生成する。2 つ目で validated JSON から人間可読の `preflight-codex.md` が生成される
- 次アクション: `preflight-result.json` を Read ツールで取得し、`preflight-result.json.verdict == "PASS"` の場合だけ Step 5 へ進む。`FAIL` なら下記の失敗時処理へ進む。合成コマンドの失敗（malformed structured output）は上記のとおり Codex semantic コマンドの 1 回だけの再実行対象とし、再実行でも失敗するなら自動投稿を中止してユーザーに報告する

```bash
python3 $preflight_validator_path --schema $preflight_schema_path --from-semantic ~/claude-loop-pr-codex/$dir_name/preflight-semantic.json --manifest ~/claude-loop-pr-codex/$dir_name/payload-manifest.json --emit-json > ~/claude-loop-pr-codex/$dir_name/preflight-result.json
```

```bash
python3 $preflight_validator_path --schema $preflight_schema_path --data ~/claude-loop-pr-codex/$dir_name/preflight-result.json --emit-markdown > ~/claude-loop-pr-codex/$dir_name/preflight-codex.md
```

#### 失敗時の Claude 側の処理

retry policy は以下の 3 分類だけとする。auto-fix ループや同一 prompt の 3 回リトライは行わない。

1. **static stage の失敗**（Step 3 validator / SARIF validator / builder / `--verify` の非ゼロ終了 = deterministic failure）: Codex を呼ばず fail-closed。Step 4 の builder テンプレートの 1 回だけの再実行で解消しなければ中断し、該当コマンドの stderr をユーザーに提示する
2. **malformed structured output / transient failure**（Codex 実行失敗、`preflight-semantic.json` 欠落、decisions の finding_id 過不足、合成コマンドの INVALID）: 同じ prompt で Codex semantic コマンドを 1 回だけ再実行する。解消しなければ自動投稿を中止してユーザーに報告する
3. **semantic refutation / insufficient evidence**（`preflight-result.json.verdict == "FAIL"`）: payload の自動修正では解消できないため、リトライせず即中断し、ユーザーに以下を報告する。自動で該当 finding を握りつぶして投稿してはならない
   - `preflight-result.json` と `preflight-codex.md` のパス
   - `requires_review_regeneration == true` の違反一覧（`rule` / `finding_id` / `detail`）。`counterargument_succeeded` は「指摘への反証が成立」、`insufficient_evidence` は「diff からは実在を確認できない（調査不足）」の別を添える
   - 「review 側の `findings.verified.json` / `review.md` 再生成が必要」という旨

### Step 5: 承認プロンプト

Step 3.75 の severity inclusion option 適用と Step 4.5 の Codex セルフレビューを終えた後、投稿前の最終確認として以下のサマリをテキストで提示する。`$send_mode=interactive` では明示的な承認を求める。`$send_mode=auto_send` では最終投稿承認だけをスキップし、このサマリを表示したうえで承認入力なしで Step 5.5 へ進む:

```
対象 PR: <$pr_url> (<$title>)
event: <REQUEST_CHANGES | APPROVE | COMMENT>
CI gate: <success|failure|pending|skipped|未取得>（failure/pending の場合は APPROVE 抑止）
self-PR gate: <true|false>（true の場合は APPROVE / REQUEST_CHANGES を COMMENT に抑止）
findings source: ~/claude-loop-pr-codex/<$dir_name>/findings.verified.json
review file: ~/claude-loop-pr-codex/<$dir_name>/review.md
SARIF artifact: ~/claude-loop-pr-codex/<$dir_name>/findings.sarif (local-only, Code Scanning upload なし)
body プレビュー:
  <$posted_summary の先頭 200 文字。長ければ "..." で省略>
インラインコメント: Must Fix N 件
Should Fix inline comments: included <yes|no> (<included_count>/<candidate_count> 件、--include-should-fix で投稿可能候補を含める)
Nit inline comments: included <yes|no> (<included_count>/<candidate_count> 件、--include-nit で投稿可能候補を含める)
Nit artifact: <~/claude-loop-pr-codex/<$dir_name>/nits.md | nit: 0 件>
（Should Fix / Nit は指定時に投稿可能なものを inline comment に含めます。diff 範囲外は body へ退避します）
（--include-should-fix 未指定時、投稿可能な Should Fix は body の「改善提案」セクションに折りたたみで記載します）
行範囲外で除外したインラインコメント (Must Fix / Should Fix / Nit): K 件
  - <path>:L<line> (本文末尾の「行コメント不可」セクションに移動)
payload: ~/claude-loop-pr-codex/<$dir_name>/review-payload.json
preflight result: ~/claude-loop-pr-codex/<$dir_name>/preflight-result.json
移動先 (投稿後): ~/claude-loop-pr-codex/sent/<$dir_name>-<$head_sha_short>

この内容で投稿してよろしいですか？ (yes/no; interactive のみ。auto_send は承認入力なしで続行)
```

`$out_of_range_comments` が空の場合も、サマリ行は `行範囲外で除外したインラインコメント: 0 件` として表示する。除外したエントリの箇条書きは 1 件以上ある場合のみ表示する。
fallback path では `Should Fix inline comments: included no (0/0 件、--include-should-fix で投稿可能候補を含める)`、`Nit inline comments: included no (0/0 件、--include-nit で投稿可能候補を含める)`、`Nit artifact: nit: 0 件` と表示する。primary path で `$nit_findings` が 1 件以上ある場合は `nits.md` のパスを表示し、0 件なら `nit: 0 件` と表示する。

interactive mode では、ユーザーの応答が `yes` / `y` / `はい` 等の明示的な承認である場合のみ Step 5.5 に進む。それ以外（`no` / `n` / `いいえ` / 曖昧・無回答）の場合は処理を中断し、以下を報告して終了する。auto_send mode ではこの承認入力を行わず、Step 4.5 PASS 後に Step 5.5 の safety gate へ進む:

- 投稿はスキップした旨
- payload ファイルは保持されている旨 (`~/claude-loop-pr-codex/$dir_name/review-payload.json`)
- Nit 件数。`nits.md` を生成した場合は `~/claude-loop-pr-codex/$dir_name/nits.md`、0 件なら `nit: 0 件`
- 再実行したい場合は再度 `/pr-codex:send` を叩くか、payload を手動編集してから `gh api --method POST ... --input <payload>` で直接投稿できる旨

承認拒否時は `sent/` への移動は行わない。

### Step 5.5: 投稿直前 safety gate

Step 6 の GitHub write の直前に、interactive / auto_send のどちらでも以下を必ず実行する。これにより `--auto-send` でも古い review を自動投稿せず、preflight 後にローカル artifact が書き換わった場合（HEAD SHA gate では検出できないローカル TOCTOU）も投稿しない。

- いつ使うか: Step 5 で interactive の承認を得た直後、または auto_send で最終投稿承認だけをスキップした直後
- 判定条件: `build_review_payload.py --verify` が終了コード 0（`payload-manifest.json` の構造と required role の存在を検証し、記録された payload / findings / review.md / metadata / ranges / SARIF / diff の sha256 digest がすべて現物と一致し、digest 検証済みの入力から payload / manifest をドライラン再生成して `comment_map` / `event` / `counts` / `semantic_targets` / `withheld` / comments 内容が現物と完全一致する）
- 次アクション: 成功なら二重投稿防止 gate へ。不一致なら preflight 後にローカル artifact が変更されたため中断し、Step 6 は実行しない

```bash
python3 $payload_builder_path --verify --manifest ~/claude-loop-pr-codex/$dir_name/payload-manifest.json
```

- いつ使うか: manifest digest 再確認の直後
- 判定条件: `review-response.json` が存在しない、または存在しても `.html_url` が空である。`.html_url` が既に存在する場合は二重投稿の可能性があるため中断する
- 次アクション: 成功なら現在の PR head 再取得へ。失敗なら Step 8 の失敗報告へ進み、Step 6 は実行しない

```bash
test ! -f ~/claude-loop-pr-codex/$dir_name/review-response.json || jq -e '(.html_url // "") == ""' ~/claude-loop-pr-codex/$dir_name/review-response.json
```

- いつ使うか: 二重投稿防止 gate の直後
- 判定条件: 標準出力が現在の PR head SHA。`gh api "/repos/$org/$repository/pulls/$pr_number" --jq '.head.sha'` で取得する
- 次アクション: 出力を `$current_head_sha` として保持し、次の比較テンプレートへ進む

```bash
gh api "/repos/$org/$repository/pulls/$pr_number" --jq '.head.sha'
```

- いつ使うか: `$current_head_sha` を取得した直後
- 判定条件: `$current_head_sha` が `metadata.json.head_sha`（Step 2 で保持した `$head_sha`）と一致する
- 次アクション: 一致すれば Step 6 へ。不一致ならレビュー生成後に追加 commit が入ったため中断し、古い review を自動投稿しない

```bash
test "$current_head_sha" = "$head_sha"
```

### Step 6: `gh api` で投稿

- いつ使うか: Step 5.5 の二重投稿防止 gate と head SHA 再確認が成功した直後に実行する
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

ユーザーに以下をテキストで報告して終了する。成功時は、成功報告を出した直後に slash command として `/clear` を単独で実行し、新しい conversation へ移る。`/clear` は GitHub 投稿と `sent/` 移動が両方成功した後だけ実行し、失敗時、承認拒否時、Step 4.5 verifier FAIL、Step 5.5 safety gate 中断、または Step 7 失敗時には実行しない。`/clear` に `/pr-codex:review` など後続コマンドを同じ行で連結してはならない。

成功時:

- 対象 PR: `$pr_url` (`$title`)
- 投稿した review の URL: `$review_url`
- 選択した `event`
- self-PR 抑止の有無（`$self_review=true` により `COMMENT` にした場合は、その旨を 1 行で表示する）
- インラインコメント件数 (Must Fix のみ)
- Should Fix inline comment 同梱結果 (`included yes/no` と件数)
- Nit inline comment 同梱結果 (`included yes/no` と件数)
- Nit 件数。`nits.md` を生成した場合は移動後の path `~/claude-loop-pr-codex/sent/$dir_name-$head_sha_short/nits.md`、0 件なら `nit: 0 件`
- 行範囲外で除外したインラインコメント件数
- preflight result: `~/claude-loop-pr-codex/sent/$dir_name-$head_sha_short/preflight-result.json`
- 移動先: `~/claude-loop-pr-codex/sent/$dir_name-$head_sha_short`
- context reset: 成功報告後に `/clear` を実行

失敗時（Step 6 が非ゼロ終了、Step 7 の移動先衝突、または Step 7 の移動完了検証が失敗した場合）:

- エラー内容または状況 (`gh api` の stderr、Step 7 の移動先衝突、または Step 7 の移動完了検証失敗)
- Nit 件数。`nits.md` を生成した場合は未移動の path `~/claude-loop-pr-codex/$dir_name/nits.md`、0 件なら `nit: 0 件`
- 推定原因:
  - 422 → Step 3.5 で PR diff 範囲外のインラインコメントは除外済みのため、残ったコメントの `path` / `line` / `start_line` が GitHub 側で解決不能になっている可能性がある。`review-payload.json` の `comments` と `pr.diff.ranges.txt` / `pr.diff` を照合し、必要なら payload から該当コメントを除外するようユーザーに案内
  - 422 のエラーメッセージに self-approval 拒否（`Can not approve your own pull request` / `Can not request changes on your own pull request` 等）が含まれる場合 → PR 作成者と投稿アカウントが同一のため `APPROVE` / `REQUEST_CHANGES` を投稿できない。Step 2b の検知後に投稿アカウントを切り替えた、organization / App 経由の投稿で作者と同一 identity に解決された等の競合が原因のため、`gh auth status` で投稿アカウントを確認するよう案内する（既存の 422 ハンドリング同様、リトライや event の自動差し替えはしない）
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
- `review.md` に Must Fix が一件も無い → Should Fix / Nit の明示指定があれば inline comment として投稿する。`$self_review == true` なら CI 状態にかかわらず `event: COMMENT` に抑止し、self-PR 理由を総評と Step 5 に表示する。`$self_review == false` かつ `ci-status.json.state` が `failure` / `pending` の場合は `event: COMMENT` に抑止して CI 状態を body と Step 5 に表示し、`$self_review == false` かつ CI 抑止が無い場合のみ Must Fix 0 件の結論として `event: APPROVE` + body (総評 + 良い点 + 確認した範囲) で投稿する
- `review.md` の `## 総評` セクションが空 or 見つからない → ユーザーに通知して処理中断。`sent/` 移動は行わない
- Step 3.5 で `pr.diff.ranges.txt` が空 → インラインコメント候補はすべて body 末尾の `## 行コメント不可 (diff 範囲外)` に移動し、`comments` 配列には含めない
- Step 4 の `build_review_payload.py` または Step 4.5 の static stage（SARIF validator / `--verify`）が非ゼロ終了（deterministic failure）→ Codex を呼ばず、builder テンプレートを 1 回だけ再実行して解消しなければ投稿を中止（fail-closed）
- Step 4.5 の Codex semantic 実行が失敗、`preflight-semantic.json` が生成されない、または `tasks/validate_preflight_result.py --from-semantic` が INVALID（malformed structured output。decisions の finding_id 過不足を含む）→ 同じ prompt で Codex semantic コマンドを 1 回だけ再実行し、解消しなければ投稿を中止
- Step 4.5 の `preflight-result.json.verdict == "FAIL"`（`counterargument_succeeded` / `insufficient_evidence`）→ review 側の再生成が必要としてリトライせず即中断し、`preflight-result.json` / `preflight-codex.md` のパスと違反一覧を提示
- Step 5.5 の `build_review_payload.py --verify` が digest 不一致 → preflight 後にローカル artifact が変更されたため中断し、GitHub write は行わない
- Step 0 で未知オプション、解釈できない位置引数、位置引数が2つ以上、重複オプション、または `--include-nit` 単独 → `unsupported argument` として中断し、payload 生成や GitHub write は行わない
- Step 1 direct mode で PR 番号のみ指定が複数 directory に一致 → 曖昧として中断し、PR URL 指定を案内する
- Step 1 direct mode で指定 PR の active directory がなく `sent/` に履歴がある → 「指定 PR は既に send 済み（`sent/` にある）」と報告して中断する
- Step 1 direct mode で指定 PR の active directory も sent 履歴もない → 「指定 PR の completed レビューが無い。先に `/pr-codex:review <PR URL>` を実行」と案内して中断する
- Step 1 direct mode で `status.json.state != completed`、`review.md` 欠落、または `findings.verified.json` 欠落 → 理由を添えて中断し、auto 選定や Markdown fallback へは切り替えない
- Step 5.5 で `review-response.json.html_url` が既に存在 → 二重投稿防止のため中断し、`gh api` は実行しない
- Step 5.5 で現在の PR head SHA が `metadata.json.head_sha` と一致しない → レビュー生成後に追加 commit が入ったため中断し、古い review を自動投稿しない
- `gh api` 422/403/404 → Step 8 の失敗報告で分岐し、`sent/` 移動は行わない
- Step 2b の identity 取得（`gh api user` または PR 作者の取得）が非ゼロ終了、空出力、または複数行出力 → 投稿前に中断する（fail-closed）。builder / Step 4.5 preflight は実行せず、`sent/` 移動も行わない。失敗した API と、`gh auth status` の確認・再実行というリトライ手順を報告する
- Step 7 で `sent/$dir_name-$head_sha_short/` がすでに存在 → ユーザーに通知して処理中断（投稿はすでに完了している点に注意）。`sent/` 移動は行わず、`review-response.json` を残した状態で終了する
- Step 7 の移動完了検証が失敗 → `mv` が silent に失敗した可能性があるため Step 8 の失敗報告で手動確認を促し、`review-response.json` を残した状態で終了する
- ユーザーが Step 5 で承認を拒否 → 何もせず終了。payload ファイルは残す

## 実装上の制約

本スキルは通常の permission mode で使うことを想定する。引数なしは対話実行、`--auto-send` は scheduler / `/loop` など非対話運用向けに最終投稿承認だけをスキップする。どちらのモードでも既存 `/pr-codex:review` と統一感を持たせるため、以下の原則を踏襲する:

1. 各テンプレートは 1 テンプレート = 1 シェル実行単位として扱う
2. テンプレートの改変は変数置換のみ許可する。フラグ、引数順、引用符、リダイレクトはテンプレート記載どおりに使う
3. シェル演算子はテンプレート中に明示された `|` `<` `>` `2>` `&&` `||` `>&2` のみ許可する
4. `findings.verified.json` は必須の一次入力とし、`review.md` parser fallback は使わない。parse failure / shape failure / validator failure / `location.side != RIGHT` / 件数不一致 / posting policy 不整合時に Markdown fallback へ自動切替してはならない
5. payload JSON、`preflight-prompt.md`、`nits.md` の生成は Write ツールで行う（`jq -n` によるインラインでの複雑な配列組み立てや shell 文字列内 prompt 埋め込みは使わない）
6. `$()` / `for` / ヒアドキュメントは Step 1 common と Step 2b の固定テンプレートに明示された箇所以外では使わない。`while` / `xargs` は使わない
7. `mv` は `sent/` への移動以外では使わない
8. `gh` の write 系操作は `gh api --method POST .../reviews` のみとし、`gh pr review` / `gh pr comment` / `gh pr merge` などは使わない
9. 1 回の実行で処理する対象ディレクトリは 1 件のみとする。位置引数なしでは名前昇順の auto 選定を使い、PR URL / PR 番号指定時は direct mode として指定 PR の directory だけを検証する
10. 投稿前の Step 5 承認プロンプトは interactive mode では必須。`--auto-send` では最終投稿承認だけをスキップできるが、Step 5.5 の二重投稿防止と head SHA 再確認は必須。Should Fix / Nit は default では含めず、`--include-should-fix` / `--include-nit` 指定時だけ全件を inline comment に含める。`--include-nit` は `--include-should-fix` との併用必須とする
11. Step 3 の `python3 "$plugin_root/tasks/validate_findings.py" ...` と Step 4 の `python3 "$plugin_root/tasks/build_review_payload.py" ...` を **必ず**実行する。`findings.verified.json` 欠落、validator 失敗、または builder 失敗時に payload 生成や Markdown fallback へ進んではならない。payload を Claude がメモリ上で組み立てて Write ツールで書き出すことも禁止する
12. Step 4.5 の verifier pipeline は **必須**。スキップしてはならない。static stage（SARIF validator / `--verify`）の終了コード 0 と、`preflight-result.json` の cross-field validation 通過と `verdict == "PASS"` を確認するまで Step 5 に進まない。static stage が FAIL の場合に Codex semantic を実行してはならない。schema 検証観点では `$plugin_root/schemas/findings.v1.json`、`$plugin_root/schemas/sarif-2.1.0.json`、`$plugin_root/schemas/preflight-result.v1.json`、`$plugin_root/schemas/preflight-semantic.v1.json` の絶対パスを prompt / verifier コマンドに埋め込み、`--cd` 配下の相対 `schemas/` には依存しない

## ファイル構成

スキル本体:

```
$CLAUDE_PLUGIN_ROOT/skills/send/
  └── SKILL.md                ← 本ファイル
$CLAUDE_PLUGIN_ROOT/tasks/
  ├── validate_findings.py        ← findings.verified.json の schema / fingerprint / format / range validator
  ├── build_review_payload.py     ← review-payload.json / payload-manifest.json の決定論的 builder と manifest digest 検証 (--verify)
  ├── generate_findings_sarif.py  ← findings.verified.json から local-only SARIF を生成
  ├── validate_findings_sarif.py  ← findings.sarif の schema / count consistency validator
  └── validate_preflight_result.py ← preflight-result.json の合成 (--from-semantic / --semantic-skipped) / cross-field validator / 人間可読化 (--emit-markdown)
$CLAUDE_PLUGIN_ROOT/schemas/
  ├── findings.v1.json
  ├── sarif-2.1.0.json
  ├── preflight-result.v1.json
  └── preflight-semantic.v1.json
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
        ├── claude-review.json
        ├── codex-review.json
        ├── claude.log
        ├── review-payload.json     ← Step 4 の builder が生成する投稿予定 payload
        ├── payload-manifest.json   ← Step 4 の builder が生成する comment_map / counts / sha256 digest（`payload-manifest.v1`）
        ├── preflight-prompt.md     ← Step 4.5 の Codex semantic prompt（Write ツールで生成。must_fix 0 件なら作成しない）
        ├── preflight-semantic.json ← Step 4.5 の Codex semantic decisions (`schemas/preflight-semantic.v1.json`、`--output-last-message` で直接保存)
        ├── preflight-codex.md      ← Step 4.5 の validated JSON から派生生成した人間可読 verifier 結果
        ├── preflight-result.json   ← Step 4.5 の構造化 verifier 結果 (`schemas/preflight-result.v1.json`、`--from-semantic` / `--semantic-skipped` で合成)
        ├── preflight-codex.log     ← Codex 実行時の標準出力・標準エラー（`2>&1`）
        ├── nits.md                 ← primary path で Nit がある場合のみ生成（local artifact として保持）
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
              ├── payload-manifest.json  ← 追加: comment_map / digest manifest
              ├── review-response.json   ← 追加: gh api のレスポンス (.html_url 等を含む)
              ├── pr.diff
              ├── pr.diff.ranges.txt
              ├── claude-review.json
              ├── codex-review.json
              ├── claude.log
              ├── preflight-prompt.md
              ├── preflight-semantic.json
              ├── preflight-codex.md
              ├── preflight-result.json
              ├── preflight-codex.log
              ├── nits.md                ← Nit があった場合のみ。他 artifact と一緒に移動される
              └── codex.log
```
