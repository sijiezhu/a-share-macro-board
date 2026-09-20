# A股市场情绪指标算法 — 设计稿 v0.2（待评审）

状态：**设计阶段，未实现**。本文件只描述算法、字段、接口与测试，不含完整实现。
实现完成后需同步更新 `docs/DATA_SOURCES.md`（新增输入字段的来源与状态）与 `README.md`。
v0.1 备份：`/tmp/SENTIMENT_ALGORITHM.v0.1.md`（临时文件，不入库）。

---

## 一、目标与边界

### 1.1 目标

用中短期视角（未来 1—4 周）**描述**A股市场情绪，输出：

参与度 `participation_index`、方向 `direction_index`、综合情绪 `sentiment_index`、
短期动量、四象限状态 `state`、
恐慌抛售得分 `panic_score`、冰点反转观察信号 `reversal_watch`、
量价状态 `macd_volume_state`。

### 1.2 非目标（硬约束）

- 不输出投资建议、买卖指令、目标价、仓位建议。
- 不把任何指标解释为未来上涨/下跌概率，不作为买卖信号。
- 不使用模拟数据冒充真实数据；未接通的字段一律标注「不可用」并跳过对应子指标。
- 不做前向填充或插值来"造"样本（`missing_policy` 默认 `nan`）。
- 不把 PE 百分位与情绪分位混用。

### 1.3 对外表述规范

| 禁止 | 允许 |
| --- | --- |
| 「恐慌得分 80，说明即将反弹」 | 「恐慌抛售得分 80/100（覆盖率 4/5），触发条件：…（描述性指标）」 |
| 「情绪指数 65，市场将上涨」 | 「综合情绪 65/100，位于贪婪/主升象限」 |
| 「冰点反转信号 = 买入信号」 | 「冰点反转**观察**条件全部满足（4/4），仅为状态观察」 |
| 「情绪分位 90%，上涨概率 90%」 | 「情绪指数在过去 252 个交易日中处于第 90 百分位」 |

### 1.4 工程属性

纯函数、无 IO、无随机数、不读取当前时间、不联网；同输入必得同输出；
参数全部走配置对象，函数体内不出现魔数。

### 1.5 v0.2 变更摘要（本次评审意见落地）

| # | 意见 | 落地方式 |
| --- | --- | --- |
| 1 | 不同指标应支持不同时间跨度 | 引入**分量注册表** `ComponentSpec`：每个分量独立声明尺度（可单尺度、可多尺度加权混合）、`min_periods`、方向、可用性滞后；默认仍为规格书的 252 日单尺度，另附推荐的多尺度画像 |
| 2 | 恐慌分用 renormalize，输出 `panic_coverage` 与 `panic_confidence`，`coverage < 0.6` 不触发 `reversal_watch` | 删除"严格求和"策略；`panic_score = 100 × 命中 / 可评估`；新增覆盖度与置信度两列；覆盖率门控写进反转信号，并输出门控原因码 |
| 3 | 算法层用中性状态码，展示层映射文案 | 新增 `codes.py`（纯英文码值，无中文）与 `labels.py`（中文文案）；`STATE_PANIC` -> 「恐慌抛售（放量急跌型冰点）」，`STATE_COLD` -> 「缩量阴跌（清淡型冰点）」 |
| 4 | 分位口径二选一（D1） | **已定：模式 B**（严格早于当日 + 中位秩），与 `AGENTS.md` / `docs/DATA_SOURCES.md` 的股债利差分位同口径；模式 A 保留为可配置项 |

---

## 二、口径决策

### D1【已确认】分位算法采用模式 B（与现有页面统一）

| | 本模块（模式 A，可选） | `AGENTS.md` / `docs/DATA_SOURCES.md`（模式 B，**默认**） |
| --- | --- | --- |
| 样本范围 | 窗口内**含当日** | 严格早于当前数据日期 |
| 排名公式 | `100 × (≤ 当前值的样本数 ÷ 有效样本数)` | `100 × (小于 + 0.5 × 等于) ÷ 样本数` |
| 最小样本 | 该窗口的 `min_periods`（252 窗口默认 120） | 默认同样取推导值 120；需要满窗时显式写 `min_periods=window` |
| 未达最小样本 | 输出 NaN | 不显示，标注「样本不足」 |

默认 `percentile_method='midrank_exclusive'`（模式 B）；模式 A 通过
`percentile_method='rank_inclusive'` 或逐尺度 `ScaleSpec.method` 显式启用。
两条配套要求：对外一律称「**252 日滚动分位**」（不得称"5 年百分位"）；
`docs/DATA_SOURCES.md` 需新增一节说明本模块分位与股债利差分位**现在同口径**。

**预热与数据年限（v0.3 修正）**：`min_periods` 与分位公式是两个独立旋钮——口径用模式 B，
最小样本仍按规格取 120。于是 252 窗口的分量在第 121 个交易日就有分位，
量比分量因为要先做 20 日均量、到第 140 个交易日才有分位，
`sentiment_pct_252`（对已被预热一次的综合指数再取分位）从第 241 个交易日可用，
即**约一年数据即可完整跑通**。样本未满窗口时页面须标「可用历史百分位」，
而不是「252 日分位」；需要严格满窗的口径时显式写 `ScaleSpec(252, min_periods=252)`。
指数在样本刚够时可能只覆盖部分分量（覆盖率与缺失清单逐日输出，见 §4.4）。

#### D1 补充：两种口径的实际差异（pandas 3.0.5 实测）

| 维度 | 模式 A `rank_inclusive` | 模式 B `midrank_exclusive` |
| --- | --- | --- |
| 当日值 | 既是"被定位的值"，也是样本之一 | 只作为被定位的值，不进样本 |
| 样本区间 | `[t-251, t]` | `[t-252, t-1]` |
| 公式 | `count(<= x) / n` | `(count(< x) + 0.5 × count(= x)) / n` |
| 取值范围 | `[100/n, 100]`：最低也拿 `1/n` | `[0, 100]`：可到 0 |
| 并列值 | 全部算"不高于"，并列越多越偏热 | 中位秩，并列取中性 |
| 常数序列 | 恒 `100`（"永远最热"） | 恒 `50`（"永远中性"） |
| 首次可用 | 第 120 个样本（252 窗口） | 第 121 个样本（`min_periods=120`）；强制满窗则第 253 个 |

实测差异（`window=252`）：

- **连续、无并列的分量**（换手率、成交额、量比、20 日收益、MACD柱、两融、PCR、IV）：
  平均 `|A − B| = 0.21` 分，最大 `0.81` 分，差异可忽略；
  唯一结构性差异是下界（A 最低约 `0.4` 分，B 能到 `0`）。
- **大量并列的离散分量**（涨跌停比、上涨家数占比）：平均 `|A − B| = 12.6` 分，
  最大 `17.5` 分；同一交易日示例：当日值处于最高档 -> `A = 100.0`、`B = 88.7`。
- **并列扎堆的极端**：`[1,2,2,2]` 取值为 2 时 `A = 100.0`、`B = 66.7`；
  常数序列 `A = 100`、`B = 50`。

对本模块的影响：10 个分量里 8 个是连续量，两者几乎等价；
真正被影响的是 `up_ratio` 与 `limit_up_down_ratio` 两个方向分量（涨跌停比为 0 的天数极多，
并列严重），它们会通过方向指数影响四象限状态；另外 A 偏热会让
`panic_turnover_pct > 80` 这个条件更容易触发。
### D2【已确认】恐慌分采用 renormalize + 覆盖度/置信度 + 门控

- 只保留 renormalize：`panic_score = 100 × 命中条件数 / 可评估条件数`。
  理由（你的原话落地）：严格求和时 5 个条件各 +20，在字段缺失常态下阈值永远打不到。
- 新增 `panic_coverage = 可评估条件数 / 5`。
- 新增 `panic_confidence`（0—1）：`coverage × readiness`，
  `readiness = min(1, 该日分位有效样本数 / 该分位窗口的 min_periods)`，
  取恐慌条件所用分位分量的最小值；不依赖分位的条件不参与该最小值。
  即"条件齐不齐"与"窗口样本够不够"两个维度折进同一个 0—1 的数。
- `panic_score` 在 `可评估条件数 >= 1` 时始终输出；`= 0` 时输出 NaN。
- **门控**：`panic_coverage < 0.6` -> `reversal_watch = False`，
  并在 `reversal_block_reason` 写入 `REV_BLOCK_PANIC_COVERAGE_LOW`。
- 保留 `panic_hits`（整数命中数）用于解释；不再输出 `panic_score_raw`，
  避免与 renormalize 后的分数混淆（`20 × hits != panic_score`）。

### D3【已确认】算法层状态码 + 展示层文案

算法层只输出中性码值，中文文案在展示层映射：

| 码值 | 展示文案 |
| --- | --- |
| `STATE_GREED` | 贪婪/主升 |
| `STATE_PANIC` | 恐慌抛售（放量急跌型冰点） |
| `STATE_COLD` | 缩量阴跌（清淡型冰点） |
| `STATE_THAW` | 温和回暖 |
| `STATE_NEUTRAL` | 中性震荡 |
| `STATE_UNAVAILABLE` | 不可用（数据不足） |

同一原则推广到其余分类输出：`macd_volume_state`、`data_quality`、
`participation_band` / `direction_band` 全部用码值；展示层通过 `labels.py` 提供中文。
该原则可机械校验：`codes.py` 源码中不得出现中文（见测试 T44）。
输出仍附带 `state_label` 便捷列，方便 Streamlit 直接取用。

### D4【已确认】每个指标支持自己的时间跨度

框架层面升级为"分量注册表"：

- 每个分量独立声明 `scales`：`(window, weight, min_periods, method)` 的列表，
  支持"单尺度"与"多尺度加权混合"（例：涨跌停比 63 日 + 252 日各占 50%）。
- 每个分量独立声明 `horizon` 标签（`short` ≤ 63 / `mid` 64—252 / `long` > 252），
  供页面分组展示；多尺度分量标为 `mixed`。
- 每个分量独立声明 `availability_lag`（两融默认 1，其余 0）。
- `min_periods` 缺省时按该尺度窗口推导：`ceil(window × min_periods_ratio)`，
  默认 `min_periods_ratio = 120/252 ≈ 0.476`（252 -> 120，63 -> 30，504 -> 240），
  可整体覆盖，也可逐分量显式指定。
