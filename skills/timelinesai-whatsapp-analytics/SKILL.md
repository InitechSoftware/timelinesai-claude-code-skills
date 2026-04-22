---
name: timelinesai-whatsapp-analytics
description: Use when the user asks analytical questions about their WhatsApp workspace — response time, unanswered chats, unread queues, volume or workload, topic trends, complaint tracking, lead scoring, campaign attribution, or any open-ended "why/what/who/how many" question about WhatsApp customer conversations. Fetches live data from the TimelinesAI public API ($TIMELINES_AI_API_KEY) and aggregates client-side. Trigger words include WhatsApp, TimelinesAI, first-reply time, SLA, unanswered, inbox, unread, response time, lead, qualified, refund, complaint, and any mention of specific customers by name or phone.
---

# TimelinesAI WhatsApp analytics for Claude Code

You are helping the user analyze their WhatsApp customer conversations. TimelinesAI runs their WhatsApp gateway and exposes every chat, message, label, note, and delivery event via a public REST API. You have `curl` and a shell. Use them.

This skill is the operational summary — everything you need for analytics is below. If something here is missing or you need a schema detail, the canonical upstream reference is at https://timelinesai.mintlify.app/public-api-reference/overview. Do not tell the user to read it; read it yourself if needed.

## Auth and base URL

```
Base URL : https://app.timelines.ai/integrations/api
Auth     : Authorization: Bearer $TIMELINES_AI_API_KEY
```

The `TIMELINES_AI_API_KEY` environment variable should already be set. If `curl` returns `401 Unauthorized`, tell the user to re-check the token via `app.timelines.ai → Integrations → Public API → Copy`.

**Never** use a different base URL. Older blog posts reference `api.timelines.ai` with an `X-API-KEY` header — that is outdated.

**Never** put a trailing slash on any path. `GET /chats` works; `GET /chats/` returns a branded HTML 404 page that looks like a network problem but isn't.

## Read endpoints

| Method | Path | What it returns |
|---|---|---|
| GET | `/whatsapp_accounts` | Connected WhatsApp numbers (JID, phone, status, account_name). Use first to confirm setup. |
| GET | `/chats` | Chat list. Filters: `?phone=%2B...`, `?label=...`, `?read=false`, `?name=...`, `?limit=N`. Each chat has `whatsapp_account_id` (owning JID), `unread_count`, `last_message_timestamp`, `responsible_email`. |
| GET | `/chats/{id}` | One chat's full detail (metadata, assignee, owning JID, label list, unread count). |
| GET | `/chats/{id}/messages` | Message history with `?limit=N`. Fields: `from_me`, `sender_phone`, `sender_name`, `text`, `timestamp`, `message_type` (`whatsapp` vs `note`), `origin`, `uid`. |
| GET | `/chats/{id}/labels` | Labels currently on this chat. |
| GET | `/messages/{uid}/status_history` | Sent → Delivered → Read timeline for an outbound message. |
| GET | `/messages/{uid}/reactions` | Reactions on a message. |
| GET | `/files` | Files the user has uploaded via the API. |
| GET | `/webhooks` | Webhook subscriptions. Not used for analytics; useful for "why did something stop replying?" debugging. |
| GET | `/quotas` | Remaining message credits. |

## Write endpoints (only for the Actions recipes below)

| Method | Path | What it does |
|---|---|---|
| POST | `/chats/{id}/labels` | Set labels on a chat. **REPLACE** semantics — read current labels first, merge, write back the full array. Body: `{"labels":["a","b"]}`. |
| POST | `/chats/{id}/notes` | Attach a private note to a chat. Visible only inside TimelinesAI. |
| POST | `/chats/{id}/messages` | Send a real WhatsApp message into an existing chat. Sender is whichever WhatsApp number owns the chat — you don't pick it. |
| PATCH | `/chats/{id}` | Update chat metadata — assignee email, read state. |

## Response shapes — read carefully

Every successful response is `{"status":"ok","data":{...}}`. List endpoints nest again under a typed key:

