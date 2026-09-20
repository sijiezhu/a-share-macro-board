# 部署（用户级 systemd）

这套 unit 把看板做成两个常驻/定时服务，**不需要 root**（用户级 systemd + linger）：

| 单元 | 作用 |
| --- | --- |
| `macroboard-web.service` | Streamlit 页面，监听 `0.0.0.0:8501`（局域网可访问），崩溃自动拉起 |
| `macroboard-update.service` | 单次数据采集（`update_data.py --quiet`） |
| `macroboard-update.timer` | 每天 08:30（Asia/Shanghai）触发上面那个 service，错过的会在开机后补跑 |

## 安装

```bash
mkdir -p ~/.config/systemd/user
install -m 644 deploy/macroboard-web.service deploy/macroboard-update.service \
    deploy/macroboard-update.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now macroboard-update.timer macroboard-web.service
```

前提：`~/.config/systemd/user` 之外无特殊要求；`loginctl show-user "$USER" | grep Linger`
应显示 `Linger=yes`（否则注销后用户服务会被停掉，可用
`sudo loginctl enable-linger "$USER"` 打开）。

## 核对

```bash
systemctl --user status macroboard-web.service macroboard-update.timer --no-pager
systemctl --user list-timers macroboard-update.timer --no-pager
curl -sS -o /dev/null -w '%{http_code}\n' http://192.168.1.199:8501/      # 期望 200
curl -sS http://192.168.1.199:8501/_stcore/health                         # 期望 ok
journalctl --user -u macroboard-web -n 30 --no-pager
journalctl --user -u macroboard-update -n 30 --no-pager

# 手动触发一次采集（与定时任务走同一条路径，会消耗妙想调用次数）
systemctl --user start macroboard-update.service
```

## 局域网访问（重要）

### 先澄清一个常见误解

`192.168.1.0/24` **不能作为监听地址**：`--server.address` 只接受本机自己拥有的 IP，
CIDR 是"网段"而不是"本机地址"。想收窄暴露面，能选的是下面三种：

| 取值 | 含义 | 暴露面 |
| --- | --- | --- |
| `127.0.0.1` | 只监听 loopback | 仅本机（局域网访问会 connection refused） |
| `192.168.1.199` | 只监听这张网卡的地址 | 只有从 `192.168.1.0/24` 可达；本机 loopback 不再响应 |
| `0.0.0.0` | 监听所有接口 | 含未来新增的 VPN / docker 网桥等，最宽 |

**所以"绑具体网卡 IP"确实比 `0.0.0.0` 稍窄**——两者在同一网段内可达性一样，
差别在于以后这台机多出别的网络接口时，`0.0.0.0` 会顺带把它们也暴露。
真正的"按来源网段过滤"要靠防火墙（见下），不是靠绑定地址。

### 当前部署取值

`--server.address 0.0.0.0`（2026-09-19 由用户确认局域网可信后切换）。

- 本机 `http://127.0.0.1:8501`、局域网 `http://192.168.1.199:8501` 均实测 HTTP 200，
  `/_stcore/health` 返回 `ok`；
- 选择 `0.0.0.0` 而不是绑具体网卡 IP 的原因：本机地址由 DHCP 下发
  （`default via 192.168.1.1 dev enp1s0 proto dhcp`），绑具体 IP 在租约变化后会启动失败；
- ⚠️ Streamlit 启动日志会打印 `External URL: http://125.33.50.239:8501`（它探测到的公网 IP）。
  **这本身不代表公网可达**，但只要路由器做了 8501 端口映射、启用了 UPnP 或把本机设为 DMZ，
  看板就会真的暴露到公网。请确认路由器上没有这些设置——这一步只能在路由器侧检查。

### 暴露面：行情数据只读

页面没有登录鉴权，同网段任何设备都能看到库里全部数据（全部指标历史）。
**会写入/消耗配额的动作已默认关闭**：手动「立即采集一次」按钮需要显式设置
`Environment=MACROBOARD_ENABLE_MANUAL_REFRESH=1` 才会出现（默认关闭，见 `app.py`），
日常采集由每天 08:30 的 timer 触发。

