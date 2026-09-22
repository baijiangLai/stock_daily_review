# 项目长期记忆 — stock-preview

## 项目性质
A 股持仓复盘工作流（`review_workflow`）。核心原则：**K 线形态、趋势分类、支撑压力、买卖触发全部由
确定性规则引擎计算，LLM 只负责解释**，不允许模型自行判断技术形态。

## 运行约定
- Python 解释器一律用仓库内虚拟环境：`.venv/bin/python`（不要用系统 python）。
- 跑测试务必加 `PYTHONPYCACHEPREFIX=$PWD/.pycache`，否则会在仓库里生成 `__pycache__`：
  ```bash
  PYTHONPYCACHEPREFIX=$PWD/.pycache .venv/bin/python -m unittest discover -s tests -v
  ```
- 本机 zsh 下用 Bash 工具执行含中文的 `grep` 经常无输出，改用 Grep 工具。

## 常用 CLI（`python -m review_workflow <子命令>`）
| 子命令 | 用途 |
| --- | --- |
| `start --date YYYY-MM-DD --provider local --execute` | 执行当日完整复盘 |
| `start ... --force --skip-capture --execute` | 复用已有截图，只重渲染（改规则后重生成文档用这个） |
| `resume --date ... --provider local --execute` | 只续跑失败/待重试的股票 |
| `render --date ...` | 只重渲染最终文档 |
| `document --date ...` | 输出最终 Markdown（只读，不改历史数据） |
| `status --date ...` | 查看工作流状态（只读） |

## 关键文件
- `review_workflow/infrastructure/market_structure.py` — 规则引擎核心：`analyze_trend_from_bars()`（趋势）、
  `build_price_zones()`（支撑/压力区域）、`_merge_zones()` / `_clip_zone()` / `_separate_side()`（区域归一化）。
- `review_workflow/infrastructure/local_review.py` — 每日个股复盘渲染 + 公开行情接口抓取。
- `review_workflow/infrastructure/weekly_strategy.py` — 周策略执行单（`signal_schema` 当前为 **4**）。
- 产出目录：`screenshots/YYYY/MM/DD/<股票名_代码>/` 存截图，同级的 `YYYYMMDD_持股个股复盘.md` 是最终文档。
- 持仓配置：`my_stock.txt`（含成本/股数/计划持有周期）。
- 策略文件 `signal_schema` 升级必须保证**向后兼容**：旧文件要能读，旧字段（如 `support_1/2`、`pressure_1/2`）
  必须保留，新字段只增不删。

## 数据源
- 日线/周线历史：搜狐 `q.stock.sohu.com/hisHq`。
- 实时行情/估值：东方财富 `push2delay.eastmoney.com/api/qt/stock/get`。
  **注意裸域名会返回空响应**（HTTP 200 + 0 字节），必须走 `EASTMONEY_QUOTE_HOSTS` 轮换
  （编号分片如 `82.push2delay.eastmoney.com` 可用）。
  **push2 集群可能整体限流**：HTTPS 全部被拒时 **HTTP 80 端口分片通常仍可用**
  （`http://82.push2delay.eastmoney.com`，已加入轮换列表）；板块行情（BK 代码）只能靠东财，
  腾讯不认 BK 代码；快照与估值另有腾讯兜底 `qt.gtimg.cn/q=sh600522`（GBK，~ 分隔字段）。
- 周策略执行单 Markdown 是**每股一节**结构（`## 股票名（代码）`，内含策略/形态趋势/区域/指标/截图），
  不要在文档开头新增跨股票聚合表。

## 截图防护约定（`capture_eastmoney.py`）
- 登录态过期 → `LoginRequiredError`（CaptureError 子类），提示 `--login`，**不重试**。
- 截图前必有 `clear_occlusions()` 命中测试；**登录弹窗绝不允许静默隐藏**——藏掉遮罩截到的
  仍是未登录废图，必须报错让人重新登录。普通广告/浮层才走「自动隐藏 + 复测，最多 3 轮」。
- 隐藏失败仍有遮挡 → `PersistentOcclusionError` → `capture_with_recapture` 自动
  **reload 页面重抓（最多 2 次）**，板块页用 `wait_for_board_page` 恢复就绪；仍失败才报错。
- 复用截图时（`--force --skip-capture`）不会重新检测登录态，属于设计内行为。
