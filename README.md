# pr-codex

GitHub PRを **Claude Code** と **Codex CLI** の2者レビュー方式で自動レビューする Claude Code プラグイン。

## 特徴

- **2者レビュー**: Claude Code と Codex CLI が独立してPRをレビューし、結果を統合
- **自動巡回**: `/loop` と組み合わせて定期的にレビュー依頼PRを検出・処理
- **冪等実行**: status.json による状態管理で、完了済みPRのスキップや失敗時の再実行に対応
- **最小権限**: 各レビューツールは読み取り専用で動作し、PRへのコメント投稿時はユーザー承認を得てから行う
- **生成と投稿を分離**: レビュー生成 (`/pr-codex:review`) と投稿 (`/pr-codex:send`) を別スキルに分け、投稿前にユーザーが内容を承認する
- **反復精緻化 + halting**: `refine` / `challenge` / `verify` を round 管理し、`max_rounds` / `time_budget_ms` / `no_new_evidence` / `repeated_contradiction` で停止する

## 必要なもの

- Claude Code
- Codex CLI (`codex-cli 0.128.0` 以上、`codex --ask-for-approval never -m gpt-5.5 ... exec` が使えること)
- GitHub CLI (`gh`)
- `jq`（SKILL.md 内の全テンプレートで利用する。macOS 標準では未インストール）
- `python3`（同梱 validator / eval runner で `findings.candidates.json` / `findings.verified.json` / `status.json` / `preflight-result.json` / `findings.sarif` / fixture scoring artifact を検証するため）
- Python package `jsonschema` (`python3 -m pip install 'jsonschema>=4,<5'`) — `tasks/validate_findings_sarif.py` が同梱 OASIS SARIF v2.1.0 schema に対して official schema validation を行うため

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
- `--effort max` — Claude Code 本体の推論設定。`/pr-codex:review --deep` / `--standard` の depth policy とは別軸
- Codex CLI 側のレビューと投稿前検証は、スキル内で `-m gpt-5.5` を指定して実行する。レビュー実行では `model_reasoning_effort` をスキル側で上書きせず、ユーザー config の値を使う。投稿前検証は `--ignore-user-config` でユーザー config から切り離す
- Codex CLI は `codex-cli 0.128.0` 以降のみ対応する。旧バージョン向けテンプレートは打ち切り、`--sandbox read-only` / `--color never` / `--ephemeral` を並べる旧形式ではなく、`-c sandbox_mode=read-only` と preflight 限定の `--ignore-user-config` を使う



```
# 手動実行でレビューする
/pr-codex:review

# 深くレビューする（高リスク・小規模PR向け）
/pr-codex:review --deep

# 高速 path を明示する
/pr-codex:review --standard

# 10分間隔で自動レビューする
/loop 10m /pr-codex:review
```

## Depth control

`/pr-codex:review` はレビュー深度を `standard` / `deep` の 2 値で記録する。既定はコストと 20 分 timeout を優先する `standard` で、`deep` は高リスク・小規模 PR または手動指定向け。

| 入力 / signal | selected depth | artifact |
| --- | --- | --- |
| `/pr-codex:review --deep` かつ `lines_added + lines_removed <= 5000` | `deep` | `depth_source=argument`, `depth_requested=deep`, `depth_downgraded=false` |
| `/pr-codex:review --deep` かつ `lines_added + lines_removed > 5000` | `standard` 強制 | `depth_source=argument`, `depth_requested=deep`, `depth_downgraded=true`, `depth_downgrade_reason` |
| `/pr-codex:review --standard` | `standard` | `depth_source=argument`, `depth_requested=standard` |
| 引数なし、`risk_tags` に `security` または `data_migration` を含み、`files_changed <= 20` かつ `lines_added + lines_removed <= 1500` | `deep` | `depth_source=auto` |
| 引数なしで上記以外 | `standard` | `depth_source=default` |
| 引数なし、かつ `lines_added + lines_removed > 5000` | `standard` | `depth_source=default`, `depth_downgraded=false`, `depth_reason` に大規模ガード理由を記録 |