```
GET /whatsapp_accounts        → data.whatsapp_accounts (array)
GET /chats                    → data.chats (array), data.has_more_pages (bool)
GET /chats/{id}/messages      → data.messages (array)
GET /chats/{id}/labels        → data.labels (array of {name})
GET /messages/{uid}/status_history → data (flat array)
GET /files                    → data (flat array)
GET /whatsapp_accounts        → data.whatsapp_accounts (array)
```

When parsing JSON, always dig into `data.<typed_key>` first and fall back to `data` if missing. Code that assumes a uniform shape breaks on the first mismatch.

## Critical gotchas

These will each produce wrong-looking-right answers. Internalize them before doing any aggregation.

- **`from_me` tells direction, `sender_phone` does not.** Each message has a boolean `from_me`: `true` = outbound (the user's team), `false` = inbound (the customer). Outbound messages still carry a `sender_phone` (the team's own WhatsApp number), so code that checks `sender_phone != ""` is wrong and will invert the conversation. **Every** analytics question hinges on `from_me`.

- **`message_type`: `whatsapp` vs `note`.** Real WhatsApp messages have `message_type == "whatsapp"`. Private team notes have `message_type == "note"`. When counting message volume or computing response times, **filter out notes** — otherwise internal bookkeeping contaminates the numbers.

- **History uses `uid`; webhooks use `message_uid`.** Same value, different key. For analytics you only see `uid`.

- **Pagination is client-side.** `GET /chats/{id}/messages` has `?limit=N` but no `?since=<date>` filter. Pull a window of N and filter client-side. For long histories, pull in chunks.

- **Under load, `403 Forbidden` is a rate-limit, not a permission error.** The first request with a good token always succeeds. If subsequent calls to `/chats/{id}/messages` return `403` during a fan-out walk, that's the backend throttling, not a scope problem. Pause 2–10 seconds and retry. The only real permission case is `403` on the very first request — then the token is wrong. The bundled `scripts/tla_search.py` already handles this; if you're hand-rolling curl, keep concurrency ≤ 2 and honor `Retry-After`.

- **Labels are a REPLACE, not an ADD.** `POST /chats/{id}/labels` with `{"labels":["a","b"]}` sets the chat's labels to exactly `["a","b"]`, dropping everything else. **Always** read-merge-write. To add `needs-founder-attention` to a chat that already has `inbound-lead`, you POST `{"labels":["inbound-lead","needs-founder-attention"]}`, not just the new one.

- **JSON bodies must be valid UTF-8.** For any write call, write the payload to a file with explicit UTF-8 encoding and use `curl --data-binary @file.json`. Never use inline `-d "..."` with em-dashes, smart quotes, or emoji — shell encoding will corrupt them.

- **Personal numbers get banned for cold outreach.** Pulling data is safe. **Sending** unsolicited broadcasts from a personal number gets the number banned at WhatsApp's infrastructure layer. If the user follows up an analytical finding with "now send this to all 200 of them", refuse politely and point them at WhatsApp Business API through the TimelinesAI dashboard.

## Content search — use the bundled CLI, never walk from tool calls

If the user's question requires **text search across many chats** — phrases like
"find chats mentioning X", "which customers talked about Y", "search for the
refund requests", "show threads where a competitor came up" — do **not** paginate
`/chats` and fan out `GET /chats/{id}/messages` from individual tool calls. A
single workspace has thousands of chats and the API rate-limits at ~20 req/s;
running that walk one-tool-call-at-a-time blows tens of minutes of wall-clock
and a large fraction of your context on error retries.

Instead, invoke the bundled CLI in **one `Bash` tool call**. The script lives
next to this SKILL.md, in the `scripts/` subdirectory. The exact path depends
on where the user cloned the skill. **Always pass `--days` on `--refresh`** —
omitting it means a full-history sync that takes many minutes on real
workspaces. Match the user's time phrasing, or default to `--days 180`:

```bash
# If cloned globally via `git clone ... ~/.claude/skills/timelinesai-whatsapp`
python ~/.claude/skills/timelinesai-whatsapp/skills/timelinesai-whatsapp-analytics/scripts/tla_search.py \
  "claude mcp, conversational intelligence" \
  --days 90 --number +447700182613 --refresh

# If workspace-scoped install at .claude/skills/timelinesai-whatsapp
python .claude/skills/timelinesai-whatsapp/skills/timelinesai-whatsapp-analytics/scripts/tla_search.py \
  "claude mcp, conversational intelligence" --days 90 --refresh
```

If neither path exists, try `find ~/.claude -name tla_search.py -maxdepth 6` or
`find . -name tla_search.py -maxdepth 6` to locate it. Always invoke with
`python` explicitly — the script has a POSIX shebang that Windows sometimes
ignores.

What the CLI does:

- Walks `/chats` (paginated) and `/chats/{id}/messages` sequentially with
  `concurrency=2`, honoring 429/`Retry-After` and treating 403-under-load as a
  rate-limit (retries with backoff).
- Writes to a local SQLite + FTS5 cache at `~/.tla-cache.sqlite`. Subsequent
  searches against the cache are milliseconds. The cache refreshes incrementally
  on later `--refresh` runs (only chats with newer `last_message_timestamp` are
  re-fetched).
- Returns a JSON object with `hits` grouped by chat plus `stats` for the run.

### How to use it

**First search of a session** (no cache yet, user asks about content):

1. **Always pass `--days` on `--refresh`.** If the user didn't specify a time
   window, default to `--days 180`. Match the user's phrasing when they give one
   ("last quarter" → `--days 90`, "last month" → `--days 30`, "this year" →
   `--days 365`). Only omit `--days` entirely if the user explicitly asks for
   **all-time** / **full-history** analysis — that path is 5–10× slower and is
   almost never what they mean.
2. Pass `--number +...` when the user has named an active WhatsApp number, or
   only has one connected. On a multi-number workspace without a clear scope,
   ask which number before running the first sync.
3. Run with `--refresh` to sync, then search. On a typical workspace, a 90-day
   sync is under a minute. Tell the user you're syncing and an estimate; don't
   just hang.
4. Report hits by chat, with 1–2 message snippets per hit. Don't dump raw JSON —
   summarize the pattern the user actually cares about (who, when, sentiment,
   what topic).

**Follow-up searches in the same session**:

Run without `--refresh`. The cache is already warm; results are instant. A quick
second run with a broader query ("now search for 'pricing' too") costs nothing.

**The user changed numbers or added a label filter**:

Re-run without `--refresh` first — the cache covers the broader set, the new
`--number` / `--label` flags narrow at query time. Only add `--refresh` if the
user says they want the newest messages.

### Flags reference

| Flag | Purpose |
|---|---|
| `"term1, term2, ..."` | Comma-separated OR terms (FTS5 phrase match, case-insensitive). |
| `--days N` | Only chats/messages within the last N days. |
| `--number +447...` | Filter by owning WhatsApp account (phone or JID substring). |
| `--label NAME` | Filter by chat label (exact match). |
| `--refresh` | Sync cache before searching. |
| `--sync-only` | Sync only (no query). Useful to warm the cache in the background. |
| `--no-cache` | Skip the cache entirely (live walk). Only use for one-off checks. |
| `--limit-hits N` | Cap rows in the output (default 500). |
| `--concurrency N` | Raise carefully. Default 2 is the tested safe value. |
| `-v` | Verbose progress on stderr. |

### Anti-patterns

- **Do not** fall back to writing your own for-loop of curl calls "just for this
  one query". If the scope is more than ~50 chats, use the CLI — the rate-limit
  handling is the whole point.
- **Do not** omit `--days` on `--refresh`. Omitting it is a full-history sync
  (5–15 minutes on a real workspace) and is almost never what the user asked
  for. If the user didn't give a window, default to `--days 180`; if the query
  names one ("last quarter", "last month"), match it. Only omit `--days` when
  the user explicitly asks for all-time history.
- **Do not** treat the first `--refresh` as a failure just because it takes
  ~30–90 seconds at `--days 180`. That is the one-time sync cost. Tell the user
  what's happening.

