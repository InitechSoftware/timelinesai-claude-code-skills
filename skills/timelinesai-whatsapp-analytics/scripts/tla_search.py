#!/usr/bin/env python3
"""
tla-search — content search across a TimelinesAI workspace.

The Public API exposes no server-side text search. This script walks
/chats and /chats/{id}/messages with conservative rate-limit handling,
a local SQLite+FTS5 cache, and resumable checkpoints so a query like
"find chats mentioning claude mcp" finishes in one tool call instead of
a thousand.

Usage:
    export TIMELINES_AI_API_KEY=tla_...
    python tla_search.py "claude mcp, conversational intelligence"
    python tla_search.py "refund" --days 30 --number +447700182613
    python tla_search.py "refund" --label inbound-lead --limit-chats 100
    python tla_search.py "refund" --refresh       # rebuild cache, then search
    python tla_search.py --sync-only --days 90    # warm the cache, no search

Query: comma-separated terms are OR'd. Each term is matched case-insensitive
with word boundaries. Quote terms that contain commas.

Exit codes:
    0  success (may still have zero hits)
    1  bad arguments / missing token
    2  irrecoverable API failure
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import http.client
import json
import os
import re
import socket
import sqlite3
import ssl
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock

API_HOST = "app.timelines.ai"
API_BASE_PATH = "/integrations/api"
API_BASE = f"https://{API_HOST}{API_BASE_PATH}"
DEFAULT_CACHE = Path.home() / ".tla-cache.sqlite"
USER_AGENT = "tla-search/1.0 (+https://github.com/InitechSoftware/timelinesai-claude-code-skills)"


# ----------------------------- HTTP client -----------------------------

# Important: this client opens a FRESH TLS connection per request.
# The TimelinesAI API fronts `/integrations/api/*` with Cloudflare, which
# flags bursts of requests on the same keep-alive connection and starts
# returning 403 to every subsequent call on that socket — but fresh
# connections keep succeeding. Empirically, `urllib.request` (and any
# stdlib wrapper that pools) triggers this; `http.client` opened+closed
# per request does not. The TLS handshake (~200–350 ms) is also a natural
# soft rate limit that keeps us safely below the workspace-level ceiling.

@dataclass
class RateState:
    lock: Lock = field(default_factory=Lock)
    next_allowed_at: float = 0.0  # monotonic
    min_interval: float = 0.0     # floor between requests (stacked on top of TLS handshake)
    penalty_until: float = 0.0


class Client:
    """Serial HTTP client with per-request connections and soft penalty box.

    Concurrency: intentionally single-shot. If you think you need to add
    threaded workers, re-read the note above first. Fan-out here triggers
    connection-level throttling that looks like an auth error. If real
    parallelism ever becomes necessary, open a *separate* HTTPSConnection
    per worker and cap to 2 — beyond that the workspace-level throttle
    kicks in regardless of per-connection pacing.
    """

    def __init__(self, token: str, verbose: bool = False, min_interval: float = 0.0):
        self.token = token
        self.verbose = verbose
        self.rate = RateState(min_interval=min_interval)
        self._first_request_done = False

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(msg, file=sys.stderr, flush=True)

    def _acquire_slot(self) -> None:
        while True:
            with self.rate.lock:
                now = time.monotonic()
                gate = max(self.rate.next_allowed_at, self.rate.penalty_until)
                if now >= gate:
                    if self.rate.min_interval > 0:
                        self.rate.next_allowed_at = now + self.rate.min_interval
                    return
                wait = gate - now
            time.sleep(min(wait, 30))

    def _penalize(self, delay: float) -> None:
        with self.rate.lock:
            self.rate.penalty_until = max(self.rate.penalty_until, time.monotonic() + delay)

    def get(self, path: str, params: dict | None = None) -> dict:
        url_path = API_BASE_PATH + path
        if params:
            q = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
            if q:
                url_path = f"{url_path}?{q}"

        max_tries = 8
        for attempt in range(1, max_tries + 1):
            self._acquire_slot()
            conn = http.client.HTTPSConnection(API_HOST, timeout=60, context=ssl.create_default_context())
            try:
                conn.request(
                    "GET",
                    url_path,
                    headers={
                        "Authorization": f"Bearer {self.token}",
                        "User-Agent": USER_AGENT,
                        "Accept": "application/json",
                        "Connection": "close",
                    },
                )
                resp = conn.getresponse()
                body = resp.read()
                retry_after = resp.getheader("Retry-After")
                if resp.status == 200:
                    self._first_request_done = True
                    return json.loads(body)
                if resp.status in (429, 403, 502, 503, 504):
                    if resp.status == 403 and not self._first_request_done:
                        raise RuntimeError(
                            f"403 on first request — check TIMELINES_AI_API_KEY (path={path}, body={body[:200]!r})"
                        )
                    if retry_after and retry_after.isdigit():
                        delay = min(int(retry_after), 60)
                    else:
                        delay = min(1.5 * (2 ** (attempt - 1)), 30.0)
                    self._penalize(delay)
                    self._log(f"  {resp.status} on {path} — cooling {delay:.1f}s (try {attempt}/{max_tries})")
                    continue
                raise RuntimeError(f"HTTP {resp.status} on {path}: {body[:300]!r}")
            except (socket.timeout, TimeoutError, ConnectionError, OSError) as e:
                delay = min(2 ** attempt, 30)
                self._log(f"  net error on {path}: {e} — retry in {delay}s (try {attempt}/{max_tries})")
                time.sleep(delay)
                continue
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        raise RuntimeError(f"gave up after {max_tries} tries: {path}")


# ----------------------------- Cache (SQLite + FTS5) ------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    id INTEGER PRIMARY KEY,
    name TEXT, phone TEXT, jid TEXT, whatsapp_account_id TEXT,
    labels_csv TEXT, responsible_email TEXT,
    is_group INTEGER, closed INTEGER,
    last_message_timestamp TEXT,
    last_message_uid TEXT,
    created_timestamp TEXT,
    synced_at TEXT,
    msgs_up_to_ts TEXT
);
CREATE INDEX IF NOT EXISTS idx_chats_last_ts ON chats(last_message_timestamp);
CREATE INDEX IF NOT EXISTS idx_chats_acct ON chats(whatsapp_account_id);

CREATE TABLE IF NOT EXISTS messages (
    uid TEXT PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    timestamp TEXT,
    from_me INTEGER,
    sender_phone TEXT,
    sender_name TEXT,
    text TEXT,
    message_type TEXT,
    origin TEXT,
    status TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id, timestamp);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    text,
    content='messages',
    content_rowid='rowid',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, text) VALUES (new.rowid, new.text);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text) VALUES('delete', old.rowid, old.text);
END;
CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text) VALUES('delete', old.rowid, old.text);
    INSERT INTO messages_fts(rowid, text) VALUES (new.rowid, new.text);
END;

CREATE TABLE IF NOT EXISTS sync_state (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def open_cache(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def upsert_chat(conn: sqlite3.Connection, c: dict) -> None:
    labels = c.get("labels") or []
    labels_csv = ",".join(
        (l.get("name") if isinstance(l, dict) else str(l)) for l in labels if l
    )
    conn.execute(
        """
        INSERT INTO chats(id, name, phone, jid, whatsapp_account_id, labels_csv,
                          responsible_email, is_group, closed,
                          last_message_timestamp, last_message_uid,
                          created_timestamp, synced_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
            name=excluded.name,
            phone=excluded.phone,
            jid=excluded.jid,
            whatsapp_account_id=excluded.whatsapp_account_id,
            labels_csv=excluded.labels_csv,
            responsible_email=excluded.responsible_email,
            is_group=excluded.is_group,
            closed=excluded.closed,
            last_message_timestamp=excluded.last_message_timestamp,
            last_message_uid=excluded.last_message_uid,
            created_timestamp=COALESCE(chats.created_timestamp, excluded.created_timestamp),
            synced_at=excluded.synced_at
        """,
        (
            c["id"],
            c.get("name"),
            c.get("phone"),
            c.get("jid"),
            c.get("whatsapp_account_id"),
            labels_csv,
            c.get("responsible_email"),
            1 if c.get("is_group") else 0,
            1 if c.get("closed") else 0,
            c.get("last_message_timestamp"),
            c.get("last_message_uid"),
            c.get("created_timestamp"),
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
        ),
    )


def upsert_message(conn: sqlite3.Connection, chat_id: int, m: dict) -> None:
    conn.execute(
        """
        INSERT INTO messages(uid, chat_id, timestamp, from_me, sender_phone,
                             sender_name, text, message_type, origin, status)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(uid) DO UPDATE SET
            text=excluded.text,
            status=excluded.status
        """,
        (
            m.get("uid"),
            chat_id,
            m.get("timestamp"),
            1 if m.get("from_me") else 0,
            m.get("sender_phone"),
            m.get("sender_name"),
            m.get("text") or "",
            m.get("message_type"),
            m.get("origin"),
            m.get("status"),
        ),
    )


# ----------------------------- Sync -----------------------------------

def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    # "2026-04-21 15:51:25 +0300"
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S %z")
    except ValueError:
        try:
            return datetime.fromisoformat(s)
        except Exception:
            return None


def walk_chats(client: Client, log: callable) -> list[dict]:
    """Paginate /chats fully. Returns list of chat dicts."""
    all_chats: list[dict] = []
    page = 1
    t0 = time.time()
    while True:
        d = client.get("/chats", params={"page": page})
        chats = d.get("data", {}).get("chats", [])
        all_chats.extend(chats)
        more = d.get("data", {}).get("has_more_pages")
        log(f"  /chats page={page} got={len(chats)} total={len(all_chats)} more={more} t={time.time()-t0:.1f}s")
        if not more:
            break
        page += 1
        if page > 2000:
            log("  safety break at 2000 pages")
            break
    return all_chats


def chat_needs_refresh(cached: dict | None, remote: dict) -> bool:
    if not cached:
        return True
    c_ts = parse_ts(cached.get("msgs_up_to_ts"))
    r_ts = parse_ts(remote.get("last_message_timestamp"))
    if r_ts is None:
        return False
    if c_ts is None:
        return True
    return r_ts > c_ts


def sync(
    client: Client,
    conn: sqlite3.Connection,
    days: int | None,
    number: str | None,
    label: str | None,
    msgs_per_chat: int,
    concurrency: int,
    log: callable,
    force_full: bool = False,
) -> dict:
    """Refresh the cache. Returns stats dict."""
    chats = walk_chats(client, log)
    log(f"  chat list: {len(chats)} total")

    # In-scope filter
    cutoff = None
    if days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    def in_scope(c: dict) -> bool:
        if number:
            acct = c.get("whatsapp_account_id") or ""
            if number.lstrip("+") not in acct and number != acct:
                return False
        if label:
            names = [
                (l.get("name") if isinstance(l, dict) else str(l))
                for l in (c.get("labels") or [])
            ]
            if label not in names:
                return False
        if cutoff is not None:
            ts = parse_ts(c.get("last_message_timestamp"))
            if ts is None or ts < cutoff:
                return False
        return True

    scoped = [c for c in chats if in_scope(c)]
    log(f"  scoped to {len(scoped)} chats ({days=}, {number=}, {label=})")

    # Upsert chat metadata for the scoped set.
    with conn:
        for c in scoped:
            upsert_chat(conn, c)

    # Decide which chats need a message fetch.
    cached_by_id = {
        row["id"]: dict(row)
        for row in conn.execute(
            "SELECT id, msgs_up_to_ts FROM chats WHERE id IN ("
            + ",".join(str(c["id"]) for c in scoped)
            + ")"
            if scoped else "SELECT id, msgs_up_to_ts FROM chats WHERE 0"
        )
    }
    to_fetch: list[dict] = []
    for c in scoped:
        cached = cached_by_id.get(c["id"])
        if force_full or chat_needs_refresh(cached, c):
            to_fetch.append(c)
    log(f"  need message fetch: {len(to_fetch)}/{len(scoped)}")

    # Pull messages per chat, concurrency-limited.
    stats = {"chats_fetched": 0, "messages_written": 0, "errors": 0}
    errs_by_id: dict[int, str] = {}

    def fetch(c: dict) -> tuple[int, list[dict] | None, str | None]:
        try:
            d = client.get(
                f"/chats/{c['id']}/messages",
                params={"limit": msgs_per_chat},
            )
            msgs = d.get("data", {}).get("messages", [])
            return c["id"], msgs, None
        except Exception as e:
            return c["id"], None, str(e)

    t0 = time.time()
    done = 0
    with cf.ThreadPoolExecutor(max_workers=max(1, concurrency)) as ex:
        futures = [ex.submit(fetch, c) for c in to_fetch]
        for fut in cf.as_completed(futures):
            chat_id, msgs, err = fut.result()
            done += 1
            if err:
                stats["errors"] += 1
                errs_by_id[chat_id] = err
            else:
                with conn:
                    for m in msgs:
                        upsert_message(conn, chat_id, m)
                    # Mark the chat as covered up to the newest message fetched.
                    if msgs:
                        newest = max((m.get("timestamp") or "") for m in msgs)
                        conn.execute(
                            "UPDATE chats SET msgs_up_to_ts=? WHERE id=?",
                            (newest, chat_id),
                        )
                stats["chats_fetched"] += 1
                stats["messages_written"] += len(msgs)
            if done % 100 == 0 or done == len(to_fetch):
                log(
                    f"  fetch {done}/{len(to_fetch)} "
                    f"t={time.time()-t0:.0f}s msgs={stats['messages_written']} "
                    f"errs={stats['errors']}"
                )

    with conn:
        conn.execute(
            "INSERT INTO sync_state(key,value) VALUES('last_sync', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (datetime.now(timezone.utc).isoformat(timespec="seconds"),),
        )

    stats["chats_scoped"] = len(scoped)
    stats["chats_to_fetch"] = len(to_fetch)
    stats["elapsed_seconds"] = int(time.time() - t0)
    stats["errors_sample"] = list(errs_by_id.items())[:5]
    return stats


# ----------------------------- Search ---------------------------------

def fts_match_expr(terms: list[str]) -> str:
    """Build an FTS5 MATCH expression from user terms.

    Each term is turned into a quoted phrase. Multiple terms OR'd.
    """
    def quote(t: str) -> str:
        # Escape embedded double quotes per FTS5 syntax.
        return '"' + t.replace('"', '""') + '"'
    return " OR ".join(quote(t.strip()) for t in terms if t.strip())


def search_cache(
    conn: sqlite3.Connection,
    terms: list[str],
    days: int | None,
    number: str | None,
    label: str | None,
    limit: int,
) -> list[dict]:
    sql = """
        SELECT m.uid, m.chat_id, m.timestamp, m.from_me,
               m.sender_name, m.sender_phone, m.text, m.message_type,
               c.name AS chat_name, c.phone AS chat_phone,
               c.whatsapp_account_id, c.last_message_timestamp,
               c.labels_csv
        FROM messages_fts
        JOIN messages m ON m.rowid = messages_fts.rowid
        JOIN chats c ON c.id = m.chat_id
        WHERE messages_fts MATCH ?
    """
    args: list = [fts_match_expr(terms)]
    if days is not None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        sql += " AND m.timestamp >= ?"
        args.append(cutoff)
    if number:
        sql += " AND c.whatsapp_account_id LIKE ?"
        args.append(f"%{number.lstrip('+')}%")
    if label:
        sql += " AND (',' || c.labels_csv || ',') LIKE ?"
        args.append(f"%,{label},%")
    sql += " ORDER BY m.timestamp DESC LIMIT ?"
    args.append(limit)
    cur = conn.execute(sql, args)
    rows = [dict(r) for r in cur.fetchall()]
    return rows


def group_hits(rows: list[dict]) -> list[dict]:
    by_chat: dict[int, dict] = {}
    for r in rows:
        c = by_chat.setdefault(
            r["chat_id"],
            {
                "chat": {
                    "id": r["chat_id"],
                    "name": r["chat_name"],
                    "phone": r["chat_phone"],
                    "whatsapp_account_id": r["whatsapp_account_id"],
                    "last_message_timestamp": r["last_message_timestamp"],
                    "labels": [l for l in (r["labels_csv"] or "").split(",") if l],
                },
                "matches": [],
            },
        )
        c["matches"].append(
            {
                "uid": r["uid"],
                "timestamp": r["timestamp"],
                "from_me": bool(r["from_me"]),
                "sender_name": r["sender_name"],
                "sender_phone": r["sender_phone"],
                "message_type": r["message_type"],
                "snippet": (r["text"] or "")[:280],
            }
        )
    return list(by_chat.values())


# ----------------------------- Direct search (no cache) ---------------

def compile_patterns(terms: list[str]) -> list[re.Pattern]:
    return [re.compile(r"\b" + re.escape(t) + r"\b", re.I) for t in terms if t.strip()]


def direct_search(
    client: Client,
    terms: list[str],
    days: int | None,
    number: str | None,
    label: str | None,
    msgs_per_chat: int,
    limit_chats: int | None,
    concurrency: int,
    log: callable,
) -> tuple[list[dict], dict]:
    """Walk the API live, no cache. Returns (hits, stats)."""
    patterns = compile_patterns(terms)
    chats = walk_chats(client, log)
    cutoff = None
    if days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    def in_scope(c: dict) -> bool:
        if number:
            acct = c.get("whatsapp_account_id") or ""
            if number.lstrip("+") not in acct and number != acct:
                return False
        if label:
            names = [
                (l.get("name") if isinstance(l, dict) else str(l))
                for l in (c.get("labels") or [])
            ]
            if label not in names:
                return False
        if cutoff is not None:
            ts = parse_ts(c.get("last_message_timestamp"))
            if ts is None or ts < cutoff:
                return False
        return True

    scoped = [c for c in chats if in_scope(c)]
    if limit_chats:
        scoped = scoped[:limit_chats]
    log(f"  direct search scoped to {len(scoped)} chats")

    stats = {
        "chats_total": len(chats),
        "chats_scoped": len(scoped),
        "chats_walked": 0,
        "messages_scanned": 0,
        "errors": 0,
    }
    hits: list[dict] = []

    def work(c: dict):
        try:
            d = client.get(f"/chats/{c['id']}/messages", params={"limit": msgs_per_chat})
            msgs = d.get("data", {}).get("messages", [])
            matches = []
            for m in msgs:
                txt = m.get("text") or ""
                matched_term = None
                for p, term in zip(patterns, terms):
                    if p.search(txt):
                        matched_term = term
                        break
                if matched_term:
                    matches.append(
                        {
                            "uid": m.get("uid"),
                            "timestamp": m.get("timestamp"),
                            "from_me": bool(m.get("from_me")),
                            "sender_name": m.get("sender_name"),
                            "sender_phone": m.get("sender_phone"),
                            "message_type": m.get("message_type"),
                            "matched": matched_term,
                            "snippet": txt[:280],
                        }
                    )
            return c, msgs, matches, None
        except Exception as e:
            return c, [], [], str(e)

    t0 = time.time()
    done = 0
    with cf.ThreadPoolExecutor(max_workers=max(1, concurrency)) as ex:
        futures = [ex.submit(work, c) for c in scoped]
        for fut in cf.as_completed(futures):
            c, msgs, matches, err = fut.result()
            done += 1
            stats["chats_walked"] += 1
            stats["messages_scanned"] += len(msgs)
            if err:
                stats["errors"] += 1
            if matches:
                hits.append(
                    {
                        "chat": {
                            "id": c["id"],
                            "name": c.get("name"),
                            "phone": c.get("phone"),
                            "whatsapp_account_id": c.get("whatsapp_account_id"),
                            "last_message_timestamp": c.get("last_message_timestamp"),
                            "labels": [
                                (l.get("name") if isinstance(l, dict) else str(l))
                                for l in (c.get("labels") or [])
                            ],
                        },
                        "matches": matches,
                    }
                )
            if done % 100 == 0 or done == len(scoped):
                log(
                    f"  walk {done}/{len(scoped)} t={time.time()-t0:.0f}s "
                    f"hits={len(hits)} errs={stats['errors']}"
                )
    stats["elapsed_seconds"] = int(time.time() - t0)
    return hits, stats


# ----------------------------- CLI ------------------------------------

def parse_terms(raw: str) -> list[str]:
    # comma separates OR terms; keep multi-word phrases intact.
    return [t.strip() for t in raw.split(",") if t.strip()]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="tla-search",
        description="Content search across a TimelinesAI workspace.",
    )
    p.add_argument("query", nargs="?", help="Comma-separated terms (OR'd).")
    p.add_argument("--days", type=int, default=None, help="Only chats with activity within last N days.")
    p.add_argument("--number", default=None, help="Filter by WhatsApp account (phone or JID substring).")
    p.add_argument("--label", default=None, help="Filter by chat label (exact match).")
    p.add_argument("--msgs-per-chat", type=int, default=200, help="Messages pulled per chat (default 200).")
    p.add_argument("--limit-chats", type=int, default=None, help="Cap on chats walked (debug).")
    p.add_argument("--limit-hits", type=int, default=500, help="Cap on result rows (default 500).")
    p.add_argument("--concurrency", type=int, default=2, help="Parallel workers (default 2; tested safe with fresh-connection-per-request pattern).")
    p.add_argument("--cache", default=str(DEFAULT_CACHE), help="SQLite cache path (default ~/.tla-cache.sqlite).")
    p.add_argument("--no-cache", action="store_true", help="Direct API walk, no local cache.")
    p.add_argument("--refresh", action="store_true", help="Sync cache before searching.")
    p.add_argument("--sync-only", action="store_true", help="Only sync the cache, don't search.")
    p.add_argument("--force-full", action="store_true", help="Re-fetch every scoped chat's messages.")
    p.add_argument("--output", default=None, help="Write JSON to file instead of stdout.")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    token = os.environ.get("TIMELINES_AI_API_KEY")
    if not token:
        print("ERROR: TIMELINES_AI_API_KEY is not set.", file=sys.stderr)
        return 1

    def log(msg: str) -> None:
        if args.verbose:
            print(msg, file=sys.stderr, flush=True)

    client = Client(token, verbose=args.verbose)

    if args.no_cache:
        if not args.query:
            print("ERROR: query is required with --no-cache.", file=sys.stderr)
            return 1
        terms = parse_terms(args.query)
        t0 = time.time()
        hits, stats = direct_search(
            client, terms,
            days=args.days, number=args.number, label=args.label,
            msgs_per_chat=args.msgs_per_chat,
            limit_chats=args.limit_chats,
            concurrency=args.concurrency,
            log=log,
        )
        result = {
            "mode": "direct",
            "query": terms,
            "scope": {"days": args.days, "number": args.number, "label": args.label},
            "stats": stats,
            "hits": hits,
            "elapsed_seconds": int(time.time() - t0),
        }
    else:
        cache_path = Path(args.cache).expanduser()
        conn = open_cache(cache_path)
        sync_stats = None
        if args.refresh or args.sync_only:
            log(f"Syncing cache at {cache_path}...")
            sync_stats = sync(
                client, conn,
                days=args.days, number=args.number, label=args.label,
                msgs_per_chat=args.msgs_per_chat,
                concurrency=args.concurrency,
                log=log,
                force_full=args.force_full,
            )
            log(f"Sync: {sync_stats}")

        if args.sync_only:
            result = {"mode": "sync-only", "cache": str(cache_path), "stats": sync_stats}
        else:
            if not args.query:
                print("ERROR: query is required unless --sync-only.", file=sys.stderr)
                return 1
            terms = parse_terms(args.query)
            t0 = time.time()
            rows = search_cache(conn, terms, args.days, args.number, args.label, args.limit_hits)
            hits = group_hits(rows)
            result = {
                "mode": "cache",
                "query": terms,
                "scope": {"days": args.days, "number": args.number, "label": args.label},
                "cache": str(cache_path),
                "stats": {
                    "messages_matched": len(rows),
                    "chats_with_hits": len(hits),
                    "elapsed_seconds": round(time.time() - t0, 2),
                },
                "sync": sync_stats,
                "hits": hits,
            }

    out = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    if args.output:
        Path(args.output).write_text(out, encoding="utf-8")
        print(args.output)
    else:
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
