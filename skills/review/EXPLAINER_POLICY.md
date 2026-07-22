# explainer / send policy (Step 4c 後半 / send)

このファイルは `/pr-codex:review` Step 4c 後半（explainer）が `findings.verified.json` から `review.md` / `findings.sarif` を派生生成する際、および `/pr-codex:send` が inline comment を整形する際のポリシーである。hunter prompt には注入しない。

## review.md セクション構成
`review.md` は canonical `findings.verified.json` から機械的に導出する:

- `## 総評`: 全体評価と承認可否を 1-2 文で明示する（人間向け要約として記述してよいが、Must Fix / Should Fix の件数や内容が canonical findings と矛盾してはならない）
- `## 重大な問題 (Must Fix)`: `severity=must_fix` のみ
- `## 改善提案 (Should Fix)`: `severity=should_fix` かつ `posting.post_policy=body_summary` のみ。4軸 gate 不通過で `local_only` に降格した finding は載せない
- `## 軽微な指摘 (Nit)`: `severity=nit` を箇条書きで簡潔に。各項目に必ず `path/to/file.ext:L<行番号>` 表記を付ける
- `## 良い点`: 評価できるコードや設計判断を簡潔に述べる。厳しいレビューでも、良い点は認める
- `## 補足`: 投稿対象外の補足事項。`severity=note` や `posting.post_policy=local_only/suppress` の finding、コメント可能行がない範囲外の参考指摘、レビュー上の前提、確認できなかった事項を置く。なければ `なし`

## Should Fix inline comment 整形ルール

`/pr-codex:send --include-should-fix` が指定された場合のみ、`severity == "should_fix" && posting.post_policy == "body_summary"` の finding を PR inline comment として投稿してよい。整形ルールは Must Fix と同じく path/line/body を持つ inline comment とする。

- 上限なし。上位判定は `findings.verified.json` の `findings[]` 配列順をそのまま使う
- 1 件あたり 3 行以内: `path:L<行>`、改善内容 1 行、提案 1 行
- カテゴリ別グルーピングは行わず、単純な箇条書きにする
- body section は作らない。diff 範囲外退避時だけ `## 行コメント不可 (diff 範囲外)` に混ぜる
- Nit / 補足は Should Fix inline comment に混ぜない

## SARIF 派生成果物の公開境界

`findings.verified.json` から `findings.sarif` を生成する際の `posting.post_policy` → SARIF 表現は以下に固定する。SARIF は M2 では local-only artifact であり、GitHub Code Scanning upload は自動化しない。

| `posting.post_policy` | SARIF 上の扱い |
|---|---|
| `inline` | `suppressions` なし。Must Fix inline と同じ公開可能スコープ |
| `body_summary` | `should_fix` は `suppressions` なし。`nit` は noise 防止のため `suppressions` を付ける |
| `local_only` | `suppressions: [{kind: "external", status: "accepted", justification: "local_only per pr-codex post_policy"}]` |
| `suppress` | SARIF に出力しない。canonical 内部記録だけに残す |

`severity` は `must_fix → error` / `should_fix → warning` / `nit → note` / `note → none` に写像する。`category == "security"` の finding は `security` extension を必須とし、SARIF には `properties.security_severity_label` などの public-safe metadata だけを出力し、exploit 詳細を含めない。
