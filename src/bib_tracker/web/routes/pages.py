"""Server-rendered pages.

Each handler builds a context and hands it to render(), which serves the full
page or just the fragment depending on whether HTMX asked.
"""

from __future__ import annotations

import csv
import io
from datetime import date
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from ...db.connection import Database
from ...library.media_class import MediaClass
from ...metadata.covers import VARIANTS, placeholder_svg, read_cover
from ...metadata.lookup import lookup_isbn
from ..filters import MEDIA_CLASS_LABELS
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


@router.get("/loans", include_in_schema=False)
async def loans_redirect(request: Request) -> RedirectResponse:
    """The loans table merged into the history table; bookmarks and dashboard
    links keep working by landing on the same filter the old page showed."""
    return RedirectResponse(url="/history?state=open", status_code=302)


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
            m.cover_sha256,
            l.last_due_date, l.times_renewed, l.max_renewals, l.duration_days,
            l.duration_uncertainty_days,
            m.effective_price_cents AS price_cents, m.price_basis,
            r.rating,
            CAST(julianday(l.last_due_date) - julianday(date('now')) AS INTEGER) AS days_remaining,
            (SELECT COUNT(*) FROM loans x WHERE x.media_id = l.media_id) AS borrow_count
        FROM loans l
        JOIN media m ON m.id = l.media_id
        JOIN copies c ON c.id = l.copy_id
        JOIN accounts a ON a.id = l.account_id
        LEFT JOIN loan_overrides o ON o.loan_key = l.loan_key
        LEFT JOIN ratings r ON r.media_id = m.id
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
        # The renew banner renders only when a fragment carries outcomes;
        # an empty list keeps the full page quiet.
        "renewals": [],
    }
    return render(request, "pages/history.html", "partials/history_results.html", context)


@router.get("/history/add", response_class=HTMLResponse)
async def history_add_form(request: Request) -> HTMLResponse:
    return render(request, "pages/history_add.html", None, await _shell(request, "/history"))


@router.post("/history/add/lookup", response_class=HTMLResponse)
async def history_add_lookup(request: Request) -> HTMLResponse:
    """Resolve an ISBN before saving, so the match can be eyeballed first."""
    form = await request.form()
    isbn = str(form.get("isbn") or "").replace("-", "").strip()
    if not isbn:
        return render(
            request, "partials/lookup_result.html", "partials/lookup_result.html", {"record": None, "searched": False}
        )

    worker = getattr(request.app.state, "enrichment", None)
    record = None
    if worker is not None:
        record = await lookup_isbn(worker, isbn)

    return render(
        request,
        "partials/lookup_result.html",
        "partials/lookup_result.html",
        {"record": record, "searched": True},
    )


@router.post("/history/add", response_class=HTMLResponse)
async def history_add(request: Request) -> HTMLResponse:
    from ...library.manual import ManualLoan, add_past_loan

    form = await request.form()
    try:
        entry = ManualLoan(
            account=str(form["account"]),
            title=str(form["title"]).strip(),
            author=str(form.get("author") or "").strip() or None,
            isbn=str(form.get("isbn") or "").replace("-", "").strip() or None,
            media_class=MediaClass(str(form.get("media_class") or "book")),
            lend_date=date.fromisoformat(str(form["lend_date"])),
            return_date=(date.fromisoformat(str(form["return_date"])) if form.get("return_date") else None),
        )
    except (KeyError, ValueError) as err:
        raise HTTPException(status_code=400, detail=f"Unvollständige Angaben: {err}") from err

    if entry.return_date and entry.return_date < entry.lend_date:
        raise HTTPException(status_code=400, detail="Das Rückgabedatum liegt vor dem Ausleihdatum")

    try:
        await add_past_loan(_db(request), request.app.state.settings, entry)
    except KeyError as err:
        raise HTTPException(status_code=404, detail=str(err)) from err

    return HTMLResponse(f'<div class="notice">Eingetragen: <a href="/history">{entry.title}</a></div>')


@router.get("/stats", response_class=HTMLResponse)
async def stats_page(request: Request) -> HTMLResponse:
    from ... import stats as stats_module

    db = _db(request)
    settings = request.app.state.settings
    data = await stats_module.overview(db, settings)

    context = await _shell(request, "/stats")
    context |= data
    context["pace_points"] = [{"label": row["week"], "value": row["count"], "token": "primary"} for row in data["pace"]]
    return render(request, "pages/stats.html", None, context)


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

    provider_rows = await db.fetch_all(
        "SELECT provider, rating_value, rating_scale, rating_count FROM metadata_records"
        " WHERE media_id = ? AND status = 'ok' AND rating_value IS NOT NULL",
        (media_id,),
    )

    context = await _shell(request, "/history")
    context |= {
        "media": dict(media),
        "loans": [dict(row) for row in rows],
        "external_ratings": [
            {
                "provider": row["provider"],
                "value": row["rating_value"],
                "scale": row["rating_scale"],
                "count": row["rating_count"],
            }
            for row in provider_rows
        ],
    }
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