`run-plan.json` には `depth_actual` / `depth_source` / `depth_reason` / `depth_requested` / `depth_downgraded` / `depth_downgrade_reason` を保存するため、standard/deep の選択は deterministic に追跡できる。

`recommended_mode` (`standard` / `focused` / `skip`) は depth とは直交する別軸。`recommended_mode` は「観点や対象範囲の絞り込み」、depth は「1観点あたりの掘り下げ深さ」を表す。たとえば `recommended_mode=focused` かつ `depth_actual=deep` の組み合わせは有効で、focused fallback / skip recommendation と矛盾しない。

## レビューフロー

1. **PR候補の取得** — GitHub Search API で `review-requested` のPRを一覧取得
2. **候補の選定** — 未レビュー・失敗・追加コミットありの最初の1件を選定
3. **作業ディレクトリの準備** — PRブランチを各ツール用に個別に shallow clone
4. **2者レビュー実行** — Claude Code と Codex CLI が並行してレビュー
5. **反復精緻化と結果の統合** — 両者の指摘を `refine` / `challenge` / `verify` round で精査し、halting 後に `review-rounds.json` / `findings.verified.json` / `review.md` を生成
6. **結果報告** — レビュー結果の要約をユーザーに報告
4. **stage 化されたレビュー実行** — 既存 Step を logical stage として扱う（ranker / hunter / verifier / explainer）
   - ranker: `run-plan.json` で PR risk/area と実行方針を分類
   - hunter: Claude Code と Codex CLI が並行して広めに候補を集め、`findings.candidates.json` を残す
   - verifier: 4軸 + evidence ladder + counterexample で絞り込み、`findings.verified.json` を canonical artifact にする
   - explainer: verified findings から `review.md` と local-only `findings.sarif` を派生生成する
5. **結果報告** — レビュー結果の要約をユーザーに報告

Stage ごとの責務、input/output artifact、halting 条件は [`skills/review/STAGES.md`](skills/review/STAGES.md) を参照してください。

## レビューの投稿

`/pr-codex:review` は `review.md` をローカル生成するのみで、PRへの投稿は行わない。投稿は別スキル `/pr-codex:send` を手動で実行する。

```
/pr-codex:send
```

## 投稿後フィードバックの学習

`/pr-codex:learn` は投稿後に GitHub から返ってきた明示 signal だけを、次回レビュー改善用のローカル artifact として保存する。生成物は `learn-result.json` と `feedback-artifacts/*.json` で、secret/token/ローカルパスは scrub される。

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

実体は同梱 helper `$CLAUDE_PLUGIN_ROOT/tasks/learn_feedback.py` で、snapshot JSON から冪等に artifact を生成する。

```bash
python3 $CLAUDE_PLUGIN_ROOT/tasks/learn_feedback.py --input feedback-snapshot.json --output-dir ~/claude-loop-pr-codex/learn/yuki777-pr-codex-60-103766c
```

`/pr-codex:send` の挙動:

