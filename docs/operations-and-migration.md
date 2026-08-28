# MaxRead 运维与迁移说明

这份文档描述当前 `读不动了 / MaxRead` 的实际运行边界。仓库只保存代码、示例配置和部署脚本；真实密钥、飞书认证状态、SQLite 数据库和论文产物属于机器状态，不进入 Git。

## 当前交付边界

迁移后的建议布局：

```text
/opt/maxread/                 主程序代码、虚拟环境和主 SQLite 库
/opt/maxread/.env             主程序运行配置，权限 600
/opt/maxread/var/maxread/     论文 source、图片、模型输出和 pipeline_artifacts
/opt/maxread/features/mail_ingestion/data/  邮箱凭据、原始邮件、附件和招聘 SQLite（不提交）
/opt/maxread-duty/            独立值班提醒，不属于论文阅读队列
/root/.lark-cli/              lark-cli 的机器人认证配置，不提交、不公开
```

5090 不再是新部署的前置依赖。新机器不应指向 `ziplab-5090` 做视觉审阅，也不应同时运行第二个同一飞书应用的消息监听器。旧目录 `/opt/maxread-stage-20260821` 只作为历史备份，迁移完成并核对数据前不要删除。

值班提醒是独立程序：它使用 `/opt/maxread-duty/duty-reminder.json` 和自己的 SQLite 状态库，每天北京时间 07:00 向固定群发送一次。迁移主程序时不要复制、重建或启动第二份值班服务。

## 模型与 token

当前本机 `.env` 的有效模型配置是：

| 项目 | 当前值/形式 | 是否秘密 |
| --- | --- | --- |
| 主模型 | 部署当前为 `gpt-5.6-sol`；以私有 `.env` 为准 | 否 |
| API 模式 | `responses`（未配置时默认值） | 否 |
| OpenAI-compatible Base URL | 部署私有配置，不写入仓库 | 是/部署信息 |
| `OPENAI_API_KEY` | 已配置的网关 API key | 是，禁止打印和提交 |
| `OPENAI_SUB_MODULE` | `codex-internal` | 否/部署标识 |
| 视觉模型 | 默认复用主模型；只有配置 `MAXREAD_VISUAL_*` 才独立 | 否 |
| 飞书身份 | `MAXREAD_FEISHU_AS=bot` | 否 |

运行所需的敏感或半敏感信息：

| 信息 | 放在哪里 | 用途 |
| --- | --- | --- |
| `OPENAI_API_KEY` | 目标机 `.env` 或 secret manager，权限 600 | 生成、审阅和可选视觉修复 |
| 飞书应用 `app_id/app_secret` | `lark-cli` 配置/认证存储 | 接收消息、写文档、上传图片、发送回复 |
| `MAXREAD_DUTY_CHAT_ID` 或值班 JSON 中的 `chat_id` | 目标机私有配置，权限 600 | 固定群值班提醒 |
| `MAXREAD_GITHUB_TOKEN` | 仅 bootstrap/拉代码时临时使用 | 私有仓库访问；不是运行时 token |
| ZeroTier、SSH 私钥 | 仅远程维护机本地 | 迁移或远程浏览器访问；不属于 MaxRead 运行时 |

`open_id`、群 ID、文档 token 会被记录在业务数据库或运行日志中。它们不等同于模型 API key，但仍应视为内部数据，不应放入公开仓库。不要把 `deploy_key`、`.env`、`~/.lark-cli`、OAuth token、值班 JSON 或数据库上传到 GitHub。

在阿里云上复用已经配置好的机器人身份时，先执行：

```bash
/usr/local/bin/lark-cli doctor
/usr/local/bin/lark-cli whoami
```

输出只用于确认状态，不要把完整配置或 token 贴到聊天中。若是全新机器，使用 `lark-cli config init --new` 安全录入应用密钥，再执行 `lark-cli doctor`；主程序保持 `MAXREAD_FEISHU_AS=bot`，不要为了机器人功能登录个人用户。

## Python 环境

已核对的目标机环境：Ubuntu 24.04，Python 3.12.3。项目声明 `Python >=3.9`，建议生产统一用 3.11 或 3.12 的虚拟环境：

```bash
python3 -m venv /opt/maxread/.venv
/opt/maxread/.venv/bin/python -m pip install --upgrade pip wheel
/opt/maxread/.venv/bin/pip install -e /opt/maxread
```

`pyproject.toml` 的直接依赖：

