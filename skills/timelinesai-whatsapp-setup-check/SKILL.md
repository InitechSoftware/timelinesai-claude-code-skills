---
name: timelinesai-whatsapp-setup-check
description: Use when the user reports that something looks wrong with their TimelinesAI setup — Claude Code says the token doesn't work, a WhatsApp number has gone quiet, a webhook stopped firing, or they just installed the skills and want to confirm everything is wired up. Runs smoke tests against the TimelinesAI public API and reports what's healthy vs what's broken. Trigger phrases: "token isn't working", "why can't you see my numbers", "setup check", "is the token correct", "verify my TimelinesAI connection", "smoke test", "quota left", "is the webhook still registered".
---

# TimelinesAI setup smoke test

You are diagnosing a TimelinesAI setup issue. Walk through the five checks below in order. Report PASS/FAIL for each with the exact error body when something fails. **Don't try to fix anything yourself** — surface the diagnosis and tell the user what to do.

## Checks

### 1. Token is set

```bash
# Claude Code: check the env var is actually visible
if [ -z "$TIMELINES_AI_API_KEY" ]; then
  echo "FAIL: TIMELINES_AI_API_KEY is not set in this shell."
else
  echo "PASS: TIMELINES_AI_API_KEY is set (length: ${#TIMELINES_AI_API_KEY})"
fi
```

**If FAIL:** tell the user to `export TIMELINES_AI_API_KEY="tla_xxxxxxxxxxxxxxxx"` (macOS/Linux) or set it in Windows Environment Variables. They need to **restart Claude Code's shell session** after setting it.

### 2. Token is valid

```bash
curl -sS -w "\nHTTP: %{http_code}\n" \
  -H "Authorization: Bearer $TIMELINES_AI_API_KEY" \
  "https://app.timelines.ai/integrations/api/whatsapp_accounts"
```

**Expected:** `HTTP: 200` with a JSON body starting `{"status":"ok","data":{"whatsapp_accounts":[...]}}`.

**If HTTP 401 with plain-text `Unauthorized`:** the token is invalid or revoked. Tell the user to regenerate it at `app.timelines.ai → Integrations → Public API → Regenerate`. Warn them that the old token stops working immediately.

**If HTML 404 starting `<!DOCTYPE html>`:** the URL has a typo or trailing slash. This skill always uses the correct base URL — if the user is seeing this, they're running manual `curl` commands with the wrong shape.

### 3. At least one WhatsApp number connected

Parse the response from check 2. You're looking for:

```json
{
  "status": "ok",
  "data": {
    "whatsapp_accounts": [
      {
        "id": "15550100@s.whatsapp.net",
        "phone": "+15550100",
        "status": "connected",
        "account_name": "Your Business"
      }
    ]
  }
}
```

**PASS:** at least one account with `status: "connected"` (or `active`).

**FAIL — empty array:** no WhatsApp number is attached to this workspace. Tell the user to go to `app.timelines.ai`, add a WhatsApp account, and scan the QR code.

**FAIL — status is `disconnected`, `logged_out`, or `qr_required`:** the WhatsApp Web session dropped (this happens; it's WhatsApp's side, not TimelinesAI's). Tell the user to re-scan the QR code from `app.timelines.ai → WhatsApp Accounts`. No analytics the user runs will be accurate until they do.

### 4. Quota is healthy

```bash
curl -sS -H "Authorization: Bearer $TIMELINES_AI_API_KEY" \
  "https://app.timelines.ai/integrations/api/quotas"
```

**PASS:** non-zero remaining credits. Report the number.

**FAIL — zero or near-zero:** tell the user that analytics (reads) still work but any send action you might take on their behalf will fail. Point them at billing in the TimelinesAI dashboard.

### 5. Existing webhook subscriptions (optional)

Only run this check if the user mentions webhooks, a stopped autoresponder, or an always-on agent.

```bash
curl -sS -H "Authorization: Bearer $TIMELINES_AI_API_KEY" \
  "https://app.timelines.ai/integrations/api/webhooks"
```

**PASS:** one or more subscriptions with `enabled: true`. Report the URL each one points at.

**If the user expected a webhook to be there and it isn't:** tokens occasionally drop subscriptions when regenerated. The user needs to re-register via `POST /webhooks`. Point them at the OpenClaw guide at `timelines.ai/guide/openclaw-whatsapp-skills` for the always-on agent pattern — this skill is for analytics, not for running receivers.

## Report format

Output should be compact. Use this shape:

```
TimelinesAI setup check:

✓ PASS  TIMELINES_AI_API_KEY is set
✓ PASS  Token valid (HTTP 200)
✓ PASS  1 connected WhatsApp number: +15550100 (status: connected)
✓ PASS  Quota: 487 credits remaining
— SKIP  Webhook check (not requested)

Everything's wired up. You can ask WhatsApp analytics questions now.
```

If anything fails, stop the list at the first failure, report the failure clearly, and give the user the one specific action to take. Don't dump a wall of diagnostics.
