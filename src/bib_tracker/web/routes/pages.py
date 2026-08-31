"""Server-rendered pages.

Each handler builds a context and hands it to render(), which serves the full
page or just the fragment depending on whether HTMX asked.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from ...db.connection import Database
from ..render import render

router = APIRouter()

MEDIA_CLASSES = ("book", "audiobook", "music", "movie", "game", "magazine", "other")


def _db(request: Request) -> Database:
    db: Database = request.app.state.db
    return db


async def _accounts(db: Database) -> list[dict[str, Any]]:
    rows = await db.fetch_all(
        "SELECT id, name, display_name, colour, last_success_at FROM accounts WHERE removed_at IS NULL ORDER BY name"
    )
    return [dict(row) for row in rows]


async def _counts(db: Database) -> dict[str, int]:
    open_loans = await db.fetch_one("SELECT COUNT(*) AS n FROM loans WHERE state = 'open'")
    failed = await db.fetch_one(
        "SELECT COUNT(*) AS n FROM poll_runs WHERE status NOT IN ('success', 'running')"
        " AND started_at >= datetime('now', '-7 day')"
    )
    unrated = await db.fetch_one(
        """
        SELECT COUNT(DISTINCT l.media_id) AS n
        FROM loans l LEFT JOIN ratings r ON r.media_id = l.media_id
        WHERE l.state = 'returned' AND r.rating IS NULL AND r.dismissed_at IS NULL
              AND l.return_date >= date('now', '-90 day')
        """
    )
    return {
        "open_loans": int(open_loans["n"]) if open_loans else 0,
        "failed_runs": int(failed["n"]) if failed else 0,
        "unrated": int(unrated["n"]) if unrated else 0,
    }


async def _shell(request: Request, active: str) -> dict[str, Any]:
    db = _db(request)
    return {
        "active": active,
        "accounts": await _accounts(db),
        "counts": await _counts(db),
    }


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    db = _db(request)
    context = await _shell(request, "/")

    open_loans = await db.fetch_one("SELECT COUNT(*) AS n FROM loans WHERE state = 'open'")
    returned = await db.fetch_one("SELECT COUNT(*) AS n FROM loans WHERE state = 'returned'")
    works = await db.fetch_one("SELECT COUNT(*) AS n FROM media")
    tracking_since = await db.fetch_one("SELECT MIN(started_at) AS since FROM poll_runs")

    # Duration statistics exclude loans whose start is unknowable, and say so.
    median = await db.fetch_one(
        """
        SELECT AVG(days_held) AS avg_days, COUNT(*) AS n FROM v_loan_durations
        WHERE state = 'returned' AND lend_unreliable = 0
        """
    )

    since = tracking_since["since"][:10] if tracking_since and tracking_since["since"] else None
    tiles = [
        {"value": int(open_loans["n"]) if open_loans else 0, "label": "ausgeliehen"},
        {"value": int(returned["n"]) if returned else 0, "label": "zurückgegeben"},
        {"value": int(works["n"]) if works else 0, "label": "Werke"},
    ]
    if median and median["n"]:
        tiles.append(
            {
                "value": f"{round(median['avg_days'])} T",
                "label": f"Ø Leihdauer ({median['n']} Ausleihen)",
                "confidence": "estimate",
            }
        )
    if since:
        days = (date.today() - date.fromisoformat(since)).days
        tiles.append({"value": f"{days} T", "label": "aufgezeichnet seit"})

    context |= {
        "tiles": tiles,
        "due_soon": await _due_soon(db),
        "recent": await _recent_returns(db),
    }
    return render(request, "pages/dashboard.html", None, context)


async def _due_soon(db: Database) -> list[dict[str, Any]]:
    rows = await db.fetch_all(
        """
        SELECT m.title, a.name AS account, l.last_due_date AS due_date,
               CAST(julianday(l.last_due_date) - julianday(date('now')) AS INTEGER) AS days_remaining
        FROM loans l
        JOIN media m ON m.id = l.media_id
        JOIN accounts a ON a.id = l.account_id
        WHERE l.state = 'open' AND l.last_due_date <= date('now', '+7 day')
        ORDER BY l.last_due_date
        """
    )
    return [dict(row) for row in rows]


async def _recent_returns(db: Database) -> list[dict[str, Any]]:
    rows = await db.fetch_all(
        """
        SELECT m.id AS media_id, m.title, m.media_class, d.days_held AS duration_days
        FROM v_loan_durations d
        JOIN media m ON m.id = d.media_id
        WHERE d.state = 'returned'
        ORDER BY d.eff_return_date DESC
        LIMIT 8
        """
    )
    return [dict(row) for row in rows]


@router.get("/loans", response_class=HTMLResponse)
async def loans(request: Request) -> HTMLResponse:
    db = _db(request)
    rows = await db.fetch_all(
        """
        SELECT l.loan_key, m.id AS media_id, m.title, m.author, m.media_class,
               a.name AS account, a.colour AS account_colour,
               l.last_due_date AS due_date, l.lend_date, l.lend_date_source,
               l.lend_date_earliest, l.lend_date_latest,
               l.times_renewed, l.max_renewals, l.can_be_renewed,
               CAST(julianday(l.last_due_date) - julianday(date('now')) AS INTEGER) AS days_remaining
        FROM loans l
        JOIN media m ON m.id = l.media_id
        JOIN accounts a ON a.id = l.account_id
        WHERE l.state = 'open'
        ORDER BY l.last_due_date, m.title
        """
    )
    context = await _shell(request, "/loans")
    context["loans"] = [dict(row) for row in rows]
    return render(request, "pages/loans.html", "partials/loans_table.html", context)


@router.get("/history", response_class=HTMLResponse)
async def history(request: Request) -> HTMLResponse:
    db = _db(request)
    params = request.query_params

    state = params.get("state") or "all"
    search = (params.get("q") or "").strip()
    media_classes = [value for value in params.getlist("media_class") if value in MEDIA_CLASSES]
    accounts = params.getlist("account")

    where: list[str] = []
    args: list[Any] = []

    if state == "open":
        where.append("l.state = 'open'")
    elif state == "returned":
        where.append("l.state = 'returned'")
    elif state == "overdue":
        where.append("l.was_overdue = 1")

    if media_classes:
        where.append(f"m.media_class IN ({','.join('?' * len(media_classes))})")
        args.extend(media_classes)

    if accounts:
        where.append(f"a.name IN ({','.join('?' * len(accounts))})")
        args.extend(accounts)

    if search:
        # LIKE rather than FTS for a short fragment: prefix matching on two or
        # three characters is what a person typing expects.
        where.append("(m.title LIKE ? OR m.author LIKE ? OR c.call_number LIKE ?)")
        pattern = f"%{search}%"
        args.extend([pattern, pattern, pattern])

    clause = f"WHERE {' AND '.join(where)}" if where else ""

    rows = await db.fetch_all(
        f"""
        SELECT
            l.loan_key, l.state, m.id AS media_id, m.title, m.author, m.media_class,
            a.name AS account, a.colour AS account_colour, c.branch,
            COALESCE(o.lend_date, l.lend_date) AS lend_date,
            CASE WHEN o.lend_date IS NOT NULL THEN 'manual' ELSE l.lend_date_source END
                AS lend_date_source,
            l.lend_date_earliest, l.lend_date_latest,
            COALESCE(o.return_date, l.return_date) AS return_date,
            CASE WHEN o.return_date IS NOT NULL THEN 'manual' ELSE l.return_date_source END
                AS return_date_source,
            l.return_date_earliest, l.return_date_latest,
            l.last_due_date, l.times_renewed, l.max_renewals, l.duration_days,
            l.duration_uncertainty_days,
            CAST(julianday(l.last_due_date) - julianday(date('now')) AS INTEGER) AS days_remaining,
            (SELECT COUNT(*) FROM loans x WHERE x.media_id = l.media_id) AS borrow_count
        FROM loans l
        JOIN media m ON m.id = l.media_id
        JOIN copies c ON c.id = l.copy_id
        JOIN accounts a ON a.id = l.account_id
        LEFT JOIN loan_overrides o ON o.loan_key = l.loan_key
        {clause}
        ORDER BY lend_date DESC, l.id DESC
        LIMIT 200
        """,
        args,
    )

    loan_rows = [dict(row) for row in rows]
    works = len({row["media_id"] for row in loan_rows})
    untracked = sum(1 for row in loan_rows if row["lend_date_source"] == "before_tracking")

    context = await _shell(request, "/history")
    context |= {
        "loans": loan_rows,
        "total": len(loan_rows),
        "works": works,
        "untracked": untracked,
        "filters": {"q": search, "state": state, "media_class": media_classes, "account": accounts},
        "query_string": urlencode([(key, value) for key, value in params.multi_items() if key != "page"]),
    }
    return render(request, "pages/history.html", "partials/history_results.html", context)


@router.get("/runs", response_class=HTMLResponse)
async def runs(request: Request) -> HTMLResponse:
    db = _db(request)
    rows = await db.fetch_all(
        """
        SELECT r.*, a.name AS account FROM poll_runs r
        JOIN accounts a ON a.id = r.account_id
        ORDER BY r.started_at DESC, r.id DESC LIMIT 100
        """
    )
    stats = await db.fetch_one(
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS ok,
            SUM(CASE WHEN status = 'suspect' THEN 1 ELSE 0 END) AS suspect
        FROM poll_runs WHERE finished_at IS NOT NULL
        """
    )

    total = int(stats["total"]) if stats and stats["total"] else 0
    ok = int(stats["ok"]) if stats and stats["ok"] else 0
    health = f"{ok} von {total} Abfragen erfolgreich" if total else "Noch keine Abfrage gelaufen"

    context = await _shell(request, "/runs")
    context |= {
        "runs": [dict(row) for row in rows],
        "health": health,
        "suspect": int(stats["suspect"]) if stats and stats["suspect"] else 0,
    }
    return render(request, "pages/runs.html", None, context)


