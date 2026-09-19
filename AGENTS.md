# Consignes agents IA — A股投资信息看板

本项目是个人使用的宏观/估值观察看板。任何对外说明都必须遵守下面的口径。

## 必须说明

- 数据来源与字段以 `docs/DATA_SOURCES.md` 为准，引用时一并给出该文件；
- 股债利差公式：`spread = 1 / PE_TTM - 中国10年期国债收益率`，收益率按小数计算；
- 百分位口径：过去 5 年、严格早于当前数据日期的有效样本，
  `100 ×（小于 + 0.5 × 等于）÷ 样本数`，样本 < 252 时不显示；
- 不同市场的数据日期可以不同，页面按指标分别标注数据日期；
- 曲线默认「一个视图内统一分辨率」（1年日线 / 3年周线 / 5年月线），不把不同分辨率
  混在同一线性时间轴上；重采样只取区间内最后一个真实观测，不平均、不插值；
  抽稀只影响绘图，股债利差百分位始终用全部有效样本计算；
- 已知限制：中债官网直连未接通（改用东财镜像并已交叉验证）、
  在岸人民币使用日线收盘而非官方 16:30 收盘价、妙想接口存在短时限流。

## 不可以做

- 不把股债利差百分位说成未来上涨概率，也不作为买卖信号；
- 不输出买卖指令、目标价或仓位建议；
- 不把 PE 百分位当作股债利差百分位；
- 不用前向填充或插值制造利差样本，不计算两个输入不同日期的利差；
- 不在缺少来源说明的情况下更换统计口径（如用国开债替代国债、
  用中间价或离岸 CNH 替代在岸即期、用期货价替代现货金价）；
- 不使用模拟数据冒充真实数据；未接通就标注「未接通」；
- 不声称定时任务已经上线（除非确实部署并验证过）。

## 校验

```bash
PYTHONPYCACHEPREFIX=/tmp/macroboard_pycache .venv/bin/python -m py_compile app.py update_data.py scripts/verify_samples.py macroboard/*.py macroboard/sources/*.py
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/ruff check .
.venv/bin/python scripts/verify_samples.py      # 需联网
```