- `Pillow>=10`：图片规范化、裁剪和上传前处理
- `PyMuPDF>=1.24`：PDF/图片辅助处理
- `pypdf>=5`：没有 `pdftotext` 时的 PDF 文本 fallback

源码优先策略是 `MAXREAD_REQUIRE_SOURCE=true`：正常论文流程优先读取 arXiv TeX source，PDF 文本抽取不是主路径；只有 source 不可用或其他输入需要时才使用 PDF fallback。

## 其他软件

必需：

- `lark-cli`，目标机当前路径为 `/usr/local/bin/lark-cli`
- 能访问模型网关、飞书 API 和允许的 arXiv/中转地址
- `systemd`（生产守护进程推荐）或手动运行脚本

推荐/按功能启用：

- `pdftotext`（Poppler）：更好的 PDF 文本 fallback；没有它仍可使用 `pypdf`
- Node.js/npm：网页文章的 Playwright 渲染路径
- Chromium/Playwright：网页抓取和真实飞书页面视觉审阅
- Ghostscript：部分图片/PDF 格式的可选辅助工具

新部署默认关闭视觉审阅。生产优先使用飞书服务端 Docx → PDF 导出，要求
`drive:export:readonly`、`docs:document:export`、Poppler 和一次真实文档 smoke test；
Playwright/Chromium 仅作备用网页渲染路径：

```text
MAXREAD_VISUAL_QA_ENABLED=false
```

安装并验证同机 runner 后，才改为 `true`。视觉审阅是发布后的附加门，不应通过指向旧机器来“假装可用”；浏览器基础设施失败应被标记为 visual QA infrastructure error，而不是把模型生成失败。

阿里云安装命令：

```bash
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y \
  poppler-utils ghostscript fonts-noto-cjk fonts-noto-color-emoji
/opt/maxread/.venv/bin/pip install -e '/opt/maxread[browser]'
/opt/maxread/.venv/bin/python -m playwright install-deps chromium
/opt/maxread/.venv/bin/python -m playwright install chromium
npm install --prefix /opt/maxread/var/maxread/playwright-deps playwright
```

`deploy/visual_qa/run_visual_qa.sh` 通过
`MAXREAD_VISUAL_QA_PYTHON` 指定虚拟环境，通过
`PLAYWRIGHT_BROWSERS_PATH` 复用浏览器缓存；`MAXREAD_PLAYWRIGHT_NODE_MODULES`
则供网页文章的 Node 渲染路径使用。

## 推荐配置

下面是适合新机器的保守起点，真实 key 不要填在仓库文件里：

```dotenv
OPENAI_API_KEY=<gateway-api-key>
OPENAI_BASE_URL=<openai-compatible-base-url>
OPENAI_SUB_MODULE=<optional-routing-label>
MAXREAD_OPENAI_API_MODE=responses
MAXREAD_MODEL=gpt-5.6-sol

MAXREAD_FEISHU_AS=bot
MAXREAD_LARK_CLI=/usr/local/bin/lark-cli
MAXREAD_REQUIRE_SOURCE=true
MAXREAD_ARXIV_PARALLEL_STREAMS=2
MAXREAD_QUEUE_WORKERS=2
MAXREAD_LLM_CONCURRENCY=1
MAXREAD_FEISHU_CONCURRENCY=5
MAXREAD_GENERATION_REPAIR_ROUNDS=2
MAXREAD_QUALITY_REPAIR_ROUNDS=2
MAXREAD_AUTO_RETRY_ATTEMPTS=1
MAXREAD_VISUAL_QA_ENABLED=true
MAXREAD_VISUAL_QA_EXPORT_PDF=true
```

`MAXREAD_ARXIV_PARALLEL_STREAMS=2` 只用于大包的有限 Range 下载；不要在没有测量和限流保护时继续扩大。`MAXREAD_SECTIONAL_GENERATION_ENABLED`、图片识别 worker 和质量块并行可以按 API 配额逐步打开。

## 数据库格式

主库是 SQLite，路径由 `MAXREAD_DB` 决定，默认是仓库根目录的 `maxread.sqlite3`。表结构和字段用途见 [`database-schema.md`](database-schema.md)。数据库保存：

- 论文、网页文章和已发布文档索引
- 队列任务、worker lease、状态机版本、checkpoint 和事件审计
- 使用记录、用户身份缓存、反馈和质量问题
- 值班模块的配置/提醒状态（若使用内置 duty 模块）

