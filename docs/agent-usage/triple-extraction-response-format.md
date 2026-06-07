# Triple Extraction Response Format

Responses must be strict JSON with no extra top-level fields:

```json
{
  "request_id": "req_000001",
  "status": "ok",
  "output": {
    "triples": [
      {
        "subject": "美国商务部",
        "predicate": "限制",
        "object": "先进AI芯片出口",
        "fact": "美国商务部限制先进AI芯片出口。",
        "valid_at": "2024-01-01",
        "invalid_at": null,
        "source": "现实种子",
        "source_file": "seed.md",
        "evidence": "原文证据片段",
        "confidence": 0.82,
        "metadata": {}
      }
    ]
  }
}
```

Rules:

- `request_id` must exactly match the request file.
- `status` must be `ok`, `error`, or `skipped`.
- `output` must match `expected_schema`.
- `confidence` must be between `0.0` and `1.0`.
- Do not add extra fields.
- Do not invent facts not present in the seed or referenced context.
