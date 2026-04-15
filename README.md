# timelinesai-claude-code-skills

Ready-made [Claude Code](https://claude.com/claude-code) skills for analyzing and operating WhatsApp via the [TimelinesAI](https://timelines.ai) public API.

Drop them into your Claude Code skills directory and ask questions like:

> *"What was our average first-reply time on WhatsApp this week?"*
>
> *"Which chats have an incoming message we haven't replied to yet, ordered by how long they've been waiting?"*
>
> *"Summarize every conversation with ACME Corp over the last 30 days and tell me what they're stuck on."*

Claude Code already has `curl`, a shell, and the reasoning. These skills teach it the TimelinesAI API shape, the gotchas, and the analytics idioms — so you ask questions in English and it knows which endpoint to hit.

For the full walkthrough (what you can ask, setup, recipes, example session), see the guide: **[timelines.ai/guide/claude-code-whatsapp-analytics](https://timelines.ai/guide/claude-code-whatsapp-analytics)**.

---

## Quick start

### 1. Install Claude Code

Claude Code is included with [Claude Pro](https://claude.com/pricing) at $20/month. Three ways to open it:

- **Web** — [claude.ai/code](https://claude.ai/code) (easiest, no install)
- **Desktop app** — [claude.com/download](https://claude.com/download)
- **Terminal CLI** — [claude.com/claude-code](https://claude.com/claude-code)

Every skill here works identically across all three.

### 2. Get a TimelinesAI Public API token

In [app.timelines.ai](https://app.timelines.ai), go to **Integrations → Public API → Copy**. Save it:

```bash
# macOS / Linux
export TIMELINES_AI_API_KEY="tla_xxxxxxxxxxxxxxxx"

# Windows PowerShell
$env:TIMELINES_AI_API_KEY = "tla_xxxxxxxxxxxxxxxx"
```

One token covers every WhatsApp number connected to your workspace.

### 3. Install the skills

```bash
# Workspace-scoped (recommended)
mkdir -p .claude/skills
git clone https://github.com/InitechSoftware/timelinesai-claude-code-skills.git \
  .claude/skills/timelinesai-whatsapp

# Or global (every Claude Code session in every project)
git clone https://github.com/InitechSoftware/timelinesai-claude-code-skills.git \
  ~/.claude/skills/timelinesai-whatsapp
```

Claude Code auto-loads skills from both locations on startup. Ask your next question about WhatsApp and it just works.

### 4. Smoke-test

Inside Claude Code, type:

> *"Check that my TimelinesAI token works and list my connected WhatsApp numbers."*

You should see your connected number with `status: connected`. If you see a 401, the token didn't land — re-check step 2. If you see `[]`, the token is valid but no number is connected yet — finish the QR scan in `app.timelines.ai` first.

---

## What's in here

Three skills, each a single `SKILL.md` file. Claude Code loads the frontmatter's `description` to decide when each one should fire.

| Skill | What it does |
|---|---|
| [`timelinesai-whatsapp-analytics`](./skills/timelinesai-whatsapp-analytics/SKILL.md) | The big one. Teaches Claude Code the full TimelinesAI read API, the gotchas, and the analytics recipes from the guide — response time, unanswered chats, volume, content analysis, lead funnels, deep investigations. Triggers on most analytical WhatsApp questions. |
| [`timelinesai-whatsapp-draft-replies`](./skills/timelinesai-whatsapp-draft-replies/SKILL.md) | Narrow "draft replies as private notes" skill. Keeps Claude Code's suggestions out of live conversations and puts them in front of humans instead. Triggers when you ask for drafts, not sends. |
| [`timelinesai-whatsapp-setup-check`](./skills/timelinesai-whatsapp-setup-check/SKILL.md) | Smoke tests — token auth, connected numbers, webhook subscriptions, quota. Triggers when something looks off ("why is Claude Code saying the token is wrong?"). |

You can install all three at once (they don't conflict), or symlink just the ones you want.

---

## Related

- **Full guide** — [Analyze WhatsApp with Claude Code + TimelinesAI](https://timelines.ai/guide/claude-code-whatsapp-analytics)
- **TimelinesAI Public API reference** — [timelinesai.mintlify.app](https://timelinesai.mintlify.app/public-api-reference/overview)
- **Always-on agent companion** — [Build a WhatsApp agent with OpenClaw + TimelinesAI](https://timelines.ai/guide/openclaw-whatsapp-skills) — same API, different shape (webhooks, autoresponders, always-on receiver)
- **Claude Code docs** — [claude.com/claude-code](https://claude.com/claude-code)

---

## Contributing

Issues and PRs welcome. For bug reports that involve specific API calls returning unexpected shapes, please include the exact `curl` you ran and the response body — the `SKILL.md` files are living documents and get updated whenever the API shape shifts.

## License

MIT — see [LICENSE](./LICENSE).
