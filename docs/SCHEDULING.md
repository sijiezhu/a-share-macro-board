# 定时采集与页面常驻配置

> 当前状态（2026-09-19 实测）：**已在本机以用户级 systemd 部署并验证**。
> - `macroboard-web.service`：active，监听 `0.0.0.0:8501`（本机与局域网都可访问）；
>   `curl http://192.168.1.199:8501/` 返回 200、`/_stcore/health` 返回 `ok`；
>   页面无登录鉴权：同网段设备可查看全部数据（只读）；
>   手动「立即采集一次」按钮已默认关闭（需 `MACROBOARD_ENABLE_MANUAL_REFRESH=1` 才出现）；
>   启动日志会打印 `External URL`（Streamlit 探测到的公网 IP），
>   请确认路由器没有做 8501 端口映射 / UPnP / DMZ，详见 `deploy/README.md`；
> - `macroboard-update.timer`：enabled，2026-09-23 起为每天 **15:01 与 20:30**（Asia/Shanghai）
>   各触发一次（此前仅 15:01，更早为 08:30；改时间后需 `systemctl --user daemon-reload` 才生效）；
>   - 15:01：收盘后尽早取数，让当日读数尽快出现在页面上；
>   - 20:30：补采一次。源站对涨跌停家数等序列的发布晚于 15:01，只跑 15:01 时库里会先落下
>     一行「部分字段有值」的最新记录，指数读数要到次日才补齐（2026-09-23 实测：15:01 采集
>     报「成功 17/17」，但多数序列的最新日期仍停在上一交易日）；
>   - 两次跑的是同一个幂等作业，重复执行只是覆盖同一批观测，不会写重复数据；
> - `macroboard-update.service`：已由 timer 触发跑过一次，退出码 0，日志「成功 15/15」；
> - `loginctl show-user` 的 `Linger=yes`，注销后用户级服务仍继续运行。
>
> 仓库里的可直接安装版本在 `deploy/`（unit 文件 + 安装/核对/卸载说明）。
> 下面保留 cron 与 systemd 两种示例，供换机器或改用系统级服务时参考。

采集与页面展示是分开的：`update_data.py` 负责抓取并写入 SQLite，
`app.py` 只读数据库。定时任务只需调用前者。

> 下面示例里的 `/path/to/a-share-macro-board` 请替换成本机项目目录；
> systemd 部分使用 `%h`（用户主目录）以免写死绝对路径。

## 1. cron（每天北京时间 15:01 与 20:30）

服务器时区为 Asia/Shanghai 时：

```cron
1 15 * * * cd /path/to/a-share-macro-board && set -a && . ./.env && set +a && .venv/bin/python update_data.py >> logs/update_data.log 2>&1
30 20 * * * cd /path/to/a-share-macro-board && set -a && . ./.env && set +a && .venv/bin/python update_data.py >> logs/update_data.log 2>&1
```

服务器时区不是北京时间时，用 `CRON_TZ` 明确指定：

```cron
CRON_TZ=Asia/Shanghai
1 15 * * * cd /path/to/a-share-macro-board && set -a && . ./.env && set +a && .venv/bin/python update_data.py >> logs/update_data.log 2>&1
30 20 * * * cd /path/to/a-share-macro-board && set -a && . ./.env && set +a && .venv/bin/python update_data.py >> logs/update_data.log 2>&1
```

注意：

- cron 不加载交互式 shell 的变量，因此必须在命令行里 `set -a && . ./.env && set +a`。
- `update_data.py` 在任一指标失败时返回退出码 1，便于 cron 邮件或其他监控捕获；
  失败不会影响页面，页面继续显示上一成功值并标注「更新失败」。
- 单轮耗时含失败重试预算：国内源失败重试量级为秒级；现货黄金走海外链路（Cloudflare），
  单独配了 5 次尝试 + `(8, 20)` 秒分离超时，**连接型失败最坏约 60 秒、读取型最坏约 120 秒**。
  看到采集停在这一步一两分钟属正常重试，不是卡死；两次采集（15:01 / 20:30）互为兜底。