- 规格书默认仍是"全部 252 单尺度"，以保证与规格数值一致；
  另提供 `PROFILE_MULTISCALE_SUGGESTED` 推荐画像（见 §3.4）供选择。

---

## 三、数据字典

### 3.1 输入字段

`compute_sentiment(df, config)` 的输入：日频 DataFrame，**一行 = 一个交易日**。

| # | 字段 | 类型 | 单位 | 必需 | 口径 / 建议来源 | 缺失时的影响 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | `date` | datetime64 / date / ISO 字符串 | — | **硬必需** | A股交易日；不重采样、不按自然日补齐 | 缺失或无法解析 -> `ValueError` |
| 2 | `close` | float | 点 | 软必需 | 指数收盘（建议沪深300，与现有卡片一致） | `return_*`、MACD 全部 NaN -> 方向缺失 2 分量 |
| 3 | `volume` | float | 股 / 手 | 否 | 沪深两市A股合计成交量 | 量比、量价状态、反转缩量条件不可用 |
| 4 | `amount` | float | 元 | 否 | 沪深两市A股合计成交额 | 参与度缺 1 分量 |
| 5 | `turnover_rate` | float | % | 否 | 两市A股整体换手率，**按总股本口径**（2026-09-19 确认） | 参与度缺 1 分量、恐慌条件 1 不可评估 |
| 6 | `margin_net_buy` | float | 元 | 否 | 融资净买入额（融资买入 − 融资偿还） | 参与度缺 1 分量 |
| 7 | `up_count` | float | 家 | 否 | 沪深A股上涨家数（现有妙想源） | 上涨/下跌占比不可用 |
| 8 | `down_count` | float | 家 | 否 | 沪深A股下跌家数（现有妙想源） | 同上 |
| 9 | `limit_up_count` | float | 家 | 否 | 涨停家数（口径待登记，见 §3.2） | 涨跌停比不可用 |
| 10 | `limit_down_count` | float | 家 | 否 | 跌停家数 | 同上 |
| 11 | `pcr` | float | 无量纲 | 否 | 期权 Put/Call Ratio（建议 300ETF 期权成交量 PCR） | 方向缺 1 分量 |
| 12 | `implied_volatility` | float | % | 否 | 300ETF 期权隐含波动率 / 波动率指数 | 方向缺 1 分量 |

**校验规则（全部可配置）**

- `close <= 0`、`volume < 0`、`amount < 0`、`pcr <= 0`、`implied_volatility <= 0`、
  家数字段为负 -> 视为无效值置 NaN；
- `volume == 0`（停牌）默认按 NaN 处理（`zero_volume_policy='nan'`）；
- `turnover_rate` 允许为 0，不允许为负；
- 数值列统一转 `float64`，`inf/-inf` 置 NaN；
- `up_count + down_count == 0` -> 占比为 NaN，不做 0 除；
- `limit_down_count` 缺失时**不**用 0 顶替（否则 `limit_up/(0+1)` 失真）-> 该分量为 NaN；
- `amount == 0` 视为有效观测（极端缩量）。

### 3.2 必须在 `DATA_SOURCES.md` 补登记的条目（实现前置条件）

现状：`docs/DATA_SOURCES.md` 只登记了国债、汇率、黄金、A股涨跌家数、沪深300 收盘与 PE。
以下字段**尚无来源说明**，按项目规则不得在缺来源说明的情况下启用：

| 字段 | 建议来源 | 状态 |
| --- | --- | --- |
| `amount` / `volume` | 沪深两市成交额/成交量（东财或妙想） | 待接通与登记 |
| `turnover_rate` | 两市A股换手率，**总股本口径**（2026-09-19 确认） | 已登记 |
| `margin_net_buy` | 两融：融资净买入额（**T+1 公布**，见下） | 待接通与登记 |
| `limit_up_count` / `limit_down_count` | 涨跌停家数（需明确「收盘封板」还是「触及涨跌停」） | 待接通与登记 |
| `pcr` | 300ETF 期权成交量 PCR | 待接通与登记 |
| `implied_volatility` | 300ETF 期权平值 IV（中国波指 iVX 已停止发布） | **未接通** |

**可得时点（防未来函数的关键）**：两融数据 T 日盘后不公布，T+1 才可得。
分量注册表里 `margin_net_buy` 的 `availability_lag = 1`（默认），
表示 t 日计算只使用 `≤ t-1` 的观测；若上游已按"可获得日"落库则该值设 0。
该口径必须写进 `docs/DATA_SOURCES.md`。

#### 3.2.1 妙想取数实测（2026-09-19，`macroboard/sources/mx.py` 的 `query()`）

对每个字段发出自然语言查询并检查妙想回传的 `returnName` / `returnSourceCode`：

| 查询语句（自然年区间） | 妙想命中字段 | 返回样例（2026-09-18） | 结论 |
| --- | --- | --- | --- |
| 沪深两市A股成交额 …每个交易日 | `成交额(合计)`，sourceCode `TVAL`，实体 沪深A股(板块)，DAY | 2.077e12 元 | **可取数**，单位元 |
| 沪深两市A股换手率 …每个交易日 | 换手率（两市A股） | 1.49（%） | **可取数**；口径按**总股本**（2026-09-19 确认，分母为总股本而非自由流通市值） |
| 沪深A股涨停家数 …每个交易日 | 涨停家数（沪深A股） | 79 家 | **可取数**（跌停家数同法） |
| 沪深两市A股成交量 …每个交易日 | `成交量(合计)`，sourceCode `TVOL` | 1.082e11 股 | **可取数**，单位股 |
| 沪深A股跌停家数 …每个交易日 | `跌停家数`，sourceCode `LOWSTOC` | 1 家 | **可取数** |
| 沪深两市融资净买入额 …每个交易日 | `融资买入额(合计)`，sourceCode `SUMFINBUYAMT` | 8.30e10 元 | **口径需修正**：妙想命中"融资买入额"而非"净买入"，需同时取**融资偿还额**（`SUMREPAYM`）并按同一日期相减 |
| 50ETF期权认沽认购比 …每个交易日 | —（返回"没有数据表"） | — | **未接通**，需换问法或另找来源 |

结论：`amount` / `turnover_rate` / `limit_up_count` / `limit_down_count` 可直接登记；
`margin_net_buy` 用"融资买入额 − 融资偿还额（同日）"合成，仍然遵守"两个输入必须同日、
不做前向填充"的既有口径；`pcr` / `implied_volatility` 暂标 **未接通**，
对应分量在算法里自动走"不可用"分支（方向指数按 5/6 覆盖度计算并标注）。

**落地实测（2026-09-19，`update_data.py --only …`）**：6 个字段各抓到 267 条（400 天回看窗口），
状态全部 `normal`，库中并集 1211 个交易日（2021-09-22 ~ 2026-09-18）。
用真实库数据跑 `compute_sentiment`：

- 参与度覆盖率 0.75（缺 `volume_ratio` 分量），方向覆盖率 0.667（`pcr`/`iv` 未接通），
  `data_quality = DQ_PARTIAL`，`unavailable_columns = ['pcr','implied_volatility']`；
- 那次运行时只有 **15 行**有 `participation_index`——因为当时代码把模式 B 与"满窗"绑在一起
  （267 − 252 = 15）。v0.3 已把 `min_periods` 解耦回规格的 120，同一份数据下
  **可用行数 147**、`sentiment_pct_252` 27 行、量比分量从第 1083 行（并集口径）加入。

**年限对照（默认 `min_periods=120`）**

| 目标 | 需要的最少交易日 | 约合 |
| --- | --- | --- |
| 模块开始出数（参与度/方向/状态） | 121 | 半年 |
| 量比分量加入（满 4 个参与度分量） | 140 | 7 个月 |
| `sentiment_pct_252` 开始出数 | 241 | 1 年 |
| 最近一年**每天**都有情绪读数 | 121 + 252 = 373 | 1.5 年 |
| 最近一年**每天**都有 `sentiment_pct` | 241 + 252 = 493 | 2 年 |

所以"回补 5 年"是我此前把满窗绑进口径后的过度要求；默认口径下 **2 年**已能支撑
"最近一年曲线 + 分位"，只想看最新读数则 400 天增量就够。

### 3.3 输出字段

**schema 由配置生成**：`output_columns(config) -> tuple[str, ...]`；
`DEFAULT_OUTPUT_COLUMNS = output_columns(SentimentConfig())`。
默认配置下的输出（`date` 为 `datetime64[ns]`，分类列为 string，布尔为 `bool`）：

固定字段（与分量尺度无关）：

| 字段 | 类型 | 取值/范围 | 说明 |
| --- | --- | --- | --- |
| `date` | datetime64 | 升序、唯一 | 交易日 |
| `participation_index` | float | 0—100 或 NaN | 参与度指数 |
| `direction_index` | float | 0—100 或 NaN | 方向指数 |
| `sentiment_index` | float | 0—100 或 NaN | `0.5×参与度 + 0.5×方向` |
| `sentiment_pct_{w}` | float | 0—100 或 NaN | 综合情绪自身分位（默认 `sentiment_pct_252`） |
| `sentiment_momentum_{m}d` | float | −100—100 或 NaN | 默认 `sentiment_momentum_20d` |
| `participation_momentum_{m}d` | float | 同上 | 参与度动量（扩展） |
| `direction_momentum_{m}d` | float | 同上 | 方向动量（扩展） |
| `state` | string | `STATE_*` 码值 | 四象限状态（中性码） |
| `state_label` | string | 中文文案 | 展示层映射结果（扩展） |
| `participation_band` / `direction_band` | string | `BAND_HIGH/MID/LOW/UNAVAILABLE` | 用于 2×2 网格（扩展） |
| `panic_score` | float | 0—100 或 NaN | 恐慌抛售得分（renormalize） |
| `panic_hits` | int | 0—5 或 NA | 命中条件数（扩展） |
| `panic_coverage` | float | 0—1 | 可评估条件数 / 5 |
| `panic_confidence` | float | 0—1 | `coverage × readiness` |
| `panic_reasons` | tuple[str,…] | 稳定顺序 | 命中的条件码 |
| `reversal_watch` | bool | True/False | 冰点反转观察信号（永不 NA） |
| `reversal_reasons` | tuple[str,…] | 稳定顺序 | 满足的条件码 |
| `reversal_block_reason` | string / None | `REV_BLOCK_*` | 未触发时的门控/缺失原因 |