论文文件和模型输出不塞进 SQLite，而是在 `MAXREAD_WORKDIR` 下保存，典型路径是：

```text
var/maxread/papers/<paper-id>/
  <paper-id>.source
  source/
  rendered_figures/
  pipeline_artifacts/
var/maxread/articles/<article-id>/
```

`pipeline_artifacts` 是失败恢复的依据，包含生成草稿、Markdown/XML、质量报告、视觉 QA 结果、截图引用和模型回复。清缓存前先确认没有未完成任务，也不要只迁 SQLite 而丢掉对应的 `pipeline_artifacts`。

成功交付后的 PDF、TeX source、解压源码树和渲染图片属于可重建缓存：worker
完成任务后立即删除，`maxread-cache-cleanup.timer` 每天兜底清理一次；
`pipeline_artifacts` 保留。内容版本升级时使用 `invalidate-cache` 将旧的 `done`
记录改为 `legacy`，后续同一来源会重新生成，不命中旧文档。

### 数据迁移原则

1. 先确认旧监听器已经停止，避免复制过程中 SQLite 仍在写入。
2. 先在目标机做带时间戳的数据库和工作目录备份。
3. 用 SQLite backup 或文件复制迁移主库，再让新代码启动一次自动补齐 schema。
4. 迁移后检查表数量、队列状态和 `pipeline_artifacts` 是否存在。
5. 旧机和新机不能同时消费同一飞书应用的 `im.message.receive_v1` 事件。

示例命令（路径按实际机器调整）：

```bash
sqlite3 /path/to/maxread.sqlite3 ".backup '/path/to/maxread.sqlite3.backup'"
rsync -a --exclude='.env' /path/to/var/maxread/ /opt/maxread/var/maxread/
```

迁移时以 SQLite 逐表计数和抽样校验作为基线，不在文档中硬编码会过期的生产记录数量。

## 服务与切换

主程序分为两个进程：

- `maxread.service`：唯一飞书事件监听器和后台队列 worker
- `maxread-admin.service`：只读/运维控制台，不参与论文消费

值班提醒 `maxread-duty-reminder.service` 独立于以上两个服务。安装脚本现在只安装它的 unit，不默认启用或发送消息；只有明确配置值班服务时才手动启用。

首次切换建议保持服务停止，先做健康检查：

```bash
cd /opt/maxread
./.venv/bin/python -m maxread.cli --help
/usr/local/bin/lark-cli doctor
```

确认无误后再显式启动主服务：

```bash
# 首次安装 unit（root/systemd 模式）
sed -e 's#__INSTALL_DIR__#/opt/maxread#g' \
    -e 's#__SERVICE_USER__#root#g' \
    -e 's#__SERVICE_GROUP__#root#g' \
    -e 's#__SERVICE_HOME__#/root#g' \
    deploy/systemd/maxread-system.service > /etc/systemd/system/maxread.service
sed -e 's#__INSTALL_DIR__#/opt/maxread#g' \
    -e 's#__SERVICE_USER__#root#g' \
    -e 's#__SERVICE_GROUP__#root#g' \
    -e 's#__SERVICE_HOME__#/root#g' \
    deploy/systemd/maxread-admin-system.service > /etc/systemd/system/maxread-admin.service
systemctl daemon-reload

# 通过独立的切换窗口启动，下面一条才会真正拉起服务
systemctl enable --now maxread.service maxread-admin.service
```

切换时只保留一个 listener。若新服务未验证，不要停止仍在承载用户流量的旧服务；先旁路安装、检查数据库和依赖，最后安排一个明确的停旧/启新的窗口。

## 故障定位顺序

1. `systemctl status` 和 `journalctl`：确认进程是否存活、是否反复重启。
2. 管理台任务日志：查看 `workflow_state`、`stage`、`last_event` 和心跳时间。
3. `pipeline_artifacts`：区分模型输出问题、格式编译问题、飞书写入问题和视觉 runner 问题。
4. arXiv 下载：检查 DNS、连接重置、429/5xx 和 source archive 是否为空；不要立即扩大并发。
5. SQLite：若出现跨线程错误，确保每个 worker 使用自己的连接，不共享 `sqlite3.Connection`。

可恢复的模型/网络/浏览器基础设施错误默认自动重放一次；确定性 source/格式问题走受控 repair loop，达到预算后进入可重试失败，不自动无限循环。重试必须读取同一任务的历史草稿和检查结果，不能把话题中任意 URL 当作论文 ID。