页面自身唯一的写入是本机访问统计（页脚显示的人次与匿名独立访客），写在
`data/visits.sqlite3`（与行情库分开；存 `SHA-256(盐 + IP + User-Agent)` 指纹、
客户端 IP 原文与访问时刻，User-Agent 不存原文），不修改任何行情观测值。
要保持页面完全不写入，
给本单元加 `Environment=MACROBOARD_VISIT_LOG=0`（页面显示「访问统计：已关闭」）。

`MX_APIKEY` 只在进程环境里，页面没有任何接口能读它。

### 方案 A：SSH 隧道（推荐，零暴露，无需改服务）

在你自己的电脑上执行（保持这个窗口开着）：

```bash
ssh -N -L 8501:127.0.0.1:8501 user@192.168.1.199
```

然后在**自己电脑**的浏览器打开 `http://127.0.0.1:8501`。
流量全程走 SSH 加密，服务端不需要监听局域网，也不需要在服务器上装任何东西。

### 方案 B：反向代理 + 基础认证（需要你安装软件）

服务器上没有 nginx/caddy，且安装需要 root。装好后用这类配置把 8501 包一层：

```caddy
http://:8080 {
    basicauth { <用户名> <bcrypt-hash> }
    reverse_proxy 127.0.0.1:8501
}
```

注意 Streamlit 依赖 WebSocket，反代需支持 `Upgrade`（caddy 的 `reverse_proxy` 默认支持）。

### 方案 C：绑本机网卡 IP（当前采用）

```bash
P=~/.config/systemd/user/macroboard-web.service
sed -i 's/--server.address .*/--server.address 192.168.1.199 \\/' "$P"
systemctl --user daemon-reload && systemctl --user restart macroboard-web.service
curl -sS -o /dev/null -w '%{http_code}\n' http://192.168.1.199:8501/      # 期望 200
curl -sS http://192.168.1.199:8501/_stcore/health                          # 期望 ok
```

回退：把地址改回 `127.0.0.1` 再 `daemon-reload && restart`。
不要在公共 Wi-Fi、宿舍网、办公网使用，也不要在没有 TLS 的情况下映射到公网。

### 方案 C+：绑 `0.0.0.0`（对 IP 变化免疫）

```bash
sed -i 's/--server.address .*/--server.address 0.0.0.0 \\/' ~/.config/systemd/user/macroboard-web.service
systemctl --user daemon-reload && systemctl --user restart macroboard-web.service
```

好处是 DHCP 换 IP 也不会起不来；代价是监听所有接口（含将来新增的虚拟网卡）。

### 方案 D：绑 `0.0.0.0` + 防火墙只放行自己的设备

```bash
sudo ufw allow from 192.168.1.<你的设备IP> to any port 8501 proto tcp
sudo ufw deny 8501/tcp
```

需要 root 权限；比方案 C 窄，但仍是无鉴权访问。

如果只是**按来源网段过滤**（保留 `0.0.0.0` 对 IP 变化免疫的优点，又不想对整个网段开放），
正确的是防火墙而不是绑定地址（同样需要 root）：

```bash
sudo ufw deny 8501/tcp
sudo ufw allow from 192.168.1.0/24 to any port 8501 proto tcp   # 只放行本网段
# 或只放行单台设备：
# sudo ufw allow from 192.168.1.66 to any port 8501 proto tcp
```

nftables 等价写法：
`nft add rule inet filter input tcp dport 8501 ip saddr != 192.168.1.0/24 drop`。

注意防火墙只限制**来源**，页面本身仍没有身份校验：被放行的网段内，设备之间不区分身份。

## 换数据库 / 换端口

- 数据库路径：unit 里加一行 `Environment=MACROBOARD_DB=%h/.../other.sqlite3`；
- 端口：改 `--server.port`，与反向代理配置保持一致。

## 卸载

```bash
systemctl --user disable --now macroboard-update.timer macroboard-web.service
rm ~/.config/systemd/user/macroboard-{web,update}.service ~/.config/systemd/user/macroboard-update.timer
systemctl --user daemon-reload
```
