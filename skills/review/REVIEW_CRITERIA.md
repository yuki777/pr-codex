## 目的と完了条件
このレビューの目的は、PR の変更が本番投入可能かを判断し、マージ前に直すべき問題を具体的に示すことです。

完了条件:
- `pr.diff` と `pr.diff.ranges.txt` を根拠に、レビュー対象の変更行だけを評価する
- Must Fix / Should Fix / Nit の各指摘に、head 基準のファイルパスと行番号を付ける
- 重要度、問題の理由、修正提案が読み手にそのまま実行できる粒度になっている
- 既存レビューコメントを取得できる場合は、重複指摘を避ける

停止条件:
- `pr.diff` が存在しない、または空の場合は、呼び出し元プロンプトの指示に従って `PR_DIFF_UNAVAILABLE` のみを返す
- 関連 URL や追加情報を取得できない場合でも推測で補わず、`pr.diff` と checkout 済みソースから確認できる範囲でレビューを完了する
- 必要な根拠や行番号を特定できない指摘は Must Fix / Should Fix / Nit に含めない

## MCP利用時の追加情報収集
MCPが使える場合は、以下も取得してレビューに活用すること:
- PRの説明文・コメント（レビュー意図や背景の把握）
- 既存のレビューコメント（重複指摘の回避）

## レビュー観点
以下の観点で厳密にレビューし、問題を見逃さないこと:

### 1. 設計・アーキテクチャ
- SOLID原則への違反
- 不適切な責務分離
- レイヤー違反（例: ドメイン層がインフラ層に依存）
- 過剰な結合、不足した凝集
- 拡張性・保守性の考慮不足

### 2. コード品質
- 命名の不適切さ（曖昧、誤解を招く、規約違反）
- 不要な複雑性（YAGNI違反、過剰な抽象化）
- DRY違反（コピペコード）
- マジックナンバー・マジックストリング
- デッドコード、到達不能コード

### 3. バグ・潜在的問題
- エッジケースの未処理（null, 空配列, 境界値）
- 競合状態・スレッドセーフティ
- リソースリーク（未クローズのコネクション等）
- 型安全性の欠如
- エラーハンドリングの不備（握りつぶし、不適切なリカバリ）

### 4. セキュリティ
- インジェクション脆弱性（SQL, XSS, コマンド）
- 認証・認可の不備
- 機密情報のハードコード・ログ出力
- 入力バリデーションの不足

- security-sensitive finding は `category: "security"` とし、通常の `severity`（Must/Should/Nit/Note）に加えて `security` extension を必ず付ける:
  - `security.severity`: `critical` / `high` / `medium` / `low` / `info`
  - `security.confidence`: `high` / `medium` / `low`
  - `security.exploitability`: `proven_in_changed_code` / `triggerable_from_changed_code` / `theoretical` / `unknown`
  - `security.public_safe_summary`: public repo に載せても安全な要約。exploit command、payload、secret、攻撃手順の詳細を書かない
  - `security.disclosure_policy`: `inline_safe` / `body_summary_safe` / `local_only`

- 禁止: exploit 実行、secret の露出、攻撃手順や PoC の詳細公開、Kali/network pentest 的な実行。レビューは diff と checkout 済みソースの静的確認に限定する。
- `critical` / `high` は公開 inline comment にしない。`body_summary_safe` または `local_only` として、公開 body には `public_safe_summary` レベルの安全な説明だけを使う。

### 5. パフォーマンス
- N+1問題
- 不要なメモリ確保・コピー
- 非効率なアルゴリズム・データ構造の選択
- キャッシュの考慮不足

### 6. テスト
- テストカバレッジの不足
- テストの意図が不明確
- テストが実装に密結合（リファクタリング耐性の欠如）
- 境界値テストの欠如

### 7. 横展開の一貫性・欠落コードの検出
差分に「書かれているコード」だけでなく、「書かれるべきなのに書かれていないコード」を検出すること。これは差分だけでは見えない問題であり、意識的にチェックしなければ見逃す。

- パターンの横展開漏れ: 同一の変更パターンが複数ファイルに適用されている場合、全対象ファイルに同じパターンが適用されているか確認する。「AとBにはある変更がCとDにはない」ケースを見逃さない
- 機能の配線だけで初期化が欠落: UIコンポーネントやイベントハンドラの配線（props, emit, import）は追加されているが、それを動作させるための前提条件（フラグ設定、初期化処理、データ取得）が欠けていないか
- 変更対象ファイルの網羅性: PRの変更ファイル一覧を確認し、類似の役割を持つファイルが変更対象から漏れていないか

