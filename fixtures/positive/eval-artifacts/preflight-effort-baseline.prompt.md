あなたは GitHub PR レビュー投稿前の独立検証エージェントです。以下の各 Must Fix finding について、この指摘が誤りである可能性を1つだけ探索し、preflight-semantic.v1 JSONだけを返してください。ツールは使わず、下記の finding と diff だけを根拠にしてください。

判定は confirmed / refuted / insufficient_evidence のいずれかです。反証を挙げられない場合のみ confirmed とし、counterargument に最有力の反証仮説と棄却理由を1〜2文で書いてください。decisions には対象 finding_id を過不足なく含めてください。

## Must Fix findings
```json
[
  {
    "source_agents": [
      "codex-baseline-xhigh"
    ],
    "merged_from": [
      "eval:codex-baseline-xhigh"
    ],
    "location": {
      "path": "src/auth/refund_service.py",
      "start_line": 46,
      "side": "RIGHT"
    },
    "severity": "must_fix",
    "category": "security",
    "title": "先頭要素だけのテナント検証により他テナントの支払いを返金できる",
    "problem": "一括取得した支払いのうち先頭要素だけを actor のテナントと照合しており、2件目以降は認可確認なしで返金しています。",
    "reason": "actor 自身のテナントに属する支払いを先頭にし、その後へ別テナントの支払いを含めると検査を通過します。ループは残りの各 payment を無条件で record_refund に渡すため、変更コード上でテナント境界を越えた返金が実行されます。",
    "suggestion": "ループ内で全 payment の tenant_id を actor.tenant_id と照合し、1件でも不一致なら返金処理を始める前にバッチ全体を拒否してください。可能ならリポジトリ取得自体も tenant_id でスコープしてください。",
    "evidence_level": "verified",
    "axes": {
      "real": "yes",
      "triggerable": "yes",
      "impactful": "yes"
    },
    "blast_radius": "component",
    "posting": {
      "post_policy": "inline",
      "explanation_postable": true,
      "audience": "eval_harness"
    },
    "security": {
      "severity": "medium",
      "confidence": "high",
      "exploitability": "triggerable_from_changed_code",
      "disclosure_policy": "inline_safe",
      "public_safe_summary": "先頭要素だけのテナント検証により他テナントの支払いを返金できる"
    },
    "id": "852ad6a38743d63dfc3fbe8fdc49f1db784055a0ead01a38bd4c255537d409f8",
    "fingerprint": "852ad6a38743d63dfc3fbe8fdc49f1db784055a0ead01a38bd4c255537d409f8"
  },
  {
    "source_agents": [
      "codex-baseline-xhigh"
    ],
    "merged_from": [
      "eval:codex-baseline-xhigh"
    ],
    "location": {
      "path": "src/auth/refund_service.py",
      "start_line": 53,
      "side": "RIGHT"
    },
    "severity": "must_fix",
    "category": "bug",
    "title": "テナント接頭辞の削除で返金の冪等性キーが衝突する",
    "problem": "リポジトリのグローバルな冪等性キーから tenant_id が削除されています。",
    "reason": "Payment.id はテナント内でしか一意でなく、record_refund の冪等性キーは全テナント共通の名前空間です。異なるテナントが同じ idempotency_key と payment.id を使うと同一キーになり、一方の返金が他方の既存処理として扱われるなど、返金の欠落または誤関連付けが発生します。",
    "suggestion": "従来どおり tenant_id を含め、例えば f\"{actor.tenant_id}:{idempotency_key}:{payment.id}\" をリポジトリキーとして使用してください。",
    "evidence_level": "verified",
    "axes": {
      "real": "yes",
      "triggerable": "yes",
      "impactful": "yes"
    },
    "blast_radius": "component",
    "posting": {
      "post_policy": "inline",
      "explanation_postable": true,
      "audience": "eval_harness"
    },
    "id": "eb3d969b9b2b92c3cf132d9970dceb1b0b1e5e62c09464caaf24a1a8cbcc0e52",
    "fingerprint": "eb3d969b9b2b92c3cf132d9970dceb1b0b1e5e62c09464caaf24a1a8cbcc0e52"
  },
  {
    "source_agents": [
      "codex-baseline-xhigh"
    ],
    "merged_from": [
      "eval:codex-baseline-xhigh"
    ],
    "location": {
      "path": "src/billing/pagination.py",
      "start_line": 59,
      "side": "RIGHT"
    },
    "severity": "must_fix",
    "category": "bug",
    "title": "時刻だけのカーソルでは同一時刻の行を欠落なくページングできない",
    "problem": "次ページカーソルから payment_id が削除され、created_at だけが保存されています。",
    "reason": "クエリの順序は (created_at DESC, id DESC) ですが、同じ created_at を持つ複数行の途中でページが切れると、時刻だけでは最後に返した行の位置を表現できません。次回の created_before 条件は同時刻の未返却行を除外するか、同時刻の既返却行を再取得するため、欠落または重複が必ず発生します。",
    "suggestion": "Cursor に payment_id を残し、エンコード、デコード、PaymentQuery.page_before のすべてで (created_at, payment_id) の複合カーソルを渡して辞書順の境界条件を適用してください。",
    "evidence_level": "verified",
    "axes": {
      "real": "yes",
      "triggerable": "yes",
      "impactful": "yes"
    },
    "blast_radius": "component",
    "posting": {
      "post_policy": "inline",
      "explanation_postable": true,
      "audience": "eval_harness"
    },
    "id": "9d5e211ba329346bf9121fb2026490dd5f6d000a33e15da0ae04f8cad12b179c",
    "fingerprint": "9d5e211ba329346bf9121fb2026490dd5f6d000a33e15da0ae04f8cad12b179c"
  }
]
```

