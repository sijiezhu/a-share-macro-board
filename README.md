# A股投资信息看板

个人用的中文单页看板：展示美国/中国 10 年期国债收益率、美元兑人民币在岸汇率、
现货黄金参考价、A股市场情绪参考，以及**沪深300股债利差与百分位**。

用于宏观和估值观察：**不做自动交易，不输出买卖指令，不给出预测**。

## 核心链路

```
真实数据获取 → 本地 SQLite 保存 → 利差与百分位计算 → 页面展示 → 定时更新（配置见 docs/SCHEDULING.md）
```

- `update_data.py`：采集入口，写入 `data/macroboard.sqlite3`（幂等，重复执行不产生重复记录）。
- `app.py`：唯一页面，只读行情数据库，不发网络请求（页脚的访问统计只写本机的
  `visits.sqlite3`，与行情库分开）；手动「立即采集一次」按钮默认关闭
  （需要时设 `MACROBOARD_ENABLE_MANUAL_REFRESH=1` 才出现），采集统一由定时任务负责。
- `macroboard/`：采集、持久化、计算与页面数据组装。
- `macroboard/sentiment/`：A股市场情绪指标算法（参与度 / 方向 / 综合情绪 / 四象限状态 /
  恐慌抛售得分 / 冰点反转观察 / 量价状态），设计与口径见 `docs/SENTIMENT_ALGORITHM.md`。
- `docs/DATA_SOURCES.md`：来源、接口、字段、权限、历史覆盖、验证结果与已知限制。
- `docs/SCHEDULING.md`：每天北京时间 08:30 的 cron / systemd timer 示例。

## 安装

需要 Python 3.11 ~ 3.14。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --requirement requirements-dev.txt
```

## 配置

复制配置示例并填入妙想 API Key（其余数据源无需凭证）：

```bash
cp .env.example .env
chmod 600 .env
$EDITOR .env          # MX_APIKEY=...
```

`.env` 已被 `.gitignore` 忽略；凭证只通过环境变量提供，不写入代码与日志。
没有 `MX_APIKEY` 时，A股情绪与沪深300相关指标会明确显示「未接通（缺少授权）」，
其余指标照常更新。

## 首次采集与启动

```bash
set -a; . ./.env; set +a
.venv/bin/python update_data.py --full     # 首次：补齐历史（估值/国债 6 年，其余 5 年）
.venv/bin/streamlit run app.py              # 页面
```

### 作为常驻服务运行（推荐）

仓库里的 `deploy/` 提供用户级 systemd 单元（无需 root）：页面常驻 + 每天
08:30（Asia/Shanghai）自动采集，崩溃自动拉起，开机自启。

```bash
mkdir -p ~/.config/systemd/user
install -m 644 deploy/macroboard-web.service deploy/macroboard-update.service \
    deploy/macroboard-update.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now macroboard-update.timer macroboard-web.service

