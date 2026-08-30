# 招聘邮箱本地采集器

第一阶段只做只读采集：增量扫描邮箱、保存原始邮件和正文、提取附件、SQLite 去重，并给候选来信打一个可解释的预筛分数。只读是硬性代码策略：它不会修改已读状态、移动或删除邮件，也不会回复、转发或发送消息。

所有以下命令都在 `maxread/features/mail_ingestion/` 目录执行。

## 当前结论

当前飞书用户身份已经拥有邮箱 API scope，但飞书接口返回：

- `primary_email_address` 为空；
- `accessible_mailboxes` 为空。

因此本机第一版使用标准 IMAP 接外部组邮箱。Outlook.com 使用 `outlook.office365.com:993`、SSL/TLS，并要求 OAuth2/Modern Auth；需要先在 Outlook 设置中开启 IMAP。

普通账号密码会被服务器以 `Basic authentication is disabled` 拒绝。默认复用 `better-email-mcp` 提供的公共客户端 ID，通过设备码申请只读 IMAP scope；不需要自行注册 Azure 应用：

```bash
./bin/mail-collector outlook-auth
```

命令会展示微软设备码登录地址与验证码；授权完成后，token 缓存写入 `data/secrets/outlook-token.json`，权限为 `600`，并自动清空 `.env` 中原先的密码字段。若公共客户端以后被撤销、达到配额或不再受信任，可用 `--client-id` 切换到自有注册。

## 本地离线验收

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
./bin/mail-collector init
./bin/mail-collector import-eml /path/to/sample.eml
./bin/mail-collector status
```

本地数据默认写入：

```text
data/
├── mail_collector.sqlite3
└── messages/<uid>/
    ├── message.eml
    ├── body.txt
    └── 01-resume.pdf
