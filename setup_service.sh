#!/bin/bash
# Sets up trading daemon as a systemd user service

SERVICE_DIR="$HOME/.config/systemd/user"
SERVICE_FILE="$SERVICE_DIR/trading-agent.service"
TRADING_DIR="$HOME/.openclaw/workspace/projects/trading"
PYTHON=$(which python3)

mkdir -p "$SERVICE_DIR"

cat > "$SERVICE_FILE" << EOF
[Unit]
Description=Trading Agent Daemon
After=network.target

[Service]
Type=simple
WorkingDirectory=$TRADING_DIR
ExecStart=$PYTHON $TRADING_DIR/bot_daemon.py
Restart=on-failure
RestartSec=30
StandardOutput=append:$TRADING_DIR/daemon.log
StandardError=append:$TRADING_DIR/daemon.log
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable trading-agent
systemctl --user start trading-agent

echo "Service status:"
systemctl --user status trading-agent --no-pager
