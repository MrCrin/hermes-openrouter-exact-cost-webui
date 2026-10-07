"""Route implementations for the OpenRouter Exact Cost sidecar.

This is the ONLY file this extension authors on top of the canonical scaffold
(``sidecar.py`` and ``sidecar_base.py`` stay byte-identical to their canonical
copies). Auth is handled entirely by the scaffold: every route here is reachable
only with a valid WebUI-injected ``X-Hermes-Sidecar-Token``; ``/health`` is the
sole tokenless route.

Routes (proxied at /api/extensions/hermes-openrouter-exact-cost/sidecar/...):
  GET /api/session-cost?session=<hermes_session_id>
      Exact OpenRouter spend for that session.
  GET /api/session-costs?ids=<id>,<id>,...
      The same for several sessions at once (for the session list).

HOW THE NUMBER IS OBTAINED (and why not another way)
----------------------------------------------------
Hermes logs every main-loop API call with the OpenRouter generation id it was
billed under (``id=gen-...``), tagged with the Hermes session id. We harvest
those ids per session, price each via ``GET /api/v1/generation``, and cache the
answer in a local SQLite file. Consequences:

* the figure is OpenRouter's own ``total_cost``, not a local estimate;
* the cache makes repeat calls free and means log rotation can never lose
  history we have already reconciled;
* ``/api/v1/analytics/query`` (the documented "spend by session_id" endpoint) is
  deliberately NOT used: it returns 403 for a normal API key
  ("Only management keys can access analytics").

HONESTY RULE (why this file is fussy about completeness)
--------------------------------------------------------
Two different things can stop a call from being priced, and they must not be
reported the same way.

1. **Not attributable.** ``id=`` on the log line is whatever id the *provider*
   returned (``agent/turn_usage.py``: ``_rid = getattr(response, "id", None)``).
   Only OpenRouter returns ``gen-...``. A turn served by another provider has no
   OpenRouter record at all and can never be priced from here.

2. **Not yet queryable.** ``/generation`` is eventually consistent: a call that
   OpenRouter has just served and billed can 404 for a short while before its
   record appears (verified against this repo's own session — a call at
   00:49:33 resolved while 00:49:37 and 00:49:47 were still 404). Treating that
   as "missing spend" would cry wolf on every active turn.

So calls that cannot be priced are split three ways rather than lumped together:

    other_provider_calls: N  no OpenRouter generation id — a different provider
                             served them and they are invisible here by design
    unpriced_calls: N        had an OpenRouter id but is still unpricable past
                             the grace window (unknown cost, not zero cost)
    pending_calls: N         younger than _PENDING_GRACE — expected to resolve

``complete`` is true only when the first two are both 0, and ``has_data`` is
false when the session never used OpenRouter at all. A caller must never present
an incomplete figure as exact, and must never show ``$0.00`` for a session that
actually spent money on another provider.

Config: HERMES_HOME (default ``~/.hermes``), OPENROUTER_API_KEY (else read from
``$HERMES_HOME/.env``).
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API = "https://openrouter.ai/api/v1"
_LOG_GLOB = "agent.log*"
_HARVEST_TTL = 20.0        # seconds; avoids re-scanning the log on every poll
_PENDING_GRACE = 600.0     # seconds a just-billed call may 404 before it counts
_GEN_RE = re.compile(r"id=(gen-[A-Za-z0-9_-]+)")
_CALL_RE = re.compile(r"API call #\d+")
_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")

# {session_id: (monotonic_ts, [(gen_id, log_epoch), ...], unattributable_count)}
_harvest_cache: dict[str, tuple[float, list[tuple[str, float]], int]] = {}


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()


def _api_key() -> str | None:
    key = os.environ.get("OPENROUTER_API_KEY")
    if key:
        return key.strip()
    env = _hermes_home() / ".env"
    try:
        for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith("OPENROUTER_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return None


def _db_path() -> Path:
    return Path(__file__).with_name("costs.db")


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(_db_path(), timeout=10)
    con.execute(
        "CREATE TABLE IF NOT EXISTS generations ("
        " gen_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,"
        " model TEXT, cost_usd REAL, created_at REAL,"
        " priced_at REAL NOT NULL)"
    )
    con.execute("CREATE INDEX IF NOT EXISTS ix_gen_session ON generations(session_id)")
    return con


def _log_epoch(line: str) -> float:
    """Wall-clock epoch of a log line, or 0.0 if the timestamp is unreadable.

    Hermes writes UTC timestamps, so no local-timezone conversion is applied.
    """
    m = _TS_RE.match(line)
    if not m:
        return 0.0
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        ).timestamp()
    except ValueError:
        return 0.0


def _harvest(session_id: str) -> tuple[list[tuple[str, float]], int]:
    """([(generation_id, log_time), ...], count of unattributable call lines).

    A line is an API call iff it carries ``API call #N``. If it also carries an
    OpenRouter ``id=gen-...`` we can price it; otherwise it was served by another
    provider (or logged without an id) and is counted as unattributable rather
    than silently dropped from the total.
    """
    now = time.monotonic()
    hit = _harvest_cache.get(session_id)
    if hit and now - hit[0] < _HARVEST_TTL:
        return hit[1], hit[2]

    logs = sorted((_hermes_home() / "logs").glob(_LOG_GLOB))
    logs.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    needle = f"[{session_id}]"
    found: list[tuple[str, float]] = []
    seen: set[str] = set()
    unattributable = 0
    for log in logs:
        try:
            with log.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if needle not in line:
                        continue
                    m = _GEN_RE.search(line)
                    if m:
                        if m.group(1) not in seen:
                            seen.add(m.group(1))
                            found.append((m.group(1), _log_epoch(line)))
                    elif _CALL_RE.search(line):
                        unattributable += 1
        except OSError:
            continue
    _harvest_cache[session_id] = (now, found, unattributable)
    return found, unattributable


def _price(gen_id: str, key: str) -> dict | None:
    """One generation's billed cost from OpenRouter, or None if unrecoverable."""
    url = f"{API}/generation?" + urllib.parse.urlencode({"id": gen_id})
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.load(resp).get("data") or {}
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError):
        return None
    return {
        "model": data.get("model"),
        "cost_usd": float(data.get("total_cost") or 0.0),
        "created_at": data.get("created_at"),
    }


