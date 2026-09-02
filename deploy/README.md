# VPS Ubuntu (2C1G 新加坡) 部署

## 0. 一键部署（推荐）

在全新 Ubuntu VPS（root）上一条命令完成：装 uv → 拉仓库 → 依赖+测试 → 生成纸盘配置 →
可选填 OKX key → systemd 常驻 → 可选装 Caddy 看板（8765 + Basic Auth，密码交互设置）。

```bash
curl -fsSL https://raw.githubusercontent.com/liyunlong2008/yuncs/master/deploy/install.sh -o install.sh && bash install.sh
```

- 重复执行 = 拉最新代码并重启服务（配置和密钥保留不动）
- 脚本会提示是否填 key / 是否装 Caddy，全部可回车跳过
- 装完后：看板 `http://VPS_IP:8765`（记得安全组放行 8765）；日志 `journalctl -u yuncs -f`

以下手动步骤供排障或自定义路径时使用（脚本做的事与 1~4 步等价）。

## 1. 安装依赖

```bash
# Python 运行时交给 uv 管理
curl -LsSf https://astral.sh/uv/install.sh | sh
# 重启 shell 后
cd ~/yuncs
uv sync --frozen          # 用 lock 文件安装，不联网解析
```

## 2. 配置

```bash
cp config.example.toml config.toml
# 修改 config.toml：
#   [exchange] mode = "paper"（先纸盘验证）或 "live"
#   [exchange] proxy = ""            # VPS 直连，不要填本地代理
#   [exchange] feed = "auto"         # 直连会走 WebSocket
#   [api] host = "127.0.0.1"         # 只绑本机，公网访问靠 caddy 反代（8765）

# secrets.toml 单独上传（不提交 git）：
scp secrets.toml user@vps:~/yuncs/secrets.toml
#   [okx] api_key / api_secret / passphrase（实盘模式必须，开交易权限）
```

## 3. systemd 常驻

```bash
sudo cp deploy/yuncs@.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now yuncs@$(whoami)   # 以当前用户身份运行
# 常用命令
sudo systemctl status yuncs@$(whoami)         # 看状态
journalctl -u yuncs@$(whoami) -f              # 看日志（loguru 同时落 logs/）
sudo systemctl restart yuncs@$(whoami)        # 改配置后重启
```

`yuncs@.service` 要点：模板单元以 `%i`（用户名）定位工作目录、`Restart=always`、
`RestartSec=5`、日志走 journald + loguru 文件双通道、512M 内存上限。

## 4. Caddy 反代看板（Basic Auth，对外端口 8765）

```bash
sudo apt install -y caddy
caddy hash-password            # 输入两次密码，复制输出的哈希
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile
sudo nano /etc/caddy/Caddyfile # 把 <密码哈希> 替换为上一步输出
sudo systemctl reload caddy
```

- 看板访问：`http://VPS_IP:8765`，用户名 yuncs + 你设的密码
- 云防火墙/安全组放行 8765/tcp；机器人本身始终只绑 127.0.0.1:8000，**不直接暴露**
- 有域名时把 Caddyfile 的 `:8765` 换成 `your.domain.com`，Caddy 自动签发 HTTPS
- 无域名不配 Caddy 也可用 SSH 隧道本地看：`ssh -L 8000:127.0.0.1:8000 user@vps`

## 5. 升级

```bash
cd ~/yuncs && git pull && uv sync --frozen && sudo systemctl restart yuncs
```

## 6. 运维注意

- **先纸盘跑至少几轮**，确认轮次结束行为（动态出局线触发/超时 → 自动开新一轮）符合预期，再切实盘
- 实盘用 OKX 子账户 + 单独 API key + 提币白名单，避免主账户风险；`initial_balance=0` 时实盘每轮自动用实际余额
- 每天看一次 `journalctl -u yuncs` 与 `data/bot.db` 的权益曲线
- 紧急停止：`sudo systemctl stop yuncs`（进程退出前会平仓结算当前轮）；
  或 `curl -X POST http://127.0.0.1:8000/api/kill`（优雅平仓）
