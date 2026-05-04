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
    expected-findings.json  # oracle (expected-findings.v1)
    README.md               # PR 要約 + 仕込み意図 + 想定 oracle カテゴリ
  medium/                   # PR #143
  large/                    # PR #171
```

## scoring (M1 gate)

oracle 評価結果は3指標を出す:

- `exact_pass_rate` — `axes` が完全一致
- `acceptable_pass_rate` — profile + acceptable_overrides 内に収まる
- `false_positive_rate` — `expected_outcome=known_false_positive_trap` を Must Fix にしてしまった率

**M1 gate**: `acceptable_pass_rate ≥ 0.8`, `false_positive_rate ≤ 0.1`

## 関連

- [#15 ロードマップ](https://github.com/yuki777/pr-codex/issues/15)
- [#16 F1 canonical findings.json](https://github.com/yuki777/pr-codex/issues/16) — runtime schema
- [#22 fixtures 整備](https://github.com/yuki777/pr-codex/issues/22) — このディレクトリ
- 議論ログ全文 → [Gist](https://gist.github.com/yuki777/f9cea4caf8decf8b28c05d8436f4d3e7)
