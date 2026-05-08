---
user-invocable: true
name: pr-codex-learn
description: "投稿後の GitHub review thread / resolved / outdated / false-positive 信号から public-safe な feedback artifact を生成する"
argument-hint: "[snapshot.json] [output-dir]"
allowed-tools: ["Bash", "Read", "Write", "Glob", "Grep"]
---

# pr-codex-learn

`/pr-codex:send` 後に返ってきた明示的な GitHub feedback だけを、次回レビュー改善用のローカル artifact として蓄積する。暗黙の merge、author 無反応、bot/generated marker だけでは学習しない。

## 学習対象

- GraphQL review thread の `isResolved: true`: `addressed` signal
- GraphQL review thread の `isOutdated: true`: `superseded` signal
- 明示ラベル/コメント `pr-codex/false-positive`: `false_positive` signal

## 学習しないもの

- author 無反応の未解決 thread
- PR が merge された事実だけ
- `<!-- hermes-auto:... -->` など bot/generated marker だけ
- raw log、secret、ローカルパスを含む文脈

## 出力

`tasks/learn_feedback.py` は snapshot JSON から以下を生成する。

- `learn-result.json`: 集計、学習ポリシー、無視した thread の理由
- `feedback-artifacts/*.json`: signal ごとの public-safe artifact

artifact は token らしき値とローカルパスを redaction し、コメント本文は excerpt に切り詰める。

## 使い方

既に snapshot JSON がある場合:

```bash
python3 tasks/learn_feedback.py \
  --input /path/to/feedback-snapshot.json \
  --output-dir ~/claude-loop-pr-codex/learn/<org>-<repo>-<pr>-<head_sha_short>
```

`snapshot` には少なくとも次のキーを含める。

```json
{
  "repository": "owner/repo",
  "pr_number": 123,
  "head_sha": "...",
  "review_threads": [],
  "labels": [],
  "comments": []
}
```

`review_threads` は GitHub GraphQL の `reviewThreads.nodes` 形式を使う。`labels` は `[{"name":"..."}]` または文字列配列、`comments` は false-positive の明示コメントを含む PR issue comments を渡す。

## public-safe ルール

- GitHub に投稿する必要はない。artifact はローカル保持を前提にする
- artifact に secret / API key / token / credential file contents / ローカル credential path / raw sensitive log を入れない
- false-positive は `pr-codex/false-positive` が明示され、対象 thread id がコメント本文に含まれる場合だけ扱う
- 同じ snapshot を再実行しても同じファイル名へ上書きするため冪等