分量派生字段（**列名随该分量的尺度配置变化**），命名规则：

| 分量方向 | 尺度列（每尺度一列） | 混合列 | 计数列 |
| --- | --- | --- | --- |
| direct（越高越偏多） | `{name}_pct_{window}`（始终输出） | `{name}_pct`（仅多尺度时输出） | `{name}_pct_n` |
| reverse（PCR / IV） | `{name}_reverse_pct_{window}`（仅多尺度时输出） | `{name}_reverse_pct`（始终输出） | `{name}_reverse_pct_n` |

该规则让默认配置的输出列名与规格书完全一致（direct 带窗口后缀、reverse 不带），
同时多尺度时不会丢信息。计数列为主尺度的当日有效样本数，供置信度与质检使用。
默认配置下即规格要求的：

`turnover_pct_252`、`amount_pct_252`、`volume_ratio_pct_252`、`margin_net_buy_pct_252`、
`return_20d_pct_252`、`macd_hist_norm_pct_252`、`limit_up_down_ratio_pct_252`、
`up_ratio_pct_252`、`pcr_reverse_pct`、`iv_reverse_pct`。

基础指标字段（与规格一致）：

`macd`（DIF）、`macd_signal`（DEA）、`macd_hist`、`macd_hist_norm`、`volume_ratio`、
`return_20d`、`up_ratio`、`down_ratio`、`limit_up_down_ratio`。

量价与质量字段：

`macd_volume_state`（码值）、`macd_volume_state_label`（扩展）、`macd_bullish_divergence`、
`macd_golden_cross`、`macd_dead_cross`、`days_since_golden_cross`、`volume_shrink_ratio`、
`participation_coverage`、`direction_coverage`、`participation_missing`、`direction_missing`、
`scale_partial_components`、`data_quality`（`DQ_*`）、`unavailable_columns`。

### 3.4 分量注册表（默认尺度与推荐尺度）

| 分量名 `name` | 输入列 / 派生 | 组 | 方向 `sign` | 规格默认尺度 | 推荐画像尺度（示意） | horizon | `lag` |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `turnover` | `turnover_rate` | 参与度 | direct | 252 (1.0) | 63 (0.4) + 252 (0.6) | mixed | 0 |
| `amount` | `amount` | 参与度 | direct | 252 (1.0) | 63 (0.4) + 252 (0.6) | mixed | 0 |
| `volume_ratio` | `volume` -> 量比 | 参与度 | direct | 252 (1.0) | 63 (0.5) + 252 (0.5) | mixed | 0 |
| `margin_net_buy` | `margin_net_buy` | 参与度 | direct | 252 (1.0) | 252 (1.0) | mid | 1 |
| `return` | `close` -> `return_{h}d` | 方向 | direct | 252 (1.0) | 63 (0.4) + 252 (0.6) | mixed | 0 |
| `macd_hist` | `close` -> `macd_hist_norm` | 方向 | direct | 252 (1.0) | 126 (0.5) + 252 (0.5) | mixed | 0 |
| `limit_up_down` | 涨跌停家数 -> 比值 | 方向 | direct | 252 (1.0) | 63 (0.6) + 252 (0.4) | mixed | 0 |
| `up_ratio` | 涨跌家数 -> 占比 | 方向 | direct | 252 (1.0) | 63 (0.5) + 252 (0.5) | mixed | 0 |
| `pcr` | `pcr` | 方向 | **reverse** | 252 (1.0) | 252 (1.0) | mid | 0 |
| `iv` | `implied_volatility` | 方向 | **reverse** | 252 (1.0) | 252 (1.0) | mid | 0 |

推荐画像属于**待评审的建议值**，不是默认值；默认一律为规格书的 252 单尺度。
切换画像只改配置、不改代码。反向分量的输出列名保留 `_reverse_pct` 后缀以示方向。

### 3.5 分位窗口选择（实测依据；默认仍为 252，待定）

用真实库数据（2025-07-01 ~ 2026-09-18，300 个交易日；两融按 `availability_lag=1`）跑四个画像，
看窗口长度到底换来什么、代价是什么：

| 画像 | 分位窗口 | 可用行 | 综合指数日变化 σ | 末行参与度 | 末行方向 | 末行状态 |
| --- | --- | --- | --- | --- | --- | --- |
| 规格默认 | 252 | 147 | 12.60 | 35.2 | 63.4 | 温和回暖 |
| 季度 | 63 | 237 | 12.19 | 46.4 | 68.3 | 中性震荡 |
| 混合（见下） | 21 / 63 | 257 | 13.85 | 70.6 | 71.8 | 贪婪/主升 |
| 一个月 | 21 | 257 | 14.60 | 73.8 | 67.9 | 贪婪/主升 |

读法：

- **窗口换的是参照系，不是精度**。参与度末行读数从 35（相对过去一年）变到 74（相对过去一个月），
  状态随之从"温和回暖"翻到"贪婪/主升"。这是设计选择，必须由口径决定，不能靠手感。
- **预热大幅缩短**：300 行样本里可用行数 147 -> 257，即"要 2 年数据"变成"要 1 个月数据"。
- **代价是噪声**：综合指数日变化 σ 从 12.19（63 日）升到 14.60（21 日），约 +20%；
  方向指数几乎不受影响（63~72），受影响的主要是参与度。

一个统计约束：`volume_ratio`（20 日均量）、`return_20d`（20 日收益）、`macd_hist_norm`
（EMA12/26/9，等效平滑期约 17~20 日）本身自带 20 日量级平滑，
在 21 日窗口里只剩 1~2 个互相独立的观测，分位更多反映"相邻两天的微小差别"而非"位置"。
因此提供两个画像：

| 画像 | 组成 | 说明 |
| --- | --- | --- |
| `PROFILE_FAST` | 全部分量 21 日 | 参照系统一（都是"相对过去一个月"），噪声最高 |
| `PROFILE_FAST_MIXED` | 未平滑分量 21 日；`volume_ratio`/`return_20d`/`macd_hist_norm` 用 63 日 | 保持统计有效性，噪声回落约 5% |

切换方式（不改代码）：

```python
from macroboard.sentiment.registry import PROFILE_FAST, PROFILE_FAST_MIXED, config_for_profile
frame = compute_sentiment(df, config_for_profile(PROFILE_FAST, percentile_window=21))
```

`percentile_window` 同时作用于分量分位与综合情绪自身的分位。
**默认值保持规格书的 252 单尺度**（2026-09-19 评审结论：先保留 252，窗口留作后期可调旋钮）。

### 3.6 窗口调整入口（三个层级，都不改算法代码）

**1) 一行常量（推荐，适合后期随时切换）**

```python
# macroboard/config.py
SENTIMENT_PERCENTILE_WINDOW: int | str = 252   # 21 / 63 / 126 / 252，或 '1个月' / '1个季度' / '半年' / '1年'
SENTIMENT_WINDOW_PROFILE: str = 'spec'         # 'spec' | 'mixed' | 'fast' | 'multiscale'
```

`analyze(df)` / `compute_sentiment(df)` 不传配置时就读这两个常量，
页面、脚本、测试走同一个入口，改完即生效（无需改调用方）。

**2) 一次函数调用（适合做敏感性实验）**

```python
from macroboard.sentiment import config_for_window
compute_sentiment(df, config_for_window('1个季度'))                 # 全部 63 日
compute_sentiment(df, config_for_window(21, profile='mixed'))       # 未平滑 21 日 + 平滑分量 63 日
```

**3) 逐分量配置（适合只调某一项）**

```python
from dataclasses import replace
from macroboard.sentiment import ScaleSpec, config_for_window
config = config_for_window(252)
components = tuple(
    replace(c, scales=(ScaleSpec(63),)) if c.name == 'turnover' else c
    for c in config.quality.components
)
config = config.replace(quality={'components': components})
```

切换窗口后需要同步三处，避免页面与文档口径不一致：

1. `docs/DATA_SOURCES.md` 的「A股情绪模块的分位口径」一节（窗口与 `min_periods` 的推导值）；
2. 页面标注（生效窗口 + 样本未满窗口时的「可用历史百分位」）；
3. 黄金值回归中的钉死值（`tests/test_sentiment_pipeline.py::test_pinned_values_for_synthetic_scenario`），
   因为窗口决定读数水平。

参考预热（默认 `min_periods = ceil(window × 120/252)`）：

| 窗口 | 分量分位起点 | 综合情绪自身分位起点 | 约需数据 |
| --- | --- | --- | --- |
| 21（1 个月） | 第 11 行 | 第 21 行 | 1 个月 |
| 63（1 个季度） | 第 31 行 | 第 61 行 | 1 个季度 |
| 126（半年） | 第 61 行 | 第 121 行 | 半年 |
| 252（1 年，默认） | 第 121 行 | 第 241 行 | 1 年 |

---

## 四、算法设计

### 4.0 总流程

```
输入校验 → 规范化（排序/去重/类型/无效值）→ 字段可用性画像
   → 基础指标（收益、MACD、量比、涨跌停比、家数占比）
   → 分量注册表遍历：每个分量按自己的尺度算分位（可多尺度加权）
   → 参与度指数 / 方向指数 / 综合情绪 / 各维度动量
   → 状态码 + 频带
   → 恐慌抛售得分（含 coverage / confidence）→ 冰点反转观察（含门控）
   → 量价状态 + 底背离
   → 边界钳制 + 可用性标注 + 展示层文案 → 输出
```

### 4.1 预处理

1. 校验 `config`（见 §6.4），非法配置直接 `ValueError`（不静默纠正）；
   同时校验注册表：分量名唯一、权重和为 1、尺度权重和为 1、窗口为正整数。
2. `date` 解析为 `datetime64[ns]`；无法解析 -> `ValueError`。
3. 按 `date` 升序**稳定排序**；重复日期按 `dup_policy`（默认 `last`）保留。
4. 数值列 `to_numeric(coerce)`，`inf/-inf -> NaN`。
5. 异常值：默认只做无效值处理，不做缩尾（分位本身是秩变换）。
   可选 `winsorize`：在**因果窗口**内按分位缩尾，默认关闭；开启时页面须标注"已缩尾"。
6. **不做前向填充**（默认 `missing_policy='nan'`）；可选白名单 ffill（带 `limit`），
   价格与家数字段禁止填充；任何填充都会写入 `DQ_PARTIAL` 并在页面标注。
