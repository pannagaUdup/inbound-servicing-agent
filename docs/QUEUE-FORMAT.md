# Queue & batched-turn format

What the async backbone carries, and what the worker hands to processing (agent + LLM/perception)
**after the 3-second batch window**.

## 1. Per-message entry (what's pushed to the queue, pre-batch)

Each inbound WhatsApp message is parsed into one `InboundMessage` and enqueued
(durable Redis Stream `sa:inbound` + per-conversation list `sa:conv:{cid}`).

```jsonc
{
  "message_id":  "wamid....",      // WhatsApp wam id (dedupe key)
  "sender":      "919353976407",   // full international number
  "type":        "text",           // text | image | voice | button
  "text":        "I have paid",    // body (text) / payload (button) / reply-id (interactive); null for media
  "media":       null,             // MediaEvidence once perception runs (see §3); null until then
  "media_id":    null,             // Meta media handle (image/voice only) — worker downloads by this
  "media_mime":  null,             // e.g. "image/jpeg", "audio/ogg"
  "received_at": "2026-11-19T14:09:00Z"
}
```

Per type:
- **text** → `text` = body, `media_id` = null
- **button / interactive** → `type` = "button", `text` = payload / reply-id
- **image** → `type` = "image", `text` = null, `media_id` + `media_mime` set
- **voice** → `type` = "voice", `text` = null, `media_id` + `media_mime` set

## 2. Batched turn (what the worker processes AFTER 3s)

When a conversation is picked up, the worker waits `SA_BATCH_SECONDS` (default 3s),
then drains that conversation's list **in order**. The batch is:

```jsonc
{
  "conversation_id": "wa:9353976407",          // partition key (wa:<last-10-digits>)
  "batched_at": "2026-11-19T14:09:03Z",
  "messages": [                                 // every message within the 3s window, in order
    { /* InboundMessage */ }, { /* InboundMessage */ }, ...
  ]
}
```

The worker then turns the batch into turns:
- **all text/button bubbles → ONE merged turn** (texts joined, in order).
- **each image/voice → its own turn**, with `media_id` downloaded and `media` filled by perception.

### Example — borrower sent "I have" + "paid" + a screenshot within 3s
```jsonc
{
  "conversation_id": "wa:9353976407",
  "messages": [
    {
      "message_id": "wamid.AAA", "sender": "919353976407",
      "type": "text", "text": "I have paid", "media": null,
      "media_id": null, "media_mime": null, "received_at": "2026-11-19T14:09:00Z"
    },
    {
      "message_id": "wamid.BBB", "sender": "919353976407",
      "type": "image", "text": null,
      "media_id": "4114351848871289", "media_mime": "image/jpeg",
      "media": {                                  // filled by perception after download (see §3)
        "kind": "upi_receipt", "utr": "123456789012", "amount": 3200,
        "txn_date": "2026-11-14", "payee_vpa": "collect@cnbank",
        "status": "success", "ocr_text": "...", "transcript": null, "confidence": 0.98
      },
      "received_at": "2026-11-19T14:09:02Z"
    }
  ]
}
```

## 3. `MediaEvidence` — what perception returns (LLM/T3 contract)

For image/voice, the worker downloads the bytes (by `media_id`) and hands them to
perception, which returns this object; the worker sets it as `media` on the message.

```jsonc
{
  "kind":       "upi_receipt",     // upi_receipt | bank_statement | id_document | voice_note | other
  "transcript": null,              // voice: speech-to-text output
  "utr":        "123456789012",    // receipt: extracted fields ↓
  "amount":     3200,
  "txn_date":   "2026-11-14",      // ISO date
  "payee_vpa":  "collect@cnbank",
  "status":     "success",
  "ocr_text":   "...",             // raw OCR text (screened for injection)
  "confidence": 0.98
}
```

Voice example:
```jsonc
{ "kind": "voice_note", "transcript": "haan maine payment kar diya", "confidence": 0.95,
  "utr": null, "amount": null, "txn_date": null, "payee_vpa": null, "status": null, "ocr_text": null }
```

## Notes
- The queue carries only the **reference** (`media_id` + `media_mime`), never raw bytes — ingress stays <100ms and Redis stays light.
- The worker downloads bytes just-in-time and stores them (`SA_MEDIA_DIR`, local stand-in for S3).
- Text turns incur the 3s batch delay; image/voice get a **holding reply within 3s** while perception runs.