- 首次部署需要先手动执行一次 `python update_data.py --full` 补齐历史。

## 2. systemd timer（等价方案）

`~/.config/systemd/user/macroboard-update.service`：

```ini
[Unit]
Description=A股投资信息看板 数据采集

[Service]
Type=oneshot
WorkingDirectory=%h/a-share-macro-board
EnvironmentFile=%h/a-share-macro-board/.env
ExecStart=%h/a-share-macro-board/.venv/bin/python update_data.py
```

`~/.config/systemd/user/macroboard-update.timer`：

```ini
[Unit]
Description=每天 15:01 与 20:30 (Asia/Shanghai) 采集

[Timer]
OnCalendar=Mon..Sun 15:01 Asia/Shanghai
OnCalendar=Mon..Sun 20:30 Asia/Shanghai
Persistent=true

[Install]
WantedBy=timers.target
```

启用：

```bash
systemctl --user daemon-reload
systemctl --user enable --now macroboard-update.timer
systemctl --user list-timers macroboard-update.timer
```

## 3. 页面常驻

```bash
cd /path/to/a-share-macro-board
set -a; . ./.env; set +a
.venv/bin/streamlit run app.py --server.port 8501 --server.address 0.0.0.0
```

如需开机自启与崩溃拉起，可再补一个 `macroboard-web.service`（Type=simple，
ExecStart 同上），并 `systemctl --user enable --now macroboard-web.service`。

注意：`--server.address 0.0.0.0` 会把页面暴露到局域网，只在本机查看时可改为
`--server.address 127.0.0.1`。对公网开放必须自行加 TLS 与反向代理。

## 4. 本次实际部署（仓库内 `deploy/`）

本机采用的是用户级 systemd（无需 root，已确认 `Linger=yes`）：

```bash
mkdir -p ~/.config/systemd/user
install -m 644 deploy/macroboard-web.service deploy/macroboard-update.service \
    deploy/macroboard-update.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now macroboard-update.timer macroboard-web.service
```

核对与运维命令：

```bash
systemctl --user status macroboard-web.service macroboard-update.timer --no-pager
systemctl --user list-timers macroboard-update.timer --no-pager
curl -sS -o /dev/null -w '%{http_code}\n' http://192.168.1.199:8501/  # 期望 200
curl -sS http://192.168.1.199:8501/_stcore/health                     # 期望 ok
journalctl --user -u macroboard-web -n 30 --no-pager                # 页面日志
journalctl --user -u macroboard-update -n 30 --no-pager             # 采集日志
systemctl --user restart macroboard-web.service                     # 重启页面
systemctl --user start macroboard-update.service                    # 手动补采一次
```

> **改完 Python 代码一定要 `restart macroboard-web.service`。**
> Python 不会重新导入已加载的模块（`sys.modules` 缓存），Streamlit 也只重跑 `app.py`，
> 而 `--server.fileWatcherType none` 关掉了自动重载。不重启的话页面会继续用
> 服务启动那一刻的旧代码，且页面内部自洽、不报错——只是数值是旧口径的。
> 完整说明与排查方法见 `deploy/README.md` 的「改完代码必须重启页面服务」。

当前监听 `192.168.1.199`（只在本机网卡上监听）。注意 `192.168.1.0/24` 这类
**CIDR 不能作为监听地址**：`--server.address` 只接受本机 IP，按来源网段过滤要用防火墙。
三种取值与取舍见 `deploy/README.md`「局域网访问」；
对公网开放前必须自行加反向代理、TLS 与访问控制。

回补历史（不影响定时任务，二者写同一个库）：

```bash
.venv/bin/python update_data.py --years 2 --only amount --only volume --only turnover_rate \
  --only margin_net_buy --only margin_turnover --only limit_up_count --only limit_down_count
```
