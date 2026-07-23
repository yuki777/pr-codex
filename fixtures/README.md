# pr-codex fixtures

レビュー検証用の **frozen PR fixture** 集。`bearsunday/BEAR.Sunday` の merged PR から3本を採用。

| サイズ | PR | 内容 | LOC | files |
|---|---|---|---|---|
| small | [#164](https://github.com/bearsunday/BEAR.Sunday/pull/164) | Deprecate AbstractApp class | +7/-2 | 2 |
| medium | [#143](https://github.com/bearsunday/BEAR.Sunday/pull/143) | Add RouterInterface type Globals and Server | +28/-11 | 4 |
| large | [#171](https://github.com/bearsunday/BEAR.Sunday/pull/171) | Drop PHP 7.4 support and optimized for PHP8 | +119/-157 | 20 |

## 設計方針 (Issue #22 + Claude × Codex 議論で確定)

1. **frozen patch** — `diff.patch` は GitHub PR 取得時点で凍結。base/head branch が将来 force-push / 削除されてもfixture が壊れない。
2. **runtime / oracle 分離** — 採点用 oracle は `expected-findings.json` (`expected-findings.v1` schema) で wrapper 構造。canonical findings 形式は使わない。
3. **license 同梱** — BEAR.Sunday は MIT。`fixtures/LICENSES/BEAR.Sunday.MIT.txt` にオリジナルの notice を保存。各 fixture の `metadata.json` から相対参照。
4. **negative / noisy-real-world fixture として位置づける** — 成熟 OSS の merged PR は false-positive 検出と smoke test に向くが、recall 測定には不向き。recall fixture (synthetic overlay / bug-fix PR before-diff) は M2 以降で別途追加予定。

## ディレクトリ構成

```
fixtures/
  LICENSES/
    BEAR.Sunday.MIT.txt
  small/                    # PR #164
    diff.patch              # frozen
    metadata.json           # repo / pr_number / sha / license / frozen_patch_path
    run-plan.expected.json  # 自動 depth policy の expected artifact
    expected-findings.json  # oracle (expected-findings.v1)
    README.md               # PR 要約 + 仕込み意図 + 想定 oracle カテゴリ
  eval-report.example.json  # F11 eval-report.v1 の round_metrics 付き最小例
  medium/                   # PR #143
  large/                    # PR #171
```

## scoring (M1 gate)

F11 以降、oracle 評価結果は `tasks/score_fixture.py` で `score-report.v1` として出力する。`expected-findings.json` は `schemas/expected-findings.v1.json`、score report は `schemas/score-report.v1.json` で検証される。

```bash
python3 tasks/score_fixture.py \
  --expected fixtures/small/expected-findings.json \
  --actual fixtures/small/scoring-stubs/perfect.findings.verified.json \
  --out artifacts/score-small.json
```

実行時 artifact は `artifacts/` に保存する（gitignore 済み）。CI では `fixtures/<size>/scoring-stubs/` の固定 `findings.verified.json` を使うため、LLM や `gh api` は呼ばない。

oracle 評価結果は4指標を出す:

- `exact_pass_rate` — `axes` と `blast_radius` が oracle と完全一致
- `acceptable_pass_rate` — `axes` が `expected_axes ∪ acceptable_overrides`、severity が `acceptable_severities`、evidence が最低水準内に収まり、`blast_radius` が `expected_blast_radius` と一致
- `false_positive_rate` — `expected_outcome=known_false_positive_trap` を Must Fix にしてしまった率
- `recall_known_bug` — `expected_outcome=known_bug` が fixture の location/semantic matching 契約で検出された率

`blast_radius` は runtime の Must Fix 判定には使わない非ゲート metadata だが、fixture 品質評価では `breakdown[].blast_radius_diff` に expected/actual/acceptable を決定的に記録し、`unknown` への退行を品質維持として扱わない。

F11 eval report (`schemas/eval-report.v1.json`) は、baseline と iterative run の差分を比較するため各 run に `round_metrics` を必ず含める。最低限の round metrics は `rounds_completed` / `max_rounds` / `halt_reason` / `elapsed_ms` / `time_budget_ms` / `verifier_fail_candidates` / `suppressed_candidate_count` / `no_new_evidence_rounds` / `repeated_contradiction_events` / `insufficient_evidence_events` / `changed_candidate_count` / `evidence_added_count` / `disposition_changed_count` / `remaining_active_count` / `oscillation_detected`。これにより F5 の round 有無で timeout 内完了率、false positive 率、state 変化、oscillation 抑止の差分を fixture ごとに比較できる。host state metrics を採取していない過去の record だけは推測せず `null` とする。

**M1 gate**: `acceptable_pass_rate ≥ 0.8`, `false_positive_rate ≤ 0.1`
matching は actual の `id` ではなく fixture の location/semantic 条件で行う。`line_range` がある `known_bug` は path と行位置の重複と title keyword の一致に加え、category の一致を必須にする。`line_range` がない旧 fixture だけは従来どおり `(location_match.path, category)` で照合する。同一条件に複数候補がある場合は `expected_axes` との Hamming 距離が最小の actual を貪欲に選ぶ。`known_false_positive_trap` は fixture 全体にかかる罠として扱い、title keyword または path/category で該当 actual を検出する。title keyword による trap / acceptable-risk promotion 検出は、model 出力の category が揺れても検出できるよう category には依存しない。
matching されなかった actual のうち `severity ∈ {must_fix, should_fix}` は `score-report.v1.unmatched_actuals[]` に残し、過検知候補として後から確認できるようにする。

**M1 gate**: 各 fixture の `scoring_gate` に従う。現状は `acceptable_pass_rate` と `false_positive_rate` を主ゲートにし、medium だけ `exact_pass_rate_min` も固定している。`score-report.v1` には oracle 由来の `scoring_gate` / `oracle_sha256` / `expected_finding_ids` を埋め込み、`gate_checks[]` の必須チェック名・閾値と照合する。M1→M2 集約時も fixture ID ごとの固定 oracle 閾値・oracle digest・expected-id/outcome sequence と一致しない report は fail として扱う。

M1→M2 gate report は `tasks/m1_m2_gate.py` で生成する。運用実測値 (`payload_422_count`, Step 4.5 PASS 率など) は外部の `m1-m2-inputs.v1` として渡し、欠落項目は `unknown` として記録する。

## 関連

- [#15 ロードマップ](https://github.com/yuki777/pr-codex/issues/15)
- [#16 F1 canonical findings.json](https://github.com/yuki777/pr-codex/issues/16) — runtime schema
- [#22 fixtures 整備](https://github.com/yuki777/pr-codex/issues/22) — このディレクトリ
- 議論ログ全文 → [Gist](https://gist.github.com/yuki777/f9cea4caf8decf8b28c05d8436f4d3e7)
