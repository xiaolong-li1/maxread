# Remote paper worker

MaxRead keeps Aliyun as the only user-facing coordinator and database. Paper
generation runs on ziplab-5090; web articles remain local to Aliyun.

## Ownership boundary

| Component | Aliyun | ziplab-5090 |
| --- | --- | --- |
| Feishu listener and web UI | yes | no |
| Authoritative SQLite database | yes | no |
| Queue claim/finalization | coordinator | remote client |
| Paper source, LLM, rendering and visual QA | no | yes |
| Web article processing | yes | no |
| User notifications | yes | no |

The worker never opens the Aliyun SQLite file. It claims paper jobs through an
authenticated coordinator API, sends heartbeats and workflow transitions, and
returns the final paper record, document URL, review findings, and cleanup
summary. Aliyun atomically commits the terminal state and notifies watchers.

## Transport and security

The coordinator listens only on `127.0.0.1:8765`. A dedicated SSH key on 5090
opens two local forwards:

- `127.0.0.1:18765` -> Aliyun coordinator `127.0.0.1:8765`
- `127.0.0.1:18890` -> Aliyun arXiv proxy `127.0.0.1:17890`

The Aliyun `authorized_keys` entry must use `restrict`, `port-forwarding`, and
two exact `permitopen` options. Worker HTTP requests additionally require the
shared `MAXREAD_WORKER_TOKEN`. Do not expose `/api/worker/*` through Nginx.

## Role configuration

Aliyun:

```dotenv
MAXREAD_QUEUE_SOURCE_KINDS=article
MAXREAD_WORKER_TOKEN=<shared-random-token>
```

5090:

```dotenv
MAXREAD_WORKER_COORDINATOR_URL=http://127.0.0.1:18765
MAXREAD_WORKER_TOKEN=<shared-random-token>
MAXREAD_WORKER_NAME=ziplab-5090
MAXREAD_WORKER_POLL_SECONDS=2
MAXREAD_ARXIV_PROXY_URL=http://127.0.0.1:18890
MAXREAD_ARXIV_PROXY_REQUIRED=true
MAXREAD_LLM_CONCURRENCY=5
MAXREAD_SECTIONAL_GENERATION_WORKERS=5
```

Install `deploy/systemd/maxread-aliyun-tunnel.service` and
`deploy/systemd/maxread-paper-worker.service` as user services. The 5090 user
must have lingering enabled. Only the tunnel and paper-worker units are active;
the old MaxRead listener/admin units stay disabled.

## State flow

1. Aliyun receives a paper request and writes one queue row.
2. 5090 claims only `paper` jobs and Aliyun records the remote worker lease.
3. Each pipeline transition, model-call timing event, and heartbeat is written
   to Aliyun. Max reads this central timeline and reports execution node 5090.
4. The remote `complete` event is informational. The subsequent authenticated
   `finish` request atomically updates the paper, completes the queue row,
   clears the worker lease, and triggers user notification.
5. Source/PDF/render caches are deleted on 5090 after successful delivery;
   pipeline artifacts remain available for retry diagnostics.

If the tunnel or worker dies, the Aliyun stale-heartbeat recovery returns the
same job to the queue. A published checkpoint resumes visual QA rather than
regenerating the document.

## Rollback

1. Stop `maxread-paper-worker.service` on 5090.
2. Wait for or recover any remote leased paper job.
3. Set Aliyun `MAXREAD_QUEUE_SOURCE_KINDS=paper,article`.
4. Restart `maxread.service` on Aliyun.

No database copy or merge is required because Aliyun remains authoritative.
