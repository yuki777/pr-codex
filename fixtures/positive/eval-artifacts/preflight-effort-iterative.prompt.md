あなたは GitHub PR レビュー投稿前の独立検証エージェントです。以下の各 Must Fix finding について、この指摘が誤りである可能性を1つだけ探索し、preflight-semantic.v1 JSONだけを返してください。ツールは使わず、下記の finding と diff だけを根拠にしてください。

判定は confirmed / refuted / insufficient_evidence のいずれかです。反証を挙げられない場合のみ confirmed とし、counterargument に最有力の反証仮説と棄却理由を1〜2文で書いてください。decisions には対象 finding_id を過不足なく含めてください。

## Must Fix findings
```json
[
  {
    "source_agents": [
      "fixed-final-round3"
    ],
    "merged_from": [
      "eval:fixed-final-round3"
    ],
    "location": {
      "path": "src/auth/refund_service.py",
      "start_line": 46,
      "side": "RIGHT"
    },
    "severity": "must_fix",
    "category": "security",
    "title": "Batch authorization validates only the first payment",
    "problem": "The service checks only `payments[0].tenant_id` before iterating over and refunding every captured payment returned by the unscoped batch lookup.",
    "reason": "Payment IDs are tenant-local and `get_many` receives no tenant, so a returned batch can contain payments from different tenants. If the first payment belongs to the actor, every later payment bypasses ownership validation and reaches `record_refund`, enabling a cross-tenant refund and integrity violation.",
    "suggestion": "Validate every payment's tenant before performing any refund, rejecting the whole batch if one does not belong to the actor. Also prefer a tenant-scoped repository lookup.",
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
      "public_safe_summary": "Batch authorization validates only the first payment"
    },
    "id": "f0689ec3fd819352f34f8c54583299f645a33f8729a3ed0b3b740d85c0022148",
    "fingerprint": "f0689ec3fd819352f34f8c54583299f645a33f8729a3ed0b3b740d85c0022148"
  },
  {
    "source_agents": [
      "fixed-final-round3"
    ],
    "merged_from": [
      "eval:fixed-final-round3"
    ],
    "location": {
      "path": "src/auth/refund_service.py",
      "start_line": 53,
      "side": "RIGHT"
    },
    "severity": "must_fix",
    "category": "security",
    "title": "Global refund idempotency keys can collide across tenants",
    "problem": "The repository key omits the tenant even though idempotency keys use a global namespace and payment IDs are unique only within a tenant.",
    "reason": "Distinct tenants can use the same client key for the same tenant-local payment ID, producing identical repository keys. The repository must then treat separate cross-tenant refund operations as the same idempotent operation, suppressing or misattributing a legitimate refund.",
    "suggestion": "Include the tenant namespace in every repository key, such as `f\"{actor.tenant_id}:{idempotency_key}:{payment.id}\"`.",
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
      "public_safe_summary": "Global refund idempotency keys can collide across tenants"
    },
    "id": "48b812456c7ad5c91ba06c42f741108db1f2e70a438cf2ff107a29f1e069bac3",
    "fingerprint": "48b812456c7ad5c91ba06c42f741108db1f2e70a438cf2ff107a29f1e069bac3"
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
