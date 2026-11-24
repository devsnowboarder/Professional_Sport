#!/bin/zsh
set -euo pipefail

PROJECT_DIR="/Users/michaellau/python2026/sport/nflFootball"
SCRIPT="$PROJECT_DIR/nflGames.py"
HTML_FILE="$PROJECT_DIR/nfl_scores.html"

# Destination (update the subfolder if needed)
REMOTE_DEST="fastsnow@fastsnowboarder.com:~/public_html/nfl/"

# Locate the .venv Python binary (supports running from terminal, cron, or Jenkins)
if [ -x "$PROJECT_DIR/.venv/bin/python" ]; then
    PYTHON_BIN="$PROJECT_DIR/.venv/bin/python"
elif [ -x "/Users/michaellau/python2026/.venv/bin/python" ]; then
    PYTHON_BIN="/Users/michaellau/python2026/.venv/bin/python"
else
    PYTHON_BIN="python3"
fi

echo "Running $SCRIPT..."
cd "$PROJECT_DIR"
"$PYTHON_BIN" "$SCRIPT" -o "$HTML_FILE"

if [ ! -f "$HTML_FILE" ]; then
    echo "Error: $HTML_FILE was not generated."
    exit 1
fi



echo "Uploading $HTML_FILE to $REMOTE_DEST..."
scp "$HTML_FILE" "$REMOTE_DEST"

echo "Done!"cd