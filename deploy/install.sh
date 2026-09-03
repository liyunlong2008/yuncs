#!/usr/bin/env bash
# yuncs 一键部署（Ubuntu，root 目录，纸盘模式）
# 用法：curl -fsSL https://raw.githubusercontent.com/liyunlong2008/yuncs/master/deploy/install.sh -o install.sh && bash install.sh
# 重复执行 = 拉最新代码并重启服务（幂等）。
set -euo pipefail

REPO="https://github.com/liyunlong2008/yuncs.git"
INSTALL_DIR="${INSTALL_DIR:-/root/yuncs}"
DASH_PORT=8765

log()  { echo -e "\033[1;32m==>\033[0m $*"; }
warn() { echo -e "\033[1;33m !!\033[0m $*"; }

# ---------- 0. 前置 ----------
if [ "$(id -u)" != "0" ]; then warn "建议用 root 执行（脚本按 /root/yuncs 部署）"; fi
export DEBIAN_FRONTEND=noninteractive
# 新 VPS 首启常见 unattended-upgrades 占 apt 锁：停掉并收尾，apt 等待上限 180s
systemctl stop unattended-upgrades 2>/dev/null || true
dpkg --configure -a 2>/dev/null || true
apt_quiet() { apt-get -o DPkg::Lock::Timeout=180 "$@"; }
apt_quiet update -qq
apt_quiet install -y -qq curl git >/dev/null

# ---------- 1. uv ----------
if ! command -v uv >/dev/null 2>&1; then
    log "安装 uv"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
else
    log "uv 已安装: $(uv --version)"
fi

# ---------- 2. 代码（存在则更新） ----------
if [ -d "$INSTALL_DIR/.git" ]; then
    log "更新代码 $INSTALL_DIR"
    git -C "$INSTALL_DIR" pull --ff-only
else
    log "克隆仓库到 $INSTALL_DIR"
    git clone "$REPO" "$INSTALL_DIR"
fi
cd "$INSTALL_DIR"

# ---------- 3. 依赖 ----------
log "安装 Python 3.12 + 依赖"
uv python install 3.12
uv sync --frozen
log "跑测试确认"
uv run pytest -q

# ---------- 4. 配置 ----------
if [ ! -f config.toml ]; then
    log "生成 config.toml（纸盘，无代理，初始 20U）"
    cp config.example.toml config.toml
    sed -i 's/^initial_balance = 0.*$/initial_balance = 20/' config.toml
else
    log "config.toml 已存在，保持不动"
fi

# ---------- 5. 密钥（可选，跳过则费率用默认值） ----------
if [ ! -f secrets.toml ]; then
    read -rp "填写 OKX API key？[y/N] " yn
    if [ "$yn" = "y" ] || [ "$yn" = "Y" ]; then
        read -rp "  api_key: " K
        read -rp "  api_secret: " S
        read -rp "  passphrase: " P
        printf '[okx]\napi_key = "%s"\napi_secret = "%s"\npassphrase = "%s"\n' "$K" "$S" "$P" > secrets.toml
        chmod 600 secrets.toml
        log "secrets.toml 已写入"
    else
        cp secrets.example.toml secrets.toml
        log "跳过（纸盘不需要 key，费率用默认 0.05%/0.02%）"
    fi
else
    log "secrets.toml 已存在，保持不动"
fi

# ---------- 6. systemd ----------
log "安装 systemd 服务"
sed "s|WorkingDirectory=/home/%i/yuncs|WorkingDirectory=$INSTALL_DIR|;
     s|ExecStart=/home/%i/.local/bin/uv|ExecStart=$HOME/.local/bin/uv|;
     s|User=%i|User=$(id -un)|" deploy/yuncs@.service > /etc/systemd/system/yuncs.service
systemctl daemon-reload
systemctl enable --now yuncs 2>/dev/null || systemctl restart yuncs
sleep 25
systemctl is-active --quiet yuncs && log "yuncs 运行中" || { warn "yuncs 未运行，看日志：journalctl -u yuncs -n 50"; exit 1; }
log "最近日志："
journalctl -u yuncs -n 5 --no-pager | sed 's/^/    /'

# ---------- 7. Caddy 看板（可选） ----------
read -rp "安装/更新 Caddy 看板（对外端口 $DASH_PORT + Basic Auth）？[Y/n] " yn
if [ "$yn" != "n" ] && [ "$yn" != "N" ]; then
    # 从 Caddy 官方源装最新版（Ubuntu 仓库的包太老，2.6 没有 basic_auth 指令）
    if ! caddy --version 2>/dev/null | grep -qE '^v?2\.(1[0-9]|[89])'; then
        apt_quiet install -y -qq debian-keyring debian-archive-keyring apt-transport-https >/dev/null
        curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --batch --yes --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
        curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
        apt_quiet update -qq
    fi
    apt_quiet install -y -qq caddy >/dev/null
    while true; do
        read -srp "设置看板密码: " PW1; echo
        read -srp "再输一次: " PW2; echo
        [ "$PW1" = "$PW2" ] && [ -n "$PW1" ] && break
        warn "两次不一致或为空，重试"
    done
    HASH=$(caddy hash-password --plaintext "$PW1")
    sed "s|:8765|:$DASH_PORT|; s|<密码哈希>|$HASH|" deploy/Caddyfile > /etc/caddy/Caddyfile
    # Caddy <2.8 的 Basic Auth 指令名是 basicauth（无下划线）
    CADDY_MAJOR=$(caddy version | cut -d. -f1,2)
    if [ "$(printf '%s\n' "2.8" "$CADDY_MAJOR" | sort -V | head -1)" = "2.8" ]; then
        sed -i 's/basic_auth/basicauth/' /etc/caddy/Caddyfile
    fi
    systemctl reload caddy 2>/dev/null || systemctl restart caddy
    systemctl is-active --quiet caddy && log "caddy 运行中" || { warn "caddy 启动失败：journalctl -u caddy -n 20"; exit 1; }
    IP=$(curl -s -4 ifconfig.me || echo "<VPS_IP>")
    log "看板: http://$IP:$DASH_PORT （用户名 yuncs）"
    warn "记得在云安全组放行 $DASH_PORT/tcp"
    log "看板: http://$IP:$DASH_PORT （用户名 yuncs）"
    warn "记得在云安全组放行 $DASH_PORT/tcp"
else
    log "跳过 Caddy（本地隧道看板：ssh -L 8000:127.0.0.1:8000 root@<VPS_IP>）"
fi

log "完成。常用命令：journalctl -u yuncs -f | systemctl restart yuncs | systemctl stop yuncs"