### When the CLI is the wrong tool

- The question is "top topics this month" or "refund tracking" — these are
  reasoning tasks on message content, not keyword lookups. Use the recipes in
  *Content analysis* below, on a narrower cohort pulled directly.
- The user points at a single chat by name/phone — use `GET /chats?name=...`
  instead of the global search. Faster and cheaper.

---

## Analytics recipes

Use these as templates. All of them combine 1–3 endpoints with client-side aggregation.

### Response time and SLA

#### Average first-reply time over a window

Pull `GET /chats?limit=100`, then for each chat `GET /chats/{id}/messages?limit=50`. In each chat, find consecutive `(from_me: false) → (from_me: true)` pairs; diff the timestamps; these are first-reply latencies. Average inside the window (filter by timestamp client-side). Report mean plus the five worst chats so the user can act.

For p50/p95: don't just average. Sort the latencies and pick percentiles. A healthy-looking mean with a 2-hour p95 usually means one customer got stranded.

#### By teammate

Same walk, but bucket by `sender_name` on the outbound message or the chat's `responsible_email`. Output a ranking with mean, median, chat count per person.

#### By WhatsApp number

Bucket by `whatsapp_account_id` (the owning JID). Useful when the user runs sales + support on two numbers.

### Unanswered and service-window

#### Currently unanswered

`GET /chats?read=false&limit=100` returns unread chats. For each, `GET /chats/{id}/messages?limit=20` and check whether the last message is `from_me: false`. If yes, compute wait time from that message's timestamp. Sort descending, show the oldest 5–10.

#### About to cross the 24-hour window

Same unread pull, filter to chats where the last inbound message is **20–24 hours old**. After 24 hours on a personal number, the user can't freely reply anymore (Business API territory). Surface this list urgently — it's a genuine "act now or lose the option" situation.

#### Already lost to the window

Filter to chats where the last inbound is **more than 24 hours old** with no outbound since. These are not recoverable on a personal number; useful as a leading-indicator metric for lost pipeline.

### Volume and workload

#### Daily / weekly volume

Walk `/chats`, then `/chats/{id}/messages` for each active chat in the window. Count by day, bucket by `from_me`. Output as a table or a small ASCII bar chart.

#### Per WhatsApp number

Same aggregation bucketed by `whatsapp_account_id`. If a number went quiet, check `GET /whatsapp_accounts` for its status — a dropped WhatsApp Web session looks like "quiet" in the data.

#### Per teammate

Count outbound (`from_me: true`, `message_type: whatsapp`) grouped by `sender_name` or chat `responsible_email`.

### Content analysis

These are reasoning tasks over raw message text, not aggregations. Pull the window, then use your own reasoning.

> If the user's query is a **keyword or phrase lookup** ("chats mentioning X",
> "conversations where someone asked about Y"), stop and use
> `scripts/tla_search.py` — see *Content search* above. The recipes below
> assume you already have a narrow cohort of chats (e.g. last 7 days on one
> number) whose full text you can reason over directly.

#### Top topics

Pull recent chats and their message history. Cluster customer messages (`from_me: false`) by topic, count chats per topic, return the top N. The first time the user runs this it's usually surprising — call out the gap between "what we thought people care about" and "what they actually ask about" if you see it.

#### Unmet demand

Same pull, different prompt: "are customers repeatedly asking for any feature, product, or service we don't currently offer?" This is the single most valuable analytical question and it's purely a reasoning task — no aggregation, no SQL, no dashboard can do it.

#### Complaint / refund tracking

Pull message history for active chats. Flag refund/complaint intent. For each flagged chat, pull the full thread and summarize the outcome. Output: table with chat, summary, outcome, final-reply date.

#### Per-customer deep-dive

`GET /chats?name=ACME` (or `?phone=+...`) → `GET /chats/{id}/messages` for the narrowest chat set. Summarize chronologically; flag shifts in tone; recommend a next message.

### Lead funnels

#### Score a cohort by buying intent

