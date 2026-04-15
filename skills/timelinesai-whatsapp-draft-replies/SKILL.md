---
name: timelinesai-whatsapp-draft-replies
description: Use when the user wants you to draft WhatsApp reply messages WITHOUT actually sending them. Saves each draft as a private TimelinesAI note (POST /chats/{id}/notes) prefixed with "DRAFT:" so humans review and send manually from the TimelinesAI inbox. Trigger phrases include "draft replies", "draft me", "don't send", "review before sending", "suggest replies", or any request for WhatsApp suggestions where the user clearly wants human review in the loop. This skill deliberately does NOT call POST /chats/{id}/messages — use the broader timelinesai-whatsapp-analytics skill for real sends.
---

# Draft WhatsApp replies as private notes

You are drafting replies for a human to review. **Never send** via `POST /chats/{id}/messages` inside this skill. Drafts live as private TimelinesAI notes, visible only to the user's team, with a `DRAFT:` prefix so teammates know they're machine-written suggestions.

## Auth

```
Base URL : https://app.timelines.ai/integrations/api
Auth     : Authorization: Bearer $TIMELINES_AI_API_KEY
```

No trailing slashes. Bodies must be valid UTF-8 — always use a file + `curl --data-binary`, never inline `-d "..."`.

## The draft recipe

For each chat the user wants drafts for:

1. **Fetch recent context.** `GET /chats/{id}/messages?limit=20`. Filter on `message_type == "whatsapp"` and sort by `timestamp` to reconstruct the conversation. Skip existing notes (`message_type == "note"`) — those are either internal bookkeeping or previous drafts.
2. **Identify what the customer is waiting on.** The last message where `from_me: false` is the unanswered question. Read a few messages before it for context.
3. **Compose a reply in the same tone and language the team has been using.** Match their punctuation, signature style, any recurring product names or phrasing.
4. **Post the draft as a note.**

```bash
cat > /tmp/note.json <<'JSON'
{"text":"DRAFT: Yes, invoice went out at 10:14 this morning - forwarding the email now."}
JSON

curl -sS -X POST \
  -H "Authorization: Bearer $TIMELINES_AI_API_KEY" \
  -H "Content-Type: application/json" \
  --data-binary @/tmp/note.json \
  "https://app.timelines.ai/integrations/api/chats/12345678/notes"
```

5. **Report to the user.** List the chats you drafted for, the one-line summary of each draft, and a reminder that the drafts are visible in the TimelinesAI inbox for review.

## Rules

- **Always** prefix with `DRAFT:` so humans can filter.
- **Never** call `POST /chats/{id}/messages`. If the user explicitly asks you to send a reply instead of drafting one, defer to the broader `timelinesai-whatsapp-analytics` skill or ask them to confirm the send with specific chat IDs.
- **One draft per chat** — don't offer A/B alternatives unless the user asked for them. Alternatives become noise the human has to filter through.
- **Don't invent facts.** If you can't answer the customer's question without inventing a shipping date, an internal policy, or a product spec, write a draft that acknowledges the question and says "looking into it" instead of fabricating an answer. Notes are safer than bad messages.
- **Skip chats with existing human drafts in progress.** If the most recent note on a chat already starts with `DRAFT:`, don't overwrite it — the human may be mid-edit.
- **Skip chats labelled `escalate`, `needs-human`, or `do-not-auto-reply`** (check `GET /chats/{id}/labels` first). Those are explicit "hands off" signals.

## Scope

This skill only drafts; it does not analyze, score, tag, or compute metrics. For broader analytics ("who is slowest to reply", "top topics this month"), the `timelinesai-whatsapp-analytics` skill is the right tool.
