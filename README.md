# 分布式网页抓取调度平台

面向分布式网页抓取任务调度、节点协同、请求分配、反爬策略编排、数据校验入库与抓取质量大盘的采集工程管理平台。

这是一个前后端分离的管理平台：前端 React + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 React + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 采集节点 | `worker` | 工作节点 | 节点编号、主机地址、并发槽位数 |
| 请求分配 | `dispatch` | 分配策略 | 策略编号、目标域名、分配算法 |
| 抓取作业 | `crawljob` | 作业定义 | 作业编号、目标站点、起始URL |
| URL过滤 | `urlfilter` | 过滤规则 | 规则编号、包含模式、排除模式 |
| DNS解析 | `dnsresolver` | 解析配置 | 配置编号、域名模式、DNS服务器 |
| 内容提取 | `contentextract` | 提取器 | 提取器编号、内容类型、正文选择器 |
| 存储策略 | `storagestrategy` | 存储方案 | 方案编号、存储类型、连接参数 |
| 渲染任务 | `rendertask` | 渲染作业 | 作业编号、目标URL、浏览器版本 |
| 采集质量 | `qualityreport` | 质量报告 | 报告编号、作业编号、总URL数 |
| 带宽控制 | `bandwidth` | 带宽策略 | 策略编号、节点标识、下载带宽上限 |
| 重试队列 | `retryqueue` | 重试条目 | 条目编号、失败URL、失败原因码 |
| 翻页管理 | `pagemarker` | 翻页规则 | 规则编号、目标站点、翻页类型 |
| 增量标记 | `incrementmark` | 增量水位 | 水位编号、目标源、增量字段名 |
| 配置同步 | `configsync` | 同步任务/同步结果/同步审计 | 任务编号、源/目标节点、握手状态、批次号、源快照版本、生效前后版本、差异摘要、幂等提交次数 |
| 采集API | `scrapeapi` | API定义 | API编号、API名称、请求路径 |
| 数据抽样 | `datasample` | 抽样方案 | 方案编号、数据源、抽样方式 |
| 采集告警 | `alertpolicy` | 告警策略 | 策略编号、监控指标、触发条件 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.tsx`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 配置同步域（configsync）

通用 CRUD 之外，配置同步模块实现了一套有真实状态机的领域规则，数据落在内存仓库的
五张表里：`configsync`（任务）、`config_snapshot`（源配置快照）、
`target_progress`（目标应用进度）、`sync_result`（同步结果）、`sync_audit`（同步审计）。

### 核心规则

- **双端版本握手**：任务经「待握手 → 握手待确认 → 已握手」才能进入分段传输；握手未形成、
  目标节点离线、仍有配置段未应用时，都不能确认完成，杜绝“配置不完整却显示同步完成”。
- **分段传输与断点续传**：配置按段下发，进度记录已应用段；中断后从“第一个未应用段”续传，
  已固化段不重复应用。
- **版本冲突分批**：分段传输中源配置被再次修改、目标仍应用旧段时判定冲突，旧批次保留旧源
  版本，新源版本另起批次，两个源版本落不同批次，冲突态不能直接完成。
- **唯一完成结果（幂等 + 并发控制）**：所有写操作在 `store.lock` 内串行；完成结果带幂等键
  `任务编号:源版本:complete`，重复发起、并发执行、连续提交都只产生一个完成版本，
  重复请求只累加“提交次数”。同一目标节点同时只允许一个活动任务。
- **同步结果与审计数据集**：结果记录生效前后版本、差异摘要（含未应用项）、失败原因、发起人、
  幂等提交次数与容量侧回写痕迹；每个动作（含被拒尝试）都写一条审计，处理痕迹可逐项核对。
- **容量侧回写与执行侧采集**：完成后把实际生效版本回写到可调度节点（worker）记录并随记录保留；
  执行侧通过 `/api/worker/collect-config-versions` 把节点配置版本、差异摘要、最后同步时间
  采集到配置节点清单。
- **归属权限**：写动作仅管理员可执行，非管理员只读查看同步差异。角色经请求头
  `X-User-Role: admin|viewer` 传入（操作人姓名可放在请求体 `values.操作人`，避免非
  Latin-1 字符进 HTTP 头）。

### 主要接口

- `GET/POST /api/configsync`、`POST /api/configsync/{id}/actions`
  动作：发起握手、握手确认、应用分段、标记中断、节点恢复、断点续传、发布源新版本、
  检测版本冲突、提交同步结果、标记失败（可用状态见 `GET /api/configsync/status-meta`）。
- 数据集：`GET /api/configsync/results|audits|snapshots|progress`，
  明细 `GET /api/configsync/{id}`，全量导出 `GET /api/configsync/export`。
- 容量侧/执行侧：`POST /api/worker/apply-sync-result`、
  `POST /api/worker/collect-config-versions`、`GET /api/worker/config-nodes`。

### 测试

- 服务层回归（无需起服务）：`cd backend && .venv/bin/python -m unittest tests.test_configsync_domain -v`
- 端到端规则验证（需先起后端）：脚本 `backend` 外独立运行，对真实 HTTP 端口发请求。

领域：数据工程
子方向：数据采集、爬虫与RPA
