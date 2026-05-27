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

いずれも pr-codex が投稿した review thread だけを対象にする。snapshot の `review_author` / `review_authors`（未指定時は `chatgpt-codex-connector`）と thread 先頭コメント author が一致しない thread は無視する。

## 学習しないもの

- author 無反応の未解決 thread
- pr-codex 以外（人間レビュアーや別 bot）が投稿した review thread
- PR が merge された事実だけ
- `<!-- hermes-auto:... -->` など bot/generated marker だけ
- raw log、secret、ローカルパスを含む文脈
- PR type / path / finding class で限定できない episode

## 出力

`tasks/learn_feedback.py` は snapshot JSON から以下を生成する。

- `learn-result.json`: 集計、学習ポリシー、無視した thread の理由
- `feedback-artifacts/*.json`: signal ごとの public-safe artifact
- optional `episodes.jsonl`: `tasks/episode_memory.py write` で上記 artifact から作る repo-local episode store

artifact は token らしき値とローカルパスを redaction し、コメント本文は excerpt に切り詰める。episode store へ昇格する場合も raw comment/log は入れず、PR type / path / finding class を必ず付ける。

## 使い方

`/pr-codex:learn [snapshot.json] [output-dir]` として呼び出されたら、Claude は `$ARGUMENTS` を shell で再分割せず、Claude が解釈済みの 1 番目の引数を `SNAPSHOT_JSON`、2 番目の引数を `OUTPUT_DIR` として直接 bind する。これにより空白を含む quoted path を保持したまま、現在の作業ディレクトリではなく plugin root 配下の `tasks/learn_feedback.py` に渡す。

```bash
# Claude が slash-command の解釈済み引数から直接 bind する。shell で再分割しない。
SNAPSHOT_JSON="<1 番目の引数: snapshot.json>"
OUTPUT_DIR="<2 番目の引数: output-dir>"
if [ -z "${CLAUDE_PLUGIN_ROOT:-}" ]; then
  LEARN_SKILL_PATH="$(find "$PWD" .. -path '*/skills/learn/SKILL.md' -print -quit 2>/dev/null || true)"
  if [ -z "$LEARN_SKILL_PATH" ]; then
    echo "CLAUDE_PLUGIN_ROOT が未設定で、skills/learn/SKILL.md から plugin root を推定できません" >&2
    exit 1
  fi
  CLAUDE_PLUGIN_ROOT="${LEARN_SKILL_PATH%/skills/learn/SKILL.md}"
fi
CLAUDE_PLUGIN_ROOT="$(cd "$CLAUDE_PLUGIN_ROOT" && pwd)"
HELPER="$CLAUDE_PLUGIN_ROOT/tasks/learn_feedback.py"

python3 "$HELPER" \
  --input "$SNAPSHOT_JSON" \
  --output-dir "$OUTPUT_DIR"
```

例:

```bash
/pr-codex:learn feedback.json out
/pr-codex:learn "feedback snapshot.json" "learn out"
```

上記は `$CLAUDE_PLUGIN_ROOT/tasks/learn_feedback.py` を絶対パスとして解決してから、それぞれ `--input "feedback.json" --output-dir "out"`、`--input "feedback snapshot.json" --output-dir "learn out"` として実行する。

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
