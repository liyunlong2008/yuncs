# VPS Ubuntu (2C1G 新加坡) 部署

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
#   [api] host = "127.0.0.1"         # 只绑本机，公网访问靠 nginx

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

## 4. nginx 反代看板（带 Basic Auth）

```bash
sudo apt install -y nginx apache2-utils
sudo htpasswd -c /etc/nginx/.htpasswd yuncs   # 设置看板密码
sudo cp deploy/yuncs.nginx /etc/nginx/sites-available/yuncs
sudo ln -s /etc/nginx/sites-available/yuncs /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

> 看板默认只绑 127.0.0.1:8000，**不要裸奔公网**；域名在 `deploy/yuncs.nginx` 里替换。
> 无域名可跳过 nginx，用 SSH 隧道本地看：`ssh -L 8000:127.0.0.1:8000 user@vps`

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