具体的な確認手順:
1. 差分内で繰り返されるパターン（同じ変数の追加、同じコンポーネントの配置等）を特定する
2. そのパターンが適用されるべき全ファイルをリストアップする（差分外のファイルも含む）
3. 各ファイルで必要な変更がすべて揃っているか、実際にファイルを読んで確認する。差分だけで判断しない

## 行番号規約
指摘に付ける行番号は、以下の規約で必ず head 基準に統一すること。base 基準や diff 内のオフセットを書いてはいけない。

- 行番号は `clone-claude/` および `clone-codex/` にチェックアウトされた head の行番号で書く（実ファイルを Read して確定する）
- Must Fix / Should Fix の見出し行番号は、必ず `pr.diff.ranges.txt` に記載された同一 `path` の新ファイル側 hunk 範囲内に収める。GitHub Reviews API はこの範囲外の inline comment を 422 で拒否する
- 範囲指定 `path:L<開始>-L<終了>` を使う場合は、開始行と終了行の両方が同一 hunk 範囲内に含まれる場合だけ使う。複数 hunk をまたぐ行範囲を見出しにしてはいけない
- 問題の本質が `pr.diff.ranges.txt` の範囲外にある場合は、同一ファイルの範囲内にある最も近い変更行を見出しに使い、本文で `(参考: path:L<行番号>)` として元の範囲外行を補足する
- 同一ファイルにコメント可能行がない範囲外指摘は、Must Fix / Should Fix の見出しとして出力せず、`### 補足` に参考情報として記載する
- 削除行に対する指摘は、削除位置の直後または直前の head 側に存在する行を `line` として指し、本文に「直前の削除に対する指摘」または「直後の削除に対する指摘」と明記する
- head 側の行が直接特定できない場合は、`pr.diff` の hunk header `@@ -OLD,N +NEW,M @@` を使い、`+NEW` 側オフセットから head 行を逆算する

## エビデンスラダーと採用基準

各 finding には根拠の強さに応じて 5 段の `evidence_level` を 1 つだけ付ける。
ラダー段階は決定論的に選び、1 つの finding で複数段の条件を満たす場合は最も高い到達段階に揃える。

| Level | 名称 | 採用条件 |
|---|---|---|
| 1 | `suspicion` | hunter が候補として挙げただけ。具体的根拠なし |
| 2 | `corroborated` | 静的解析・型・lint・他箇所のパターン・2 者の同一指摘で裏付け |
| 3 | `trigger_path_identified` | head diff 上で発火条件が特定できる |
| 4 | `impact_explained` | 影響範囲と修正方針が具体的に書ける |
| 5 | `verified` | 反証検討を経て採用 (verifier / 再現テスト / CI / 静的解析で確認) |

### 採用基準

- **Must Fix**: 原則 `verified` 以上。例外規則 (下記) で救済された場合のみ昇格可
- **Should Fix**: `corroborated` 以上
- それ未満 (`suspicion` 単独): `## 補足` セクションへ退避し、GitHub には投稿しない

### 例外規則 (verified への昇格)

CI / type system / 既存 lint で検出される類の「明白な静的解析的バグ」は、
trigger path が再現できなくても `corroborated` かつ `impact_explained` が
両方揃えば `verified` 扱いにしてよい。

ただし救済根拠は finding の `evidence[]` に **必ず**
`type: static_analysis | ci_log | test` のいずれかで残すこと。
`type: manual_review` のみでの昇格は禁止。

### 説明品質との分離

`explanation_postable: bool` は「説明品質 (この finding の説明が
そのまま投稿可能か)」を表す独立フィールドであり、エビデンスラダーとは
直交する。`evidence_level=suspicion` は schema 制約で必ず
`explanation_postable=false` になるが、`verified` でも説明品質が
低ければ `explanation_postable=false` にできる。

## 出力フォーマット
レビュー結果は以下の形式で出力すること:

### 総評
全体的な評価を1-2文で述べる。承認可否を明示する。

### 重大な問題 (Must Fix)
マージ前に必ず修正すべき問題。Must Fix は **4軸ゲート (REAL=yes ∧ TRIGGERABLE=yes ∧ IMPACTFUL=yes ∧ (GENERAL=yes ∨ specific-impact 説明済)) を満たす指摘のみ** とする。REAL / TRIGGERABLE / IMPACTFUL のいずれかが `yes` に達しない場合、または GENERAL が `yes` でなく specific-impact も説明できない場合は Should Fix 以下へ降格する。各項目に以下を含める:
- 箇所: ファイルパスと行番号（必ず `path/to/file.ext:L<行番号>` または `path/to/file.ext:L<開始>-L<終了>` 形式）
- 問題: 何が問題か
- 理由: なぜ問題か
- 提案: どう修正すべきか
- 軸: `REAL=yes / TRIGGERABLE=yes / IMPACTFUL=yes / GENERAL=yes` のように 4 軸の値を 1 行で明記する
- Must Fix 昇格根拠: `unknown` 不在、または `GENERAL` が `yes` でない場合に specific-impact 説明済である理由を明記する