7. 「交易日对齐」由上游负责：不重采样、不按自然日补齐；相邻行即相邻交易日。

### 4.2 基础指标

```
ema_fast   = EMA(close_state, span=macd_fast,  adjust=False)
ema_slow   = EMA(close_state, span=macd_slow,  adjust=False)
macd       = dif = ema_fast - ema_slow
macd_signal= dea = EMA(dif, span=macd_signal, adjust=False)
macd_hist  = 2 * (dif - dea)
macd_hist_norm = macd_hist / close * 100            # close<=0 -> NaN
return_{h}d = close / close.shift(h) - 1            # 默认 h ∈ return_horizons = (20,)
volume_ratio = volume / volume.rolling(volume_ma, min_periods=volume_ma).mean()
limit_up_down_ratio = limit_up_count / (limit_down_count + 1)
total = up_count + down_count
up_ratio = up_count / total; down_ratio = down_count / total   # total==0 -> NaN
```

**EMA 必须用 `adjust=False`**（递归式，与通达信/同花顺 MACD 一致；实测同输入下
`span=2` 分别为 2.714… 与 2.800…）。`MACD柱 = 2×(DIF−DEA)` 同为国内口径。

**价格缺口策略** `price_gap_policy='ffill_state'`（默认）：
EMA 递归用 `close.ffill(limit=price_gap_ffill_limit)` 作为状态输入，
  但 `close` 原本为 NaN 的行，输出的 `macd/macd_signal/macd_hist/macd_hist_norm` **一律置 NaN**
  （填充只影响递归状态，绝不发布填充值）。备选 `'nan_propagate'` 同样**不发布缺失行的数值**，
  差异只在缺失之后的 EMA 递归状态（pandas `ewm` 在 NaN 处会结转上一状态，故须由测试钉死：
  实测同一输入下两种策略在缺失日之后相差 0.07 量级）。
`warmup_bars`（默认 0）>0 时屏蔽种子未收敛的区间。

### 4.3 分量注册表与多尺度分位（核心变更）

```python
@dataclass(frozen=True, slots=True)
class ScaleSpec:
    window: int                      # 252 / 126 / 63 / 504 …
    weight: float = 1.0
    min_periods: int | None = None   # None -> ceil(window * min_periods_ratio)
    method: PercentileMethod | None = None   # None -> 全局 percentile_method

@dataclass(frozen=True, slots=True)
class ComponentSpec:
    name: str                        # 输出列前缀，如 'turnover'
    source: str                      # 输入列或派生量名，如 'turnover_rate'
    group: Literal['participation','direction']
    sign: Literal['direct','reverse']
    scales: tuple[ScaleSpec, ...]
    weight: float = 1.0              # 组内权重（默认等权）
    availability_lag: int = 0
    horizon: Literal['short','mid','long','mixed'] | None = None
```

计算规则：

```
对分量 c 的每个尺度 s：
    x_c   = 预处理后的输入（含 shift(availability_lag)）
    pct_cs, n_cs = rolling_percentile(x_c, window=s.window,
                                      min_periods=s.min_periods,
                                      method=s.method)         # 返回分位与有效样本数
    pct_cs = 100 - pct_cs   若 sign == 'reverse'

分量混合（多尺度）：
    可用尺度集合 A = {s : pct_cs 非 NaN}
    |A| / len(scales) < scale_min_coverage(默认 0.5)  ->  pct_c = NaN
    pct_c = Σ_{s∈A} (s.weight / Σ_{s∈A} s.weight) × pct_cs
    主尺度 primary(c) = 权重最大者（并列取 window 最大者）
    n_c = n_{c,primary}
    scale_partial(c) = (|A| < len(scales))
```

分位实现（沿用 v0.1 已实测的结论）：

- 模式 B `midrank_exclusive`（**默认**）：`(小于 + 0.5×等于) / 有效样本数`，
  样本取 t 严格之前（`[t-window, t-1]`）；`min_periods` 独立按比例推导
  （252 窗口 -> 120），需要满窗时显式配置。
  该式与 `rank(method='average')` 相差 `0.5/n`，故不用 `rank()` 实现，改用
  `numpy.lib.stride_tricks.sliding_window_view` 做 nan 感知的显式计数
  （取值永远不会触及下标 >= t 的元素），并由朴素三重循环参考实现在测试中逐点校验。
- 模式 A `rank_inclusive`（可选，规格书原始口径）：
  `series.rolling(window, min_periods).rank(method='max', pct=True) * 100`。
  已在 pandas 3.0.5 实测：`pct=True` 的分母是窗口内**有效样本数**
  （`[1, NaN, 3, 3]`、`window=3, min_periods=2` 末位为 100.0，按窗口长度应为 66.7）。

`min_periods` 推导：`ceil(window × min_periods_ratio)`，默认 `120/252`；
显式配置优先于推导值；`min_periods > window` 视为非法配置。

### 4.4 参与度 / 方向 / 综合情绪 / 动量

```
参与度分量 = [c for c in components if c.group == 'participation']   # 默认 4 个
方向分量   = [c for c in components if c.group == 'direction']       # 默认 6 个

index_group = 组内可用分量的加权平均（NaN 分量跳过）
coverage_group = 组内可用分量的权重和 / 组内权重总和
coverage_group < min_index_coverage(0.5) -> index = NaN（标注不可用）

sentiment_index = 0.5 × participation_index + 0.5 × direction_index
    index_missing='strict'（默认）：任一为 NaN -> 综合为 NaN
    index_missing='renormalize'：仅用可评估的一项（页面须标注"单项口径"）

sentiment_pct_{w}      = rolling_percentile(sentiment_index, 尺度 sentiment_percentile_scales)
*_momentum_{m}d        = index - index.shift(m)      # 三个维度各自独立配置 m，默认 20
```

等权为默认；若配置映射权重，则校验键集合与权重和。衍生品（`pcr` + `iv`）在方向内占 2/6，
折算到综合情绪为 1/6，符合"衍生品权重不宜过高"；若需进一步压低，
可调 `weight`（默认不改）。

#### 页面口径：暂未接通的分量直接从分母里剔除（2026-09-19 评审结论）

`pcr` 与 `iv` 的数据源尚未接通（见 `docs/DATA_SOURCES.md`），页面不再把它们报成"缺失"，
而是**不纳入合成**（`macroboard/sentiment_view.py::OPTIONAL_OFF_COMPONENTS`）。

- 数值等价：`compose_index` 本来就会按可用分量重归一，等权时
  "6 个分量里 2 个缺失" 与 "只定义 4 个分量" 的结果完全相同
  （实测方向指数 63.39285714285714 两种口径一致）；
- 好处：覆盖率常显 100%、`data_quality` 不再长期是 `DQ_PARTIAL`，页面也不再出现
  "未接通/缺失"字样；
- 接通后只要把 `pcr` / `iv` 从 `OPTIONAL_OFF_COMPONENTS` 里移除，方向指数会恢复为
  6 分量等权，算法层不需要改动；
- **算法层默认配置不受影响**：`DEFAULT_COMPONENTS` 仍包含 `pcr` / `iv`，
  `output_columns()` 仍产出 `pcr_reverse_pct` / `iv_reverse_pct`，
  规格书的列名与口径保持完整；页面只是换了一份组件清单。

### 4.5 状态（码值）

**严格不等式**，边界值（恰好 60/40）落入 `STATE_NEUTRAL`：

```
若 参与度 或 方向 为 NaN        -> STATE_UNAVAILABLE
否则 参与度 > 60 且 方向 > 60    -> STATE_GREED
否则 参与度 > 60 且 方向 < 40    -> STATE_PANIC
否则 参与度 < 40 且 方向 < 40    -> STATE_COLD
否则 参与度 < 40 且 方向 > 60    -> STATE_THAW
否则                            -> STATE_NEUTRAL
```

频带（用于真 2×2 网格）：按参与度阈值与方向阈值把两侧各切成
`BAND_HIGH` / `BAND_MID` / `BAND_LOW`，缺失为 `BAND_UNAVAILABLE`，
输出 `participation_band`、`direction_band` 两列。

### 4.6 恐慌抛售得分（renormalize + coverage + confidence）

| 条件码 | 规则 | 依赖 |
| --- | --- | --- |
| `PANIC_TURNOVER_PCT_HIGH` | 换手率分量分位 `turnover_pct > 80` | `turnover_rate` |
| `PANIC_RETURN_20D_LOW` | `return_20d < -0.08` | `close` |
| `PANIC_VOLUME_RATIO_HIGH` | `volume_ratio > 1.5` | `volume` |
| `PANIC_HIST_NEGATIVE_FALLING` | `hist < 0 且 hist < hist.shift(1)` | `close` |
| `PANIC_DOWN_RATIO_HIGH` | `down_ratio > 0.70` | `up_count`,`down_count` |

```
n_eval = 可评估条件数（依赖字段在 t 日有效；分位类条件额外要求该分量分位非 NaN）
n_hit  = 命中数

panic_score      = 100 × n_hit / n_eval        # n_eval >= 1，否则 NaN
panic_hits       = n_hit
panic_coverage   = n_eval / 5
readiness        = min over 分位类条件所用分量 ( min(1, n_c / min_periods_c) )
panic_confidence = panic_coverage × readiness   # 无分位类条件时 readiness = 1
panic_reasons    = 命中条件码（固定顺序 tuple）
panic_missing    = 不可评估条件码（固定顺序 tuple）
```

阈值语义：分位类条件使用该分量的**混合分位值** `{name}_pct`；
若用户把 `turnover` 改成 63+252 双尺度，则"80"指的是"在这两个窗口构成的位置上 > 80"，
页面必须显示该分量当前生效的尺度，避免读者按 252 单尺度理解。

### 4.7 冰点反转观察信号（含门控）

| 条件码 | 规则 |
| --- | --- |
| `REV_PANIC_SCORE` | `panic_score > 60` |
| `REV_HIST_SHRINKING` | `hist[t]<0 且 hist[t-1]<0 且 hist[t-2]<0 且 hist[t] > hist[t-1] > hist[t-2]` |
| `REV_VOLUME_SHRUNK` | `volume < (1 − reversal_volume_shrink) × volume.rolling(volume_ma).max()`（峰值窗口含当日） |
| `REV_DIRECTION_REBOUND` | `direction_index > direction_index.shift(direction_rebound_lag)`（默认 5） |