@router.get("/media/{media_id}/cover/{variant}.webp")
async def cover(request: Request, media_id: int, variant: str) -> Response:
    """Serve a stored cover, or a generated stand-in.

    Never a 404: a missing cover should look like a plain book, not a broken
    image, and a wall of placeholders should still read as a shelf.
    """
    if variant not in VARIANTS:
        raise HTTPException(status_code=404, detail="Unknown variant")

    db = _db(request)
    stored = await read_cover(db, media_id, variant)
    if stored is None:
        row = await db.fetch_one("SELECT title, media_class FROM media WHERE id = ?", (media_id,))
        title = row["title"] if row else "?"
        media_class = MEDIA_CLASS_LABELS.get(row["media_class"], "") if row else ""
        return Response(
            placeholder_svg(title, media_class),
            media_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=3600"},
        )

    data, mime, etag = stored
    if request.headers.get("if-none-match") == f'"{etag}"':
        return Response(status_code=304)

    return Response(
        data,
        media_type=mime,
        headers={
            "ETag": f'"{etag}"',
            # The URL carries the digest, so a new cover is a new URL.
            "Cache-Control": "public, max-age=31536000, immutable",
        },
    )


EXPORT_COLUMNS = (
    "account",
    "title",
    "author",
    "media_class",
    "state",
    "lend_date",
    "lend_date_source",
    "lend_date_earliest",
    "lend_date_latest",
    "return_date",
    "return_date_source",
    "return_date_earliest",
    "return_date_latest",
    "duration_days",
    "duration_uncertainty_days",
    "first_due_date",
    "last_due_date",
    "times_renewed",
    "max_renewals",
    "was_overdue",
    "max_overdue_days",
    "branch",
    "call_number",
    "isbn13",
    "price_cents",
    "price_basis",
    "rating",
)


async def _export_rows(db: Database) -> list[dict[str, Any]]:
    """Everything, with each date's provenance beside it.

    An export that dropped the source columns would turn estimates into facts
    the moment the file left the application.
    """
    rows = await db.fetch_all(
        """
        SELECT
            a.name AS account, m.title, m.author, m.media_class, l.state,
            COALESCE(o.lend_date, l.lend_date) AS lend_date,
            CASE WHEN o.lend_date IS NOT NULL THEN 'manual' ELSE l.lend_date_source END
                AS lend_date_source,
            l.lend_date_earliest, l.lend_date_latest,
            COALESCE(o.return_date, l.return_date) AS return_date,
            CASE WHEN o.return_date IS NOT NULL THEN 'manual' ELSE l.return_date_source END
                AS return_date_source,
            l.return_date_earliest, l.return_date_latest,
            l.duration_days, l.duration_uncertainty_days,
            l.first_due_date, l.last_due_date, l.times_renewed, l.max_renewals,
            l.was_overdue, l.max_overdue_days,
            c.branch, c.call_number, m.isbn13,
            m.effective_price_cents AS price_cents, m.price_basis, r.rating
        FROM loans l
        JOIN media m ON m.id = l.media_id
        JOIN copies c ON c.id = l.copy_id
        JOIN accounts a ON a.id = l.account_id
        LEFT JOIN loan_overrides o ON o.loan_key = l.loan_key
        LEFT JOIN ratings r ON r.media_id = m.id
        ORDER BY lend_date DESC, l.id DESC
        """
    )
    return [dict(row) for row in rows]


@router.get("/export/history.csv")
async def export_csv(request: Request) -> Response:
    rows = await _export_rows(_db(request))

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(EXPORT_COLUMNS), extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)

    return Response(
        buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="bib-tracker-verlauf.csv"'},
    )


@router.get("/export/history.json")
async def export_json(request: Request) -> JSONResponse:
    return JSONResponse({"loans": await _export_rows(_db(request))})


@router.get("/partials/poll-status", response_class=HTMLResponse)
async def poll_status(request: Request) -> HTMLResponse:
    context = {"accounts": await _accounts(_db(request))}
    return render(request, "partials/poll_status.html", "partials/poll_status.html", context)
