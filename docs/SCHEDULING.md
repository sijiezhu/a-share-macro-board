# 定时采集配置示例

> 说明：本文只提供配置示例。截止到本次交付，**定时任务尚未在服务器上实际部署**，
> 页面上的「最近采集时间」反映的是手动执行 `update_data.py` 的结果。

采集与页面展示是分开的：`update_data.py` 负责抓取并写入 SQLite，
`app.py` 只读数据库。定时任务只需调用前者。

> 下面示例里的 `/path/to/a-share-macro-board` 请替换成本机项目目录；
> systemd 部分使用 `%h`（用户主目录）以免写死绝对路径。

## 1. cron（每天北京时间 08:30）

服务器时区为 Asia/Shanghai 时：

```cron
30 8 * * * cd /path/to/a-share-macro-board && set -a && . ./.env && set +a && .venv/bin/python update_data.py >> logs/update_data.log 2>&1
```

服务器时区不是北京时间时，用 `CRON_TZ` 明确指定：

```cron
CRON_TZ=Asia/Shanghai
30 8 * * * cd /path/to/a-share-macro-board && set -a && . ./.env && set +a && .venv/bin/python update_data.py >> logs/update_data.log 2>&1
```

注意：

- cron 不加载交互式 shell 的变量，因此必须在命令行里 `set -a && . ./.env && set +a`。
- `update_data.py` 在任一指标失败时返回退出码 1，便于 cron 邮件或其他监控捕获；
  失败不会影响页面，页面继续显示上一成功值并标注「更新失败」。
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
Description=每天 08:30 (Asia/Shanghai) 采集

[Timer]
OnCalendar=Mon..Sun 08:30 Asia/Shanghai
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