```
# 门控（本次评审新增，优先于条件判定）
若 panic_coverage < 0.6            -> reversal_watch = False
                                      reversal_block_reason = REV_BLOCK_PANIC_COVERAGE_LOW
若 panic_score 为 NaN              -> reversal_watch = False
                                      reversal_block_reason = REV_BLOCK_PANIC_SCORE_MISSING
否则若任一条件不可评估              -> reversal_watch = False
                                      reversal_block_reason = REV_BLOCK_CONDITION_DATA_MISSING
                                      reversal_missing 记录缺失条件码
否则                              -> reversal_watch = all(4 条为真)
                                      block_reason = None
reversal_reasons = 满足的条件码（tuple，固定顺序）
```

`reversal_watch` **恒为 True/False**（不输出 NA）：信号要么触发要么不触发，
数据不足时输出 False 并在 `reversal_block_reason` 说明原因。

### 4.8 量价状态与底背离（码值）

```
golden_cross_t = (macd > macd_signal) & (macd.shift(1) <= macd_signal.shift(1))
dead_cross_t   = (macd < macd_signal) & (macd.shift(1) >= macd_signal.shift(1))
divergence_t   = macd_bullish_divergence
```

优先级：**金叉/死叉（事件） > 底背离 > 中性**。

| 条件 | 码值 | 展示文案 |
| --- | --- | --- |
| 金叉 且 量比 > 1 | `MACD_VOL_GOLDEN_SURGE` | 放量金叉 |
| 金叉 且 量比 < 1 | `MACD_VOL_GOLDEN_SHRINK` | 缩量金叉 |
| 死叉 且 量比 > 1 | `MACD_VOL_DEAD_SURGE` | 放量死叉 |
| 死叉 且 量比 < 1 | `MACD_VOL_DEAD_SHRINK` | 缩量死叉 |
| 金叉 且 量比 == 1 或缺失 | `MACD_VOL_GOLDEN_UNKNOWN` | 金叉（量比不可用） |
| 死叉 且 量比 == 1 或缺失 | `MACD_VOL_DEAD_UNKNOWN` | 死叉（量比不可用） |
| 无交叉 且 底背离 | `MACD_VOL_DIVERGENCE` | 底背离 |
| 其余 | `MACD_VOL_NEUTRAL` | 中性 |

可选 `cross_latch_days`（默认 0 = 仅当日）；`days_since_golden_cross` 始终输出真实天数。

**底背离**（`divergence_mode='window_min'`，默认，严格按规格）：

```
price_new_low    = close[t] == close.rolling(divergence_lookback).min()[t]
hist_not_new_low = hist[t] > hist.rolling(divergence_lookback).min().shift(1)[t]
divergence       = price_new_low & hist_not_new_low
```

可选 `divergence_require_negative_hist`（默认 False）、`divergence_mode='swing_low'`（默认关闭）。

### 4.9 可用性与缺失处理

- 缺列（`on_missing_column='mark'`，默认）：不抛异常，按全 NaN 处理并记入 `unavailable_columns`；
  `'raise'` 时抛 `ValueError`。
- 列状态：`ok` / `missing` / `all_nan` / `insufficient_history` / `invalid_values`。
- 预热（`min_periods=120`）：252 尺度分量自第 121 行起有值（量比分量第 140 行），
  `sentiment_pct_252` 自第 241 行起有值；需要满窗时把 `min_periods` 设为窗口长度；
  预热期内相关列为 NaN 且 `data_quality` 标 `DQ_INSUFFICIENT`，不写"中性"以免被误读。
- 每个 0—100 指标输出前 `clip(0,100)` 并断言；NaN 保持 NaN。
- 空 DataFrame -> 返回带完整 schema 的空表；单行 / 全 NaN -> 指标 NaN + `STATE_UNAVAILABLE`
  + `DQ_INSUFFICIENT`。

### 4.10 防未来函数检查清单

1. 只用 `shift(+n)`、`rolling(center=False)`、`ewm(adjust=…)`、`expanding`；
   禁止负向 `shift`、`center=True`、`bfill`、`interpolate`、整段 `.mean()`。
2. 分位只看窗口内 t 及之前（模式 A）或严格早于 t（模式 B）。
3. 分量的 `availability_lag` 处理公告时点差（两融默认 1）。
4. 任何 `ffill` 只允许向后且带 `limit`。
5. 自动化验证：前缀不变性（T4）+ 未来污染（T5）+ 多尺度下的同两项复测（T38）。

---

## 五、伪代码

```text
function compute_sentiment(df, config=None):
    cfg = config or DEFAULT_CONFIG
    validate_config(cfg)                    # 含注册表校验，抛中文 ValueError
    if 'date' not in df.columns: raise ValueError(...)

    frame = normalize_input(df, cfg)
    if frame.empty: return empty_output(cfg)
    status = profile_columns(frame)

    # ---- 基础指标（§4.2）
    close = frame['close']; close_state = ffill_state(close, cfg)
    dif, dea = ema_pair(close_state, cfg)
    hist = 2*(dif-dea); hist_norm = hist/close*100
    if cfg.price_gap_policy=='ffill_state': mask(dif, dea, hist, hist_norm, where=close.isna())
    returns = {h: close/close.shift(h)-1 for h in cfg.return_horizons}
    volume_ratio = clean_volume(frame['volume'], cfg) / rolling_mean(volume, cfg.volume_ma)
    limit_up_down_ratio = where(valid(limit_up)&valid(limit_down),
                               limit_up/(limit_down+1), NaN)
    up_ratio, down_ratio = ratios(up, down)

    # ---- 分量注册表（§4.3）：每个分量按自己的尺度
    component_pct = {}          # name -> Series (0..100)
    component_meta = {}         # name -> {scales, primary, n, partial}
    for c in cfg.components:
        x = resolve_source(c, frame, returns, hist_norm, volume_ratio, limit_up_down_ratio, up_ratio)
        x = x.shift(c.availability_lag)
        per_scale = {}
        for s in c.scales:
            pct, n = rolling_percentile(x, s.window, s.min_periods or derive(s), s.method)
            if c.sign == 'reverse': pct = 100 - pct
            per_scale[s.window] = (pct, n)
        component_pct[c.name], component_meta[c.name] = blend_scales(per_scale, c, cfg)

    # ---- 合成（§4.4）
    participation_index, p_cov, p_missing = weighted_mean_available(
        cfg, group='participation', values=component_pct, min_cov=cfg.min_index_coverage)
    direction_index, d_cov, d_missing = weighted_mean_available(
        cfg, group='direction', values=component_pct, min_cov=cfg.min_index_coverage)
    sentiment_index = combine(participation_index, direction_index, cfg.index_missing)
    sentiment_pct = rolling_percentile(sentiment_index, cfg.sentiment_percentile_scales)
    momentum = {k: idx - idx.shift(cfg.momentum[k])
                for k, idx in (('participation', participation_index),
                               ('direction', direction_index),
                               ('sentiment', sentiment_index))}

    # ---- 状态 / 恐慌 / 反转 / 量价（§4.5—4.8）
    state = classify_state(participation_index, direction_index, cfg.thresholds)   # 返回码值
    bands = bands_of(participation_index, direction_index, cfg.thresholds)
    panic = score_panic(component_pct, component_meta, returns, volume_ratio, hist,
                        down_ratio, cfg)          # score/hits/coverage/confidence/reasons/missing
    reversal = watch_reversal(panic, hist, volume, direction_index, cfg)  # 含门控与 block_reason
    vol_state, divergence = macd_volume_state(dif, dea, volume_ratio, close, hist, cfg)

    out = assemble(...); clip_0_100(out)
    out = attach_labels(out, cfg.include_labels)   # 仅展示层，不参与计算
    return out

function rolling_percentile(series, window, min_periods, method):
    if method == 'midrank_exclusive':        # 默认：严格早于当日 + 中位秩（min_periods 由配置给）
        pct = explicit_midrank(series, window=window, min_periods=window)
    else:                                    # rank_inclusive：含当日，(<= x)/n
        pct = series.rolling(window, min_periods=min_periods).rank(method='max', pct=True) * 100
    n = series.rolling(window).count()       # 窗口内有效样本数（供 readiness / 质检）
    return pct, n

function blend_scales(per_scale, c, cfg):
    mat = DataFrame({w: pct for w, (pct, _) in per_scale.items()})
    wts = Series({s.window: s.weight for s in c.scales})
    avail = mat.notna()
    ratio = avail.mul(wts, axis=1).sum(axis=1) / wts.sum()
    value = (mat.fillna(0)*wts).sum(axis=1) / avail.mul(wts, axis=1).sum(axis=1).replace(0, NaN)
    value = value.where(ratio >= cfg.scale_min_coverage, NaN)
    partial = avail.sum(axis=1) < len(c.scales)
    primary = argmax_weight_then_window(c.scales)
    return value, {..., 'n': per_scale[primary].n, 'partial': partial}
```

---

## 六、接口定义

### 6.1 文件布局

```
macroboard/sentiment/
    __init__.py     # 公开 API 导出（compute_sentiment / analyze / sentiment_snapshot + 配置 + 码值）
    codes.py        # 纯英文码值常量（STATE_* / BAND_* / PANIC_* / REV_* / MACD_VOL_* / DQ_*）——无中文
    labels.py       # 展示层中文映射（STATE_LABELS / PANIC_REASON_LABELS / …）+ describe_state
    config.py       # ScaleSpec / ComponentSpec / SentimentConfig 及子配置（frozen dataclass）
    registry.py     # DEFAULT_COMPONENTS（规格默认）与 PROFILE_MULTISCALE_SUGGESTED（推荐画像）
    inputs.py       # 输入校验、规范化、可用性画像、AvailabilityReport
    features.py     # 基础指标 + rolling_percentile + 分量分位与多尺度混合
    scoring.py      # 指数合成 / 状态 / 恐慌 / 反转 / 量价状态
    pipeline.py     # analyze / compute_sentiment / sentiment_snapshot / output_columns
macroboard/sentiment_view.py    # 页面数据组装：DB 序列 -> 情绪输入、快照、曲线、口径说明
tests/test_sentiment_features.py
tests/test_sentiment_scoring.py
tests/test_sentiment_pipeline.py
tests/test_sentiment_labels.py
tests/test_sentiment_config.py
tests/test_sentiment_inputs.py
tests/test_sentiment_view.py    # 视图层：字段适配、预热、缺失、文案
tests/test_app_smoke.py         # 整页冒烟（AppTest，指向临时库）
# 黄金值回归直接在测试内用确定性生成器构造样本，不再落 CSV 夹具
```

