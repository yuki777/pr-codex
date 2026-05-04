# Fixture: large / BEAR.Sunday PR #171

**PR**: [Drop PHP 7.4 support and optimized for PHP8](https://github.com/bearsunday/BEAR.Sunday/pull/171)
**規模**: +119/-157, 20 files
**Merged**: 2022-11-21

## PR の要約

PHP 7.4 サポートを drop し、constructor property promotion / `str_contains()` / readonly properties など PHP 8 構文に modernize する大規模 PR。`composer.json` の `require.php` 更新、`psalm.xml` / `phpcs.xml` / `rector.php` などの解析設定変更を含む。

## レビュー検証としての位置づけ

- カテゴリ: **mass-modernization / multi-config-coordination**
- 主用途: **large PR でレビュアーが文脈 (PR 全体の意図) を保てるかの測定**
- 想定 oracle カテゴリ: `should_fix` (設定不整合) + 多数の `known_false_positive_trap`

## 仕込み意図 (oracle 設計)

このサイズの PR では Claude / Codex が **PR の意図 (PHP 7.4 drop)** を見失い、以下のような **誤検知** をしやすい:

1. 「PHP 7.4 互換性が壊れる」 — **PR の目的そのもの** → `known_false_positive_trap` (`pr_intent: php8_migration` で除外)
2. 「constructor property promotion は古い PHP で動かない」 — 同上
3. 「`str_contains()` は polyfill が必要」 — 同上

一方で本物の指摘候補は:

- **`composer.json` の `require.php` は `^8.0` だが `psalm.xml` の `phpVersion` が `7.4` のまま** — 設定不整合 (`should_fix`, profile `should_fix_lax`)
- **`rector.php` で同じ `AnnotationBindingRector::class` が2回登録されている** — 軽微な mistake (`nit`)

## 仕込みたい oracle

```
pr_intent: "php8_migration"

expected_findings:
  - id: "psalm-xml-phpversion-mismatch"
    expected_outcome: known_bug
    severity: should_fix
    profile: should_fix_lax

  - id: "rector-duplicate-binding-rector"
    expected_outcome: known_bug
    severity: nit

  - id: "php74-compat-trap"
    expected_outcome: known_false_positive_trap
    out_of_scope_reason: "PR intentionally drops PHP 7.4 support"

  - id: "constructor-property-promotion-trap"
    expected_outcome: known_false_positive_trap
    out_of_scope_reason: "PR is the PHP 8 migration"
```

## scoring 期待値 (M1 gate)

- `acceptable_pass_rate ≥ 0.7` (large PR は緩めに設定)
- `false_positive_rate ≤ 0.15` — 大規模 PR では誤検知が多くなりがちなので medium より緩い基準

## License

BEAR.Sunday は MIT License。元 LICENSE → [`../LICENSES/BEAR.Sunday.MIT.txt`](../LICENSES/BEAR.Sunday.MIT.txt)
