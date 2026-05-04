# Fixture: small / BEAR.Sunday PR #164

**PR**: [Deprecate AbstractApp class](https://github.com/bearsunday/BEAR.Sunday/pull/164)
**規模**: +7/-2, 2 files
**Merged**: 2022-01-08

## PR の要約

`AbstractApp` クラスを `@deprecated` マークし、BEAR.Skeleton 側の `App` モジュールへの誘導コメントを追加するだけのシンプルな PR。

## レビュー検証としての位置づけ

- カテゴリ: **noisy-real-world / consistency**
- 主用途: **false-positive 検出と smoke test**
- 想定 oracle カテゴリ: `should_fix` / `nit`、`post_policy: local_only`

## 仕込み意図 (oracle 設計)

成熟プロジェクトの軽微な deprecation PR では、レビュアー (Claude / Codex) が以下のような **誤検知や過剰指摘** をしやすい:

1. 「deprecation 移行先がリポジトリ外 (BEAR.Skeleton) のため不親切」 — 実は意図的設計
2. 「deprecated class が削除されていない」 — deprecation は段階的な慣習
3. 「同時に `@since` や `@removed-in` が付いてない」 — 必須ではない

本 fixture では:
- `expected_outcome: known_false_positive_trap` で 1, 2 を **誤検知としてマーク**
- `expected_outcome: acceptable_risk` で 3 を **指摘してもよいが Must Fix ではない** とマーク
- profile `noise_filter` を主に使い、`should_fix_lax` で1件だけ「migration note を local 追加」を should_fix として許容

## scoring 期待値 (M1 gate)

- `acceptable_pass_rate ≥ 0.8`
- `false_positive_rate ≤ 0.1` — このサイズで誤検知 Must Fix を出さないことが最重要

## License

BEAR.Sunday は MIT License。元 LICENSE → [`../LICENSES/BEAR.Sunday.MIT.txt`](../LICENSES/BEAR.Sunday.MIT.txt)
