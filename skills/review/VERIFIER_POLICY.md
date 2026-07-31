# verifier policy (Step 4c 前半)

このファイルは `/pr-codex:review` Step 4c 前半（verifier）が candidates を `findings.verified.json` へ絞り込む際のポリシーである。hunter prompt には注入しない。hunter 観点は `HUNTER_CRITERIA.md`、explainer / send のポリシーは `EXPLAINER_POLICY.md` を参照。

## 3軸ゲート
Must Fix は **REAL=yes ∧ TRIGGERABLE=yes ∧ IMPACTFUL=yes** をすべて満たし、かつ `evidence_level=verified` の finding だけとする。いずれかが `no` / `unknown` の場合は Should Fix 以下へ降格する。特定条件でだけ発火する問題でも、この 3 軸と verified 条件を満たすなら Must Fix にできる。

3軸の判定基準:

| 軸 | yes | no | unknown |
|---|---|---|---|
| REAL | この場所で本当に問題がある | 誤解 / 仕様通り / 既存議論で解決済み | 推測または再現不能 |
| TRIGGERABLE | 実環境のコードパスで発火する | 静的に到達不能 / dead code | 発火条件が再現不能 |
| IMPACTFUL | merge を止めるべき影響度 (data loss / security / 仕様不一致) | 影響限定的、ローカル / 軽微 | 影響範囲が確認できない |

各軸は `yes` / `no` / `unknown` のいずれかだけを使い、severity だけから `yes` を推測しない。採用したい理由ではなく落とす理由を優先探索し、`unknown` を `yes` 扱いしない。

`blast_radius` は影響の広がりを表す非ゲート metadata であり、`isolated` / `component` / `systemic` / `unknown` のいずれかを必ず記録する。優先順位付けと人間向け説明には使ってよいが、Must Fix gate には使わない。

## 二者一致の扱い
Claude hunter と Codex hunter は非対称な重点役割を持つため、二者の同一指摘は**独立した証拠として扱わない**。一致は challenge / verify round での検証優先度を上げるシグナルとしてのみ使う。`evidence_level` は一致の有無ではなく、静的解析・型・lint・他箇所のパターン・trigger path の特定など、一致以外の根拠だけで決める。

## エビデンスラダーと採用基準

各 finding には根拠の強さに応じて 5 段の `evidence_level` を 1 つだけ付ける。
ラダー段階は決定論的に選び、1 つの finding で複数段の条件を満たす場合は最も高い到達段階に揃える。

| Level | 名称 | 採用条件 |
|---|---|---|
| 1 | `suspicion` | hunter が候補として挙げただけ。具体的根拠なし |
| 2 | `corroborated` | 静的解析・型・lint・他箇所のパターンで裏付け（二者一致は含めない） |
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

`evidence_level` の判定根拠は finding の `reason` / `suggestion` に明示すること。

## security extension
security-sensitive finding は `category: "security"` とし、通常の `severity`（Must/Should/Nit/Note）に加えて `security` extension を必ず付ける:
- `security.severity`: `critical` / `high` / `medium` / `low` / `info`
- `security.confidence`: `high` / `medium` / `low`
- `security.exploitability`: `proven_in_changed_code` / `triggerable_from_changed_code` / `theoretical` / `unknown`
- `security.public_safe_summary`: public repo に載せても安全な要約。exploit command、payload、secret、攻撃手順の詳細を書かない
- `security.disclosure_policy`: `inline_safe` / `body_summary_safe` / `local_only`

`critical` / `high` は公開 inline comment にしない。`body_summary_safe` または `local_only` として、公開 body には `public_safe_summary` レベルの安全な説明だけを使う。

## Root-cause clustering

複数 finding が同一 root cause に由来する場合は、`findings.verified.json` の top-level `root_cause_clusters[]` にまとめてよい。Markdown の `review.md` には full finding を残し、GitHub 投稿時だけ representative + affected findings summary に集約する。

- cluster は `id` / `summary` / `representative_finding_id` / `finding_ids` を持つ
- 各 member finding には同じ `root_cause_id` を付ける
- representative は cluster 内で最も高い severity の finding にする。Must Fix を含む cluster の representative は Must Fix でなければならない
- severity は cluster によって下げない。重複抑制は投稿表現の問題であり、canonical artifact では個々の finding と severity を維持する
- distinct bugs を無理に統合しない。修正箇所・原因・再現経路が異なる場合は別 cluster または cluster なしにする
