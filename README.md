# pr-codex

GitHub PRを **Claude Code** と **Codex CLI** の2者レビュー方式で自動レビューする Claude Code プラグイン。

## 特徴

- **2者レビュー**: Claude Code と Codex CLI が独立してPRをレビューし、結果を統合
- **自動巡回**: `/loop` と組み合わせて定期的にレビュー依頼PRを検出・処理
- **冪等実行**: status.json による状態管理で、完了済みPRのスキップや失敗時の再実行に対応
- **最小権限**: 各レビューツールは読み取り専用で動作し、PRへのコメント投稿時はユーザー承認または明示的な `--auto-send` / `--auto-submit` 指定後の safety gate 通過を必要とする
- **生成と投稿を分離**: レビュー生成 (`/pr-codex:review`) と投稿 (`/pr-codex:send`) を別スキルに分け、通常は投稿前にユーザーが内容を承認する。`/pr-codex:review --auto-send` を明示した場合だけ、レビュー completed 後に `/pr-codex:send <PR URL> --auto-submit` 相当の auto-send phase へ進む
- **反復精緻化 + halting**: `refine` / `challenge` / `verify` を round 管理し、`max_rounds` / `time_budget_ms` / `no_new_evidence` / `repeated_contradiction` で停止する

## 必要なもの

- Claude Code
- Codex CLI (`codex-cli 0.146.0` 以上、`codex --ask-for-approval never -m gpt-5.6-sol -c 'model_reasoning_effort="max"' ... exec --output-schema ... --output-last-message ...` が使えること。send Step 4.5 の semantic preflight も `-m gpt-5.6-sol` を使うが effort は `high`)
- GitHub CLI (`gh`)
- `jq`（SKILL.md 内の全テンプレートで利用する。macOS 標準では未インストール）
- `python3`（同梱 validator / eval runner で `findings.candidates.json` / `findings.verified.json` / `status.json` / `preflight-result.json` / `findings.sarif` / fixture scoring artifact を検証するため）
- Python package `jsonschema` (`jsonschema>=4,<5`) — `tasks/validate_findings_sarif.py` が同梱 OASIS SARIF v2.1.0 schema に対して official schema validation を行うため。未インストール時は schema-invalid な SARIF を通さないよう fail-closed で失敗する

## セットアップ


### Python 依存と plugin root

`validate_findings_sarif.py` の official OASIS SARIF schema validation には `jsonschema>=4,<5` を使う。PEP 668 で system Python への install が拒否される macOS/Homebrew 環境では venv を推奨する。

```bash
python3 -m venv ~/claude-loop-pr-codex/.venv
. ~/claude-loop-pr-codex/.venv/bin/activate
python3 -m pip install 'jsonschema>=4,<5'
```

venv を使わず user site に入れる場合だけ、環境によっては次のように `--break-system-packages` が必要になる。

```bash
python3 -m pip install --user --break-system-packages 'jsonschema>=4,<5'
```

`CLAUDE_PLUGIN_ROOT が未設定` の shell でも、SKILL.md の command template は `plugin_root="${CLAUDE_PLUGIN_ROOT:-...}"` で plugin root を自己解決する。fallback block は review のセットアップ / send の Step 1 common の 1 箇所だけに置かれ（#111 で各テンプレートへのコピペを廃止）、plugin cache の `pr-codex/tasks/validate_findings.py` marker から root を算出して解決値を標準出力に出す。以降のテンプレートの `$plugin_root` は置換対象変数であり、この fallback が解決した値を実値置換して使う。fallback の解決結果を使わず、validator/tool 呼び出しを手動で絶対パスに置換しない。

### インストール

```
/plugin marketplace add yuki777/pr-codex
/plugin install pr-codex@pr-codex
```

### アップデート

```
claude plugin marketplace update pr-codex
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
- `--effort max` — Claude Code 本体の推論設定。`/pr-codex:review` の depth policy とは別軸
- Claude Code 側のレビュー (hunter) は、review Step 3 で `claude-fable-5` に固定し、`metadata.json.review_engines` に記録した同じ値を `--model` で明示指定して実行する。投稿フッター（#124）の記録値と実行モデルの一致を構成的に保証する
- Codex CLI 側のレビュー (hunter) は、スキル内で `-m gpt-5.6-sol` と `model_reasoning_effort="max"` を指定して実行する（#124）。[公式の GPT-5.6 ガイド](https://developers.openai.com/api/docs/guides/model-guidance?model=gpt-5.6)で Sol の `max` 対応を確認し、`codex-cli 0.146.0` の実行スモークでも `model: gpt-5.6-sol` / `reasoning effort: max` を確認済み。send Step 4.5 の semantic preflight も `-m gpt-5.6-sol` を使うが、7,301-byte の prompt と upstream findings 入力を high / xhigh 間で byte-identical に揃えた既存実測で品質 gate が同値かつ high の方が短時間だったため `model_reasoning_effort="high"` を維持する。hunter (review Step 4b) と投稿前検証 (send Step 4.5) はどちらも `--ignore-user-config` を使うため (#111)、user config 由来の外部 MCP は無効。GitHub 由来のレビュー文脈（PR 説明文・既存レビューコメント）は、親（メインコンテキスト）が read-only で取得した `pr-context.md`（sanitized context pack）として hunter に渡す（外部への到達経路の遮断は、4a が `--setting-sources ""` + `--tools "Read,Glob,Grep,Bash"` + `--strict-mcp-config`、4b が `--ignore-user-config`）
- Codex CLI は `codex-cli 0.146.0` 以降のみ対応する。旧バージョン向けテンプレートは打ち切り、`--sandbox read-only` / `--color never` / `--ephemeral` を並べる旧形式ではなく、`-c sandbox_mode=read-only` と hunter / preflight 共通の `--ignore-user-config` を使う。hunter (review Step 4b) と preflight verifier (send Step 4.5) は `--output-schema` / `--output-last-message` による structured output で JSON を直接受ける（hunter の GPT-5.6 Sol / max は 0.146.0 で動作確認済み）。hunter prompt は bash double-quote 埋め込みではなく prompt file + stdin（4a は `claude -p < prompt.md`、4b は `exec - < prompt.md`）で渡し、旧 4 文字エスケープ規則は廃止済み (#111)



```
# 手動実行でレビューする
/pr-codex:review

# PR URLを直接指定してレビューする
/pr-codex:review https://github.com/org/repo/pull/123

# PR URLを直接指定してレビューし、completed 後に Must Fix のみ自動投稿する
/pr-codex:review https://github.com/org/repo/pull/123 --auto-send

# 現在のgit repositoryのPR番号を指定してレビューする
/pr-codex:review 123