算法层与展示层分离是硬约束：`codes.py`、`registry.py`、`features.py`、`scoring.py` 不含中文文案。

### 6.2 公开函数与常量

```python
REQUIRED_COLUMNS: tuple[str, ...] = ('date',)
SUPPORTED_COLUMNS: tuple[str, ...] = (
    'date', 'close', 'volume', 'amount', 'turnover_rate', 'margin_net_buy',
    'up_count', 'down_count', 'limit_up_count', 'limit_down_count',
    'pcr', 'implied_volatility',
)
def output_columns(config: SentimentConfig | None = None) -> tuple[str, ...]: ...
DEFAULT_OUTPUT_COLUMNS: tuple[str, ...] = output_columns()

def compute_sentiment(df: pd.DataFrame, config: SentimentConfig | None = None) -> pd.DataFrame: ...
def analyze(df: pd.DataFrame, config: SentimentConfig | None = None) -> SentimentResult: ...
def sentiment_snapshot(frame: pd.DataFrame) -> dict[str, object]: ...
```

```python
@dataclass(frozen=True, slots=True)
class SentimentResult:
    frame: pd.DataFrame
    availability: AvailabilityReport     # 每列状态 + unavailable 列表 + 覆盖率
    component_meta: Mapping[str, ComponentMeta]   # 生效尺度、主尺度、当日有效样本数、是否部分可用
    warnings: tuple[str, ...]
    as_of: pd.Timestamp | None
    config: SentimentConfig
    def as_dict(self) -> dict[str, object]: ...

@dataclass(frozen=True, slots=True)
class ColumnStatus:
    name: str
    status: Literal['ok','missing','all_nan','insufficient_history','invalid_values']
    valid_count: int
    first_valid: pd.Timestamp | None
    last_valid: pd.Timestamp | None
    note: str = ''
```

`sentiment_snapshot` 返回最后一个交易日的摘要（指标 + 状态码 + 中文文案 + 门控原因 + 生效尺度），
供 Streamlit 卡片直接消费；不包含任何建议性字段。

### 6.3 配置对象（默认值与规格书一致）

```python
PercentileMethod = Literal['rank_inclusive', 'midrank_exclusive']

@dataclass(frozen=True, slots=True)
class ScaleSpec:
    window: int
    weight: float = 1.0
    min_periods: int | None = None
    method: PercentileMethod | None = None

@dataclass(frozen=True, slots=True)
class ComponentSpec:
    name: str
    source: str
    group: Literal['participation','direction']
    sign: Literal['direct','reverse']
    scales: tuple[ScaleSpec, ...]
    weight: float = 1.0
    availability_lag: int = 0
    horizon: Literal['short','mid','long','mixed'] | None = None

@dataclass(frozen=True, slots=True)
class SentimentWindows:
    momentum: Mapping[str, int] = field(default_factory=lambda: {'sentiment': 20,
                                                                 'participation': 20,
                                                                 'direction': 20})
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    volume_ma: int = 20
    min_periods_ratio: float = 120 / 252           # 每个尺度按窗口推导 min_periods
    return_horizons: tuple[int, ...] = (20,)
    sentiment_percentile_scales: tuple[ScaleSpec, ...] = (ScaleSpec(252),)
    direction_rebound_lag: int = 5
    divergence_lookback: int = 20

@dataclass(frozen=True, slots=True)
class SentimentThresholds:
    high_participation: float = 60.0
    low_participation: float = 40.0
    high_direction: float = 60.0
    low_direction: float = 40.0
    panic_turnover_pct: float = 80.0
    panic_return_20d: float = -0.08
    panic_volume_ratio: float = 1.5
    panic_down_ratio: float = 0.70
    reversal_volume_shrink: float = 0.30
    reversal_panic_score: float = 60.0
    min_panic_coverage_for_reversal: float = 0.60    # 本次评审新增：门控阈值

@dataclass(frozen=True, slots=True)
class SentimentQuality:
    percentile_method: PercentileMethod = 'midrank_exclusive'   # D1：与页面股债利差分位同口径
    components: tuple[ComponentSpec, ...] = DEFAULT_COMPONENTS   # 规格默认：全部 252 单尺度
    scale_min_coverage: float = 0.5        # 多尺度：至少覆盖一半尺度权重
    min_index_coverage: float = 0.5
    index_missing: Literal['strict','renormalize'] = 'strict'
    price_gap_policy: Literal['ffill_state','nan_propagate'] = 'ffill_state'
    price_gap_ffill_limit: int | None = 20
    zero_volume_policy: Literal['nan','keep'] = 'nan'
    missing_policy: Literal['nan','ffill'] = 'nan'
    ffill_limit: int = 0
    ffill_whitelist: tuple[str, ...] = ()
    winsorize_enabled: bool = False
    winsorize_window: int = 252
    winsorize_lower_q: float = 0.01
    winsorize_upper_q: float = 0.99
    dup_policy: Literal['last','first','raise'] = 'last'
    cross_latch_days: int = 0
    divergence_require_negative_hist: bool = False
    warmup_bars: int = 0
    on_missing_column: Literal['mark','raise'] = 'mark'
    include_labels: bool = True            # 附带 *_label 列（展示用）

@dataclass(frozen=True, slots=True)
class SentimentConfig:
    windows: SentimentWindows = SentimentWindows()
    thresholds: SentimentThresholds = SentimentThresholds()
    quality: SentimentQuality = SentimentQuality()
    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> SentimentConfig: ...
    def to_dict(self) -> dict[str, object]: ...
    def replace(self, **kwargs: object) -> SentimentConfig: ...
```

### 6.4 校验契约（`validate_config`）

- 注册表：分量名唯一；组内权重之和 > 0；每个分量的尺度权重之和 > 0
  （权重只要求非负，使用时按总和归一化，因此全部填 1.0 即等权）；`scales` 非空；
  同一分量内 `window` 不重复；`sign` / `group` 取值合法；
- 派生 `min_periods = ceil(window × min_periods_ratio)`，且 `2 <= min_periods <= window`；
- `momentum` 各值 ≥ 1；`return_horizons` 各值 ≥ 1 且互不重复；
  `macd_fast < macd_slow`；`macd_signal ≥ 1`；`volume_ma ≥ 2`；`divergence_lookback ≥ 2`；
- `low_participation < high_participation`、`low_direction < high_direction`；
- `0 <= scale_min_coverage <= 1`、`0 <= min_index_coverage <= 1`、
  `0 <= min_panic_coverage_for_reversal <= 1`、`0 <= reversal_volume_shrink < 1`；
- `0 <= winsorize_lower_q < winsorize_upper_q <= 1`；`warmup_bars >= 0`；`cross_latch_days >= 0`；
- 非法 -> `ValueError`，消息用中文说明哪个参数、为什么非法。

### 6.5 展示层映射（`labels.py`，与算法层解耦）

```python
STATE_LABELS = {
    codes.STATE_GREED: '贪婪/主升',
    codes.STATE_PANIC: '恐慌抛售（放量急跌型冰点）',
    codes.STATE_COLD: '缩量阴跌（清淡型冰点）',
    codes.STATE_THAW: '温和回暖',
    codes.STATE_NEUTRAL: '中性震荡',
    codes.STATE_UNAVAILABLE: '不可用（数据不足）',
}
MACD_VOL_STATE_LABELS = {...}      # 放量金叉 / 缩量金叉 / 放量死叉 / 缩量死叉 / 底背离 / 中性 / …
PANIC_REASON_LABELS = {...}        # 每个条件码一句人话，附阈值数值
REV_REASON_LABELS = {...}
REV_BLOCK_REASON_LABELS = {...}    # 门控原因，页面必须展示，避免"没触发"被误读为"不恐慌"
DATA_QUALITY_LABELS = {'DQ_OK': '正常', 'DQ_PARTIAL': '部分指标不可用', 'DQ_INSUFFICIENT': '样本不足'}

def describe_state(code: str) -> str: ...
```

---

## 七、测试用例

### 7.1 手工可验证样例

**默认口径（模式 B：严格早于当日 + 中位秩，`min_periods=5`）**：
`x = [50, 30, 40, 20, 10, 60, 35]`，`window=5`（本表用满 5 个样本让数值好核对；
模块默认 `min_periods` 按 `ceil(window×120/252)` 推导，252 窗口即 120）：

| t | 样本（严格早于 t） | 有效样本 | < x_t | = x_t | 期望 |
| --- | --- | --- | --- | --- | --- |
| 0—4 | — | < 5 | — | — | NaN（未满窗） |
| 5（值 60） | [50,30,40,20,10] | 5 | 5 | 0 | 100.0 |
| 6（值 35） | [30,40,20,10,60] | 5 | 3 | 0 | 60.0 |

**可选口径（模式 A：含当日、`<=` 计数）**：`x = [50, 30, 40, 20, 10, 60]`，
`window=5, min_periods=3`，已用 pandas 实测：

| t | 窗口 | 有效样本 | `<= x_t` 个数 | 期望 |
| --- | --- | --- | --- | --- |
| 0 | [50] | 1 | — | NaN（< 3） |
| 1 | [50,30] | 2 | — | NaN（< 3） |
| 2 | [50,30,40] | 3 | 2 | 66.666… |
| 3 | [50,30,40,20] | 4 | 1 | 25.0 |
| 4 | [50,30,40,20,10] | 5 | 1 | 20.0 |
| 5 | [30,40,20,10,60] | 5 | 5 | 100.0 |

并列值口径差异（`x = [1,2,2,2,3]`，`window=5`，t=3，当日值 2，已实测）：
模式 A（含当日、`<=`）-> 窗口 `[1,2,2,2]`，`<=2` 有 4 个 / 4 = **100.0**；
模式 B（严格早于当日、中位秩）-> 样本 `[1,2,2]`，`(1 + 0.5×2)/3` = **66.666…**。

多尺度混合：`turnover` 配 `63 (0.4) + 252 (0.6)`，某日 63 日分位 = 90、252 日分位 = 60
-> 混合值 `= 0.4×90 + 0.6×60 = 72.0`；
若 63 日分位为 NaN -> 混合值 `= 60.0`（仅 252 日尺度），`scale_partial = True`，
且可用权重 0.6 >= `scale_min_coverage` 0.5 故仍然有效。

