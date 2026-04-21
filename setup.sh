#!/bin/bash
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$DIR/venv"
ENV_FILE="$DIR/.env"

upsert_env_var() {
  local key="$1"
  local value="$2"
  touch "$ENV_FILE"
  if grep -q "^${key}=" "$ENV_FILE"; then
    sed -i.bak "s|^${key}=.*|${key}=${value}|" "$ENV_FILE"
    rm -f "$ENV_FILE.bak"
  else
    echo "${key}=${value}" >> "$ENV_FILE"
  fi
}

echo "=== AI Agent Setup ==="
echo ""

# Create virtualenv
echo "[1/4] Creating Python virtual environment..."
python3 -m venv "$VENV"
source "$VENV/bin/activate"
pip install --quiet --upgrade pip
pip install --quiet -r "$DIR/requirements.txt"
echo "Done."

# Telegram setup
echo ""
echo "[2/4] Telegram Setup"
echo "---"
echo "Step 1: Open Telegram and search for @BotFather"
echo "Step 2: Send /newbot and follow the prompts"
echo "Step 3: Copy the token BotFather gives you"
echo ""
read -p "Paste your Telegram bot token here: " BOT_TOKEN

# Update .env with token
upsert_env_var "TELEGRAM_TOKEN" "$BOT_TOKEN"

echo ""
echo "Step 4: Send any message to your new bot in Telegram (just say hi)"
echo "       (This is needed so we can detect your Chat ID)"
read -p "Press Enter once you've sent a message to your bot..."

# Fetch chat ID
CHAT_ID=$(curl -s "https://api.telegram.org/bot$BOT_TOKEN/getUpdates" \
    | python3 -c "import sys,json; data=json.load(sys.stdin); print(data['result'][-1]['message']['chat']['id'])" 2>/dev/null || echo "")

if [ -z "$CHAT_ID" ]; then
    echo "Could not auto-detect chat ID. Please paste it manually."
    read -p "Your Telegram chat ID: " CHAT_ID
fi

echo "Detected chat ID: $CHAT_ID"

# Update .env with chat id
upsert_env_var "TELEGRAM_CHAT_ID" "$CHAT_ID"

# Clear any legacy secrets from memory.json
python3 - <<EOF
import json
with open("$DIR/memory.json") as f:
    mem = json.load(f)
mem["telegram_token"] = ""
mem["telegram_chat_id"] = ""
with open("$DIR/memory.json", "w") as f:
    json.dump(mem, f, indent=2)
EOF

# Set up cron jobs
echo ""
echo "[3/4] Setting up cron jobs (10am and 10pm daily)..."
PYTHON="$VENV/bin/python3"
AGENT="$DIR/agent.py"
LOG="$DIR/agent.log"

# Add cron entries (remove old ones first if any)
(crontab -l 2>/dev/null | grep -v "ai-agent/agent.py"; \
 echo "0 10 * * * $PYTHON $AGENT >> $LOG 2>&1"; \
 echo "0 22 * * * $PYTHON $AGENT >> $LOG 2>&1") | crontab -
echo "Cron jobs added."

# Send test message
echo ""
echo "[4/4] Sending a test message to your Telegram..."
"$PYTHON" - <<EOF
import requests
requests.post(
    "https://api.telegram.org/bot$BOT_TOKEN/sendMessage",
    json={"chat_id": "$CHAT_ID", "text": "AI Agent is set up! You'll get digests at 10am and 10pm daily."}
)
print("Test message sent!")
EOF

echo ""
echo "=== Setup complete! ==="
echo ""
echo "Next steps:"
echo "  Start the bot listener:  source venv/bin/activate && python3 bot.py"
echo "  Run a digest now:        source venv/bin/activate && python3 agent.py"
echo "  Secrets are in:          .env"
echo ""
echo "Make sure Ollama is running: ollama serve"
echo "Optional: set TELEGRAM_ALLOWED_USERNAME or TELEGRAM_ALLOWED_USER_ID in .env to restrict bot access."
