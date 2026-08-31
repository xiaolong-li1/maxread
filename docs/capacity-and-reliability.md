# MaxRead capacity and reliability profile

Validated through 2026-08-30 against `gpt-5.6-sol` with medium reasoning through
the `sub2api-hk.ziplab.co/v1` Responses endpoint. Paper computation runs on
ziplab-5090; Aliyun remains the coordinator and database owner. These limits
are an operational contract, not theoretical model limits.

## Validated capacity

| Layer | Production limit | Reason |
| --- | ---: | --- |
| Concurrent documents | 2 | Two real papers completed together without memory pressure. |
| Global text/vision model calls | 10 | Ten real 123k-character requests completed 10/10 in 96.3 seconds. Twelve added no meaningful throughput; sixteen hit the account concurrency limit. |
| Section workers per document | 5 | All five logical sections may start together; two paper workers share one ten-call process semaphore. |
| Feishu writes | 1 | Publishing is short relative to generation and serial writes avoid document races. |
| PDF/visual QA | 1 | Real checks took 11 and 13 seconds; serialization costs little and avoids simultaneous export pressure. |

Keep production at ten even though twelve succeeded in one boundary run. The
two spare account slots absorb retries and occasional Aliyun article calls.
Raising the worker to sixteen is known-bad and produces provider rate limits.

## Benchmark evidence

### Provider concurrency

The first test input was the real 2308.04079 method prompt: 146,409 characters,
including metadata, TeX source, captions, figure markers, tables, and the
method-section contract. It captured a degraded gateway period before the host
restart and is retained as failure history, not as the current limit.

| Concurrent heavy requests | Success | Wall time | Decision |
| ---: | ---: | ---: | --- |
| 1 | 1/1 | 178.6 s | Historical baseline |
| 2 | 2/2 | 252.3 s | Historical stable run |
| 3 | 0/3 | 300.0 s timeout | Degraded gateway snapshot |
| 5 | 0/5 first attempts in production | 502/524 | Degraded gateway snapshot |

The 2026-08-30 post-restart recheck used the real Rope3D method prompt
(123,597 characters, including 90,013 source characters) and medium reasoning:

| Concurrent heavy requests | Success | Wall time | Median | Maximum | Decision |
| ---: | ---: | ---: | ---: | ---: | --- |
| 5 | 5/5 | 198.0 s | 54.6 s | 198.0 s | Stable, one long tail |
| 8 | 8/8 | 94.8 s | 74.1 s | 94.5 s | Stable |
| 10 | 10/10 | 96.3 s | 68.5 s | 96.2 s | Production setting |
| 12 | 12/12 | 115.2 s | 63.6 s | 114.9 s | Stable boundary, no throughput gain |
| 16 | 13/16 | 228.8 s | 85.1 s | 228.2 s | Reject: three account concurrency limits |

Ten and twelve have nearly identical completed-call throughput. Ten has a
shorter tail and leaves capacity for a retry or a coordinator-side article, so
it is the durable setting rather than the largest one-time success.

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

1. A fresh paper uses sectional generation directly. Its five logical sections
   may run together. Two remote paper slots share a global ten-call ceiling.
2. Each section owns its figure markers and tables. Successful sections remain
   valid when another section encounters a transient gateway error.
3. Figure ownership is immutable. The source parser derives `owner_section`
   from the first body `label/ref` context or the nearest recognized TeX
   section. Semantic grouping is allowed only when both owners are known and
   identical; the composite inherits that owner. Unknown or cross-section
   figures remain separate, and the publication compiler moves an escaped
   marker/caption block back to its owner before quality review.
4. Transport retries stay inside `OpenAIClient`; a section has one additional
   model repair opportunity (`MAXREAD_GENERATION_REPAIR_ROUNDS=1`).
5. Deterministic repairs run before a second model call. Preambles, duplicated
   H1s, outer fences, and complete-document suffixes do not consume a full
   regeneration when a safe local repair exists.
6. A retry with a durable complete draft gets one whole-draft repair attempt.
   If it fails, the pipeline falls back to sectional generation.
7. Service recovery prefers `02-polished.md` or `01-generated.md`; it never
   mistakes one successful section for the complete paper.

## Review and quality gates

The happy path contains one model review:

1. deterministic editorial validation;
2. one method/source consistency validation at `medium` reasoning;
3. deterministic Markdown and Docx XML quality checks;
4. model repair only for concrete blocking findings;
5. revalidate only the layer changed by repair.

Review and repair calls use a separate 240-second request deadline. A method
repair receives and returns section 3 only; the deterministic pipeline merges
it back into the complete draft. Long generation calls keep their independent
deadline.

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
6. Formula/raw-format findings are repaired as scoped XML patches, never by
   regenerating a section. The multimodal repair call receives the failing
   screenshot, current Feishu Docx XML, allowed block XML and SHA-256 hashes.
   A patch must name an allowed block, match its current hash, and replace one
   exact formula/text fragment. Unrelated XML is preserved by code.
7. A visual finding without a block ID may use deterministic repair only when
   the fresh document has exactly one repairable candidate. Ambiguous cases go
   through screenshot + XML patch selection; do not scan-and-edit the first
   suspicious block.
8. Treat `export-pending`, browser transport, and remote-runner errors as
   infrastructure failures. The queue may retry infrastructure once.
9. An infrastructure retry reloads the latest queue row and resumes the
   published checkpoint. It must not regenerate the paper.
10. If the retry budget is exhausted, retain and return the published document
   URL with an explicit manual-review status. Never discard an already useful
   document because PDF export was slow.

## Production settings

```dotenv
MAXREAD_QUEUE_WORKERS=2
MAXREAD_LLM_CONCURRENCY=10
MAXREAD_OPENAI_REVIEW_TIMEOUT=240
MAXREAD_SECTIONAL_GENERATION_ENABLED=true
MAXREAD_SECTIONAL_GENERATION_WORKERS=5
MAXREAD_FIGURE_VISION_WORKERS=2
MAXREAD_GENERATION_REPAIR_ROUNDS=1
MAXREAD_QUALITY_REPAIR_ROUNDS=2
MAXREAD_AUTO_RETRY_ATTEMPTS=1
MAXREAD_FEISHU_CONCURRENCY=1
MAXREAD_VISUAL_QA_CONCURRENCY=1
MAXREAD_VISUAL_QA_INSPECT_RETRIES=2
MAXREAD_VISUAL_QA_REPAIR_ROUNDS=2
MAXREAD_MAX_IMAGE_DISPLAY_HEIGHT=560
```

## Operational fallback

- If transient model failures exceed 10% over ten real section calls, first
  reduce `MAXREAD_LLM_CONCURRENCY` to 5 and investigate the provider. Use 1 only
  for isolation. Do not shorten paper evidence as a first response.
- If PDF infrastructure failures repeat, keep model generation running and
  pause only visual delivery. Recheck published checkpoints after Feishu
  recovers.
- If the service restarts, recover silently. Notify the user only when a
  document is delivered or when the bounded terminal policy is reached.
