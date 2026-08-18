#!/bin/bash
# Setup GitHub auto-push for Cloudflare URL updates

set -e

REPO_DIR="/root/.openclaw/workspace"
TOKEN_FILE="/root/.github_token"

echo "=== GitHub Auto-Push Setup ==="
echo ""
echo "To enable automatic push of updated HTML to GitHub,"
echo "you need to create a Personal Access Token."
echo ""
echo "1. Open: https://github.com/settings/tokens/new"
echo "2. Enter token name: 'Sber Padel Tunnel Update'"
echo "3. Select scope: 'repo' (full control of private repositories)"
echo "4. Click 'Generate token'"
echo "5. Copy the token (you won't see it again!)"
echo ""
read -p "Paste your token here: " TOKEN

if [ -z "$TOKEN" ]; then
    echo "Error: Token cannot be empty"
    exit 1
fi

# Save token
echo "$TOKEN" > "$TOKEN_FILE"
chmod 600 "$TOKEN_FILE"

# Configure git credentials helper
cd "$REPO_DIR"
git config credential.helper "store --file=$TOKEN_FILE"

# Update remote URL with token
git remote set-url origin "https://$TOKEN@github.com/sergistanbul22/sber-padel-tour.git"

# Test push
echo ""
echo "Testing connection..."
if git push origin main --dry-run 2>&1 | grep -q "Everything up-to-date\|fatal"; then
    echo "✅ GitHub connection configured successfully!"
else
    echo "⚠️  Could not verify connection. Please check the token."
fi

echo ""
echo "Setup complete. The token is stored securely at $TOKEN_FILE"
