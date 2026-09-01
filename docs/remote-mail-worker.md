# Remote recruiting mail worker

Aliyun is the user-facing control plane. Recruiting mail computation runs on
ziplab-5090 and is exposed to Aliyun only through an authenticated localhost
reverse tunnel.

## Ownership

| Component | Aliyun | ziplab-5090 |
| --- | --- | --- |
| Mail admin page and authentication | yes | no |
| Scan/config command forwarding | yes | worker endpoint |
| IMAP, attachment parsing and model extraction | no | yes |
| Feishu Docs/Base writes | no | yes |
| Weekly report timer | disabled | enabled |
| Mail SQLite | no | `/var/tmp/maxread-mail` |
| Transient mail artifacts | no | `/mnt/data/lixiaolong/maxread/mail-v2` |

SQLite stays on local ext4; it must not run on NFS. Raw mail and attachments
may live on the cloud mount. After Docs and Base writes both succeed, every
physical copy sharing that Message-ID is marked processed, the RFC822 file is
compacted to headers, and body/attachment/external-download files are removed.
Failed messages retain their complete artifacts for retry.

## Transport

`maxread-aliyun-tunnel.service` provides:

- local `18765` -> Aliyun MaxRead coordinator `8765`
- local `18890` -> Aliyun arXiv relay `17890`
- Aliyun localhost `18766` -> 5090 mail control `18766`

Both hosts use the existing `MAXREAD_WORKER_TOKEN` as
`MAXREAD_MAIL_REMOTE_TOKEN`. Aliyun sets
`MAXREAD_MAIL_REMOTE_URL=http://127.0.0.1:18766`. The reverse port is never
publicly exposed.

## Services on 5090

- `maxread-mail-control.service`
- `recruiting-pipeline.service`
- `recruiting-weekly-report.timer`

The weekly timer uses `Persistent=false`, so enabling it outside the scheduled
Monday 07:00 window never sends a catch-up report.

## Rollback

1. Disable the three 5090 units.
2. Remove `MAXREAD_MAIL_REMOTE_URL` from Aliyun.
3. Restore the mail SQLite backup and account runtime on Aliyun.
4. Enable only Aliyun `recruiting-pipeline.service` and weekly timer.
