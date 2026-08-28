# Dedicated arXiv egress

## Purpose

The production Aliyun host can reach ordinary Internet services normally, but
its direct path to arXiv's Fastly edge has exhibited TLS resets and very low
throughput. Do not change the host default route or configure process-wide
`HTTP_PROXY`/`HTTPS_PROXY`: Feishu, OpenAI, SSH, package management, and other
services must stay on the normal route.

MaxRead instead supports an application-scoped proxy:

```dotenv
MAXREAD_ARXIV_PROXY_URL=http://127.0.0.1:17890
MAXREAD_ARXIV_PROXY_REQUIRED=true
```

Only `ArxivClient` reads these variables. Metadata, source, PDF, and Range
requests use the dedicated opener; all other MaxRead network clients remain
direct.

## Production topology

```text
Aliyun MaxRead ArxivClient
  -> 127.0.0.1:17890
  -> reverse SSH listener (loopback only)
  -> egress host 127.0.0.1:17890
  -> Mihomo stable-node pool
  -> arXiv / Fastly
```

The egress host runs two linger-enabled user services:

- `maxread-egress.service`: Mihomo bound to loopback only.
- `maxread-egress-tunnel.service`: reverse SSH tunnel with keepalives and
  automatic restart.

The Aliyun tunnel account has no password or application permissions. Its
authorized key is restricted to remote forwarding and
`permitlisten="127.0.0.1:17890"`. `GatewayPorts` remains disabled, so the
proxy is never exposed publicly.

Real proxy definitions, controller secrets, and SSH private keys are runtime
secrets and must not be committed. Start from
`deploy/arxiv-egress/config.example.yaml` and
`deploy/arxiv-egress/tunnel.env.example`.

## Acceptance test

Test from Aliyun without touching the default route:

```bash
curl --proxy http://127.0.0.1:17890 -L --max-time 60 -o /dev/null \
  -w 'code=%{http_code} bytes=%{size_download} speed=%{speed_download} total=%{time_total}\n' \
  https://arxiv.org/e-print/2608.25927
```

The 2026-08-29 production validation downloaded six uncached source/PDF files
in one pass at 4.34-7.51 MB/s. A complete metadata + source + extraction smoke
test finished in 4.13 seconds. The previous direct route was approximately
15 KB/s and intermittently reset TLS.

Select nodes by sustained multi-megabyte downloads, not latency alone. The
production pool contains only nodes that passed both Range and full-file
tests; a low-latency node that failed TLS was removed.

## Operations

On the egress host:

```bash
systemctl --user status maxread-egress.service maxread-egress-tunnel.service
journalctl --user -u maxread-egress.service -u maxread-egress-tunnel.service -n 100
```

On Aliyun:

```bash
ss -lntp | grep 127.0.0.1:17890
systemctl status maxread.service
```

When the egress or tunnel is unavailable and
`MAXREAD_ARXIV_PROXY_REQUIRED=true`, arXiv acquisition fails quickly instead
of waiting on the broken direct route. The durable queue can then retry after
the sidecar recovers. Existing generated documents and non-arXiv services are
unaffected.
