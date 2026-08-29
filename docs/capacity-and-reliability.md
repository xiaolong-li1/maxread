# MaxRead capacity and reliability profile

Validated on 2026-08-29 against `gpt-5.6-sol` through the primary
`sub2api.ziplab.co/v1` Responses endpoint. The production host has 1.6 GiB of
RAM. These limits are an operational contract, not theoretical model limits.

## Validated capacity

| Layer | Production limit | Reason |
| --- | ---: | --- |
| Concurrent documents | 2 | Two real papers completed together without memory pressure. |
| Global text/vision model calls | 2 | Two identical 146k-character method prompts completed 2/2; three completed 0/3 within 300 seconds. |
| Section workers per document | 2 | Preserves parallel generation without letting one paper saturate the provider. |
| Feishu writes | 1 | Publishing is short relative to generation and serial writes avoid document races. |
| PDF/visual QA | 1 | Real checks took 11 and 13 seconds; serialization costs little and avoids simultaneous export pressure. |

Do not raise model concurrency because the queue looks long. Scale by adding a
separately measured provider/key or worker host. The current gateway becomes
less reliable before the model context limit is reached.

## Benchmark evidence

### Provider concurrency

The test input was the real 2308.04079 method prompt: 146,409 characters,
including metadata, TeX source, captions, figure markers, tables, and the
method-section contract.

| Concurrent heavy requests | Success | Wall time | Decision |
| ---: | ---: | ---: | --- |
| 1 | 1/1 | 178.6 s | Stable baseline |
| 2 | 2/2 | 252.3 s | Production maximum |
| 3 | 0/3 | 300.0 s timeout | Reject |
| 5 | 0/5 first attempts in production | 502/524 | Reject |

Reasoning remains `medium`. Text verbosity is `low` and Responses storage is
disabled. A real 146k-character method prompt produced 3,694 characters in
178.6 seconds without shortening the evidence package.

### End-to-end papers

| Paper | Fetch/prepare | Generate | Review/quality | Publish | PDF/visual | Total |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 2410.06205 | 11 s | 9 m 32 s | 1 m 13 s | 56 s | 11 s | 12 m 03 s |
| 2308.04079 | 26 s | 14 m 18 s | 1 m 23 s | 1 m 41 s | 13 s | 18 m 02 s |

Generation is about 79% of wall time. Review is not the primary bottleneck.
The last seven days contained 33 completed jobs with a mean duration of about
10 m 57 s; use the recent median rather than a fixed six-minute promise.

## Generation policy

1. A fresh paper uses sectional generation directly. Five logical sections
   exist, but only two model calls may run at once.
2. Each section owns its figure markers and tables. Successful sections remain
   valid when another section encounters a transient gateway error.
3. Transport retries stay inside `OpenAIClient`; a section has one additional
   model repair opportunity (`MAXREAD_GENERATION_REPAIR_ROUNDS=1`).
4. Deterministic repairs run before a second model call. Preambles, duplicated
   H1s, outer fences, and complete-document suffixes do not consume a full
   regeneration when a safe local repair exists.
5. A retry with a durable complete draft gets one whole-draft repair attempt.
   If it fails, the pipeline falls back to sectional generation.
6. Service recovery prefers `02-polished.md` or `01-generated.md`; it never
   mistakes one successful section for the complete paper.

## Review and quality gates

The happy path contains one model review:

1. deterministic editorial validation;
2. one method/source consistency validation at `medium` reasoning;
3. deterministic Markdown and Docx XML quality checks;
4. model repair only for concrete blocking findings;
5. revalidate only the layer changed by repair.

Do not add an unconditional second reviewer. Extra reviewers increase latency
and can introduce formatting regressions. Store every finding and the previous
attempt so a repair prompt receives exact feedback rather than repeating a
generic instruction.

## PDF and visual QA policy

1. Publish a durable checkpoint before PDF inspection.
2. Verify the live Docx XML first; then export the Feishu document to PDF.
3. Persist the Feishu export ticket and poll it for 65 seconds. Never create a
   new export ticket on every inspection attempt.
4. Inspect visible failures, not raw object counts. Formula/image/table counts
   are diagnostics only.
5. Allow two inspect retries. A concrete visual issue can receive at most two
   bounded repairs, each followed by a real PDF recheck.
6. Treat `export-pending`, browser transport, and remote-runner errors as
   infrastructure failures. The queue may retry infrastructure once.
7. An infrastructure retry reloads the latest queue row and resumes the
   published checkpoint. It must not regenerate the paper.
8. If the retry budget is exhausted, retain and return the published document
   URL with an explicit manual-review status. Never discard an already useful
   document because PDF export was slow.

## Production settings

```dotenv
MAXREAD_QUEUE_WORKERS=2
MAXREAD_LLM_CONCURRENCY=2
MAXREAD_SECTIONAL_GENERATION_ENABLED=true
MAXREAD_SECTIONAL_GENERATION_WORKERS=2
MAXREAD_FIGURE_VISION_WORKERS=2
MAXREAD_GENERATION_REPAIR_ROUNDS=1
MAXREAD_QUALITY_REPAIR_ROUNDS=2
MAXREAD_AUTO_RETRY_ATTEMPTS=1
MAXREAD_FEISHU_CONCURRENCY=1
MAXREAD_VISUAL_QA_CONCURRENCY=1
MAXREAD_VISUAL_QA_INSPECT_RETRIES=2
MAXREAD_VISUAL_QA_REPAIR_ROUNDS=2
```

## Operational fallback

- If transient model failures exceed 10% over ten real section calls, reduce
  `MAXREAD_LLM_CONCURRENCY` to 1 and investigate the provider. Do not shorten
  paper evidence as a first response.
- If PDF infrastructure failures repeat, keep model generation running and
  pause only visual delivery. Recheck published checkpoints after Feishu
  recovers.
- If the service restarts, recover silently. Notify the user only when a
  document is delivered or when the bounded terminal policy is reached.