## PR diff
```diff
diff --git a/src/auth/refund_service.py b/src/auth/refund_service.py
index 93092a4..ec43142 100644
--- a/src/auth/refund_service.py
+++ b/src/auth/refund_service.py
@@ -36,21 +36,26 @@
     def __init__(self, repository: PaymentRepository) -> None:
         self._repository = repository
 
-    def refund_one(
+    def refund_many(
         self,
         actor: Actor,
-        payment_id: str,
+        payment_ids: Sequence[str],
         idempotency_key: str,
-    ) -> str:
-        payment = self._repository.get_many([payment_id])[0]
-        if payment.tenant_id != actor.tenant_id:
+    ) -> list[str]:
+        payments = self._repository.get_many(payment_ids)
+        if payments and payments[0].tenant_id != actor.tenant_id:
             raise PermissionError("payment belongs to another tenant")
-        if payment.state != "captured":
-            raise ValueError("only captured payments can be refunded")
 
-        repository_key = f"{actor.tenant_id}:{idempotency_key}:{payment.id}"
-        return self._repository.record_refund(
-            payment,
-            payment.amount,
-            repository_key,
-        )
+        results: list[str] = []
+        for payment in payments:
+            if payment.state != "captured":
+                continue
+            repository_key = f"{idempotency_key}:{payment.id}"
+            results.append(
+                self._repository.record_refund(
+                    payment,
+                    payment.amount,
+                    repository_key,
+                )
+            )
+        return results
diff --git a/src/billing/pagination.py b/src/billing/pagination.py
index 234dc4f..2067d5b 100644
--- a/src/billing/pagination.py
+++ b/src/billing/pagination.py
@@ -14,7 +14,6 @@
 @dataclass(frozen=True)
 class Cursor:
     created_at: datetime
-    payment_id: str
 
 
 @dataclass(frozen=True)
@@ -27,7 +26,7 @@
     def page_before(
         self,
         tenant_id: str,
-        cursor: Cursor | None,
+        created_before: datetime | None,
         limit: int,
     ) -> list[PaymentRow]:
         """Return rows ordered by (created_at DESC, id DESC)."""
@@ -35,12 +34,11 @@
 
 
 def encode_cursor(cursor: Cursor) -> str:
-    return f"{cursor.created_at.isoformat()}|{cursor.payment_id}"
+    return cursor.created_at.isoformat()
 
 
 def decode_cursor(value: str) -> Cursor:
-    created_at, payment_id = value.rsplit("|", 1)
-    return Cursor(datetime.fromisoformat(created_at), payment_id)
+    return Cursor(datetime.fromisoformat(value))
 
 
 def list_recent(
@@ -50,10 +48,13 @@
     limit: int,
 ) -> Page:
     cursor = decode_cursor(after) if after else None
-    rows = query.page_before(tenant_id, cursor, limit + 1)
+    rows = query.page_before(
+        tenant_id,
+        cursor.created_at if cursor else None,
+        limit + 1,
+    )
     page_rows = rows[:limit]
     next_cursor = None
     if len(rows) > limit:
-        last = page_rows[-1]
-        next_cursor = encode_cursor(Cursor(last.created_at, last.id))
+        next_cursor = encode_cursor(Cursor(page_rows[-1].created_at))
     return Page(page_rows, next_cursor)

```