1. `~/claude-loop-pr-codex/` 配下から `status.json` が `state:completed` でかつ `findings.verified.json` / `review.md` が存在するディレクトリを1件選定する（名前昇順の先頭1件）
2. `findings.verified.json` を必須の一次入力として `Must Fix` / `Should Fix` / `Nit` の投稿方針を抽出し、`review.md` から `## 総評` / `## 良い点` を body に使う（F13 以降は Markdown parser fallback へ切り替えず、欠落・schema 不整合なら中断する。root-cause cluster がない場合は Must Fix 件数も完全一致を要求し、cluster がある場合は canonical / Markdown / SARIF は全 finding 件数、`review-payload.json` は representative comment 件数として検証する）
3. `Should Fix` のうち `post_policy: body_summary` の候補がある場合、上位 3 件を body の `## 非ブロッキング改善 (Should Fix)` に含めるかを確認する（default: no）
4. `Nit` は PR に投稿せず、primary path では `nits.md` にローカル artifact として書き出す（0 件なら作成しない）
5. Step 4.5 の verifier pipeline を schema / range / semantic / payload consistency の 4 stage で実行し、従来互換の `preflight-codex.md` に加えて `preflight-result.json` を保存する。schema stage では `findings.sarif` の schema validation と `findings.verified.json` ↔ `review.md` ↔ `review-payload.json` ↔ `findings.sarif` の Must Fix count 整合性を検証する。root-cause cluster がある場合、canonical / Markdown / SARIF は全 Must Fix finding 数を保持し、`review-payload.json` は representative inline comment 数へ減ることを許可する。semantic stage では Must Fix の反証、Should Fix body summary の対応関係、Nit の payload 混入を検証する
6. GitHub Reviews API への payload サマリをユーザーに提示し、明示的な承認を得る
7. 承認後、`gh api --method POST .../reviews` で投稿（`event` は Must Fix ありなら `REQUEST_CHANGES`、なければ `COMMENT`。`APPROVE` は自動では出さない）
8. 投稿成功後、対象ディレクトリを `~/claude-loop-pr-codex/sent/$org-$repo-$pr-$head_sha_short/` に移動する（同一 PR でも HEAD 更新後の再投稿履歴が衝突しないよう、`head_sha` の先頭 7 文字を suffix に付ける）

`/loop` には載せず、対話実行で使う。1回の実行で1件のみ処理する。

### Should Fix / Nit の取り扱い

- `Must Fix` は従来どおり GitHub review の inline comment として投稿される。`root_cause_clusters[]` がある場合は、各 cluster の `representative_finding_id` を inline 代表として扱い、同じ root cause の他 finding は代表コメント本文の affected findings summary に短く列挙する（canonical / SARIF / review.md には全 finding を残し、`review-payload.json` の `comments[]` だけ representative count になる。preflight の count gate は full count と representative payload count を別々に検証する）
- `Should Fix` は自動では投稿されない。手動実行時に `yes` を選ぶと、author が見落としやすい非ブロッキング改善だけを上位 3 件まで PR body に短く同梱できる
- `Nit` はノイズ抑制のため PR には載せず、`nits.md` に控えとして残す。投稿後は `sent/` 配下の履歴ディレクトリで確認できる
- `findings.verified.json` がない fallback path では、従来どおり `Should Fix` / `Nit` / 補足を投稿 payload に含めない

### Local artifacts と GitHub 投稿対象の境界

- GitHub Reviews API に送るのは `review-payload.json` の `body` と `comments[]` のみ。`comments[]` は Must Fix の inline comment だけを含む
- `findings.verified.json` は canonical source、`review.md` / `review-payload.json` / `findings.sarif` / `nits.md` は派生成果物。canonical を単一の真実源とし、派生成果物を手で編集して canonical に逆流させない
- `findings.sarif` は M2 では **local-only artifact**。GitHub Code Scanning への upload、CI からの公開、PR への添付は自動化しない（M3 の別 Issue で扱う）
- `posting.post_policy=suppress` の finding は SARIF にも出さず、canonical 内部記録だけに残す。`local_only` と `nit` は SARIF `suppressions[]` を付けてノイズ公開の経路を閉じる


## Regression eval / fixture scoring (F11)

CI では LLM を起動せず、固定 fixture と stub artifact だけで schema / runner の deterministic smoke test を行う。手動 deep eval では実レビューで生成した `findings.verified.json` を fixture oracle と比較し、M1→M2 gate report に集約する。

### CI-safe smoke

```bash
python3 -m unittest discover -s tasks -p "test_*.py"
```