# 現在のgit repositoryのPR番号を指定してレビューし、completed 後に Must Fix のみ自動投稿する
/pr-codex:review 123 --auto-send

# 10分間隔で自動レビューする
/loop 10m /pr-codex:review

# 10分間隔で自動レビューし、completed 後に Must Fix のみ自動投稿する
/loop 10m /pr-codex:review --auto-send
```

## Depth control

`/pr-codex:review` はレビュー深度を `standard` / `deep` の 2 値で記録する。depth はオプションでは受け付けない。ranker は全 PR を `standard` で開始し、hunter 後に small / fully verified / conflict-free gate を host controller が通過した場合だけ `deep` へ上げる。

この 20 分は review budget / run-plan budget であり、Bash tool の foreground timeout ではない。Claude Code Bash tool の foreground timeout 上限 600000 ms を超えるため、review hunters は run_in_background: true で起動し、foreground timeout=1200000 は指定しない。

| 入力 / signal | selected depth | artifact |
| --- | --- | --- |
| ranker 初期判定（全 PR） | `standard` | `depth_source=default`、candidate gate まで auto-deep を保留 |
| `lines_added + lines_removed > 5000` | `standard` | `depth_source=default`, `depth_downgraded=false`, `depth_reason` に大規模ガード理由を記録 |
| hunter 後に `recommended_mode=standard` / `budget_class=small`、全候補 `verified`・3軸既知・severity 矛盾なし | `deep` | host controller が `depth_source=auto` と専用 `depth_reason` を記録 |

`run-plan.json` には `depth_actual` / `depth_source` / `depth_reason` / `depth_requested=null` / `depth_downgraded=false` / `depth_downgrade_reason=null` を保存する。hunter 後の auto-deep は `tasks/refinement_loop.py --apply-auto-deep` だけが更新し、モデルの自由判断では変更しないため、standard/deep の選択は deterministic に追跡できる。

`recommended_mode` (`standard` / `focused` / `skip`) は depth とは直交する別軸であり、「観点や対象範囲の絞り込み」を表す。ただし現行の hunter 後 auto-deep gate は安全側に `recommended_mode=standard` を必須とするため、production workflow が自動生成する `depth_actual=deep` は `recommended_mode=standard` の場合だけである。

## レビューフロー

1. **PR候補の取得** — 引数なしなら GitHub Search API で `review-requested` かつ `status:success` のPRを一覧取得。PR URL / PR番号指定時は対象を直接解決
2. **候補の選定** — 引数なしなら未レビュー・失敗・追加コミットありで、current head の `ci-status.json.state == "success"` を満たす最初の1件だけを選定。CI が `failure` / `pending` / `skipped` / 未取得の候補はスキップする。直接指定時は review requested / approve 済み判定 / CI success gate をスキップし、`status.json` と `head_sha` で冪等性を確認
3. **作業ディレクトリの準備** — PRブランチを各ツール用に個別に shallow clone
4. **2者レビュー実行** — Claude Code と Codex CLI が並行してレビュー
5. **反復精緻化と結果の統合** — 両者の指摘を `refine` / `challenge` / `verify` round で精査し、halting 後に `review-rounds.json` / `findings.verified.json` / `review.md` を生成
6. **結果報告** — レビュー結果の要約をユーザーに報告。`--auto-send` 指定時は completed 後に `/pr-codex:send <PR URL> --auto-submit` 相当の auto-send phase へ進む
4. **stage 化されたレビュー実行** — 既存 Step を logical stage として扱う（ranker / hunter / verifier / explainer）
   - ranker: `run-plan.json` で PR risk/area と実行方針を分類
   - hunter: Claude Code と Codex CLI が並行して広めに候補を集め、structured output（`claude-review.json` / `codex-review.json`、`schemas/hunter-result.v1.json` 準拠）を `tasks/merge_hunter_results.py` が検証・合成して `findings.candidates.json` を残す。具体的根拠が不足する候補は `evidence_state=needs_evidence` として verifier へ送る
   - verifier: REAL / TRIGGERABLE / IMPACTFUL の 3軸 gate + evidence ladder + counterexample で絞り込み、非ゲート metadata の `blast_radius` を付けて `findings.verified.json` を canonical artifact にする
   - explainer: verified findings から `review.md` と local-only `findings.sarif` を派生生成する
5. **結果報告** — レビュー結果の要約をユーザーに報告。`--auto-send` 指定時は Must Fix のみを対象に send 側の verifier pipeline / head SHA gate / 二重投稿防止 gate を通して投稿する

Stage ごとの責務、input/output artifact、halting 条件は [`skills/review/STAGES.md`](skills/review/STAGES.md) を参照してください。

## レビューの投稿

通常の `/pr-codex:review` は `review.md` をローカル生成するのみで、PRへの投稿は行わない。投稿は別スキル `/pr-codex:send` で実行する。レビュー完了後に Must Fix のみ自動投稿したい場合は、`/pr-codex:review <PR URL|PR number> --auto-send` を使う。この場合も slash command を再帰実行するのではなく、completed 後に `skills/send/SKILL.md` の direct mode を `$ARGUMENTS = "$pr_url --auto-submit"` として同じ safety gate 付きで実行する。

```
/pr-codex:review https://github.com/org/repo/pull/123 --auto-send
/pr-codex:review 123 --auto-send
/pr-codex:send
/pr-codex:send --auto-submit
/pr-codex:send --include-should-fix
/pr-codex:send --auto-submit --include-should-fix --include-nit
/pr-codex:send https://github.com/org/repo/pull/123 --auto-submit
/pr-codex:send https://github.com/org/repo/pull/123 --auto-submit --include-should-fix
/pr-codex:send 123 --auto-submit
```

- `/pr-codex:send`: 従来どおり GitHub Reviews API の payload サマリを表示し、最終承認 prompt でユーザーの明示承認を得てから、Must Fix 全件のみ投稿する
- `/pr-codex:send --auto-submit`: Step 4.5 の verifier pipeline が PASS した後、最終承認 prompt なしで Must Fix 全件のみ投稿へ進む
- `/pr-codex:send <PR URL> --auto-submit`: URL に対応する completed レビューだけを対象にし、名前昇順の auto 選定は行わない
- `/pr-codex:send <PR number> --auto-submit`: `~/claude-loop-pr-codex` に同番号の active directory が 1 件だけある場合に限り対象を解決する。複数一致時は曖昧として中断し、PR URL 指定を案内する
- `/pr-codex:review <PR URL|PR number> --auto-send`: レビューが completed になった後、canonical な `metadata.json.pr_url` を対象に `/pr-codex:send <PR URL> --auto-submit` 相当の direct mode を続けて実行する。投稿対象は Must Fix のみで、Should Fix / Nit は含めない
- `--include-should-fix` は Must Fix + Should Fix を inline comment として投稿する
- `--include-nit` は `--include-should-fix` と併用し、Must Fix + 投稿可能な Should Fix + 投稿可能な Nit を inline comment として投稿する。diff 範囲外のものは body の `## 行コメント不可 (diff 範囲外)` へ退避する
- `--auto-submit` でも Step 4.5 の verifier pipeline はスキップしない。canonical artifact validation、Must Fix 件数整合、diff range、`findings.sarif`、`preflight-result.json` の cross-field validation 通過と `verdict == PASS` は必須
- unknown option、解釈できない位置引数、位置引数が2つ以上、重複オプションは unsupported argument として中断する。`--include-nit` 単独も unsupported argument として扱う
- 投稿直前に現在の PR head を再取得して `metadata.json.head_sha` と比較し、不一致なら古い review を自動投稿しない。`review-response.json` に `.html_url` が既にある場合も二重投稿防止のため中断する

