# 趋势强度/变化 与 支撑压力区域 优化说明

本文对应 `优化版本1.md` 的两项增量优化，记录**变更文件、字段变化、判定规则与测试方案**。

本次只增加信息维度，没有改动原有的形态识别、趋势分类、左侧/右侧买入条件和止损比例。

---

## 1. 变更文件

| 文件 | 变更 |
| --- | --- |
| `review_workflow/infrastructure/market_structure.py` | 新增趋势强度/趋势变化引擎与支撑压力区域引擎 |
| `review_workflow/infrastructure/local_review.py` | 每日个股复盘接入新引擎，输出趋势三要素与支撑/压力区域 |
| `review_workflow/infrastructure/weekly_strategy.py` | 周策略接入新引擎，左右侧买入与操作评价改用区域口径，`signal_schema` 升级到 4 |
| `tests/test_market_structure.py` | 趋势与区域单元测试 |
| `tests/test_weekly_strategy.py` | 右侧突破确认、区域操作评价、格式升级测试 |
| `docs/review-process-design.md` | 业务流程与规则说明同步更新 |
| `docs/market-structure-optimization.md` | 本文 |

未改动：`capture_eastmoney.py`、`portfolio_daily_review.py`、`run_daily_review.py`、
`frontend/`、数据源、CLI 参数、目录结构与状态机阶段。

---

## 2. 趋势：状态 + 强度 + 变化

### 2.1 新增字段

`analyze_trend_from_bars()` 在原有 `name` / `definition` 之外新增：

| 字段 | 含义 |
| --- | --- |
| `strength` | 趋势强度：`弱 / 中 / 强`；数据不足为 `待核实` |
| `strength_score` | 0~100 强度评分 |
| `direction` | 趋势变化：`加强 / 稳定 / 减弱 / 反转 / 待核实` |
| `direction_text` | 带箭头的变化文本，箭头跟随趋势推进方向（见 2.4） |
| `evidence` | 趋势依据列表，全部来自规则计算 |
| `interpretation` | 趋势解读文本，供模型/人工引用，不新增数据 |
| `price_distance_to_ma20` | 价格距离 MA20 的相对偏离 |
| `ma_slopes` | MA5/MA10/MA20 相对前期窗口的斜率 |
| `rsi_change` | RSI 相对前期窗口的变化 |
| `volume_change` | 最近一期成交量相对前 N 期均量的变化 |
| `overheated` | 价格高于 MA20 超过 12%，短期过热 |
| `oversold` | 价格低于 MA20 超过 12%，短期超跌 |

### 2.2 强度评分

以趋势分类为基础分，再叠加：

- 收盘价与 MA5/MA10/MA20 的相对位置（每条 +5）；
- 均线排列完整度（MA5>MA10、MA10>MA20 各 +6）；
- MA5/MA10/MA20 斜率方向（每条 +5）；
- RSI 与趋势方向一致（+5）；
- 价格距离 MA20（上限 +15，超过 12% 记 +8 并标记偏离）。

映射：`≥70 强 / ≥40 中 / 否则 弱`。

### 2.3 趋势变化

比较当前窗口与 5 个交易日（周线 4 周）之前的窗口，按趋势方向加权：

| 观察项 | 权重 |
| --- | ---: |
| MA5 / MA10 / MA20 斜率变化 | 6 / 4 / 3 |
| 价格与 MA20 距离变化 | 4 |
| RSI 变化 | 5 |
| 成交量变化 | 3 |

判定：

- 任一观察项都不可比较 → `待核实`；
- MA20 斜率由升转降（或由降转升，且两侧幅度 ≥ 0.3%）→ `反转`；
- 加权得分 ≥ 8 → `加强`；≤ -8 → `减弱`；否则 `稳定`。

约束：

- 趋势变化只在「当前方向」上判定；当前为震荡时沿用前期方向，用来表达动能衰减；
- **变化减弱不会修改趋势状态**，两个维度彼此独立。

### 2.4 变化文本的方向

`direction_text` 的箭头表示**趋势推进的方向**，不是单纯的动能升降，避免下跌趋势出现
「↑ 加强」这种读起来相反的描述：

| 趋势方向 | 加强 | 减弱 |
| --- | --- | --- |
| 上升趋势 / 回升趋势 | `↑ 加强` | `↓ 减弱` |
| 空头下跌 / 弱势破位 | `↓ 加强` | `↑ 减弱` |
| 震荡整理、方向待核实 | 沿用前期方向，无前期方向时按上升处理 | 同左 |