このテストは `fixtures/<size>/scoring-stubs/*.findings.verified.json` を使い、`expected-findings.v1` / `score-report.v1` / `m1-m2-gate.v1` の validation と scoring runner の分岐を確認する。実 LLM / `gh api` / GitHub write 操作は含めない。

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
  --score-reports artifacts/score-small.json artifacts/score-medium.json artifacts/score-large.json \
  --inputs artifacts/m1-m2-inputs.json \
  --out artifacts/m1-m2-gate.json
```

`m1-m2-gate.json` は `payload_compat_422` / `must_fix_count_consistency` / `step_4_5_pass_rate` / `run_plan_emitted` / `loop_completion_rate` / `fixture_scoring_gate` を `pass` / `fail` / `unknown` で記録し、総合結果を `pass` / `fail` / `blocked_by_unknowns` に集約する。`fixture_scoring_gate` は small / medium / large の3 fixture が揃っていることに加え、各 `score-report.v1` に埋め込まれた oracle 由来の `scoring_gate` / `oracle_sha256` / `expected_finding_ids` と `gate_checks` の閾値が fixture ごとの固定 oracle と一致することも確認する。

## ファイル構成

```
~/claude-loop-pr-codex/
  ├── $org-$repo-$pr/             # 進行中 / 未投稿のレビュー
  │     ├── status.json           # 実行状態（running / completed / failed）
  │     ├── metadata.json         # PR情報（org, repo, pr_number, head_sha 等）
  │     ├── ci-status.json        # GitHub Actions / status checks の read-only 正規化 artifact
  │     ├── ci-summary.md         # raw log を保存しない public-safe CI 要約
  │     ├── run-plan.json         # preflight 指標、recommended_mode、選択 depth、M2 routing_decision（ローカル専用）
  │     ├── pr.diff               # PR 差分 (unified diff)
  │     ├── pr.diff.ranges.txt    # GitHub inline comment 可能範囲
  │     ├── clone-claude/         # Claude Code 用 shallow clone
  │     ├── clone-codex/          # Codex CLI 用 shallow clone
  │     ├── claude-review.md      # Claude Code の生レビュー (hunter)
  │     ├── codex-review.md       # Codex CLI の生レビュー (hunter)
  │     ├── findings.candidates.json # hunter → verifier 境界の候補 (`schemas/findings.candidates.v1.json`)
  │     ├── findings.verified.json # canonical findings (`schemas/findings.v1.json`)
  │     ├── findings.sarif        # SARIF v2.1.0 派生成果物。M2 では local-only / upload しない
  │     ├── validation-report.json # validation の副成果物（canonical findings とは分離）
  │     ├── review-rounds.json    # F5 refine/challenge/verify round artifact。verifier FAIL 候補は local_only で保持
  │     ├── review.md             # 統合レビュー（最終成果物）
  │     ├── preflight-prompt.md   # /pr-codex:send Step 4.5 の Codex verifier prompt
  │     ├── preflight-codex.md    # /pr-codex:send Step 4.5 の人間可読 verifier 結果
  │     ├── preflight-result.json # /pr-codex:send Step 4.5 の構造化 verifier 結果
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
- `depth_actual` は `standard` / `deep` の 2 値。`depth_source` は `argument` / `auto` / `default`、`depth_requested` は明示指定がない場合 `null`
- `depth_downgraded == true` の場合は `depth_requested=deep` / `depth_actual=standard` / `depth_downgrade_reason` 非空でなければならない
- `recommended_mode == "skip"` の場合だけ `skip_reason` を非空にし、それ以外は `skip_reason=null` にする。`recommended_mode` は depth と直交し、GitHub への自動投稿範囲は depth では拡大しない
- hunter → verifier 境界の debug artifact は `schemas/findings.candidates.v1.json` (JSON Schema Draft 2020-12) で定義する。`findings.candidates.json` は `id == fingerprint` や 4軸 / evidence / posting policy を要求せず、verifier が canonical findings へ揃える
- canonical runtime artifact は `schemas/findings.v1.json` (JSON Schema Draft 2020-12) で定義する
- F5 の round artifact は `schemas/review-rounds.v1.json` で定義し、`review-rounds.json` に `max_rounds` / `time_budget_ms` / `no_new_evidence_rounds` / `repeated_contradiction_limit` と round metrics を保存する
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
- `ci-status.json` は `tasks/ci_status.py` が生成する `ci-status.v1` artifact で、GitHub Actions / status checks を `success` / `failure` / `pending` / `skipped` に正規化する。生成時は read-only endpoint だけを使い、rerun / cancel / write は行わない
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
- M1 で生成済みの旧 `run-plan.json` には `routing_decision` がないため、M2 partial 以降の strict schema では再生成が必要。production consumer はまだないため migration script は不要
- Timeout 完了率の実測比較は #36 (F11 regression eval) の fixture/eval 完了後に行う。本リポジトリ内の回帰確認は `python3 tasks/validate_run_plan.py` と `python3 -m unittest discover -s tasks -p "test_*.py"` を流し、routing fields と既存 timeout proxy が悪化していないことを確認する
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

