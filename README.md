# stock-preview

第一步：根据股票名称或代码，从东方财富行情页截取指定数据。

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

脚本默认使用本机已安装的 Google Chrome，无需额外下载浏览器。

## 东方财富扫码登录

东方财富部分行情/盘口内容需要登录态。首次使用或截图数据为空时，先执行：

```bash
python capture_eastmoney.py --login
```

在打开的浏览器里点击“登录”，选择扫码登录。确认页面右上角显示账号信息后，回到终端输入 `yes` 保存。登录状态保存在本机 `.auth/eastmoney-state.json`，后续个股截图会自动复用；该文件包含登录 Cookie，请勿提交或分享。

## 使用

```bash
python capture_eastmoney.py 贵州茅台
# 或
python capture_eastmoney.py 600519
```

如需观察浏览器操作过程：

```bash
python capture_eastmoney.py 600519 --headed
```

截图会保存到 `screenshots/股票名_代码_时间戳/`，包含：

1. `01_trading_data.png`：当日交易数据面板
2. `02_intraday_chart.png`：日内分时走势图
3. `03_bid_ask_5.png`：买卖五档盘口
4. `04_daily_kline.png`：日 K 线图

`metadata.json` 会记录输入值、解析后的股票代码、行情页地址和截图清单。

## 批量整日持股复盘

先创建自己的持仓文件（示例见 `my_stock.txt.example`）：

```bash
cp my_stock.txt.example my_stock.txt
```

格式为每行一只股票：

```text
股票名称,成本,持股数,计划持有时间
```

登录东方财富后，生成指定日期的整日复盘：

```bash
python portfolio_daily_review.py --date 2026-08-28
```

默认会先检查 `screenshots/年/月/日/` 下是否已有该股票的完整个股与板块截图；存在时不重新截图，直接复用并继续生成复盘。只有截图缺失或不完整时才归档旧目录并重抓。

截图会按日期整理到：

```text
screenshots/年/月/日/股票名称_股票代码/
```

每只股票会生成：

- 4 张个股截图
- 所属板块截图与 `boards_metadata.json`
- `zhipu当日复盘.md` 或 `gemini当日复盘.md`

最后在日期目录下生成：

```text
YYYYMMDD_持股个股复盘.md
```

常用参数：

```bash
# 复用当天已有截图，只重新调用 AI 模型
python portfolio_daily_review.py --date 2026-08-28 --skip-capture

# 只抓取当天个股与板块截图，不调用模型、不生成 Markdown
python portfolio_daily_review.py --date 2026-08-28 --capture-only

# 只读检查当天已有截图与提示词清单；不联网、不写文件、不调用模型
python portfolio_daily_review.py --date 2026-08-28 --dry-run

# 旧截图已存在时归档并重新截图
python portfolio_daily_review.py --date 2026-08-28 --recapture
```

## 可恢复的 Agent Workflow

如果希望后续接入前端或任务队列，可以使用 `review_workflow`。它把批量复盘拆成
“个股截图 → 个股 AI 复盘 → 组合摘要 → 最终文档渲染”的原子步骤，并将状态保存到：

```text
screenshots/YYYY/MM/DD/workflow_state.json
```

常用命令：

```bash
# 初始化工作流
.venv/bin/python -m review_workflow start --date 2026-09-10

# 初始化并执行到终态
.venv/bin/python -m review_workflow start --date 2026-09-10 --execute

# 查看 JSON 状态
.venv/bin/python -m review_workflow status --date 2026-09-10

# API 配额恢复后，从断点重试失败股票
.venv/bin/python -m review_workflow resume --date 2026-09-10 --execute

# 只重渲染最终 Markdown，不调用模型
.venv/bin/python -m review_workflow render --date 2026-09-10

# 输出最终 Markdown
.venv/bin/python -m review_workflow document --date 2026-09-10

# 启动前后端对接 API；前端可在 /api/review-runs 中提交用户输入的 holdings
.venv/bin/python -m review_workflow serve --port 8787
```

该模式的特点：

- 截图、板块数据、个股复盘和最终文档分阶段落盘。
- 单只股票失败不影响其他股票继续执行。
- 全部模型调用失败时，仍会输出包含失败原因的最终文档。
- 前端可以轮询状态 JSON，或由后端 worker 每次调用一个 `step()`。

详细状态字段和集成方式见 `docs/review-workflow.md`。
前后端 HTTP API、OpenAPI 和 TypeScript 示例见 `docs/review-workflow-api.md`。

## 前端

项目包含一个零 npm 依赖的静态前端：

```bash
# 终端 1：启动后端
.venv/bin/python -m review_workflow serve --port 8787

# 终端 2：启动前端
cd frontend
npm run dev
```

然后访问 `http://127.0.0.1:5173`，在页面中输入持仓并点击“一键自动复盘”。

> `--date` 只用于目录整理和复盘标题；东方财富页面仍返回打开页面当时的最新行情。如果周末复盘周五，请以截图中的行情日期为准。
> 批量模式的 `--board` 会应用到所有持仓，并且必须按顺序提供 `一级行业`、`核心板块`、`细分方向` 三个板块。多持仓时建议省略该参数让每只股票自动推断；确需强制指定时，更适合对单只股票执行 `run_daily_review.py`。
> `--skip-capture` / `--dry-run` 会严格校验 4 张个股截图、三个板块截图和主板块资金流/成分股截图；缺失时不会降级调用模型。

