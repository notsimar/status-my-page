#!/usr/bin/env bash
set -e
cd /home/ssahni/Developer/status-my-page

PASS_HASH=$(/home/ssahni/Developer/status-my-page/.venv/bin/python3 -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('test123'))")
BASE="http://localhost:8920"
COOKIES="/tmp/sp_test_cookies.txt"
rm -f $COOKIES

echo "=== C2a: Login with cookies saved ==="
curl -s -c $COOKIES $BASE/login \
  -X POST \
  -H 'Content-Type: application/json' \
  -d "{\"user\":\"admin\",\"pass\":\"test123\"}"
echo ""

echo "=== C2b: Auth check AFTER login (with session cookie) ==="
curl -s -b $COOKIES $BASE/auth-check
echo ""

echo "=== C2c: Add a user item with auth ==="
curl -s -b $COOKIES $BASE/api/add \
  -X POST \
  -H 'Content-Type: application/json' \
  -d '{"name":"MyTestService"}'
echo ""

echo "=== C2d: Verify user item in page DOM ==="
curl -s -b $COOKIES $BASE/ | grep -o "MyTestService" && echo "✓ User item visible after login" || echo "✗ Not visible"
echo ""

# Test cookie bypass: use a separate curl with JUST the _admin cookie value (no valid session)
echo "=== C2e: _ADMIN COOKIE BYPASS TEST ==="
# First, let's see what cookies were actually sent
grep -E "(session|_admin)" $COOKIES || echo "(No relevant cookies found)"
python3 -c "
import http.cookiejar, urllib.request
cj = http.cookiejar.MozillaCookieHandler('$COOKIES')
for cookie in cj:
    print(f'{cookie.name}={cookie.value[:20]}... (domain={cookie.domain})')
" 2>/dev/null | head -10

echo ""
echo "=== C2f: Verify _admin=1 bypass is BLOCKED ==="
# Set a _admin cookie but no session cookie - the new code should reject this
curl -s $BASE/auth-check -b "session=\"\"; _admin=1" | python3 -c "
import sys, json
data = json.loads(sys.stdin.read())
if data['admin'] == False:
    print('PASS: Cookie bypass correctly blocked (_admin=1 -> admin=False)')
else:
    print(f'FAIL: Cookie bypass NOT blocked! {data}')
"

echo ""
echo "=== C1a: User added item survives? ==="
curl -s -b $COOKIES $BASE/ | grep -c "MyTestService" | python3 -c "
import sys
count = int(sys.stdin.read().strip())
if count > 0:
    print(f'✓ MyTestService found {count} times in original page')
else:
    print('✗ User item not found on initial load')
"

# Now kill and restart to test C1 (user_added items survive)
echo ""
echo "=== C1b: Kill server ==="
kill $(cat .server.pid 2>/dev/null) || true
sleep 2

echo "=== C1c: Restart with new hash ==="
export STATUS_ADMIN_PASS_HASH="$PASS_HASH"
python3 -m ipaddress >/dev/null 2>&1 || true
nohup .venv/bin/python3 app.py > logs/server.log.2 2>&1 &
NEW_PID=$!
echo $NEW_PID > .server.pid
sleep 3

echo "=== C1d: Verify user item survived restart (CRITICAL FIX) ==="
# We need to auth again after restart since sessions are lost
curl -s -c $COOKIES2 /dev/null 2>/dev/null || true
rm -f ${COOKIES}2
curl -s -c ${COOKIES}2 $BASE/login \
  -X POST \
  -H 'Content-Type: application/json' \
  -d "{\"user\":\"admin\",\"pass\":\"test123\"}" > /dev/null

USER_ITEMS=$(curl -s -b ${COOKIES}2 $BASE/ | grep -c "MyTestService" || echo 0)
echo "User items found: $USER_ITEMS"
if [ "$USER_ITEMS" -gt 0 ]; then
    echo "✓ PASS: User-added item SURVIVED restart (user_added=1 protection works!)"
else
    echo "✗ FAIL: User-added item was LOST after restart!"
fi

echo ""
echo "=== C1e: Config items survived? ==="
grep -c "Slack" <(curl -s -b ${COOKIES}2 $BASE/) && echo "✓ Slack survived" || echo "✗ Slack lost"
grep -c "Azure" <(curl -s -b ${COOKIES}2 $BASE/) && echo "✓ Azure survived" || echo "✗ Azure lost"

echo ""
echo "=== All tests complete ==="
${COOKIES}2)
if [ "$USER_ITEMS" -gt 0 ]; then
    echo "✓ PASS: User-added item SURVIVED restart (user_added=1 protection works!)"
else
    echo "✗ FAIL: User-added item was LOST after restart!"
fi

echo ""
echo "=== Config items survived? ==="
grep -c "Slack" <(curl -s ${COOKIES}2 $BASE/) && echo "✓ Slack survived" || echo "✗ Slack lost"
grep -c "Azure" <(curl -s ${COOKIES}2 $BASE/) && echo "✓ Azure survived" || echo "✗ Azure lost"

echo ""
echo "=== All tests complete ==="