- `max_rounds = 3` — round 数の上限。到達したら `halt_reason=max_rounds`
- `time_budget_ms = estimated_timeout_ms` — 追加 round 開始前に予算を確認し、超過なら `halt_reason=time_budget`
- `no_new_evidence_rounds = 1` — 新しい根拠がない round が続く場合は `halt_reason=no_new_evidence`
- `repeated_contradiction_limit = 2` — 同じ contradiction signature が繰り返されたら `halt_reason=repeated_contradiction` とし、oscillation を止める
- `verifier_fail_policy = local_artifact_only` — `verifier FAIL` 候補は `review-rounds.json.rounds[].rejected_candidates[]` に `local_only=true` で残し、`findings.verified.json` / `review.md` / GitHub 投稿 payload には含めない
- `insufficient_evidence_policy = suppress_to_local_artifact` — 根拠不足候補も local artifact のみ。raw log / secret / token / authorization / private key は保存しない

`run-plan.json.review_loop.round_metrics` は完了時に `rounds_completed` / `halt_reason` / `verifier_fail_candidates` / `suppressed_candidate_count` / `no_new_evidence_rounds` / `repeated_contradiction_events` / `insufficient_evidence_events` / `oscillation_detected` を持つ。F11 eval report (`schemas/eval-report.v1.json`) は baseline と iterative run の両方に `round_metrics` を含め、round 有無による timeout completion / false positive / oscillation 差分を比較できるようにする。

## Preflight result schema

- `/pr-codex:send` Step 4.5 は `schemas/preflight-result.v1.json` に従う `preflight-result.json` を出力する
- `verdict` は `PASS` / `FAIL` のみ。`PASS_WITH_WARNINGS` は導入せず、将来の非ブロッキング警告は `violations[].severity = "warning"` として表現する
- `stages` は `schema_validation` / `range_validation` / `semantic_preflight` / `payload_consistency` の 4 stage を必ず含む
- `violations[]` は `auto_fixable` と `requires_review_regeneration` を持ち、send 側で直せる payload ずれ（行範囲・event・body など）と review 再生成が必要な semantic/schema 不整合を分離する。`findings.sarif` schema validation 失敗や Must Fix count の不整合は `schema_validation` stage の `must_fix_count_mismatch` として FAIL にする。root-cause cluster がない場合は従来の `canonical_must_fix != markdown_must_fix != payload_must_fix != sarif_must_fix` 型の不一致を拒否し、cluster がある場合は `canonical_must_fix == markdown_must_fix == sarif_must_fix` かつ `payload_must_fix == representative_must_fix` を要求する
- `tasks/validate_preflight_result.py` は `preflight-codex.md` の `RESULT_JSON` ブロック抽出と `preflight-result.json` の cross-field validation を stdlib-only で行う

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
