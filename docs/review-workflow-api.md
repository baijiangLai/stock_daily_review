# 持股复盘前后端对接 API

后端提供本地 JSON REST API，默认监听：

```text
http://127.0.0.1:8787
```

启动：

```bash
.venv/bin/python -m review_workflow serve --port 8787
```

OpenAPI 合约：

```text
GET /api/openapi
```

前端可以直接导入该 JSON 生成客户端，也可以按本文手写请求封装。

## 分层结构

```text
review_workflow/
├── domain/          # 纯领域层：状态模型、配置、最终文档渲染
├── application/     # 应用层：Agent 编排、端口、用例服务
├── infrastructure/  # 基础设施层：JSON 状态存储、旧脚本适配、组装工厂
└── interfaces/      # 接口层：CLI、HTTP API、传输协议转换
```

依赖方向：

```text
interfaces -> application + infrastructure
infrastructure -> application ports + domain
application -> domain
```

`domain` 不依赖任何外部框架；`application` 只依赖端口协议；东方财富截图、AI 模型、文件存储都由 `infrastructure` 适配。

## 核心接口

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/api/health` | 健康检查 |
| `GET` | `/api/openapi` | OpenAPI 3.0 合约 |
| `POST` | `/api/review-runs` | 创建工作流，可选择后台执行 |
| `GET` | `/api/review-runs/{date}` | 查询状态和进度 |
| `POST` | `/api/review-runs/{date}/steps` | 同步执行一个原子步骤 |
| `POST` | `/api/review-runs/{date}/resume` | 断点恢复，可选择后台执行 |
| `POST` | `/api/review-runs/{date}/render` | 只重渲染最终 Markdown |
| `GET` | `/api/review-runs/{date}/document` | 获取最终 Markdown |

### 创建工作流

```http
POST /api/review-runs
Content-Type: application/json

{
  "date": "2026-09-10",
  "holdings": [
    {
      "name": "示例股票A",
      "cost": 10.00,
      "shares": 100,
      "plan": "1年之内"
    },
    {
      "name": "示例股票B",
      "cost": 20.00,
      "shares": 200,
      "plan": "6个月内"
    }
  ],
  "provider": "auto",
  "model": null,
  "search_provider": "auto",
  "skip_capture": false,
  "execute": false
}
```

`holdings` 是前端持仓表单的正式入参。提交后后端不会再读取 `my_stock.txt`，而是直接使用这批用户持仓创建任务；状态文件会保存这份持仓，后续 `resume` 不需要重复提交。

字段规则：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `name` | string | 股票名称或 6 位代码，例如 `示例股票A` / `600519` |
| `cost` | number/string | 成本价，必须大于 0 |
| `shares` | integer/string | 持股数量，必须是正整数 |
| `plan` | string | 计划持有时间下拉项 |

`plan` 可选项：

- `3个月内`
- `6个月内`
- `1年之内`
- `3年之内`
- `5年之内`

常用请求字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `date` | string | 必填，`YYYY-MM-DD` |
| `provider` | string | `auto` / `gemini` / `zhipu` |
| `model` | string/null | 指定模型 |
| `search_provider` | string | `auto` / `zhipu` / `model` / `none` |
| `timeout` | number | 5–3600 秒 |
| `device_scale_factor` | number | 0.5–4 |
| `headed` | boolean | 是否显示浏览器 |
| `skip_capture` | boolean | 复用截图 |
| `recapture` | boolean | 强制重抓 |
| `no_web_search` | boolean | 关闭动态检索 |
| `no_peer_capture` | boolean | 关闭核心个股表 |
| `no_portfolio_summary` | boolean | 跳过组合摘要模型调用 |
| `board` | string[] | 强制板块 |
| `peer_stock` | string[] | 强制核心个股 |
| `holdings` | array | 用户输入的持仓列表；省略时使用服务端 `my_stock.txt` |
| `force` | boolean | 覆盖已有状态重新开始 |
| `execute` | boolean | 创建后立即后台执行 |

返回 `201` 表示只创建，返回 `202` 表示已创建并后台执行。

### 查询状态

```http
GET /api/review-runs/2026-09-10
```

关键字段：

```json
{
  "status": "running",
  "stage": "capture",
  "active": false,
  "progress": {
    "total": 2,
    "analyzed": 1,
    "failed": 0,
    "remaining": 1
  },
  "stocks": [
    {
      "holding": {"name": "示例股票A", "cost": "10.00", "shares": "100", "plan": "1年"},
      "status": "analyzed",
      "stock_dir": "screenshots/2026/09/10/示例股票A_600519",
      "review_path": "screenshots/2026/09/10/示例股票A_600519/gemini当日复盘.md",
      "error": null
    }
  ],
  "output_path": "screenshots/2026/09/10/20260910_持股个股复盘.md"
}
```

### 推荐前端生命周期

#### 方式一：前端逐步驱动

适合需要精细展示每个步骤的界面：

1. `POST /api/review-runs` 创建，不传 `execute`；
2. 循环 `POST /api/review-runs/{date}/steps`；
3. 每步后用返回值更新 UI；
4. `status !== "running"` 时停止；
5. `GET /api/review-runs/{date}/document` 展示 Markdown。

#### 方式二：后台执行 + 轮询

适合“一键复盘”按钮：

1. `POST /api/review-runs`，传 `execute: true`；
2. 前端每 1–3 秒 `GET /api/review-runs/{date}`；
3. `active === true` 时显示执行中；
4. 终态后获取文档。

### 断点恢复

```http
POST /api/review-runs/2026-09-10/resume
Content-Type: application/json

{
  "retry_failed": true,
  "execute": true
}
```

`analysis_failed` 的股票会保留已抓截图，只重试 AI 复盘；`capture_failed` 的股票会重试截图。

## TypeScript 对接示例

```ts
export type ReviewRun = {
  status: "running" | "completed" | "partial_completed" | "failed";
  stage: "capture" | "analyze" | "summary" | "render" | "done";
  active?: boolean;
  progress: {
    total: number;
    analyzed: number;
    failed: number;
    remaining: number;
  };
  output_path?: string | null;
};

export type HoldingInput = {
  name: string;
  cost: number;
  shares: number;
  plan: "3个月内" | "6个月内" | "1年之内" | "3年之内" | "5年之内";
};

const base = "http://127.0.0.1:8787";

async function request<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const response = await fetch(`${base}${path}`, {
    headers: {"Content-Type": "application/json"},
    ...init,
  });
  if (!response.ok) {
    const message = await response.text();
    throw new Error(`API ${response.status}: ${message}`);
  }
  return response.json() as Promise<T>;
}

export async function createRun(date: string, holdings: HoldingInput[]) {
  return request<ReviewRun>("/api/review-runs", {
    method: "POST",
    body: JSON.stringify({date, holdings, execute: true}),
  });
}

export async function getRun(date: string) {
  return request<ReviewRun>(`/api/review-runs/${date}`);
}

export async function getDocument(date: string) {
  const response = await fetch(`${base}/api/review-runs/${date}/document`);
  if (!response.ok) throw new Error("文档尚未生成");
  return response.text();
}
```

## 安全与部署注意

- 默认只绑定 `127.0.0.1`，仅供本机前端调试。
- API 不接受前端传入任意文件路径，避免服务端任意路径读写。
- 如果部署到局域网或公网，应增加认证、HTTPS、请求限流，并避免直接暴露模型 Key。
- 后台执行同一日期的工作流时有活跃状态锁；重复提交返回 HTTP `409`。
