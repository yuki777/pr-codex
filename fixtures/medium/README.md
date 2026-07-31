# Fixture: medium / BEAR.Sunday PR #143

**PR**: [Add RouterInterface type Globals and Server](https://github.com/bearsunday/BEAR.Sunday/pull/143)
**規模**: +28/-11, 4 files
**Merged**: 2020-06-27

## PR の要約

`RouterInterface::match()` に渡す `$globals` / `$server` を Psalm の **type alias** で型表現する PR。`WebRouter::match()` 内の defensive な `assert()` を一部削除し、契約を docblock type に寄せている。

## レビュー検証としての位置づけ

- カテゴリ: **realistic-mid-refactor / type-safety**
- 主用途: **3軸 (REAL/TRIGGERABLE/IMPACTFUL) と blast_radius metadata の判定精度測定**
- 想定 oracle カテゴリ: `should_fix` (型契約強化に伴う潜在 trigger path)

## 仕込み意図 (oracle 設計)

`assert()` 削除に伴い、もし `$server` から `REQUEST_METHOD` / `REQUEST_URI` が欠ける呼び出しがあると undefined index になる可能性がある。これは:

- **REAL**: yes (該当 key を参照する code path が確実に存在)
- **TRIGGERABLE**: unknown (call sites を全部追えるかは context 依存)
- **IMPACTFUL**: yes (本番で 500 / undefined index notice)
- **blast_radius**: isolated (この箇所固有。Must Fix gate には使わない)

→ profile `should_fix_lax`、`acceptable_overrides.triggerable: ["yes", "unknown"]`

## 仕込みたい oracle

1. 「`assert()` 削除で undefined index リスク」 — `should_fix`、profile `should_fix_lax`
2. 「Psalm type alias の `Server` を実 runtime 検証する箇所がない」 — `acceptable_risk` (note レベル指摘ならOK、Must Fix にすると過剰)
3. (false positive trap) 「PHP 7.4 互換性が壊れる」 — `known_false_positive_trap` (この PR 自体は PHP 7.4 を維持)

## scoring 期待値 (M1 gate)

- `acceptable_pass_rate ≥ 0.8`
- `exact_pass_rate ≥ 0.5` (中規模なので exact 一致は期待しすぎない)
- `false_positive_rate ≤ 0.1`

## License

BEAR.Sunday は MIT License。元 LICENSE → [`../LICENSES/BEAR.Sunday.MIT.txt`](../LICENSES/BEAR.Sunday.MIT.txt)
