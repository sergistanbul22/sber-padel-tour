#!/bin/bash
# Auto-update Cloudflare tunnel URL in sber-padel-tour.html
# Runs after cloudflared-reset.service starts

set -e

HTML_FILE="/root/.openclaw/workspace/index.html"
LOG_TAG="[CF-URL-UPDATE]"
TUNNEL_SERVICE="cloudflared-reset.service"
TELEGRAM_BOT_TOKEN="8763865911:AAG2xHWXlYuT54ElXy1PgtQ8HuZQnjXdlIg"
TELEGRAM_CHAT_ID="228493828"

log() {
    echo "$LOG_TAG $1"
    logger -t cf-url-update "$1"
}

send_telegram() {
    local msg="$1"
    curl -s -X POST "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/sendMessage" \
        -d "chat_id=$TELEGRAM_CHAT_ID" \
        -d "text=$msg" \
        -d "parse_mode=HTML" > /dev/null 2>&1 || true
}

# Wait for cloudflared to establish tunnel
log "Waiting for cloudflared to start..."
sleep 15

# Extract tunnel URL from journalctl logs
TUNNEL_URL=$(journalctl -u "$TUNNEL_SERVICE" --since "30 seconds ago" --no-pager 2>/dev/null | \
    grep -oP 'https://[a-z0-9-]+\.trycloudflare\.com' | tail -1)

if [ -z "$TUNNEL_URL" ]; then
    # Try looking at older logs
    TUNNEL_URL=$(journalctl -u "$TUNNEL_SERVICE" --since "5 minutes ago" --no-pager 2>/dev/null | \
        grep -oP 'https://[a-z0-9-]+\.trycloudflare\.com' | tail -1)
fi

if [ -z "$TUNNEL_URL" ]; then
    log "ERROR: Could not find tunnel URL in logs"
    send_telegram "⚠️ <b>Cloudflare URL Update Failed</b>\n\nCould not extract tunnel URL from cloudflared logs.\nPassword reset may not work until URL is updated manually."
    exit 1
fi

log "Found tunnel URL: $TUNNEL_URL"

# Check if URL is already up to date
if grep -q "$TUNNEL_URL" "$HTML_FILE"; then
    log "URL already up to date, no changes needed"
    exit 0
fi

# Update the URL in HTML file
OLD_URL=$(grep -oP 'https://[a-z0-9-]+\.trycloudflare\.com' "$HTML_FILE" | head -1)

if [ -n "$OLD_URL" ]; then
    sed -i "s|$OLD_URL|$TUNNEL_URL|g" "$HTML_FILE"
    log "Replaced $OLD_URL -> $TUNNEL_URL"
else
    log "WARNING: No existing tunnel URL found in HTML, trying to inject..."
    # Try to find the fetch call and update it
    sed -i "s|https://[a-z0-9-]*\.trycloudflare\.com|$TUNNEL_URL|g" "$HTML_FILE"
fi

# Verify the change
if grep -q "$TUNNEL_URL" "$HTML_FILE"; then
    log "HTML updated successfully"
else
    log "ERROR: Failed to update HTML"
    send_telegram "⚠️ <b>Cloudflare URL Update Failed</b>\n\nFound URL: $TUNNEL_URL\nBut could not update HTML file."
    exit 1
fi

# Git operations
cd /root/.openclaw/workspace

# Check if git remote is configured
if git remote -v > /dev/null 2>&1 && [ -n "$(git remote -v)" ]; then
    git add index.html
    git commit -m "auto: update cloudflare tunnel URL to $TUNNEL_URL" || true
    
    if git push origin master 2>&1; then
        log "Git push successful"
        send_telegram "✅ <b>Cloudflare URL Updated</b>\n\nNew URL: <code>$TUNNEL_URL</code>\n\nHTML updated and pushed to GitHub. Password reset should work shortly."
    else
        log "Git push failed"
        send_telegram "⚠️ <b>Cloudflare URL Updated Locally</b>\n\nNew URL: <code>$TUNNEL_URL</code>\n\nHTML updated but git push failed. Please push manually or check remote settings."
    fi
else
    log "No git remote configured, skipping push"
    send_telegram "⚠️ <b>Cloudflare URL Updated Locally</b>\n\nNew URL: <code>$TUNNEL_URL</code>\n\nHTML updated locally but no git remote configured.\nPlease add remote and push:\n\n<code>git remote add origin https://github.com/sergistanbul22/sber-padel-tour.git</code>"
fi

exit 0
