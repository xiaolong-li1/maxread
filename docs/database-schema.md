# MaxRead SQLite Schema

MaxRead 使用单个 SQLite 文件，由 `maxread/db.py` 在启动时创建和增量补齐字段。当前 schema 没有独立迁移编号；新版本通过 `CREATE TABLE IF NOT EXISTS` 和 `_ensure_column()` 向旧库兼容升级。升级前仍建议做备份。

## 核心业务表

### `papers`

论文源资料和最终文档索引。主键是 `paper_id`（规范化后的 arXiv ID）。

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `paper_id` | TEXT PK | arXiv ID |
| `title`, `authors` | TEXT | 元数据 |
| `project_summary` | TEXT | MaxRead 生成的一句话定位；项目自动分类输入 |
| `arxiv_url` | TEXT | 原文入口 |
| `pdf_path`, `source_path` | TEXT | 本地下载路径 |
| `status` | TEXT | 资料/处理状态 |
| `doc_url`, `doc_token` | TEXT | 已发布飞书文档 |
| `error` | TEXT | 最近错误 |
| `created_at`, `updated_at` | DATETIME | 时间戳 |

### `documents`

网页文章或其他非论文输入的文档索引。`kind` 区分来源类型，字段结构与 `papers` 的发布索引相似。

### `queue_jobs`

全局队列的权威任务表。一个来源可以有多次尝试，但 `dedupe_key` 用于防止同一来源被意外重复入队。

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `id` | INTEGER PK | 队列任务 ID |
| `dedupe_key` | TEXT | 来源去重键 |
| `source_kind`, `source_id`, `source_url` | TEXT | 论文/文章来源 |
| `status` | TEXT | 兼容旧状态字段 |
| `workflow_state`, `state_version` | TEXT/INTEGER | durable state machine 状态和版本 |
| `stage`, `stage_updated_at` | TEXT/DATETIME | UI/进度阶段 |
| `priority`, `attempts` | INTEGER | 调度优先级和 worker 尝试次数 |
| `worker_id`, `heartbeat_at` | TEXT/DATETIME | worker lease |
| `started_at`, `finished_at` | DATETIME | 执行时间 |
| `title`, `doc_url`, `error` | TEXT | 展示和结果 |
| `checkpoint_json` | TEXT | 发布 checkpoint 与恢复上下文 |
| `last_event` | TEXT | 最近一次状态事件 |
| `suppress_progress_notifications` | INTEGER | 是否静默进度反应 |
| `recovery_reason`, `recovery_attempts` | TEXT/INTEGER | 故障恢复原因和次数 |
| `rebuild_pipeline` | INTEGER | 是否从历史资料重建 pipeline |

### `job_watchers`

把一次或多次飞书请求绑定到队列任务。重试命令通过 `chat_id`、消息关系和 `sender_id` 找到任务，而不是解析失败消息中残留的 URL。

### `job_events`

状态机审计日志：`job_id`、`event_type`、`detail` 和创建时间。它是排查“为什么只尝试了一次”“重启后是否继续”的第一依据。

### `jobs`

早期版本的 `(event_id, paper_id)` 去重表，保留用于兼容历史数据。新队列逻辑主要使用 `queue_jobs` 和 `job_watchers`。

## 质量、反馈和使用记录

### `usage_events`

管理台的使用记录：请求事件、会话、发送者、来源、标题、状态、文档 URL、错误和时间戳。`sender_id` 是 open_id 时会通过身份缓存显示姓名。

### `feedback`

用户反馈原文和 AI 分类结果：`content`、`feedback_source`、`feedback_category`、`feedback_confidence`、状态和消息上下文。

### `review_issues`

模型/规则/视觉审查发现的问题，包含 `source_kind`、`source_id`、`category`、`severity`、`detail` 和时间戳。

## 用户、服务和值班表

### `users` 与 `user_identity_cache`

`users` 保存首次介绍消息状态；`user_identity_cache` 保存 `sender_id -> display_name` 映射，避免管理台始终显示神秘 open_id。

### `web_identities` 与 `web_project_preferences`

`web_identities` 保存浏览器会话哈希和可选的飞书 `open_id` 绑定；原始
HttpOnly Cookie 不入库。`web_project_preferences` 以 `owner_key + source_id`
为主键，保存个人收藏、人工分类和软删除时间。自动分类不写死在表里，
它由标题和 `papers.project_summary` 计算；人工分类优先。

### `service_status`

单行服务状态表，固定 `id=1`：

```text
mode                  operational | degraded | maintenance | outage
reason                非正常状态时的原因
expected_recovery_at  可选的预计恢复时间
updated_by            操作人
updated_at            更新时间
```

它用于故障公告和新请求提示，不会替代 systemd 的真实存活状态。

### `duty_roster`

内置值班轮转名单：`ordinal`、`name`、`user_id`、`enabled` 和时间戳。

### `duty_settings`

值班模块的键值配置。

### `duty_reminders`

按 `reminder_date` 唯一记录每天发送状态，包含 `roster_id`、成员、`status`、`message_id`、错误和尝试次数。它保证服务重启不会重复发送已经成功的日期。

## 索引与一致性

主要索引包括：

- `usage_events(sender_id, created_at)`、`usage_events(source_kind, source_id)`
- `queue_jobs(status, priority, id)`、`queue_jobs(dedupe_key, status)`
- `queue_jobs(status, heartbeat_at)`、`queue_jobs(status, worker_id)`
- `job_watchers(job_id, notified)`、`job_events(job_id, id)`
- `review_issues(source_kind, source_id, id)`、`review_issues(category, severity)`
- 值班名单和提醒的启用/日期索引

SQLite 连接默认超时 30 秒。连接对象不应跨线程共享；每个 worker 或请求处理上下文应创建自己的 `Store`/连接。若升级后要确认 schema，可以执行：

```bash
sqlite3 /path/to/maxread.sqlite3 ".tables"
sqlite3 /path/to/maxread.sqlite3 "pragma table_info(queue_jobs);"
sqlite3 /path/to/maxread.sqlite3 "select status, count(*) from queue_jobs group by status;"
```

论文文件、截图和模型输出不在数据库中，位于 `MAXREAD_WORKDIR`；迁移只复制 SQLite 会导致历史任务能看到，但无法从失败 checkpoint 恢复。