`interpretation` 同样按方向区分措辞：下跌趋势加强写「下跌动能较前期增强，应优先控制仓位与风险」，
下跌趋势减弱写「下跌动能较前期减弱，但尚未出现趋势反转确认」。

---

## 3. 支撑/压力：价格点 → 价格区域

### 3.1 新增字段

`build_price_zones(history, close, moving_averages)` 返回：

```json
{
  "support": [
    {
      "zone_type": "SUPPORT",
      "zone_low": 34.2,
      "zone_high": 34.8,
      "strength": 4,
      "reasons": ["前期低点", "MA20附近", "前期成交密集区域"],
      "breakout_confirmation": {
        "close_below": 34.2,
        "additional_conditions": ["跌破后需要收盘确认，不只用盘中低点判断"]
      }
    }
  ],
  "resistance": ["同上结构，使用 close_above 与 volume_ratio_min"]
}
```

### 3.2 区域来源

`局部极值（前高/前低）`、`20/60/120 日高低点`、`成交密集区域`、`MA5/MA10/MA20/MA60`。

来源按「相对当日收盘价的位置」分侧：价位在收盘价上方记为压力，下方记为支撑。
因此前高被有效突破后转为支撑、前低被跌破后转为压力，避免支撑区出现在压力区上方。
当日高/低点只在区域不足两个时补位，避免短期噪音挤占结构性区域。

### 3.3 合并与不重叠

价格接近（区间重叠、中心差 ≤ 2.5%、合并后宽度 ≤ 4.5%）的来源**在同一侧内**合并为一个区域，
原因合并、反应次数累加，避免出现 36.5 / 36.6 / 36.7 这种重复区域。

合并后再做一次裁剪：支撑区上沿不超过收盘价，压力区下沿不低于收盘价；
裁剪后区间退化的区域直接丢弃，由补位逻辑补足两个区域。

最后按「离收盘价由近到远」归一化：支撑区由高到低、压力区由低到高，
远端区域收窄到近端内侧（保留 0.1% 间隔），因此同侧的支撑区1/支撑区2 不会互相覆盖。
两侧及同侧皆不重叠。

### 3.4 强度

1~5 分，综合：来源类型（结构位/均线/成交密集）、历史反应次数、价格反应幅度、
时间跨度（60/120 日）、是否与均线重合。

**只有均线作为来源的区域最高 2 分**，均线不会被直接当成支撑位。

### 3.5 突破确认

压力区默认条件：

```json
{
  "close_above": "区域上沿",
  "volume_ratio_min": 1.2,
  "additional_conditions": ["收盘位于 MA5/MA10 上方", "周线守住 5 周线或 10 周线"]
}
```

与原有右侧突破规则一致（1.2 倍 5 日均量），没有新增买入条件。

---

## 4. 输出变化

### 4.1 每日个股复盘

新增/调整：

```text
#### 形态与趋势定义
趋势定义：**强上升趋势**。定义：...
**趋势状态**：强上升趋势
**趋势强度**：强（强度评分 78/100）
**趋势变化**：↓ 减弱
**趋势依据**：
- MA5 > MA10 > MA20，均线多头排列
- MA5 走平
- ...
**趋势解读**：...

#### 支撑/压力区域
| 编号 | 区域 | 强度 | 形成原因 | 突破/跌破确认 |

### 关键价位与预案
- 支撑区1：34.20 ~ 34.80（强度 4/5，...）；
- 压力区1：36.30 ~ 36.80（强度 4/5，...）；
- 有效突破：收盘 > 36.80（压力区1 上沿），且成交量达到现有右侧突破规则要求。
```

`今日实际操作复盘` 的评价改为按区域判断：买入落在支撑区为有安全边际、
落在压力区为追高，卖出反向同理。

### 4.2 周策略执行单

- 改为**每股一节**（`## 股票名（代码）`），不再在文档开头集中堆所有股票的聚合表；
- 每只股票小节内包含：核心策略与买卖触发（核心策略、建议数量、止损线、
  左侧买入、右侧突破）、形态与趋势定义、支撑/压力区域、指标快照、日线/周线截图；
- 支撑/压力区域逐条列出区域、强度、原因与突破/跌破确认；
- 形态与趋势定义在趋势前列出强度评分与变化；
- 左侧买入区为 `支撑区2 ~ 支撑区1`，右侧买点为 `压力区1 上沿`（弱势股为压力区2）；
- 止损线规则保持不变（第二支撑下方 2% / 10 周线下方 2% / 单周 8% 风险预算）。

### 4.3 策略文件