@router.get("/media/{media_id}", response_class=HTMLResponse)
async def media_detail(request: Request, media_id: int) -> HTMLResponse:
    db = _db(request)
    media = await db.fetch_one(
        """
        SELECT m.*, r.rating, r.review, r.favourite, r.abandoned
        FROM media m LEFT JOIN ratings r ON r.media_id = m.id
        WHERE m.id = ?
        """,
        (media_id,),
    )
    if media is None:
        raise HTTPException(status_code=404, detail="Unbekanntes Werk")

    rows = await db.fetch_all(
        """
        SELECT l.*, a.name AS account FROM loans l
        JOIN accounts a ON a.id = l.account_id
        WHERE l.media_id = ? ORDER BY l.lend_date DESC
        """,
        (media_id,),
    )

    context = await _shell(request, "/history")
    context |= {"media": dict(media), "loans": [dict(row) for row in rows]}
    return render(request, "pages/media.html", None, context)


async def _unrated(db: Database, after: int | None = None) -> tuple[dict[str, Any] | None, int]:
    """The next returned-but-unrated work, and how many are waiting.

    Recent returns only: being asked about something taken back nine months
    ago is not a prompt anyone answers usefully.
    """
    exclude = "AND m.id != ?" if after is not None else ""
    args: list[Any] = [after] if after is not None else []

    rows = await db.fetch_all(
        f"""
        SELECT m.id, m.title, m.author, m.media_class,
               a.name AS account, d.eff_lend_date AS lend_date,
               d.eff_return_date AS return_date, d.days_held AS duration_days
        FROM v_loan_durations d
        JOIN media m ON m.id = d.media_id
        JOIN loans l ON l.id = d.id
        JOIN accounts a ON a.id = l.account_id
        LEFT JOIN ratings r ON r.media_id = m.id
        WHERE d.state = 'returned'
          AND r.rating IS NULL
          AND (r.dismissed_at IS NULL)
          AND d.eff_return_date >= date('now', '-90 day')
          {exclude}
        ORDER BY d.eff_return_date DESC
        """,
        args,
    )
    seen: dict[int, dict[str, Any]] = {}
    for row in rows:
        seen.setdefault(row["id"], dict(row))
    queue = list(seen.values())
    return (queue[0] if queue else None), len(queue)


@router.get("/rate", response_class=HTMLResponse)
async def rate(request: Request) -> HTMLResponse:
    media, remaining = await _unrated(_db(request))
    context = await _shell(request, "/rate")
    context |= {"media": media, "remaining": remaining}
    return render(request, "pages/rate.html", None, context)


@router.get("/rate/next", response_class=HTMLResponse)
async def rate_next(request: Request, after: int | None = None) -> HTMLResponse:
    media, remaining = await _unrated(_db(request), after)
    context = {"media": media, "remaining": remaining}
    return render(request, "partials/rate_card.html", "partials/rate_card.html", context)


@router.get("/partials/poll-status", response_class=HTMLResponse)
async def poll_status(request: Request) -> HTMLResponse:
    context = {"accounts": await _accounts(_db(request))}
    return render(request, "partials/poll_status.html", "partials/poll_status.html", context)
