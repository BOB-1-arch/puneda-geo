#!/usr/bin/env bash
# 一键部署脚本：在一台全新的 Ubuntu/Debian 云服务器（用WebShell以root登录）上，
# 装好依赖、拉代码、配置systemd服务并启动，让服务开机自启、崩溃自动重启。
#
# 用法（在服务器的网页版终端里，两条命令）：
#   curl -fsSL -o setup.sh https://raw.githubusercontent.com/bob-1-arch/puneda-geo/main/deploy/setup.sh
#   bash setup.sh sk-你的真实DeepSeekKey
#
# 重复运行是安全的：会拉最新代码、重建虚拟环境、重启服务，不会重复安装出问题。

set -euo pipefail

DEEPSEEK_API_KEY="${1:-}"
if [ -z "$DEEPSEEK_API_KEY" ]; then
  echo "用法: bash setup.sh <你的DeepSeek API Key>"
  echo "例如: bash setup.sh sk-abcdef1234567890"
  exit 1
fi

if [ "$(id -u)" -ne 0 ]; then
  echo "请用 root 用户运行（阿里云/腾讯云网页终端默认登录就是root）。"
  exit 1
fi

APP_DIR=/opt/puneda-geo
REPO_URL=https://github.com/bob-1-arch/puneda-geo.git
SERVICE_NAME=puneda-geo

echo "==> [1/5] 安装系统依赖（python3 / git）"
apt-get update -y
apt-get install -y python3 python3-venv python3-pip git

echo "==> [2/5] 拉取代码到 $APP_DIR"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" fetch origin main
  git -C "$APP_DIR" reset --hard origin/main
else
  git clone "$REPO_URL" "$APP_DIR"
fi

echo "==> [3/5] 配置Python虚拟环境并安装依赖"
cd "$APP_DIR"
python3 -m venv venv
"$APP_DIR/venv/bin/pip" install --upgrade pip
"$APP_DIR/venv/bin/pip" install -r requirements.txt

echo "==> [4/5] 写入 .env（只有本机root可读）"
cat > "$APP_DIR/.env" <<EOF
DEEPSEEK_API_KEY=$DEEPSEEK_API_KEY
EOF
chmod 600 "$APP_DIR/.env"

echo "==> [5/5] 安装并启动systemd服务（开机自启+崩溃自动重启）"
cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=PUNEDA GEO Intelligence System
After=network.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$APP_DIR/venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"

PUBLIC_IP="$(curl -fsSL --max-time 3 ifconfig.me || echo "<你服务器的公网IP>")"

echo ""
echo "=================================================="
echo "部署完成！服务已启动，并设置为开机自启、崩溃自动重启。"
echo ""
echo "浏览器打开：http://${PUBLIC_IP}:8000"
echo ""
echo "如果打不开，最常见原因是云服务商控制台里的『防火墙』/『安全组』"
echo "还没放行 8000 端口（TCP），去控制台该服务器的防火墙设置里加一条"
echo "放行规则：端口 8000，协议 TCP，来源 0.0.0.0/0。"
echo ""
echo "常用维护命令："
echo "  查看运行状态：systemctl status ${SERVICE_NAME}"
echo "  查看实时日志：journalctl -u ${SERVICE_NAME} -f"
echo "  重启服务：    systemctl restart ${SERVICE_NAME}"
echo "  以后要更新代码，重新运行这个脚本（$0）即可。"
echo "=================================================="