`GET /chats?label=inbound-lead`. For each chat, pull recent messages and score on buying intent and urgency. **Always** include a one-line justification per score — a raw number without justification isn't actionable.

#### Follow-through on qualified leads

`GET /chats?label=qualified-hot`. For each chat, find who sent last and how old it is. Bucket into `replied`, `went-dark-after-our-reply`, `still-inside-24hr`, `outside-window-lost`. Output a table.

#### Campaign attribution

Filter chats by first-message timestamp. Classify each thread as `bounced` / `conversation` / `deal` based on content. Not perfect, but better than click counts.

#### Qualification retrospective

Pull chats with the `qualified` label. Compare your classifier's output (reading the thread now) to the original tag. Report precision, recall, and the specific chats the qualifier got wrong — those are tuning signal.

### Deep investigations

The open-ended questions a dashboard can't answer.

#### "What went wrong with this customer?"

Pull the entire chat history. Summarize chronologically. Flag moments where tone or topic shifted. Write a recommended next message draft. For relationships worth salvaging, this replaces an hour of scrolling.

#### "Why did our response time get worse this month?"

Compute first-reply time sliced every way: teammate, number, hour, day-of-week, topic. Report which slice explains the jump.

#### "Typical customer journey"

Pull a cohort of 100–200 recent chats. Extract turn-count and outcome per thread. Describe the patterns: where drop-off happens, what topics distinguish successful threads from dead ones.

#### "Which outbound sends are worth repeating?"

For outbound messages (`from_me: true`), check whether the next inbound arrived inside 24 hours and what it said. Aggregate by message intent. Rank by reply rate.

## Actions based on analysis

When the user explicitly asks you to *act* on findings, keep actions small and auditable. **Never** send real WhatsApp messages unless the user asked for a specific reply to a specific chat. Default to notes and labels.

### Tag a chat

Always read-merge-write:

```bash
# 1. Read current labels
curl -sS -H "Authorization: Bearer $TIMELINES_AI_API_KEY" \
  "https://app.timelines.ai/integrations/api/chats/12345678/labels"
# → {"status":"ok","data":{"labels":[{"name":"inbound-lead"}]}}

# 2. Merge and write back the FULL list
cat > /tmp/labels.json <<'JSON'
{"labels":["inbound-lead","needs-founder-attention"]}
JSON
curl -sS -X POST \
  -H "Authorization: Bearer $TIMELINES_AI_API_KEY" \
  -H "Content-Type: application/json" \
  --data-binary @/tmp/labels.json \
  "https://app.timelines.ai/integrations/api/chats/12345678/labels"
```

### Drop an internal note

```bash
cat > /tmp/note.json <<'JSON'
{"text":"Refund requested 2026-04-14. Customer had a broken package. Check Stripe for auto-refund eligibility."}
JSON
curl -sS -X POST \
  -H "Authorization: Bearer $TIMELINES_AI_API_KEY" \
  -H "Content-Type: application/json" \
  --data-binary @/tmp/note.json \
  "https://app.timelines.ai/integrations/api/chats/12345678/notes"
```

### Draft replies (without sending)

Same as a note, with a `DRAFT:` prefix. The user reviews them inline in the TimelinesAI inbox and sends manually. This is the default shape for "draft me replies to all unanswered chats".

### Send a transactional reply (only when explicitly asked)

```bash
cat > /tmp/reply.json <<'JSON'
{"text":"Thanks! The invoice just went out - forwarding the email now."}
JSON
curl -sS -X POST \
  -H "Authorization: Bearer $TIMELINES_AI_API_KEY" \
  -H "Content-Type: application/json" \
  --data-binary @/tmp/reply.json \
  "https://app.timelines.ai/integrations/api/chats/12345678/messages"
```

Only do this when the user pointed at a specific chat and asked for a specific reply. **Never** bulk-send.

## When analysis fails

If `curl` returns unexpected shapes, surface the exact URL and response body to the user. Don't fabricate aggregated answers from failed reads. If you're unsure about a number, say so and pull a second slice to cross-check.