```

## 接真实邮箱

不要把密码或令牌粘贴到聊天窗口。推荐运行静默输入命令：

```bash
./bin/mail-collector configure --username your-group-mailbox@outlook.com --auth password
```

密码输入不会回显；生成的 `.env` 权限为 `600`。Outlook.com 不接受这种基础密码连接，因此该模式仅适合仍允许密码认证的其他 IMAP 服务；Outlook 请使用上面的 `outlook-auth`。

也可以复制配置模板后自行填写：

```bash
cp .env.example .env
chmod 600 .env
```

Outlook 推荐配置：

```dotenv
IMAP_HOST=outlook.office365.com
IMAP_PORT=993
IMAP_USERNAME=your-group-mailbox@outlook.com
IMAP_MAILBOX=INBOX
IMAP_AUTH=oauth2
IMAP_OAUTH2_ACCESS_TOKEN=replace-with-a-short-lived-access-token
```

其他仍允许密码登录的 IMAP 服务可以设置：

```dotenv
IMAP_AUTH=password
IMAP_PASSWORD=app-password-or-mailbox-password
```

先执行一次不落盘检查：

```bash
./bin/mail-collector scan --limit 5 --dry-run
```

确认连接和解析正常后，再执行增量落盘：

```bash
./bin/mail-collector scan --limit 100
./bin/mail-collector status
```

## 安全约束

- `.env` 和 `data/` 已加入 `.gitignore`；不要把邮箱令牌、密码或候选人简历提交到 Git。
- 邮件正文和 PDF 都是不可信输入，仅作为数据解析，不作为程序指令。
- IMAP 使用 `readonly=True` 和 `BODY.PEEK[]`，不会把邮件标记为已读。
- `MAIL_READ_ONLY` 必须为 `1`；任何其他值都会拒绝启动。
- 当前 CLI 没有任何邮箱写操作入口；OAuth 只写本机 token 缓存，不写邮箱。
- 附件默认最大 25 MB，超限附件只记录元数据和跳过原因。
- 邮件采集器本身不调用大模型；增量招聘管线只把本地 Evidence Pack 交给配置的抽取模型。

## 候选人字段约定（飞书多维表格）

为了避免把“2027”误当成入学年份或毕业年份，候选表使用统一的展示格式：

- `院校 / 就读信息`：`院校｜学历阶段｜就读信息`。
  - 明确毕业年份写 `预计毕业 2027`；明确入学年份写 `入学 2024`。
  - 原文直接写“大二”等相对年级时，保留为 `大二`；若同时明确预计毕业年份，则并列为 `大二｜预计毕业 2027`。
  - 只有裸年份而没有语义时写 `年份未标注：2027`，不擅自推断。
- `学业表现`：只保留 AI 从完整邮件和附件中读出的均分、百分制成绩与 GPA。
- `排名`：由 AI 独立提取绝对名次或 Top 百分位；没有证据写 `未提供`。`排名依据` 保存最短原文片段，代码不使用正则把分数、GPA 或竞赛比例改写成排名。
- `申请目的 / 科研摘要`：候选邮件用短段落换行，按需包含 `申请目的：`、`科研经历：`、`论文/发表：`、`奖项/竞赛：`；论文写会议/状态，奖学金和竞赛合并归纳。其他邮件只写一句描述。
- `最新邮件时间`：同一邮件线程去重后，记录该候选人最新一封来信的接收时间，默认按降序排列。
- `筛选状态`：只保留 `未筛选 → 面试资格 → 面试通过 → 实习生`，任一步可转 `未通过`；不再维护“初筛中”和“面试中”。“是否已分配面试”单独用复选框表示。

## 增量招聘管线

扫描与整理由 `bin/recruiting-pipeline` 驱动，邮箱仍然只读；可配置每天或每 N 天运行：

```bash
./bin/recruiting-pipeline scan-once
./bin/recruiting-pipeline run --interval-days 1
./bin/recruiting-pipeline status
```

管理员可在 `/maxread/mail` 查看两个邮箱的最近扫描、运行统计和错误聚合，
并手动触发单邮箱或全部邮箱扫描。页面可调整完整扫描与候选发布间隔，
以及招聘周报发布间隔；写操作复用 MaxRead 管理员会话，不返回邮箱凭据。
手动扫描由 `bin/recruiting-control-scan` 在独立 systemd transient unit 中执行，
运行期间暂停常驻管线，避免并发写同一个 SQLite。

如果 Base 表被清空或更换，不要删除邮件、材料文档或整个 SQLite。先预览，
再显式确认回填最近 30 天候选人：

```bash
./bin/recruiting-pipeline backfill-base --days 30 --dry-run
./bin/recruiting-pipeline backfill-base --days 30 --confirm
```

回填只清除窗口内候选人的旧 Base record 映射，保留邮件 UID 水位、材料文档、
附件上传摘要和人工流程字段；默认复用已经审核过的本地结构化字段，不重新发送
整月邮件/PDF 给模型，也不修改材料文档。只有明确需要重新抽取或修复文档时才分别
增加 `--refresh-ai`、`--refresh-docs`。中途失败的线程保持 pending，下一轮继续。常驻服务
每轮成功后刷新“最近一周新增”视图的 7 天滚动边界。

5090 的持久化服务模板位于 `deploy/recruiting-pipeline.service`。将 `RECRUITING_SCAN_INTERVAL_DAYS`、Base token/table ID、模型 API 基址和材料文档父文件夹写入机器上的非 Git 配置后，再用 `systemctl --user enable --now recruiting-pipeline.service` 启动。

处理边界固定为：

1. IMAP 先按 UID 增量扫描 Inbox 和自定义文件夹，不并发复用同一 IMAP 连接。
2. 以候选人地址 + 去掉 `Re/Fwd/回复` 前缀的主题建立线程；新邮件创建线程，follow-up 合并线程并更新 `最新邮件时间`，我方回复只追加原始线程，不重新覆盖候选字段。
3. PDF 文本提取和不同候选人的模型调用可并发；云文档、附件插入和 Base 写入串行，保证顺序和幂等。
4. `面试寄` 映射为 `面试资格`，`面试通过` 映射为 `面试通过`；其它人工状态不会被模型改写。
5. 每个线程有本地幂等记录、重试次数和失败原因。429、超时、5xx 使用带抖动的指数退避，最多 3 次；权限、参数等永久错误进入失败记录，下一轮不重复制造文档。

完整的线程状态、并发边界、模型路由和重试策略见 [`docs/recruiting-pipeline.md`](docs/recruiting-pipeline.md)。

模型默认使用 5090 上现有 API 的 `gpt-5.6-sol`，`reasoning.effort=medium`：普通抽取和 follow-up 合并各一次；不使用模型决定人工筛选结果。后续可在同一配置下比较 `low/medium` 的准确率、延迟、token 与重试率。
