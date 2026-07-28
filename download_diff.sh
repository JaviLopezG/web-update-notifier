#!/bin/bash
# download_diff.sh - Downloads a webpage twice and diffs the two versions

URL="https://ciudadanob.com/"
TMP_DIR=$(mktemp -d)

echo "Downloading first copy..."
curl -s -L "$URL" -o "$TMP_DIR/page1.html"

echo "Waiting 2 seconds..."
sleep 2

echo "Downloading second copy..."
curl -s -L "$URL" -o "$TMP_DIR/page2.html"

echo ""
echo "=== DIFF ==="
diff -u "$TMP_DIR/page1.html" "$TMP_DIR/page2.html"

if [ $? -eq 0 ]; then
    echo "No differences found."
else
    echo ""
    echo "Differences detected. Files saved in: $TMP_DIR"
fi