Review every verified observation in the packet and return only a DecisionFile JSON object with this exact shape:

```json
{
  "session_id": "the packet session id",
  "content_sha256": "the packet content SHA-256",
  "decisions": [
    {
      "observation_id": "verified observation id",
      "action": "accept or reject",
      "reviewer": "claude",
      "reason": "brief reason",
      "claim": null,
      "kind": null,
      "match": null,
      "force_new": false
    }
  ],
  "revocations": [
    {
      "target_id": "existing instruction or hypothesis id",
      "reviewer": "claude",
      "reason": "brief reason"
    }
  ]
}
```

Reject an observation when its claim is broader than its quotes or its kind is wrong, unless you correct it with a narrower `claim` or correct `kind` override. Reject an `explicit_instruction` that is a one-off request about the current topic, or re-classify it with `kind: "inference"` when the pattern could recur. A standing preference applies to how the tutor should teach in future sessions: "slayttaki terimleri Türkçeleştirme" is standing, while "bunu da detaylı anlat" is a one-off request. Map to an existing id only when it is clearly the same behaviour. Return one decision for every verified observation. Never invent observations. Do not use tools.

Packet:
$packet