## 投稿後フィードバックの学習

`/pr-codex:learn` は投稿後に GitHub から返ってきた明示 signal だけを、次回レビュー改善用のローカル artifact として保存する。生成物は `learn-result.json` と `feedback-artifacts/*.json` で、secret/token/ローカルパスは scrub される。F10 以降は、その public-safe artifact から repo-local な `episodes.jsonl` を任意生成し、次回レビュー時に限定検索できる。

学習対象:

- GraphQL review thread の `isResolved: true` → `addressed`
- GraphQL review thread の `isOutdated: true` → `superseded`
- 明示ラベル/コメント `pr-codex/false-positive` → `false_positive`

上記はいずれも pr-codex が投稿した review thread だけに適用する。snapshot の `review_author` / `review_authors`（未指定時は `chatgpt-codex-connector`）と thread 先頭コメント author が一致しない thread は学習しない。

学習しないもの:

- author 無反応の未解決 thread
- pr-codex 以外（人間レビュアーや別 bot）が投稿した review thread
- PR が merge された事実だけ
- bot/generated marker だけ

実体は同梱 helper `$CLAUDE_PLUGIN_ROOT/tasks/learn_feedback.py` で、snapshot JSON から冪等に artifact を生成する。`CLAUDE_PLUGIN_ROOT` が設定済みの環境では `python3 $CLAUDE_PLUGIN_ROOT/tasks/learn_feedback.py` として実行できる。

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
python3 "$plugin_root/tasks/learn_feedback.py" --input feedback-snapshot.json --output-dir ~/claude-loop-pr-codex/learn/yuki777-pr-codex-60-103766c
```

### Episode memory (F10)

`tasks/episode_memory.py` は `feedback-artifacts/*.json` から `episode.v1` JSONL を作る repo-local helper である。episode は **public-safe な短い summary のみ**を持ち、raw log / raw comment payload / secret / token / ローカル絶対パスは保存しない。

保存対象:

- `/pr-codex:learn` が作った `addressed` / `superseded` / `false_positive` artifact
- PR type (`pr_classification.primary_type` または `all_types`)、対象 path、finding class が明示できるもの
- 次回レビューで「参考にして再検証する」価値がある設計判断・false positive・対応済み指摘

禁止対象:

- raw GitHub log、CI raw log、credential file contents、API key / token / cookie / private key
- `/Users/...`、`/home/...`、`/tmp/...` などのローカル絶対パス
- author 無反応、merge された事実だけ、pr-codex 以外の review thread
- unrelated PR へ広く適用できない文脈を、PR type / path / finding class なしで保存すること

```bash
python3 $CLAUDE_PLUGIN_ROOT/tasks/episode_memory.py write \
  --feedback-artifact ~/claude-loop-pr-codex/learn/yuki777-pr-codex-60-103766c/feedback-artifacts/false_positive-PRRT_1.json \
  --store ~/claude-loop-pr-codex/episodes/yuki777-pr-codex/episodes.jsonl \
  --pr-type python-validator-runtime \
  --finding-class secret-handling
```

次回レビューでは、PR type / path / finding class の **3 条件すべて**で限定検索する。stale episode は `use_policy: context_only_reverify` として返り、無条件採用してはいけない。fresh episode も `use_policy: reverify_current_diff` として扱い、現在の diff で再確認する。

```bash
python3 $CLAUDE_PLUGIN_ROOT/tasks/episode_memory.py retrieve \
  --store ~/claude-loop-pr-codex/episodes/yuki777-pr-codex/episodes.jsonl \
  --pr-type python-validator-runtime \
  --path tasks/validate_run_plan.py \
  --finding-class secret-handling
```

`/pr-codex:send` の挙動:
1. 位置引数なしの場合は `~/claude-loop-pr-codex/` 配下から `status.json` が `state:completed` でかつ `findings.verified.json` / `review.md` が存在するディレクトリを1件選定する（名前昇順の先頭1件）。PR URL / PR 番号が指定された場合は、その PR に対応する completed レビューだけを対象にし、auto 選定は行わない。PR 番号のみ指定が複数 directory に一致した場合は中断して PR URL 指定を案内し、指定 PR が `sent/` にしか無い場合は既に send 済みとして中断する
2. `findings.verified.json` を必須の一次入力として、`tasks/build_review_payload.py` が `Must Fix` / `Should Fix` / `Nit` の投稿方針の抽出、`review.md` の `## 良い点` の body 反映と `## 総評` の非空検証、投稿用総評の生成（投稿対象の finding と event だけから決定論的に生成し、件数は表示せず、投稿しない severity や非公開 finding には言及しない。#120）、自動レビューフッターの付加（body 末尾に pr-codex のバージョン（`producer.version`）とレビューに使ったモデル（実行順の `Claude Code`、`Codex` の2件ちょうどを要求する `metadata.json.review_engines`）を明記し、Must Fix があり Step 4.5 の semantic preflight を実行する投稿では検証側モデルも表示する。effort は確定できないため表示しない（#128）。欠落・不正なら builder が非ゼロ終了する fail-closed。#124）、行範囲検証（`metadata.json.files[]` メンバーシップ込み）、event 判定、body セクション構築を決定論的に行い、`review-payload.json` と `payload-manifest.json`（`comment_index → finding_id` の対応表、semantic 対象の全 Must Fix id (`semantic_targets`)、非公開一覧 (`withheld`)、active severity flags、入力 artifact の role 付き sha256 digest）を生成する。security の Must Fix のうち `disclosure_policy: local_only` または `post_policy: local_only / suppress` のものは公開 payload のどこにも載せず `withheld` に記録だけ残す（非 security の Must Fix にこれらの post_policy があれば posting contract 違反として中断する。F13 以降は Markdown parser fallback へ切り替えず、欠落・schema 不整合なら中断する。root-cause cluster がない場合は Must Fix 件数も完全一致を要求し、cluster がある場合は canonical / Markdown / SARIF は全 finding 件数、`review-payload.json` は representative comment 件数として検証する）
3. `--include-should-fix` 指定時は `Should Fix` のうち `post_policy: body_summary` かつ `explanation_postable: true` の候補を inline comment に含める。未指定なら含めない
4. `--include-nit` 指定時は `Nit` のうち `post_policy: body_summary` かつ `explanation_postable: true` の候補を inline comment に含める。`local_only` / `suppress` / `explanation_postable: false` の Nit は投稿しない。`nits.md` は Nit がある場合のみ local artifact として残す。diff 範囲外のものは body の `## 行コメント不可 (diff 範囲外)` へ退避する
5. Step 4.5 の verifier pipeline を schema / range / semantic / payload consistency の 4 stage で実行する。`schema_validation` / `range_validation` / `payload_consistency` の 3 つの static stage は Python validator / builder（`validate_findings.py` / `validate_findings_sarif.py` / `build_review_payload.py --verify`）が担い、static FAIL の場合は Codex を呼ばず fail-closed で中断する。schema stage では `findings.sarif` の schema validation と `findings.verified.json` ↔ `review.md` ↔ `review-payload.json` ↔ `findings.sarif` の Must Fix count 整合性を検証する（root-cause cluster がある場合、canonical / Markdown / SARIF は全 Must Fix finding 数を保持し、`review-payload.json` は representative inline comment 数へ減ることを許可する）。オプション未指定時の Should Fix / Nit payload 混入禁止、`--include-should-fix` / `--include-nit` 指定時の inline 許可 severity、diff 範囲外または LEFT-side 非対応で body 退避された opted-in finding の valid exclusion は、builder の active severity flags 適用と `payload-manifest.json` の `comment_map` で決定論的に保証する。`--verify` は digest 照合に加えて manifest 構造・`comment_map` / `event` / `counts` / `semantic_targets` の再突き合わせも行い、manifest 自体の改竄を検出する。`semantic_preflight` だけを Codex (GPT-5.6、`-m gpt-5.6-sol`) が担い、canonical の全 Must Fix finding（cluster 非代表 member・withheld を含む `semantic_targets`）を structured output（`schemas/preflight-semantic.v1.json`）で `confirmed` / `refuted` / `insufficient_evidence` の 3 値判定する（Must Fix の反証探索。反証成功 = 不採用）。Must Fix 0 件なら Codex preflight は skip する。`validate_preflight_result.py --from-semantic` が static 結果と合成して `preflight-result.json`（verdict / stage status / counts は host 算出）を生成し、validated JSON から人間可読の `preflight-codex.md` を派生生成する
6. GitHub Reviews API への payload サマリをユーザーに提示する。引数なしは明示的な承認を得る。`--auto-submit` は最終承認 prompt なしで次へ進む
7. 投稿直前に `build_review_payload.py --verify` で `payload-manifest.json` の構造・sha256 digest・ドライラン再生成との完全一致を再確認し（ローカル TOCTOU と manifest 協調改竄の防止）、`review-response.json` の `.html_url` が未設定であることを確認し、現在の PR head を再取得して `metadata.json.head_sha` と一致することを確認する
8. 承認後または `--auto-submit` の safety gate 通過後、`gh api --method POST .../reviews` で投稿（`event` は Must Fix ありなら `REQUEST_CHANGES`、Must Fix 0 件なら `APPROVE`。ただし `ci-status.json.state` が `failure` / `pending` の場合は自動 `APPROVE` を抑止して `COMMENT` に落とし、CI 状態を body と Step 5 に表示する。また、投稿アカウントが PR 作者と同一の self-PR では GitHub が `APPROVE` / `REQUEST_CHANGES` を 422 で拒否するため、Step 2b の read-only 検知で event を常に `COMMENT` に抑止し、投稿者 identity を判定できない場合は投稿前に中断する）
9. 投稿成功後、対象ディレクトリを `~/claude-loop-pr-codex/sent/$org-$repo-$pr-$head_sha_short/` に移動する（同一 PR でも HEAD 更新後の再投稿履歴が衝突しないよう、`head_sha` の先頭 7 文字を suffix に付ける）
10. GitHub 投稿と `sent/` 移動が両方成功した場合だけ、成功報告後に `/clear` を単独で実行して新しい conversation へ移る。失敗時、承認拒否時、verifier FAIL、safety gate 中断、または `sent/` 移動失敗時には `/clear` しない

対話実行では `/pr-codex:send` を使う。scheduler / `/loop` からレビュー生成後の投稿まで1コマンドで進めたい場合は `/pr-codex:review --auto-send` を使う。既に completed の review artifact を投稿するだけなら `/pr-codex:send <PR URL> --auto-submit` を使う。`/pr-codex:review` の completed 報告末尾には、対象 PR URL と Must Fix / inline 投稿可能な Should Fix 件数入りの `/pr-codex:send <PR URL> --auto-submit` 例が表示される。`--auto-send` 指定時は、`$count_must_inline == $count_must` の場合だけ Must Fix のみを対象に auto-send phase へ進み、Should Fix / Nit は自動投稿しない。1回の実行で1件のみ処理する。

### Should Fix / Nit の取り扱い

- `Must Fix` は従来どおり GitHub review の inline comment として投稿される。`root_cause_clusters[]` がある場合は、各 cluster の `representative_finding_id` を inline 代表として扱い、同じ root cause の他 finding は代表コメント本文の affected findings summary に短く列挙する（canonical / SARIF / review.md には全 finding を残し、`review-payload.json` の `comments[]` だけ representative count になる。preflight の count gate は full count と representative payload count を別々に検証する）
- `Should Fix` は default では投稿されない。`--include-should-fix` 指定時だけ、`post_policy: body_summary` かつ `explanation_postable: true` の候補を PR の inline comment として投稿できる
- `Nit` は default では投稿せず、`nits.md` に控えとして残す。`--include-nit` 指定時だけ、`post_policy: body_summary` かつ `explanation_postable: true` の Nit を PR の inline comment として投稿できる。`local_only` / `suppress` / `explanation_postable: false` の Nit は投稿せず、投稿後は `sent/` 配下の履歴ディレクトリで確認できる
- `findings.verified.json` がない fallback path では、従来どおり `Should Fix` / `Nit` / 補足を投稿 payload に含めない

### Local artifacts と GitHub 投稿対象の境界

- GitHub Reviews API に送るのは `review-payload.json` の `body` と `comments[]` のみ。`comments[]` は Must Fix と、明示オプションで許可された Should Fix / Nit の inline comment を含む
- `findings.verified.json` は canonical source、`review.md` / `review-payload.json` / `findings.sarif` / `nits.md` は派生成果物。canonical を単一の真実源とし、派生成果物を手で編集して canonical に逆流させない
- `findings.sarif` は M2 では **local-only artifact**。GitHub Code Scanning への upload、CI からの公開、PR への添付は自動化しない（M3 の別 Issue で扱う）
- `posting.post_policy=suppress` の finding は SARIF にも出さず、canonical 内部記録だけに残す。`local_only` と `nit` は SARIF `suppressions[]` を付けてノイズ公開の経路を閉じる


## Regression eval / fixture scoring (F11)

CI では LLM を起動せず、固定 fixture と stub artifact だけで schema / runner の deterministic smoke test を行う。small / medium / large の既存 oracle に加え、Issue #112 の `fixtures/positive/` は 3 個の既知バグと 1 個の既知 false-positive trap を持つ positive-seeded fixture として検証する。手動 deep eval では実レビューで生成した `findings.verified.json` を fixture oracle と比較し、4 fixture すべてを M1→M2 gate report に集約する。

### CI-safe smoke

```bash
python3 -m unittest discover -s tasks -p "test_*.py"
```

このテストは `fixtures/{small,medium,large,positive}/scoring-stubs/*.findings.verified.json` を使い、`expected-findings.v1` / `score-report.v1` / `m1-m2-gate.v1` の validation と scoring runner の分岐を確認する。`fixtures/positive/eval-report.json` は GPT-5.6 の round policy と reasoning effort 比較を `eval-report.v1` で記録する。CI smoke に実 LLM / `gh api` / GitHub write 操作は含めない。

### Manual deep eval

実レビューの出力を採点する場合は、fixture ごとに `findings.verified.json` を用意して `artifacts/`（gitignore 済み）へ score report を出す。

```bash
mkdir -p artifacts

python3 tasks/score_fixture.py \
  --expected fixtures/small/expected-findings.json \
  --actual /path/to/small/findings.verified.json \
  --out artifacts/score-small.json

python3 tasks/score_fixture.py \
  --expected fixtures/medium/expected-findings.json \
  --actual /path/to/medium/findings.verified.json \
  --out artifacts/score-medium.json

python3 tasks/score_fixture.py \
  --expected fixtures/large/expected-findings.json \
  --actual /path/to/large/findings.verified.json \
  --out artifacts/score-large.json

python3 tasks/score_fixture.py \
  --expected fixtures/positive/expected-findings.json \
  --actual /path/to/positive/findings.verified.json \
  --out artifacts/score-positive.json
```

M1→M2 gate は運用実測値を `m1-m2-inputs.v1` として外部供給する。未計測項目は省略でき、省略された criteria は `unknown` として記録される（unknown は fail にはしない）。

```json
{
  "schema_version": "m1-m2-inputs.v1",
  "payload_422_count": 0,
  "must_fix_count_by_source": {
    "findings_verified": 2,
    "review_md": 2,
    "payload": 2
  },
  "step_4_5_pass_rate_baseline": 0.78,
  "step_4_5_pass_rate_current": 0.81,
  "run_plan_emitted": true,
  "loop_completion_rate_baseline": 0.92,
  "loop_completion_rate_current": 0.95
}
```

```bash
python3 tasks/m1_m2_gate.py \
  --score-reports artifacts/score-small.json artifacts/score-medium.json artifacts/score-large.json artifacts/score-positive.json \
  --inputs artifacts/m1-m2-inputs.json \
  --out artifacts/m1-m2-gate.json
```

`m1-m2-gate.json` は `payload_compat_422` / `must_fix_count_consistency` / `step_4_5_pass_rate` / `run_plan_emitted` / `loop_completion_rate` / `fixture_scoring_gate` を `pass` / `fail` / `unknown` で記録し、総合結果を `pass` / `fail` / `blocked_by_unknowns` に集約する。`fixture_scoring_gate` は small / medium / large の3 fixture が揃っていることに加え、各 `score-report.v1` に埋め込まれた oracle 由来の `scoring_gate` / `oracle_sha256` / `expected_finding_ids` と `gate_checks` の閾値が fixture ごとの固定 oracle と一致することも確認する。

## ファイル構成

Plugin 内の共用 helper:

```
$CLAUDE_PLUGIN_ROOT/skills/lib/
  └── extract-diff-ranges.awk    # send / review 共用の diff hunk 範囲抽出
```

Runtime artifacts:

```
~/claude-loop-pr-codex/
  ├── $org-$repo-$pr/             # 進行中 / 未投稿のレビュー
  │     ├── status.json           # 実行状態（running / completed / failed）
  │     ├── metadata.json         # PR情報（org, repo, pr_number, head_sha 等）
  │     ├── ci-status.json        # GitHub Actions / status checks の read-only 正規化 artifact
  │     ├── ci-summary.md         # raw log を保存しない public-safe CI 要約
  │     ├── run-plan.json         # preflight 指標、recommended_mode、選択 depth、M2 routing_decision、PR classification（ローカル専用）
  │     ├── pr-classification.json # PR 種別と read-only specialist checklist（run-plan から派生）
  │     ├── pr.diff               # PR 差分 (unified diff)
  │     ├── pr.diff.ranges.txt    # GitHub inline comment 可能範囲
  │     ├── clone-claude/         # Claude Code 用 shallow clone
  │     ├── clone-codex/          # Codex CLI 用 shallow clone
  │     ├── claude-review.json    # Claude Code hunter の structured output (`schemas/hunter-result.v1.json`)
  │     ├── codex-review.json     # Codex CLI hunter の structured output (`schemas/hunter-result.v1.json`)
  │     ├── findings.candidates.json # hunter → verifier 境界の候補 (`schemas/findings.candidates.v1.json`)
  │     ├── findings.verified.json # canonical findings (`schemas/findings.v1.json`)
  │     ├── findings.sarif        # SARIF v2.1.0 派生成果物。M2 では local-only / upload しない
  │     ├── validation-report.json # validation の副成果物（canonical findings とは分離）
  │     ├── review-rounds.json    # F5 refine/challenge/verify round artifact。verifier FAIL 候補は local_only で保持
  │     ├── review.md             # 統合レビュー（最終成果物）
  │     ├── review-payload.json   # /pr-codex:send Step 4 の builder が生成する投稿予定 payload
  │     ├── payload-manifest.json # /pr-codex:send Step 4 の comment_map / counts / sha256 digest
  │     ├── preflight-prompt.md   # /pr-codex:send Step 4.5 の Codex semantic prompt
  │     ├── preflight-semantic.json # /pr-codex:send Step 4.5 の Codex semantic decisions
  │     ├── preflight-codex.md    # /pr-codex:send Step 4.5 の人間可読 verifier 結果
  │     ├── preflight-result.json # /pr-codex:send Step 4.5 の構造化 verifier 結果（--from-semantic / --semantic-skipped で合成）
  │     ├── preflight-codex.log
  │     ├── nits.md               # Nit がある場合のみ。PR には投稿しない控え
  │     ├── claude.log
  │     └── codex.log
  └── sent/                       # /pr-codex:send で投稿済み
        └── $org-$repo-$pr-$head_sha_short/ # 投稿後にここへ移動される
              ├── findings.candidates.json
              ├── findings.verified.json
              ├── findings.sarif        # local-only SARIF。Code Scanning upload は自動化しない
              ├── review.md
              ├── nits.md               # Nit があった場合のみ
              ├── review-payload.json   # 投稿した GitHub Reviews API の payload
              ├── review-response.json  # gh api のレスポンス（.html_url 等）
              ├── preflight-prompt.md
              ├── preflight-codex.md
              ├── preflight-result.json
              ├── review-rounds.json
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

- `run-plan.json` は `schemas/run-plan.schema.json` で定義し、review の depth policy と `recommended_mode` を記録する
- `pr-classification.json` は `schemas/pr-classification.schema.json` で定義し、`docs-only` / `test-only` / `workflow-ci` / `review-skill-contract` / `python-validator-runtime` / `security-sensitive` / `mixed` の PR 種別と `selected_specialists` を記録する。hunter は read-only で、自動 exploit / network pentest は行わない
- `depth_actual` は `standard` / `deep` の 2 値。`depth_source` は `auto` / `default`、`depth_requested` は常に `null`
- `depth_downgraded` は常に `false`、`depth_downgrade_reason` は常に `null`。大規模 PR は downgrade ではなく default standard として扱う
- `recommended_mode == "skip"` の場合だけ `skip_reason` を非空にし、それ以外は `skip_reason=null` にする。`recommended_mode` は depth と直交し、GitHub への自動投稿範囲は depth では拡大しない。`--auto-send` でも default の投稿対象は Must Fix のみで、Should Fix / Nit は含めない
- hunter の structured output は `schemas/hunter-result.v1.json` (JSON Schema Draft 2020-12) で定義する。strict structured output 互換のため全フィールド required（optionality は nullable 型で表現）とし、`status` は `findings` / `clean` / `diff_unavailable` の 3 値。`tasks/merge_hunter_results.py` が両 hunter 出力を検証して `findings.candidates.json` へ決定論的に変換する
- hunter → verifier 境界の debug artifact は `schemas/findings.candidates.v1.json` (JSON Schema Draft 2020-12) で定義する。`findings.candidates.json` は `id == fingerprint` や 3軸 / evidence / posting policy の最終確定を要求せず、`evidence_state=needs_evidence` を正式に許容する。verifier が canonical findings へ揃え、`blast_radius` は非ゲート metadata として保持する
- canonical runtime artifact は `schemas/findings.v1.json` (JSON Schema Draft 2020-12) で定義する
- F5 の round artifact は `schemas/review-rounds.v1.json` で定義し、`review-rounds.json` に `max_rounds` / `time_budget_ms` / `no_new_evidence_rounds` / `repeated_contradiction_limit` と host state metrics を保存する。`max_rounds` は通常 2、deep / security / data migration だけ 3、hard cap は 3。`tasks/refinement_loop.py --plan-next` が round 前後の candidate snapshot、SHA-256 state digest、実測経過時間、未解決対象 ID を使って継続・停止を決定し、モデル申告だけの「新規根拠」は採用しない
- review-rounds カウンタでは、`rounds[].output_candidates_count` と `metrics.posted_candidate_count` はどちらも remaining ACTIVE candidates を表す。`posted_candidate_count` は最終 round の `output_candidates_count` であり、名前に反して GitHub に投稿した件数ではない。また、`findings.verified.json の件数ではない`（canonical findings の採用数ではない）
- fixture oracle は `schemas/expected-findings.v1.json` で定義する。runtime artifact とは分離し、`expected_outcome` / `acceptable_overrides` / `strictness_profile` / `minimum_evidence_level` など採点用メタデータを保持する
- fixture scoring の出力は `schemas/score-report.v1.json`、M1→M2 gate report は `schemas/m1-m2-gate.v1.json` で定義する
- CI read-only gate の出力は `schemas/ci-status.v1.json` で定義し、`read_only: true` と `policy.github_writes/rerun/cancel/raw_logs_persisted: false` を固定する
- `findings.verified.json` は top-level `generated_at` を持ち、per-finding `created_at` は持たない
- `findings.verified.json` は任意で top-level `root_cause_clusters[]` を持てる。各 cluster は `id` / `summary` / `representative_finding_id` / `finding_ids` を持ち、`finding.root_cause_id` から参照する。validator は cluster id の重複、未知 finding id、representative が member でない状態、representative severity が cluster 内最高 severity より低い状態を拒否する
- `findings.verified.json.pr.repository` は **投稿先の base repo** (`owner/repo`) に固定する。fork PR でも head repo ではなく、`metadata.json.repository_full_name` および `/pr-codex:send` の投稿先 `$org/$repository` と一致させる
- M1 の `finding.id` は **`fingerprint` と同値**に固定する（retry / send の `source_finding_id` / eval harness 比較で決定論的に追跡するため）
- `category` は schema enum（`bug` / `security` / `performance` / `tests` / `design` / `code_quality` / `consistency` / `runtime_error`）に固定し、自由文字列の揺れを `fingerprint` に入れない
- `fingerprint` の入力は `path` / `category` / `normalized_title` / `primary_symbol` に固定し、`line` は含めない
- JSON Schema Draft 2020-12 単体では sibling equality (`id == fingerprint`) を標準機能だけで強制しにくいため、この等値は **review/send workflow の必須 runtime gate** として扱う
- review 側は `findings.candidates.json` を completed 前に `tasks/validate_candidates.py` で、`findings.verified.json` を completed 前に同梱 validator `tasks/validate_findings.py` で検証し、send 側も `findings.verified.json` の validator に失敗したら Markdown fallback せず中断する
- `status.json` は `stage` / `failed_stage` を optional に持ち、F4 以降の新規実行では failed 時に ranker / hunter / verifier / explainer のどこで停止したかを残す。review 側は status 更新直後に `status.json` を `tasks/validate_status.py` で検証する
- `ci-status.json` は `tasks/ci_status.py` が生成する `ci-status.v1` artifact で、GitHub Actions / status checks を `success` / `failure` / `pending` / `skipped` に正規化する。生成時は read-only endpoint だけを使い、rerun / cancel / write は行わない。`/pr-codex:review` の自動選定では current head の `ci-status.json.state == "success"` の PR だけをピックアップし、`failure` / `pending` / `skipped` / 未取得は次候補へスキップする。PR URL / PR番号で直接指定した場合は CI success gate を適用せず、CI 状態をレビュー context として記録する
- `ci-summary.md` は `ci-status.json` から派生する public-safe 要約で、failed log は secret-like text とローカルパスを scrub した短い要約だけを残し、raw log は保存しない
- schema 自体は `location.side` に `LEFT` も残すが、M1 の send workflow は `RIGHT` のみ受け付ける
- `tasks/validate_findings.py` は JSON shape / enum / conditional rule / RFC3339 date-time / URI / `end_line >= start_line` / `id == fingerprint` / fingerprint 再計算 / `metadata.json` との PR context 一致を stdlib-only で検証する
- `tasks/validate_expected_findings.py` / `tasks/validate_score_report.py` / `tasks/validate_m1_m2_gate.py` は eval artifact を stdlib-only で検証する

### Run-plan artifact schema

- `/pr-codex:review` はローカル artifact として `run-plan.json` を生成し、`schemas/run-plan.schema.json` で検証する。GitHub review body / inline comment payload / SARIF には `routing_decision` を含めない
- F8/M3 では USD cost は provider/CLI が実際に報告した値だけを `run-plan.json.cost` に記録する。repo-managed pricing table や token からの USD 推定は持たず、取得できない場合は `cost.source="unavailable"` とする。公開 artifact へ出す情報は sanitize する
- `routing_decision.budget_class` は `small` / `medium` / `large` の 3 値。`files_changed`、`lines_added + lines_removed`、`risk_tags` のうち `security` / `data_migration` 件数だけから決定論的に算出する
- `routing_decision.route` は M2 では `"claude+codex"` 固定。`selected_hunters` は互換性のため残し、F4 (#40) の specialist routing で route enum を拡張する hook として扱う
- `routing_decision.model_profile` は `"standard"` / `"deep"` / `"focused-fallback"` の logical profile のみ。provider/model 名や private config path は書かない
- `routing_decision.rationale` は 240 文字以内の決定論的な事実列（例: `files_changed=N, total_lines=M, risk_tags=[...], depth=deep, mode=standard`）に限定し、LLM 自由生成文を入れない
- `pr_classification` は run-plan 内にも同じ内容を持ち、Step 3 で `pr-classification.json` として派生保存する。`selected_specialists` は `docs` / `tests` / `workflow` / `review-skill` / `python` / `security` / `generic` の read-only checklist 選択であり、GitHub 投稿範囲や write 権限を広げない
- M1 で生成済みの旧 `run-plan.json` には `routing_decision` がないため、M2 partial 以降の strict schema では再生成が必要。production consumer はまだないため migration script は不要
- Issue #112 の positive fixture 実測では、GPT-5.6 verifier の exact / acceptable / false-positive / recall gate を維持したまま、3 round (86,460 ms / 66,327 tokens) から controller 停止後の 1 round (43,452 ms / 23,285 tokens) へ削減できた。semantic preflight は byte-identical な prompt / findings 入力で xhigh と high が同じ PASS かつ全品質指標 1.0 となり、high は 34,217 ms / 23,326 tokens から 14,890 ms / 23,003 tokens へ減少した。Fable 5 は baseline prompt の acceptable 0.0 / false-positive 0.0 から、focused prompt で acceptable 0.6667 / false-positive 0.0 へ改善した（35,848 ms → 46,869 ms、token 値は provider 未報告）。再現可能な集計は `fixtures/positive/eval-report.json` に保存する
- Budget class はレビュー観点や Must Fix 検出を抑制するためには使わない。`focused-fallback` でも security / bug / test を優先しつつレビュー自体は継続する

## SARIF derived artifact

- `tasks/generate_findings_sarif.py` は `findings.verified.json` から SARIF v2.1.0 `findings.sarif` を一方向生成する。`schema_version == "findings.v1"` 専用で、canonical への逆変換はしない
- `schemas/sarif-2.1.0.json` は OASIS SARIF v2.1.0 schema を同梱したもの。`tasks/validate_findings_sarif.py` は Python `jsonschema` でこの schema に対する official schema validation を行い、さらに pr-codex cross-artifact rule（side=RIGHT、fingerprint、post_policy、Must Fix count）をオフラインで検証する
- `--ranges pr.diff.ranges.txt` を指定した場合、SARIF location は同一 path の RIGHT-side hunk 範囲内に必ず入る必要がある。`--ranges` 未指定は range gate 無効、指定したファイルが空の場合は「コメント可能範囲なし」として非空 finding の SARIF 生成/検証を失敗させる
- SARIF の `message.text` と `artifactLocation.uri` は GitHub 投稿前 scrub と同じ姿勢で扱い、POSIX / Windows drive / UNC / `file://` の host absolute path を message では `<absolute-path>` に scrub し、location では repository-relative path 以外を拒否する
- rule は category enum 8 種（`pr-codex/bug` など）を固定列挙する。`severity` は `must_fix → error` / `should_fix → warning` / `nit → note` / `note → none` に写像する
- `security` category の `must_fix` は `properties.security_severity_label = "high"` を付ける。F7 では label のみで、security high/critical の inline 抑制ロジックは変更しない
- `result.partialFingerprints.canonical` は canonical `finding.id` と同じ安定 fingerprint。`result.guid` は SARIF 公式 schema の GUID 制約を満たすため、この fingerprint から導出した deterministic UUIDv5 を使う
- `result.fixes[]` は M2 では出力しない。`suggestion` は `message.text` に含め、機械適用可能な修正としては扱わない
- `posting.post_policy=local_only` は `suppressions: [{kind: "external", status: "accepted", justification: "local_only per pr-codex post_policy"}]` を付ける。`posting.post_policy=suppress` は SARIF に出力しない。Nit は `post_policy` が壊れていても SARIF 側で suppression を要求する

## Review rounds / halting policy

`/pr-codex:review` の Step 4c は、single-pass で final findings を確定せず、候補を `refine` / `challenge` / `verify` の round で精緻化する。停止条件は `run-plan.json.review_loop.halting_policy` と `review-rounds.json.policy` に同じ値で残す。

既定の halting policy:

- `max_rounds = 2 | 3` — standard / focused は通常 2、`depth_actual=deep` または `risk_tags` に `security` / `data_migration` がある場合だけ 3。positive fixture では固定 3 round が token / 時間を増やしたため、全フローの hard cap は 3 のまま対象を狭める adaptive policy（適応ポリシー）とする。到達したら `halt_reason=max_rounds`
- `time_budget_ms = estimated_timeout_ms` — 追加 round 開始前に予算を確認し、超過なら `halt_reason=time_budget`
- `no_new_evidence_rounds = 1` — 新しい根拠がない round が続く場合は `halt_reason=no_new_evidence`
- `repeated_contradiction_limit = 2` — 同じ contradiction signature が繰り返されたら `halt_reason=repeated_contradiction` とし、oscillation を止める
- `verifier_fail_policy = local_artifact_only` — `verifier FAIL` 候補は `review-rounds.json.rounds[].rejected_candidates[]` に `local_only=true` で残し、`findings.verified.json` / `review.md` / GitHub 投稿 payload には含めない
- `insufficient_evidence_policy = suppress_to_local_artifact` — 根拠不足候補も local artifact のみ。raw log / secret / token / authorization / private key は保存しない

各 round の継続可否は `tasks/refinement_loop.py --plan-next` が実測経過時間と、round 開始前後の candidate snapshot から host 側で計算した state digest / state delta（状態差分）から決定する。round 1 は全候補、round 2 は未解決の high-risk・hunter 間不一致・新しい evidence を取得可能な候補、round 3 は round 2 で state が変化した high-risk 候補だけを対象にする。モデルが自分で停止条件や対象拡大を判断したり、文章の言い換えを新規 evidence と申告したりして継続してはならない。small かつ初期候補が fully verified / conflict-free の場合だけ、同 controller の `--apply-auto-deep` が round 1 前に `run-plan.json` を `depth_actual=deep` へ更新する。

`review-rounds.json.rounds[]` は controller 算出の `state_digest_before` / `state_digest_after` / `changed_candidate_ids` / `changed_candidate_count` / `evidence_added_count` / `disposition_changed_count` / `remaining_active_count` を保持する。`run-plan.json.review_loop.round_metrics` は完了時にこれらの集計値に加え、`rounds_completed` / `halt_reason` / `verifier_fail_candidates` / `suppressed_candidate_count` / `no_new_evidence_rounds` / `repeated_contradiction_events` / `insufficient_evidence_events` / `oscillation_detected` を持つ。F11 eval report (`schemas/eval-report.v1.json`) は baseline と iterative run の両方に同じ round metrics を含め、round 有無による timeout completion / false positive / oscillation 差分を比較できるようにする。host state metrics を採取していない過去の eval record は推測せず `null` とする。

## Preflight result schema

- `/pr-codex:send` Step 4.5 は `schemas/preflight-result.v1.json` に従う `preflight-result.json` を出力する。Codex が直接出力するのは `schemas/preflight-semantic.v1.json` に従う per-finding の semantic decisions（`confirmed` / `refuted` / `insufficient_evidence` の 3 値）だけで、`tasks/validate_preflight_result.py --from-semantic` が static stage 結果と合成して verdict / stage status / counts を host 側で算出する。Must Fix 0 件時は `--semantic-skipped` で Codex を呼ばずに合成する
- `verdict` は `PASS` / `FAIL` のみ。`PASS_WITH_WARNINGS` は導入せず、将来の非ブロッキング警告は `violations[].severity = "warning"` として表現する
- `stages` は `schema_validation` / `range_validation` / `semantic_preflight` / `payload_consistency` の 4 stage を必ず含む。`schema_validation` / `range_validation` / `payload_consistency` は Python validator / builder が担う static stage、`semantic_preflight` だけが Codex の意味判断
- `violations[]` は `auto_fixable` と `requires_review_regeneration` を持つ。semantic 判定の `refuted` は `counterargument_succeeded`、`insufficient_evidence` は `insufficient_evidence` rule に写像され、どちらも review 再生成が必要（retry しない）。`findings.sarif` schema validation 失敗や Must Fix count の不整合は static stage の Python validator が投稿前に検出する（root-cause cluster がない場合は従来の `canonical_must_fix != markdown_must_fix != payload_must_fix != sarif_must_fix` 型の不一致を拒否し、cluster がある場合は `canonical_must_fix == markdown_must_fix == sarif_must_fix` かつ `payload_must_fix == representative_must_fix` を要求する）
- `tasks/validate_preflight_result.py` は `preflight-result.json` の合成（`--from-semantic` / `--semantic-skipped`）と cross-field validation、validated JSON からの人間可読 `preflight-codex.md` 生成（`--emit-markdown`）を stdlib-only で行う

### fingerprint 正準アルゴリズム

1. `path`: `location.path` のリポジトリ相対 POSIX path をそのまま使う
2. `category`: schema enum の値をそのまま使う
3. `normalized_title`: `title` に Unicode NFKC 正規化 → Unicode lowercase → 連続空白を ASCII space 1 個へ畳み込み → 前後 trim → 末尾の Unicode punctuation（General Category が P で始まる文字）をなくなるまで除去 → 最後に右 trim、の順で処理する
4. `primary_symbol`: `title` 内で最初に backtick で囲まれた symbol を前後 trim して使う。存在しない場合は空文字列にする
5. `id = fingerprint = lowercase_hex(sha256(path + "\x1f" + category + "\x1f" + normalized_title + "\x1f" + (primary_symbol || "")))`

fingerprint は LLM では計算できないため、`/pr-codex:review` Step 4c は findings 構築前に `tasks/validate_findings.py --emit-fingerprints --data fingerprint-material.json` で正値を取得する（`--emit-fingerprints` は path / category / title の素材配列から normalized_title / primary_symbol / fingerprint を返す）

## バージョンアップ（作者向け）

リリースは [tagpr](https://github.com/Songmu/tagpr) で自動化されている。バージョンの正（唯一の管理場所）は `.claude-plugin/plugin.json` の `version` のみで、`marketplace.json` には `version` を持たせない。

1. 通常のPRを main にマージすると、tagpr がリリースPR（`.claude-plugin/plugin.json` を次版に書き換え済み）を自動作成・更新する
2. リリースしたいタイミングでそのリリースPRをマージする
3. タグ（例: `v2.13.1`）と GitHub Release が自動作成される

バンプ幅はデフォルトで patch。minor / major にしたい場合は、取り込むPRに `minor` / `major` ラベルを付けるか、リリースPRに `tagpr:minor` / `tagpr:major` ラベルを付ける。

利用者への更新配信は main 上の `plugin.json` の `version` 文字列の変化で判定されるため、リリースPRをマージするまで利用者には新しい内容が届かない。コード変更をマージしたら、リリースPRのマージを忘れないこと。利用者側は `/plugin update pr-codex` で最新版に更新できる。

## ライセンス

MIT