### 7.2 用例清单

| ID | 场景 | 期望 |
| --- | --- | --- |
| T1 | 手工样例 7.1（模式 B 默认 + 模式 A 可选，两张表） | 逐点等于表中期望 |
| T2 | `min_periods` 门槛：显式满窗时第 251 个样本前为 NaN、第 252 个样本起有值；默认 `min_periods=120` 时第 120 个样本起有值 | 通过 |
| T3 | 随机 600 行，与朴素三重循环参考实现比对（两种模式各自） | 逐点一致 |
| T4 | **前缀不变性**：`compute(df[:k])` vs `compute(df)[:k]`，k 属于 {1, 19, 120, 300, N} | 所有列（含 NaN 位置）完全一致 |
| T5 | **未来污染**：把 t>k 的行乘以 10 或置 NaN 后重算 | 前 k 行逐值不变 |
| T6 | MACD 手算（8 行小序列） | `adjust=False` 的 EMA、`hist = 2×(dif−dea)` 精确成立 |
| T7 | `close` 含 NaN（缺口） | `ffill_state` 下输出行仍 NaN，后续 EMA 连续；`nan_propagate` 行为与钉死期望一致 |
| T8 | `volume_ratio` 前 19 行 NaN，第 20 行 `= v/mean(v[0:20])` | 精确相等 |
| T9 | 0 除保护：`up+down == 0`、成交量均值 0、`close == 0` | NaN，无 inf / 无异常 |
| T10 | 参与度部分可用（仅 2 个分量） | 等于两者的加权均值，`participation_coverage` 正确，缺失项入 tuple |
| T11 | 参与度全部缺失 | NaN + `STATE_UNAVAILABLE` + `DQ_INSUFFICIENT` |
| T12 | 反向分量：PCR/IV 单调递增 | `*_reverse_pct` 单调递减且 ∈ [0,100] |
| T13 | 状态码：`(60,60) (40,40) (60.1,60.1) (60.1,39.9) (39.9,39.9) (39.9,60.1) (70,50)` | `NEUTRAL, NEUTRAL, GREED, PANIC, COLD, THAW, NEUTRAL` |
| T14 | 恐慌分单条件隔离（5 组） | 每组 `score == 100`、`hits == 1`、`coverage == 0.2`（renormalize 的直接体现） |
| T15 | 恐慌分 5 条全满足 | `score == 100`、`hits == 5`、`coverage == 1`、`confidence == 1`、5 个原因码顺序稳定 |
| T16 | 恐慌分 4 可评估、命中 2 | `score == 50`、`coverage == 0.8`、`confidence == 0.8 × readiness` |
| T17 | 恐慌分可评估数为 0 | `panic_score` NaN、`panic_hits` NA、`panic_coverage == 0` |
| T18 | 反转观察：4 条全满足且 coverage >= 0.6 | `reversal_watch is True`、4 个原因码、`reversal_block_reason is None` |
| T19 | 反转观察：逐条破坏（4 组） | 均为 False，原因码相应减少 |
| T20 | 缩量边界：`volume == 0.70×峰值` / `0.69×峰值` | 前者假、后者真（严格 `>` 30%） |
| T21 | 量价状态 4 种交叉 + 无交叉 + 量比缺失 | 4 个交叉码 / `MACD_VOL_NEUTRAL` / `MACD_VOL_GOLDEN_UNKNOWN` |
| T22 | 交叉与底背离同日 | 交叉码优先 |
| T23 | 底背离构造 | `macd_bullish_divergence is True`；价格与柱同创新低 -> False |
| T24 | **A股结构场景**：换手分位 95、20 日收益 −15%、方向 ≈ 20 | `STATE_PANIC`，`panic_score` 高，**不**判为 `STATE_GREED` |
| T25 | 缺列（无 PCR/IV/两融/涨跌停） | 不抛异常；`direction_coverage ≈ 3/6`；`unavailable_columns` 记录 |
| T26 | 列存在但全 NaN | 与缺列同处理，`ColumnStatus.status == 'all_nan'` |
| T27 | 空表 / 单行表 | 空：空 schema；单行：NaN + `STATE_UNAVAILABLE`，无异常 |
| T28 | 未排序 + 重复日期 | 升序；`dup_policy='last'` 保留末条；`'raise'` 抛错 |
| T29 | 数值边界：负数 / NaN / inf / 0 成交量 | 按 §3.1 置 NaN，无 inf 泄漏 |
| T30 | 越界断言：长随机序列全部 0—100 列 | `dropna` 后 ∈ [0,100] |
| T31 | 可复现性：跑两次 + 打乱输入行序 | 输出完全一致（数值、顺序、tuple 顺序） |
| T32 | schema 契约：实际列 == `output_columns(config)` | 顺序、dtype 完全一致 |
| T33 | 配置：阈值改 70/30 状态随动；`from_dict(to_dict())` 往返；非法配置抛错 | 通过 |
| T34 | 综合口径：只缺方向时 `strict` -> NaN；`renormalize` -> 单项并标注 | 通过 |
| T35 | 黄金值回归（默认配置，确定性合成数据） | 同输入两次结果完全一致；丢序输入结果一致；前缀不变性覆盖全部列；`participation_index` 首有效行 = 120、`sentiment_pct_252` 首有效行 = 240；末行参与度/方向/综合/恐慌分与钉死值一致（1e-9） |
| T36 | **分量级不同窗口**：`turnover` 用 252、`limit_up_down` 用 63 | 两列各自等于对应窗口分位；互不影响；综合指数按各自分位参与 |
| T37 | **`min_periods` 推导**：窗口 63 / 252 / 504 | `ceil(w×120/252)` = 30 / 120 / 240；显式配置优先；`>window` 抛错 |
| T38 | **多尺度混合**（含 T4/T5 复测） | 等于可用尺度加权均值；缺一尺度时重归一 + `scale_partial`；多尺度画像下前缀不变性与未来污染仍成立 |
| T39 | 多尺度边界：可用权重和 < `scale_min_coverage` | 该分量分位为 NaN，并计入 `scale_partial_components` |
| T40 | `panic_confidence` 定义 | 满覆盖且窗口充足 -> 1.0；`coverage=0.8`、`readiness=0.5` -> 0.4；对 coverage 与 readiness 均单调不减 |
| T41 | **门控**：`panic_coverage = 0.4`（< 0.6）且其余趋势条件满足 | `reversal_watch is False`，`reversal_block_reason == REV_BLOCK_PANIC_COVERAGE_LOW` |
| T42 | 门控对照：`panic_coverage = 0.6` 且 4 条满足 | `reversal_watch is True` |
| T43 | 门控：条件数据缺失但覆盖达标 | False + `REV_BLOCK_CONDITION_DATA_MISSING` + `reversal_missing` 记录缺失条件码 |
| T44 | **分层契约**：`codes.py` 源码不含中文（正则 `[\u4e00-\u9fff]`）；`STATE_CODES` 全部有 `STATE_LABELS` | 通过；缺映射即失败 |
| T45 | 状态码契约：`state` 只取 `STATE_CODES`；`state_label` 为中文；`include_labels=False` 时无 `*_label` 列 | 通过 |
| T46 | 嵌套预热：默认 `min_periods=120` 下分量自第 121 行、量比分量自第 140 行、`sentiment_pct_252` 自第 241 行起有值；指数初期覆盖率 0.5 且缺失清单可见 | 通过 |

### 7.3 验收标准映射

| 验收标准 | 覆盖用例 |
| --- | --- |
| 0—100 指标不越界 | T12, T14—T17, T29, T30 |
| 252 日分位正确、可手工验证 | T1, T2, T3 |
| t 日只依赖 t 日及之前 | T4, T5, T7, T38 |
| 缺失值 / 空数据 / 边界处理 | T7, T9, T10, T11, T17, T25—T29 |
| 示例数据可复现预期结果 | T31, T34, T35 |
| 恐慌与反转逻辑与规则一致 | T13—T23, T40—T43 |
| A股结构（不把换手率单当冷热） | T24 |
| 不同指标可配不同时间跨度 | T36—T39 |
| 算法层中性码 + 展示层文案 | T44, T45 |

### 7.4 测试数据说明

黄金值回归不再保存 CSV 夹具：合成样本由测试内的确定性生成器（正弦叠加 + 分段趋势，
不含随机数）构造，测试文件顶部标注 "SYNTHETIC FIXTURE — 非真实市场数据"。
这样做有两个原因：避免在仓库里留下看起来像行情的合成数据文件；避免把浮点哈希钉死，
依赖升级时回归失败但难以判断是算法变了还是数值库变了。
可复现性由"两次运行一致 + 丢序输入一致 + 前缀不变性 + 关键标量钉死"共同保证，
任何算法改动导致数值变化都会在这四项上暴露。

---

## 八、实施计划与依赖变更

| 阶段 | 内容 | 产出 | 状态 |
| --- | --- | --- | --- |
| P0 | 确认 D1（分位口径 = 模式 B）；在 `docs/DATA_SOURCES.md` 补登记 §3.2 的 6 个字段与可得时点 | 口径定稿 | **已完成**（口径已定、DATA_SOURCES 已登记、采集器已接线并跑通增量） |
| P1 | `codes.py` + `labels.py` + `config.py` + `registry.py` + `inputs.py` + `features.py` + 分位/基础指标/配置/输入/分层契约测试 | 分位与基础指标通过 | **已完成** |
| P2 | `scoring.py` + T13—T24、T40—T43、T45 | 指数、状态、恐慌、反转通过 | **已完成** |
| P3 | `pipeline.py` + `sentiment_snapshot` + `output_columns` + T25—T35 | 公开 API 与黄金值回归 | **已完成** |
| P4 | `app.py` 集成（含 DB -> 情绪模块的输入适配：`adv_count`/`dec_count` → `up_count`/`down_count`） | 页面可用 | **已完成** |

### 8.1 P1 实现状态

已落地的文件：

```
macroboard/sentiment/__init__.py   # 公开 API 导出
macroboard/sentiment/codes.py      # 中性码值（无中文，T44 机械校验）
macroboard/sentiment/labels.py     # 中文文案 + describe_state / label_series
macroboard/sentiment/config.py     # ScaleSpec / ComponentSpec / SentimentConfig + validate_config
macroboard/sentiment/registry.py   # DEFAULT_COMPONENTS（252 单尺度）+ PROFILE_MULTISCALE_SUGGESTED
macroboard/sentiment/inputs.py     # normalize_frame / profile_columns
macroboard/sentiment/features.py   # 两种分位口径、MACD、量比、收益、比值、多尺度混合、compute_features
tests/test_sentiment_features.py
tests/test_sentiment_config.py
tests/test_sentiment_inputs.py
tests/test_sentiment_labels.py
```