4軸の判定基準:

| 軸 | yes | no | unknown |
|---|---|---|---|
| REAL | この場所で本当に問題がある | 誤解 / 仕様通り / 既存議論で解決済み | 推測または再現不能 |
| TRIGGERABLE | 実環境のコードパスで発火する | 静的に到達不能 / dead code | 発火条件が再現不能 |
| IMPACTFUL | merge を止めるべき影響度 (data loss / security / 仕様不一致) | 影響限定的、ローカル / 軽微 | 影響範囲が確認できない |
| GENERAL | 横展開が必要なパターン or 同種の他箇所がある | この箇所固有 (ただし specific-impact 説明済みなら OK) | 横展開可能性が確認できない |

### Root-cause clustering

複数 finding が同一 root cause に由来する場合は、`findings.verified.json` の top-level `root_cause_clusters[]` にまとめてよい。Markdown の `review.md` には full finding を残し、GitHub 投稿時だけ representative + affected findings summary に集約する。

- cluster は `id` / `summary` / `representative_finding_id` / `finding_ids` を持つ
- 各 member finding には同じ `root_cause_id` を付ける
- representative は cluster 内で最も高い severity の finding にする。Must Fix を含む cluster の representative は Must Fix でなければならない
- severity は cluster によって下げない。重複抑制は投稿表現の問題であり、canonical artifact では個々の finding と severity を維持する
- distinct bugs を無理に統合しない。修正箇所・原因・再現経路が異なる場合は別 cluster または cluster なしにする

### 改善提案 (Should Fix)
修正が強く推奨される問題。同じフォーマットで記載。

#### body summary 整形ルール

`/pr-codex:send` でユーザーが明示 opt-in した場合のみ、`severity == "should_fix" && posting.post_policy == "body_summary"` の finding を PR body の `## 非ブロッキング改善 (Should Fix)` に要約してよい。整形ルールは以下とする:

- 上位 3 件まで。上位判定は `findings.verified.json` の `findings[]` 配列順をそのまま使う
- 1 件あたり 3 行以内: `path:L<行>`、改善内容 1 行、提案 1 行
- カテゴリ別グルーピングは行わず、単純な箇条書きにする
- body 内の配置は `## 良い点` の下、`## 行コメント不可 (diff 範囲外)` の上とする
- Nit / 補足はこのセクションに混ぜない

### 軽微な指摘 (Nit)
スタイルや好みに関する軽微な指摘。簡潔に記載（必ず `path/to/file.ext:L<行番号>` 表記を付与）。

### SARIF 派生成果物の公開境界

`findings.verified.json` から `findings.sarif` を生成する際の `posting.post_policy` → SARIF 表現は以下に固定する。SARIF は M2 では local-only artifact であり、GitHub Code Scanning upload は自動化しない。

| `posting.post_policy` | SARIF 上の扱い |
|---|---|
| `inline` | `suppressions` なし。Must Fix inline と同じ公開可能スコープ |
| `body_summary` | `should_fix` は `suppressions` なし。`nit` は noise 防止のため `suppressions` を付ける |
| `local_only` | `suppressions: [{kind: "external", status: "accepted", justification: "local_only per pr-codex post_policy"}]` |
| `suppress` | SARIF に出力しない。canonical 内部記録だけに残す |

`severity` は `must_fix → error` / `should_fix → warning` / `nit → note` / `note → none` に写像する。`category == "security"` の finding は `security` extension を必須とし、SARIF には `properties.security_severity_label = security.severity` を付ける。`security.severity == critical/high` または `security.disclosure_policy != inline_safe` の finding は public inline ではなく body summary/local-only 側に分岐する。

### 良い点
評価できるコードや設計判断があれば簡潔に述べる。厳しいレビューでも、良い点は認める。

### 補足
投稿対象外の補足事項があれば記載する。例: コメント可能行がない範囲外の参考指摘、レビュー上の前提、確認できなかった事項。なければ省略してよい。

## 重要
遠慮は不要。「動くから良い」は理由にならない。プロダクションコードとして長期的に保守可能かどうかを基準に判断すること。曖昧な表現（「〜かもしれません」「〜した方がいいかも」）は避け、断定的に指摘すること。採用したい理由ではなく落とす理由を優先探索し、実発火・影響・横展開または specific-impact を確認できない指摘を Must Fix にしないこと。
`evidence_level` の判定根拠は finding の reason / suggestion に明示すること。