- `signal_schema` 由 3 升级为 4；
- 每只股票新增 `support_zones`、`resistance_zones`、`right_trigger_price`；
- 保留原有 `support_1 / support_2 / pressure_1 / pressure_2`，旧读取方不受影响；
- 旧策略（schema ≤ 3）在持仓未变化时自动重建升级并递增 `version`，
  升级原因写入 `change_log`。

---

## 5. 测试方案

运行：

```bash
source .venv/bin/activate
PYTHONPYCACHEPREFIX=$PWD/.pycache python -m unittest discover -s tests -v
```

### 5.1 趋势测试（`tests/test_market_structure.py`）

| 用例 | 覆盖点 |
| --- | --- |
| `test_trend_strength_and_change_cover_required_market_states` | 刚形成强上升趋势 / 持续强上升趋势 / 强上升但动能减弱 / 上涨转震荡 / 震荡转上涨 / 趋势反转 |
| `test_trend_change_is_separate_from_trend_state` | 状态不变但变化为减弱；加强/反转的解读文本；过热与超跌互斥 |
| `test_trend_change_arrow_follows_trend_direction` | 下跌趋势加强为 `↓`、减弱为 `↑`，解读文本随方向改写 |
| `test_trend_analysis_requires_enough_bars` | K 线不足时强度与变化为待核实 |

### 5.2 支撑压力测试（`tests/test_market_structure.py`）

| 用例 | 覆盖点 |
| --- | --- |
| `test_build_price_zones_merges_multiple_sources` | 单一前高、多个前高合并为一个区域、均线重合、突破条件字段 |
| `test_support_zone_merges_with_moving_average` | 支撑区与均线重合后合并并提升强度 |
| `test_moving_average_alone_stays_a_weak_zone` | 只有均线来源时强度 ≤ 2 |
| `test_build_price_zones_keeps_two_far_resistance_zones` | 两个距离较远的压力区不会被错误合并 |
| `test_price_zone_breakout_conditions_are_recorded` | 突破确认条件与区域上沿一致 |
| `test_support_and_resistance_zones_never_overlap` | 支撑区整体在收盘价下方、压力区整体在上方且互不重叠；同侧两档也互不覆盖 |
| `test_same_side_zones_are_separated` | 支撑区2 上沿不压到支撑区1 下沿，压力区同理 |
| `test_broken_pivot_high_becomes_support` | 前高被突破后转为支撑，不再留在压力区 |

### 5.3 周策略与右侧规则（`tests/test_weekly_strategy.py`）

| 用例 | 覆盖点 |
| --- | --- |
| `test_right_side_breakout_requires_zone_high_and_volume` | 未站上区域上沿、量能不足、均线不足、完整满足、止损优先 |
| `test_operation_status_uses_price_zones` | 买入/卖出分别落在支撑区、压力区、中性区与右侧突破 |
| `test_friday_builds_next_week_strategy_with_daily_and_weekly_charts` | 策略包含两个支撑区与两个压力区及其字段 |
| `test_upgrades_existing_strategy_to_signal_schema` | 旧策略升级到 `signal_schema 4` |
| `test_legacy_strategy_without_zone_fields_falls_back_to_price_points` | 旧版无区域字段的策略仍可渲染，执行单与区域明细退回价格点 |

### 5.4 回归测试

- `tests/test_local_review.py`：每日复盘、操作评价与 F10/财报/新闻渲染正常；
  行情主机轮换（裸域名空响应时切换分片、全部主机失败时保留最后一次错误）；
- `tests/test_review_workflow.py`：工作流状态机、HTTP API、断点恢复正常；
- `tests/test_review_logic.py`：CLI 参数、模板拆分、板块与搜索逻辑正常；
- `tests/test_capture_eastmoney.py`：周五/周末周 K 截图逻辑正常；
- 真实历史数据读取：`screenshots/2026/09/18` 与 `screenshots/weekly/2026-W39` 可继续读取，
  旧 `signal_schema` 策略会在下次运行时自动升级。

---

## 6. 已知边界

1. 周策略止损线仍按原有「第二支撑（价格点）下方 2%」计算，未随区域改造调整，
   以避免改变止损比例；
2. `个股复盘模板内容.md` 是 AI 模式（`provider=gemini/zhipu`）的提示词模板，
   目前仍由模型自行填写支撑位/压力位；区域与强度只在 `provider=local` 由规则引擎产出，
   日常复盘建议使用 `provider=local`；
3. 成交密集区域基于已有成交量的价格分箱，不是完整筹码分布模型；
4. 趋势强度评分与阈值属于规则经验值，建议按实际运行结果一段时期后再评估，
   在此之前不再叠加新指标。