## AI 当日复盘

先生成个股截图，再在项目根目录 `.env` 中设置 API Key。当前支持智谱与 Gemini；`--provider auto` 检测到 `ZHIPU_API_KEY` 时会优先使用智谱，否则使用 Gemini。

```bash
cp /dev/null .env
chmod 600 .env
echo 'ZHIPU_API_KEY=你的智谱 API Key' >> .env
# 或
echo 'GEMINI_API_KEY=你的 Gemini API Key' >> .env
```

智谱使用官方 `https://open.bigmodel.cn/api/paas/v4/chat/completions` 多模态对话接口，默认模型 `glm-5.3-flash`。如需自定义智谱接口地址，可设置 `ZHIPU_BASE_URL`。

动态信息默认优先调用智谱独立的 Web Search API：

```text
POST https://open.bigmodel.cn/api/paas/v4/web_search
search_engine=search_std
```

搜索结果会作为带标题、链接、媒体、发布时间和摘要的文本证据提供给复盘模型，并在复盘末尾自动附加来源列表。这样可以组合使用“智谱 search_std 搜索 + Gemini 读截图生成”，避免消耗 Gemini Google Search grounding 配额。

脚本会自动读取项目根目录的 `.env`；也可以继续使用环境变量 `ZHIPU_API_KEY`、`GEMINI_API_KEY` 或 `GOOGLE_API_KEY`。

为最新一组个股截图补充板块截图，并直接生成复盘：

```bash
python run_daily_review.py
```

也可以指定某个截图目录、板块、服务商和模型：

```bash
python run_daily_review.py screenshots/平安银行_000001_20260830_155334 \
  --board 一级行业:BK1283 \
  --board 核心板块:BK0475 \
  --board 细分方向:BK1610 \
  --provider gemini \
  --model gemini-3.6-flash \
  --search-provider zhipu
```

脚本会按“当前数据截图 → 技术面分析 → 板块截图 → 板块分析 → 大事/仓位计划”的结构组织多模态提示词，并将复盘保存为截图目录下的 `zhipu当日复盘.md` 或 `gemini当日复盘.md`。板块截图会记录在 `boards_metadata.json` 中。

个股复盘请求会按 `--search-provider` 获取动态信息：默认有 `ZHIPU_API_KEY` 时使用智谱 Web Search（`search_std`），没有智谱 Key 时使用模型内置搜索。截图主要用于支撑股票与板块的价格、量能、分时/K线形态和关键技术位；板块涨跌原因、近期大事、财报、公司事件、行业事件、政策与宏观事件等动态事实必须由检索证据核实，并在正文中标注来源和日期。检索网页来源会自动附加到复盘末尾，便于复查。

工程机械板块还会自动补充“龙头/核心个股交易数据表”：先用 Gemini 无联网对话提名三一重工、徐工机械、中联重科、柳工、恒立液压、浙江鼎力、杭叉集团、安徽合力、山推股份等候选，再调用东方财富历史日线接口获取复盘日的开盘、收盘、最高、最低、涨跌幅、成交量、成交额和换手率，生成 `core_peers/日期_core_peers_trading_data.png` 后交给模型。因此“板块龙头/核心个股表现”可以基于真实日内数据填写，但统计口径是候选池比较，不等同于全板块涨幅排行榜。

常用搜索参数：

```bash
# 智谱独立 Web Search + Gemini 读图
--search-provider zhipu

# 模型内置搜索（Gemini 为 Google Search，智谱为对话内 web_search）
--search-provider model

# 不检索；动态字段明确写“待核实”
--no-web-search

# 手动指定其他板块的核心个股，可重复传入
--peer-stock 三一重工:600031 --peer-stock 徐工机械:000425

# 不抓取核心个股交易数据表
--no-peer-capture
```

## AI 分段提示词

批量复盘会在一次多模态请求中按资料可用性分段组织提示词：

1. 先提供个股当日交易数据、分时图、五档盘口与日 K 线，让模型先完成“二、 技术面与量价状态”：均线形态、关键位置、第一/第二支撑位、第一/第二压力位、量价资金特征、成交量与主力意图。
2. 再提供一级行业、核心板块、细分方向截图，让模型完成“一、 板块”；截图负责走势与形态。若提供了核心个股历史交易数据表，板块龙头和核心个股表现优先从该表读取；涨跌原因和相对大盘等仍需检索核实。
3. 最后综合前两段结论与 `my_stock.txt` 的成本、持股数、计划持有时间，并通过联网搜索填写“三、 近期大事”和“四、 仓位管理与计划交易”。

最终 Markdown 仍按模板原始顺序输出。截图和检索都不能核实的信息会明确写“待核实”，不会让模型用参数记忆虚构。板块资金流与成分股截图仍会抓取作为本地审计材料，但不会发送给模型，避免把一级行业成分股误写成核心板块/细分方向龙头。

`--skip-capture` 会直接从当日目录的 `metadata.json` 匹配持仓并复用本地截图，不需要再次访问东方财富；单股流程的 `--skip-capture` 也可继续传股票名称或代码。

如果本机不能直连模型 API，可在 `.env` 中追加代理配置（值需替换为自己的代理地址）：

```text
HTTPS_PROXY=http://127.0.0.1:7890
HTTP_PROXY=http://127.0.0.1:7890
NO_PROXY=localhost,127.0.0.1
```
