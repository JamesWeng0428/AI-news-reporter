#!/bin/bash
# Starts Ollama + the Telegram bot listener
DIR="$(cd "$(dirname "$0")" && pwd)"
BOT_SCRIPT="$DIR/bot.py"

# Stop any existing bot listener to avoid Telegram getUpdates conflicts
if pgrep -f "$BOT_SCRIPT" > /dev/null; then
    echo "Stopping existing Telegram bot listener..."
    pkill -f "$BOT_SCRIPT"
    sleep 1
fi

# Start Ollama in background if not running
if ! pgrep -x "ollama" > /dev/null; then
    echo "Starting Ollama..."
    ollama serve &
    sleep 3
fi

echo "Starting Telegram bot..."
source "$DIR/venv/bin/activate"
python3 "$DIR/bot.py"
