#!/bin/bash
# ═══════════════════════════════════════════════════════════════
# Crypto Dashboard — VPS Setup Script
# Ubuntu 22.04 / 24.04
# Run as root or with sudo
# ═══════════════════════════════════════════════════════════════

set -e

echo "▶ Installing Docker..."
apt-get update -qq
apt-get install -y ca-certificates curl gnupg lsb-release

mkdir -p /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg

echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
  > /etc/apt/sources.list.d/docker.list

apt-get update -qq
apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin

systemctl enable docker
systemctl start docker

echo "▶ Docker installed: $(docker --version)"

# ── Project setup ─────────────────────────────────────────────
PROJECT_DIR="/opt/crypto-dashboard"
mkdir -p $PROJECT_DIR
echo "▶ Project directory: $PROJECT_DIR"

# ── Firewall ──────────────────────────────────────────────────
echo "▶ Configuring firewall..."
ufw allow ssh
ufw allow 8000/tcp    # API
ufw allow 3000/tcp    # Frontend (when added)
ufw --force enable

# ── Systemd service ───────────────────────────────────────────
cat > /etc/systemd/system/crypto-dashboard.service << 'EOF'
[Unit]
Description=Crypto Trading Dashboard
Requires=docker.service
After=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/opt/crypto-dashboard
ExecStart=/usr/bin/docker compose up -d
ExecStop=/usr/bin/docker compose down
TimeoutStartSec=300

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable crypto-dashboard

echo ""
echo "═══════════════════════════════════════════════"
echo "  Setup complete!"
echo ""
echo "  Next steps:"
echo "  1. Upload project files to /opt/crypto-dashboard/"
echo "  2. cp .env.example .env && nano .env"
echo "  3. docker compose up -d"
echo "  4. docker compose logs -f backend"
echo "═══════════════════════════════════════════════"
