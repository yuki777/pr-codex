# Fixture: positive / synthetic seeded billing regression

**Source**: synthetic overlay authored for pr-codex
**規模**: +27/-21, 2 files
**種別**: `positive_seeded`

## 目的

既知バグの recall（再現率）を測るための positive fixture。モデルへ渡す入力は `base/` と `diff.patch` だけで、`expected-findings.json` の oracle と seed 情報は渡さない。

## 仕込んだ既知バグ

1. `refund_many()` が先頭 payment の tenant だけを検査し、2件目以降の別 tenant payment を refund できる。
2. tenant-local な payment id と request id だけで、global namespace の idempotency key を作るため tenant 間で衝突する。
3. `(created_at, id)` 順のページング cursor から `id` を削除したため、同一 timestamp の行をページ境界で飛ばす。

`limit + 1` の取得後に `rows[:limit]` を返す処理は has-more 判定のための正常系で、false-positive trap とする。

## scoring gate

- `recall_known_bug >= 0.66`（3件中2件以上）
- `acceptable_pass_rate >= 0.8`
- `false_positive_rate <= 0.1`

## 再現性

`base/` は patch 適用前 snapshot。`metadata.json` の `base_sha` / `head_sha` は、path と file content を辞書順に連結した synthetic tree の SHA-256。`expected-findings.json.provenance.seeded_bugs` が seed id・行・発火条件・影響を固定する。

`eval-report.json` の各 fixture run は `sample_count=1` / `repetitions=1` の単一保存 run で、prompt/config、fixture patch、oracle、sanitized findings、score report、execution manifest、scorer の repo-relative path と file bytes の SHA-256 を持つ。execution manifest は `eval-execution.v1` として stable run ID、完全な `execution` object、sample count、repetitions、評価日時を固定し、report の model / reasoning effort を含む実行条件との不一致を検出できる。`aggregate` は 6 run から導出するため provenance を持たない。

保存元の absolute path やローカル一時 root 名は保存しない。対応は次のとおり（score report は各 findings から現行 `tasks/score_fixture.py` で再生成）:

| eval run | prompt/config source | sanitized findings source |
| --- | --- | --- |
| `issue112-positive-round-policy-baseline-r1` | `fixed-round3-v2.md` | `fixed-final-round3.findings.verified.json` |
| `issue112-positive-round-policy-iterative-r1` | `final-round1.md` | `adaptive-final-round1.findings.verified.json` |
| `issue112-positive-preflight-effort-baseline-r1` | `preflight-effort-baseline.prompt.md`（shared bytes / xhigh） | `preflight-effort-baseline.findings.json`（shared upstream findings） |
| `issue112-positive-preflight-effort-iterative-r1` | `preflight-effort-iterative.prompt.md`（shared bytes / high） | `preflight-effort-iterative.findings.json`（shared upstream findings） |
| `issue112-positive-fable-prompt-baseline-r1` | `baseline.md` | `fable-baseline-default.findings.verified.json` |
| `issue112-positive-fable-prompt-iterative-r1` | `lean-v2.md` | `fable-lean-v2-default.findings.verified.json` |

preflight effort の2 run は prompt SHA-256 と findings SHA-256 が同一で、実行時の `model_reasoning_effort` だけを xhigh / high で変えている。両 run とも Must Fix 2件を `confirmed` と判定した。

再生成コマンドは `python3 tasks/score_fixture.py --expected fixtures/positive/expected-findings.json --actual <findings> --out <score-report> --evaluated-at 2026-07-23T00:00:00Z`。`eval-artifacts/` には synthetic fixture だけを含む canonical prompt、verified findings、score report、execution manifest のみを保存し、raw stderr/stdout、実行 log、secret、ローカル absolute path は保存しない。

## License

この fixture のコードは pr-codex 評価用に新規作成した CC0-1.0 の synthetic source で、第三者コードを含まない。