systemctl --user status macroboard-web.service --no-pager
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8501/   # 期望 200
```

监听地址三选一（详见 `deploy/README.md`「局域网访问」）：
`127.0.0.1`（仅本机）、`192.168.1.199`（只在本机网卡上监听）、
`0.0.0.0`（所有接口，当前采用，对 DHCP 换 IP 免疫）。注意 **CIDR 不能作为监听地址**，
按来源网段过滤要用防火墙。

页面没有登录鉴权：端口对谁开放，谁就能看到库里的全部数据。页面不改行情数据，
只把访问计数写进本机的 `data/visits.sqlite3`（不含 IP 与 User-Agent 原文，
可用 `MACROBOARD_VISIT_LOG=0` 关闭）；会触发采集的按钮已默认关闭。
对公网开放必须自行加反向代理、TLS 与访问控制。

之后增量更新（页面打开不会重新下载历史）：

```bash
.venv/bin/python update_data.py
```

常用参数：`--full` 补齐历史、`--only cn10y --only hs300_pe_ttm` 只更新指定指标、
`--db 路径` 指定数据库、`--quiet` 只输出一行总结。退出码 1 表示有指标更新失败。

## 测试与自检

```bash
.venv/bin/python -m unittest discover -s tests -v      # 单元测试
.venv/bin/ruff check .                                  # 静态检查
.venv/bin/python scripts/verify_samples.py              # 抽查历史日期，需联网
```

抽查脚本会独立重取来源数据，与本地库对比 PE、国债收益率与利差计算结果。

## 页面内容

- 顶部：最近采集时间与数据异常提示（采集成功不代表所有指标都已更新）。
- 页面按观察周期分区块，**短线在上、长线在下**：短线 A股市场情绪、长线宏观与估值；
  每个区块各有自己的「时间范围」（1个月/3个月/1年/3年/5年/全部）与「曲线粒度」控件，
  调整其中一组不会影响其他组。
- **短线指标：A股市场情绪**（默认 1 年）：A股市场情绪参考卡片、四象限状态、
  综合情绪 / 参与度 / 方向三条曲线（0—100，标出 40/60 四象限边界）、
  恐慌抛售得分及其覆盖率与置信度、冰点反转观察及门控原因、量价状态与底背离。
  区块内可展开查看构造公式、分位窗口、逐输入字段的数据日期与状态、以及未接通字段。
  情绪指标只做状态描述：不预测涨跌、不输出买卖指令、不给出目标价或仓位建议。
- **短线指标：A股市场宽度，暂时下线**（`app.py::SHOW_BREADTH_BLOCK = False`）：原区块含
  A股市场情绪参考卡片与沪深A股上涨家数占比（当日涨跌扩散度）、沪深300近20个交易日涨跌幅
  （约 1 个月动量）两条日频短线曲线，默认 1 个月窗口。数据仍在采集，把开关改回 `True`
  即可恢复，不影响其他区块。
- **长线指标：宏观经济与估值**（默认 5 年）：美国10年期、中债10年期、美元兑人民币
  （在岸即期）、现货黄金四条宏观曲线与对应卡片，以及股债利差曲线、当前百分位、
  样本区间与样本数、计算所用的 PE-TTM 与 10 年期国债收益率、可折叠的公式与口径说明。
- 卡片随分组展示：最新有效值、数据日期、来源、状态，以及相对上一有效观测的变化
  （收益率与利差用 BP，价格与汇率用百分比，比例指标用百分点）。
- 页脚：访问统计（今日 / 近 7 日 / 累计访问人次与匿名独立访客，可展开看口径与近 14 天明细）。

页面读取的数据库可以用环境变量覆盖（默认 `data/macroboard.sqlite3`）：

```bash
MACROBOARD_DB=/path/to/other.sqlite3 .venv/bin/streamlit run app.py
```

情绪指标的窗口默认是 252 个交易日（1 年）；要换参照系只改 `macroboard/config.py` 里的一行
`SENTIMENT_PERCENTILE_WINDOW`（21 / 63 / 126 / 252，或 `'1个月'` / `'1个季度'` / `'半年'` / `'1年'`），
不需要改算法或调用方，详见 `docs/SENTIMENT_ALGORITHM.md` §3.6。

### 曲线粒度

默认「自动（按范围）」：**一个视图内只使用一种分辨率**，让时间轴与数据密度匹配——
1 个月 / 3 个月 / 1 年看日线（约 21 / 63 / 250 点）、3 年看周线（约 156 点）、
5 年/全部看月线（约 60 点）。

不把日线/周线/月线混在同一张线性轴上：那样近 90 天的日度点会被压缩到右侧约 5% 宽度，
线条疏密不均，反而更难读。需要看极端细节时可切「近密远疏」（图上会标出
日线→周线→月线的切换位置）或「原始日度」。

所有粒度都只保留区间内**最后一个真实观测**（不平均、不插值），图上点均可追溯到来源数值。
抽稀只影响绘图；股债利差百分位始终使用全部有效样本计算。

## 访问统计（页面人次）

页脚显示「今日 / 近 7 日 / 累计访问人次」与对应的匿名独立访客数，展开可看近 14 天明细。
统计完全在本机完成，不联网、不使用第三方统计服务。

- **人次**：一次页面会话（打开或刷新页面）计 1 人次；会话内切换时间范围、缩放图表
  不会重复计数。
- **独立访客**：按 `SHA-256(随机盐 + 客户端 IP + User-Agent)` 前 16 位匿名指纹去重；
  指纹相同只代表「同一 IP + 同一浏览器」，不是身份识别，页面也不做登录与鉴权。
  User-Agent 只以指纹形式参与，不保存原文。
- **访客 IP**：每条记录同时把客户端 IP 原文写进同一个统计库（`visits.ip`），
  **页面暂不显示**，需要核对时直接查库（`SELECT ip, COUNT(*) FROM visits GROUP BY ip;`）；
  本机/回环访问记为空（NULL）。IP 会一直留存，删除统计文件即清除。
- **存放位置**：与行情库分开的独立文件（默认 `data/visits.sqlite3`，与 `MACROBOARD_DB`
  同目录），因此页面对行情库仍然只读；删除该文件即清空统计。
- **已知限制**：WebSocket 断开或服务重启会新建会话，同一个人可能被计成多次；
  换浏览器或换设备会被算成不同访客，同一台机器分别用 `127.0.0.1` 与局域网 IP
  打开也会算成两个访客。自然日按北京时间划分。

```bash
MACROBOARD_VISIT_LOG=0 .venv/bin/streamlit run app.py          # 关闭统计
MACROBOARD_VISIT_DB=/path/to/visits.sqlite3 .venv/bin/streamlit run app.py   # 换统计文件
```

口径与字段见 [`docs/DATA_SOURCES.md`](docs/DATA_SOURCES.md)
「访问统计（页面人次，非行情数据）」一节。

## 口径要点

```
spread = 1 / PE_TTM - 中国10年期国债收益率
```

- 收益率按小数计算：PE=12.5、国债收益率=2.00% → 6.00 个百分点 / 600 BP。
- 两个输入必须来自同一数据日期，取最近共同有效日期；不填充、不插值。
- 百分位默认过去 5 年、严格早于当前日期，用未舍入数值计算；样本 < 252 标注「样本不足」。
- 历史不足 5 年时标注「可用历史百分位」，不称为「5年百分位」。
- 这里算的是股债利差的百分位，不是 PE 的百分位。

**提醒：历史百分位不代表未来上涨概率，也不是买卖信号。**

## 免责声明

本项目仅用于个人宏观与估值观察，不构成投资建议，不提供自动交易能力。
数据来自公开免费来源，可能存在延迟、修订或中断；使用前请阅读
[`docs/DATA_SOURCES.md`](docs/DATA_SOURCES.md) 中的来源与限制说明。