已覆盖的用例：T1—T9（分位手工样例、满窗门槛、参考实现交叉验证、前缀不变性、未来污染、
MACD 递归与缺失处理、量比、0 除保护）、T36—T39（分量级窗口、min_periods 推导、
多尺度混合与覆盖率门槛）、T44（分层契约），外加配置校验、输入规范化与展示映射的补充用例，
共 99 条。`requirements.txt` 已显式加入 `pandas==3.0.5`、`numpy==2.4.6`。

实现期确认的两个细节（已写回正文）：

1. 模式 B 用 `sliding_window_view` + 前置 NaN 填充实现，窗口恒为 `[t-window, t-1]`，
   因此 `min_periods` 只决定"样本够不够"，与"窗口取哪一段"无关（默认按比例给 120）；
2. `nan_propagate` 与 `ffill_state` 都**不发布**缺失行的数值，差异仅在递归状态
   （实测相差约 0.07），由 T7 钉死。

### 8.2 P2 实现状态

`macroboard/sentiment/scoring.py` 已落地：

| 函数 | 职责 |
| --- | --- |
| `compose_index` | 组内加权平均（权重按可用分量重归一）、覆盖率、长期/当日缺失清单 |
| `index_band` | 参与度/方向的高中低频带（真 2×2 网格用） |
| `classify_state` | 四象限状态码；边界值 60/40 落 `STATE_NEUTRAL`，缺失落 `STATE_UNAVAILABLE` |
| `panic_metrics` | renormalize 得分 + `panic_hits` + `panic_coverage` + `panic_confidence` + 原因/缺失码 |
| `reversal_metrics` | 4 条观察条件 + 门控（覆盖率、得分缺失、条件数据缺失）+ `volume_shrink_ratio` |
| `macd_volume_metrics` | 量价状态码（交叉 > 底背离 > 中性）、底背离三态布尔、金叉天数 |
| `score_all` | 一次算出全部指标列 + `participation/direction/sentiment` 三套动量 + `data_quality` |

新增一个码值 `MACD_VOL_UNAVAILABLE`：规格要求"字段缺失要标注不可用"，
若把缺数据的行写成"中性"会被读成"量价正常"，属于误导。

测试：T13—T24、T40—T43、T45 全部覆盖，另含 A股结构场景端到端用例
（高换手 + 急跌 + 放量 + 跌停潮 -> `STATE_PANIC`、`panic_score > 60`）、
预热期标注、越界断言、分数层前缀不变性、自定义配置随动。全仓 `unittest` 195 条通过，
`ruff check` 与 `py_compile` 全绿。

剩余工作：P3（`pipeline.py`：`output_columns` / `compute_sentiment` / `sentiment_snapshot`
/ 黄金文件 T25—T35）与 P0 的数据落地（按 §3.2.1 的实测结论补 `DATA_SOURCES.md` 并写采集器）。

### 8.3 P3 实现状态

`macroboard/sentiment/pipeline.py` 已落地：

| 入口 | 说明 |
| --- | --- |
| `output_columns(config)` | 由配置生成输出 schema（默认 67 列，顺序固定） |
| `analyze(df, config)` | 规范化 -> 可用性画像 -> 特征/分位 -> 打分 -> 组装，返回 `SentimentResult` |
| `compute_sentiment(df, config)` | 只要 DataFrame 的便捷入口 |
| `sentiment_snapshot(frame, config=None)` | 最新一日摘要（中文文案 + 门控原因 + 生效尺度），可直接喂卡片 |
| `SentimentResult.as_dict()` | JSON 友好摘要，便于缓存 |

实现期修正的两个细节：

1. `participation_missing` / `direction_missing` 改为**逐日**计算（原先写成"当前缺失清单"
   的常量列，会在前缀不变性测试里暴露为未来函数式的行为）；
2. `unavailable_columns` 保持为**运行级**标注（整列缺失才出现），因此前缀不变性测试
   对这一列单独放宽，并在文档里写明原因。

测试：T25—T35 全部覆盖（空表/单行/缺列/全 NaN 列/非法值/丢序/重复日期/越界/可复现性/
schema 契约/配置随动/快照），全仓 `unittest` 223 条通过，`ruff check` 与 `py_compile` 全绿。

### 8.4 P0 数据落地状态

| 内容 | 位置 | 状态 |
| --- | --- | --- |
| 来源与口径登记（含两融 T+1、同日相减、PCR/IV 未接通、换手率按总股本口径） | `docs/DATA_SOURCES.md` | 已完成 |
| 查询语句与解析（成交额/成交量/换手率/涨跌停家数/融资买入/偿还） | `macroboard/sources/mx.py` | 已完成 |
| 同日相减合成净买入（缺单侧整条丢弃，不做前向填充） | `mx.build_margin_net_buy` | 已完成 + 单测 |
| 指标注册与回看窗口 | `macroboard/config.py`（`SERIES` / `LOOKBACK_DAYS`） | 已完成 |
| 采集编排（单指标失败不影响其它指标；失败保留旧值） | `macroboard/collect.py`（`A股情绪输入` job） | 已完成 + 单测 |
| 真实采集 | `update_data.py --years 2 --only …`（2026-09-19） | 已跑通：6/6 成功，各 **486 条**（2 年） |
| 数据年限要求 | 模块 | 出数 121 天；`sentiment_pct` 241 天；"最近一年曲线+分位" 493 天（约 2 年） |
| 历史回补 | `update_data.py --years N` | 5 年**非必需**；情绪字段按 2 年回补（`--years 2 --only …`，约 21 次调用） |
| 年限可配 | `update_data.py --years N` + `collect(..., years=N)` | 一次覆盖所有指标的回补年限，增量更新不受影响 |

年限修正（v0.3）：`min_periods` 已从分位口径里解耦（规格本来就是 120），
因此 5 年回补不再是可用性的前置条件。400 天增量（已完成）已能让真实数据出数，
当前同一份数据下可产出 147 行情绪读数；若要页面画出完整一年曲线，再补 2 年历史。

### 8.5 P4 页面集成状态

| 内容 | 位置 | 说明 |
| --- | --- | --- |
| DB -> 情绪输入适配 | `macroboard/sentiment_view.py::build_input_frame` | `hs300_close`→`close`、`adv_count`/`dec_count`→`up_count`/`down_count`，按日期并集对齐，不重采样、不前向填充 |
| 页面数据组装 | `macroboard/sentiment_view.py::build_sentiment_view` | 产出快照、三条曲线、逐字段数据日期/状态、未接通字段与口径说明；数据不足时返回「样本不足」而不是"中性" |
| 图表支持 | `macroboard/charts.py` | `ChartSpec` 新增 `hline_values`（情绪曲线标 40/60 阈值线）与 `y_range` 参数；新增 `SENTIMENT_SPECS` |
| 页面区块 | `app.py::render_sentiment_section` | 位于页面**「短线指标：A股市场情绪」**区块（默认时间范围 1 年，与长线宏观与估值区块的控件互不联动；「A股市场宽度」区块当前由 `app.py::SHOW_BREADTH_BLOCK` 暂时下线）：**四象限状态卡片（状态 + 参与度/方向频带 + 阈值说明）+ 四象限散点图** + 三个指数卡片 + 恐慌分/反转观察/量价状态 + 三条曲线 + 可折叠口径说明（含逐输入字段数据日期） |
| 四象限图 | `macroboard/charts.py::build_quadrant_figure` | 横轴参与度、纵轴方向（0—100），四个象限底色 + 40/60 阈值线；**只画最近 10 个交易日**，相邻点用箭头连接（过去 → 未来，颜色由浅到深），标注首末日期并高亮最新点；只画真实观测，不插值 |
| 数据库路径可覆盖 | `app.py::resolve_db_path` | 环境变量 `MACROBOARD_DB` 优先，便于测试与指向另一份库 |
| 整页冒烟 | `tests/test_app_smoke.py` | 用 Streamlit `AppTest` 在进程内跑整页：有数据与空库两种情况都要求无异常，并断言情绪区块已接线 |

真实库验证（2 年回补后 486 条）：页面显示**四象限状态「温和回暖」（参与度低 × 方向高）**、
综合情绪 49.4/100、参与度 35.3、方向 63.4、恐慌分 0/100（命中 0/5、覆盖率与置信度 100%）、
反转观察未触发、量价状态中性、分量覆盖率 100%（`pcr`/`iv` 已按 §4.4 的页面口径排除，
不再显示为缺失）；四象限图显示最近 10 个交易日（2026-09-07 → 2026-09-18）的箭头路径。

**2 年回补后的真实库结果**（2026-09-19 执行 `--years 2`，情绪字段各 486 条）：

| 指标 | 数值 |
| --- | --- |
| 有情绪读数的交易日 | **366 天**（原 147 天），曲线起点 2025-03-24 |
| 综合情绪 / 参与度 / 方向 | 49.4 / 35.3 / 63.4（温和回暖） |
| 情绪自身分位 | 48.4% |
| 恐慌抛售得分 | 0/100（命中 0/5，覆盖率 100%，置信度 100%） |
| 覆盖率 | 参与度 100%、方向 67%（`pcr`/`iv` 未接通） |
| 方向曲线起点 | 2022-04-25（方向分量用的是 5 年历史，长于参与度） |

换手率口径已在 2026-09-19 确认为**总股本**（分母为总股本，非自由流通市值），
`docs/DATA_SOURCES.md`、`macroboard/config.py` 与本节已同步；
总股本口径数值通常低于自由流通口径，因此不要与该口径之外的其他换手率序列直接比较。

测试：`unittest` 268 条通过（新增 16 条视图层 + 2 条整页冒烟），`ruff check` 与 `py_compile` 全绿。

依赖：`requirements.txt` 需显式加入 `pandas==3.0.5`、`numpy==2.4.6`
（当前仅通过 streamlit 传递依赖引入，`constraints.txt` 已有同版本锁定）。
`AGENTS.md` 的校验命令无需改动，`unittest discover` 会自动收集新测试。
