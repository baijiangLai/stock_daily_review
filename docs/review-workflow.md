# 持股复盘 Agent Workflow

这个模块把原来的“一次脚本跑完”拆成可恢复、可观测、可给前端轮询的原子工作流。
项目按 `domain / application / infrastructure / interfaces` 四层组织。

## 设计目标

1. **断点恢复**：截图、个股复盘、组合摘要、最终文档分别落盘；API 余额不足或配额耗尽后，不需要重新抓图。
2. **部分成功**：某只股票失败时，其他股票继续处理；最终文档仍会输出成功部分和失败原因。
3. **前端友好**：所有状态都持久化为 JSON，前端可以轮询 `status`，也可以在任务队列中逐步调用 `step()`。
4. **审计清楚**：状态文件记录每只股票的截图目录、个股复盘文件、尝试次数、失败阶段和错误信息。
5. **周策略延续**：每周第一个交易日记录持仓成本并生成策略执行单；周内每日读取同一策略并追加执行回顾。
6. **操作复盘**：解析 `my_stock.txt` 中紧跟股票行的买入/卖出记录，评价执行质量并写入当日个股复盘。

## 工作流阶段

```text
initialize
  -> stock[1] capture -> stock[1] analyze
  -> stock[n] capture -> stock[n] analyze
  -> portfolio summary
  -> render final document
```

每只股票有两个原子步骤：

- `capture`：解析股票、复用或抓取个股截图、补充板块和核心个股数据。
- `analyze`：AI 模式构建多模态提示词、执行检索和模型调用；`local` 模式走公开数据与本地规则，均生成 `{provider}当日复盘.md`。

全局阶段：

- `summary`：基于成功个股复盘生成组合级摘要；失败时会在最终文档中降级为人工汇总提示。
- `weekly strategy`：`local` 模式在组合摘要前读取或生成本周策略单，并把每日执行回顾写回策略文件。
- `render`：输出 `YYYYMMDD_持股个股复盘.md`，即使全部个股失败也会输出执行状态和失败原因。

## 状态文件

默认路径：

```text
screenshots/YYYY/MM/DD/workflow_state.json
```

核心字段：

| 字段 | 说明 |
| --- | --- |
| `status` | `running` / `completed` / `partial_completed` / `failed` |
| `stage` | `capture` / `analyze` / `summary` / `render` / `done` |
| `stocks[].status` | `pending` / `captured` / `analyzed` / `capture_failed` / `analysis_failed` |
| `stocks[].stock_dir` | 个股截图与复盘目录 |
| `stocks[].review_path` | 个股复盘 Markdown |
| `stocks[].error` | 最近一次失败原因 |
| `events` | 时间顺序事件日志 |
| `output_path` | 最终整日复盘文档 |

## CLI

初始化但不立即执行，适合前端创建任务：

```bash
.venv/bin/python -m review_workflow start --date 2026-09-10
```

初始化并直接跑完：

```bash
.venv/bin/python -m review_workflow start --date 2026-09-10 --execute
```

使用本地规则引擎跑完，不调用 Gemini / 智谱：

```bash
.venv/bin/python -m review_workflow start --date 2026-09-10 --provider local --execute
```

本地规则引擎适合交易日收盘后执行。个股 OHLC、均线、RSI 和量能来自搜狐历史日线；估值、板块、财报、公告与新闻来自东方财富公开接口。板块和估值实时快照会校验日期，补跑历史日期时不会把最新数据误写成当日数据。F10 会补充公司资料、主营构成、地区收入、核心概念、板块标签、股东人数、实际控制人和前五大流通股东；其中主营构成和股东数据按复盘日过滤，公司资料与概念标签属于当前快照并在文档中标注口径。

每日实际操作写在股票行下一行，例如 `35.80,买入200` 或 `36.20,卖出200`。股票行仍表示当前总持仓和综合成本；操作行只用于当日执行评价，不会自动反推持仓。复盘会检查成交价是否在当日高低价区间，估算其相对日内均价和收盘价的结果，并结合支撑、压力与周策略给出后续操作。

周策略文件位于 `screenshots/weekly/YYYY-Www/YYYY-Www_交易策略单.md` 与同名 JSON 文件。程序通过历史日线判断本周是否已有交易日；第一个交易日建立持仓成本基准，后续每日读取并追加回顾。持仓变化会自动生成新版本，价格触发关键条件则先记录为“继续执行”，由人工决定是否修改。旧版按年份存放的策略文件会在下一次读取时自动迁移到具体周目录。

查看状态：

```bash
.venv/bin/python -m review_workflow status --date 2026-09-10
```

从断点恢复并重试失败股票：

```bash
.venv/bin/python -m review_workflow resume --date 2026-09-10 --execute
```

只执行一个原子步骤，适合接任务队列：

```bash
.venv/bin/python -m review_workflow resume --date 2026-09-10 --step
```

复用已有截图，不打开东方财富页面：

```bash
.venv/bin/python -m review_workflow start --date 2026-09-10 --skip-capture --execute
```

只根据当前状态重渲染最终文档，不调用模型：

```bash
.venv/bin/python -m review_workflow render --date 2026-09-10
```

输出最终 Markdown：

```bash
.venv/bin/python -m review_workflow document --date 2026-09-10
```

CLI 输出 JSON。前端服务可以直接包装这些命令，也可以在 Python 服务中导入 `WorkflowAgent`。

## Python 集成

```python
from review_workflow.domain.config import WorkflowConfig
from review_workflow.infrastructure import create_workflow_agent

agent = create_workflow_agent(WorkflowConfig(review_date="2026-09-10"))
agent.start()

while agent.state and agent.state.status == "running":
    agent.step()
    print(agent.status())
```

后端可以将 `agent.step()` 放入 worker 线程或任务队列；前端轮询 `agent.status()` 即可展示进度、失败原因和最终文档路径。

## HTTP API

启动本机 API：

```bash
.venv/bin/python -m review_workflow serve --port 8787
```

OpenAPI 合约：

```text
GET http://127.0.0.1:8787/api/openapi
```

前后端接口文档和 TypeScript 示例见 `docs/review-workflow-api.md`。

前端可以在创建请求中直接提交用户输入的持仓：

```json
{
  "date": "2026-09-10",
  "execute": true,
  "holdings": [
    {"name": "示例股票A", "cost": 10.00, "shares": 100, "plan": "1年之内"},
    {"name": "示例股票B", "cost": 20.00, "shares": 200, "plan": "6个月内"}
  ]
}
```

## 最终文档模块

`review_workflow.domain.document.render_review_document` 是独立渲染器：

- 输入持仓、成功个股复盘、组合摘要、失败列表和元信息。
- 个股复盘可以传 `content`，也可以传 `review_path`。
- 输出 Markdown，并明确列出成功数、失败阶段和错误。
- 因此即使模型全部失败，也会生成一份可审计的“失败报告版”最终文档。