def _reconcile(session_id: str) -> dict:
    """Exact OpenRouter spend for one session, with completeness accounting."""
    key = _api_key()
    con = _connect()
    try:
        known = {
            row[0]: (row[1], row[2])
            for row in con.execute(
                "SELECT gen_id, cost_usd, model FROM generations WHERE session_id=?",
                (session_id,),
            )
        }
        wanted, other_provider = _harvest(session_id)
        wall_now = time.time()
        pending = 0
        unpriced = 0

        for gen_id, seen_at in wanted:
            if gen_id in known:
                continue
            priced = _price(gen_id, key) if key else None
            if priced is None:
                # Too fresh to have a billing record yet? Expected, not a defect.
                if seen_at and (wall_now - seen_at) <= _PENDING_GRACE:
                    pending += 1
                else:
                    unpriced += 1
                continue
            con.execute(
                "INSERT OR REPLACE INTO generations"
                " (gen_id, session_id, model, cost_usd, created_at, priced_at)"
                " VALUES (?,?,?,?,?,?)",
                (gen_id, session_id, priced["model"], priced["cost_usd"],
                 priced["created_at"], time.time()),
            )
            known[gen_id] = (priced["cost_usd"], priced["model"])
        con.commit()

        by_model: dict[str, dict] = {}
        total = 0.0
        for cost, model in known.values():
            total += cost or 0.0
            entry = by_model.setdefault(model or "?", {"calls": 0, "cost_usd": 0.0})
            entry["calls"] += 1
            entry["cost_usd"] = round(entry["cost_usd"] + (cost or 0.0), 6)

        return {
            "session": session_id,
            "total_usd": round(total, 6),
            "generations": len(known),
            "other_provider_calls": other_provider,
            "unpriced_calls": unpriced,
            "pending_calls": pending,
            "has_data": len(known) > 0,
            "complete": (other_provider + unpriced) == 0,
            "source": "openrouter_generation_api",
            "by_model": by_model,
        }
    finally:
        con.close()


def register(app) -> None:
    @app.route("GET", "/api/session-cost")
    def session_cost(req):
        session_id = (req.query_one("session") or "").strip()
        if not session_id:
            return app.json({"error": "session is required"}, status=400)
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", session_id):
            return app.json({"error": "invalid session id"}, status=400)
        if _api_key() is None:
            return app.json(
                {"error": "OPENROUTER_API_KEY not found in env or $HERMES_HOME/.env"},
                status=503,
            )
        return app.json(_reconcile(session_id))

    @app.route("GET", "/api/session-costs")
    def session_costs(req):
        raw = (req.query_one("ids") or "").strip()
        ids = [s for s in (part.strip() for part in raw.split(",")) if s][:50]
        if not ids:
            return app.json({"error": "ids is required"}, status=400)
        out = {}
        for session_id in ids:
            if re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", session_id):
                out[session_id] = _reconcile(session_id)
        return app.json({"sessions": out})
