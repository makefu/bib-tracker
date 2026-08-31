"""JSON API.

The web interface reads the same endpoints as HTMX fragments; for now they are
JSON only, which is what the VM test asserts against.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from ...db import queries
from ...db.connection import Database
from ...library.reconcile import apply_overrides
from ...services import PollInProgressError, PollService, RenewalService
from ..render import render

router = APIRouter(prefix="/api")


def _db(request: Request) -> Database:
    db: Database = request.app.state.db
    return db


def _service(request: Request) -> PollService:
    service: PollService | None = getattr(request.app.state, "poll_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Polling is not configured")
    return service


@router.get("/loans")
async def list_loans(request: Request) -> JSONResponse:
    """Everything currently on loan, from the most recent usable poll."""
    db = _db(request)
    accounts = await db.fetch_all("SELECT id, name FROM accounts WHERE removed_at IS NULL ORDER BY name")

    loans: list[dict[str, Any]] = []
    for account in accounts:
        for item in await queries.latest_snapshot(db, account["id"]):
            loans.append(
                {
                    "account": account["name"],
                    "copy_key": item["copy_key"],
                    "title": item["title"],
                    "author": item["author"],
                    "media_type": item["media_type"],
                    "call_number": item["call_number"],
                    "branch": item["branch"],
                    "due_date": item["due_date"],
                    "times_renewed": item["times_renewed"],
                    "max_renewals": item["max_renewals"],
                    "can_be_renewed": bool(item["can_be_renewed"]),
                    "isbn": item["isbn"],
                    "detail_url": item["detail_url"],
                }
            )

    return JSONResponse({"open_loans": len(loans), "loans": loans})


@router.get("/history")
async def history(request: Request, limit: int = 100, state: str | None = None) -> JSONResponse:
    """Everything ever borrowed, current and returned.

    Dates come with their source and bounds so a caller can tell an observed
    date from an inferred one rather than presenting a guess as a fact.
    """
    db = _db(request)
    where = ""
    params: list[Any] = []
    if state in {"open", "returned"}:
        where = "WHERE l.state = ?"
        params.append(state)

    rows = await db.fetch_all(
        f"""
        SELECT
            l.loan_key, l.state, a.name AS account, m.id AS media_id, m.title, m.author,
            m.media_class,
            c.branch, c.call_number,
            COALESCE(o.lend_date, l.lend_date) AS lend_date,
            CASE WHEN o.lend_date IS NOT NULL THEN 'manual' ELSE l.lend_date_source END AS lend_date_source,
            l.lend_date_earliest, l.lend_date_latest,
            COALESCE(o.return_date, l.return_date) AS return_date,
            CASE WHEN o.return_date IS NOT NULL THEN 'manual' ELSE l.return_date_source END
                AS return_date_source,
            l.return_date_earliest, l.return_date_latest,
            l.first_due_date, l.last_due_date, l.times_renewed, l.max_renewals,
            l.was_overdue, l.max_overdue_days, l.duration_days, l.duration_uncertainty_days
        FROM loans l
        JOIN media m ON m.id = l.media_id
        JOIN copies c ON c.id = l.copy_id
        JOIN accounts a ON a.id = l.account_id
        LEFT JOIN loan_overrides o ON o.loan_key = l.loan_key
        {where}
        ORDER BY lend_date DESC, l.id DESC
        LIMIT ?
        """,
        [*params, limit],
    )

    loans = [dict(row) for row in rows]
    for item in loans:
        item["was_overdue"] = bool(item["was_overdue"])
    return JSONResponse({"count": len(loans), "loans": loans})


@router.post("/loans/{loan_key}/override")
async def override_loan(request: Request, loan_key: str) -> JSONResponse:
    """Correct an inferred date by hand.

    Stored outside the derived history so it survives a rebuild.
    """
    db = _db(request)
    payload = await request.json()

    row = await db.fetch_one("SELECT 1 FROM loans WHERE loan_key = ?", (loan_key,))
    if row is None:
        raise HTTPException(status_code=404, detail=f"No loan {loan_key!r}")

    async with db.write() as w:
        await w.execute(
            """
            INSERT INTO loan_overrides (loan_key, lend_date, return_date, state, note)
            VALUES (:loan_key, :lend_date, :return_date, :state, :note)
            ON CONFLICT (loan_key) DO UPDATE SET
                lend_date = excluded.lend_date, return_date = excluded.return_date,
                state = excluded.state, note = excluded.note
            """,
            {
                "loan_key": loan_key,
                "lend_date": payload.get("lend_date"),
                "return_date": payload.get("return_date"),
                "state": payload.get("state"),
                "note": payload.get("note"),
            },
        )

    async with db.write() as w:
        await apply_overrides(w)

    return JSONResponse({"loan_key": loan_key, "status": "corrected"})


@router.get("/runs/latest")
async def latest_run(request: Request, account: str | None = None) -> JSONResponse:
    db = _db(request)
    account_id = None
    if account is not None:
        row = await queries.get_account(db, account)
        if row is None:
            raise HTTPException(status_code=404, detail=f"Unknown account {account!r}")
        account_id = row.id

    run = await queries.latest_run(db, account_id)
    if run is None:
        raise HTTPException(status_code=404, detail="No poll has run yet")
    return JSONResponse(run)


@router.get("/runs/{run_id}")
async def get_run(request: Request, run_id: int) -> JSONResponse:
    run = await queries.get_run(_db(request), run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"No run {run_id}")
    return JSONResponse(run)


@router.post("/accounts/{name}/poll")
async def poll_account(request: Request, name: str) -> JSONResponse:
    service = _service(request)
    try:
        run_id = await service.poll(name, trigger="manual")
    except PollInProgressError as err:
        raise HTTPException(status_code=409, detail=str(err)) from err
    except KeyError as err:
        raise HTTPException(status_code=404, detail=f"Unknown account {name!r}") from err
    return JSONResponse({"run_id": run_id}, status_code=202)


@router.post("/poll")
async def poll_all(request: Request) -> JSONResponse:
    service = _service(request)
    return JSONResponse({"runs": await service.poll_all(trigger="manual")}, status_code=202)


@router.post("/media/{media_id}/rating", response_class=HTMLResponse)
async def rate_media(request: Request, media_id: int) -> HTMLResponse:
    """Set or clear your own rating. Returns the widget, for an HTMX swap."""
    db = _db(request)
    form = await request.form()
    raw = form.get("rating")
    try:
        value = int(str(raw))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="rating must be a number") from None
    if value and not 1 <= value <= 5:
        raise HTTPException(status_code=400, detail="rating must be between 1 and 5")

    media = await db.fetch_one("SELECT id, title FROM media WHERE id = ?", (media_id,))
    if media is None:
        raise HTTPException(status_code=404, detail=f"No media {media_id}")

    async with db.write() as w:
        if value == 0:
            await w.execute(
                "UPDATE ratings SET rating = NULL, updated_at = datetime('now') WHERE media_id = ?",
                (media_id,),
            )
        else:
            await w.execute(
                """
                INSERT INTO ratings (media_id, rating) VALUES (?, ?)
                ON CONFLICT (media_id) DO UPDATE SET
                    rating = excluded.rating, updated_at = datetime('now')
                """,
                (media_id, value),
            )

    context = {"media": {"id": media_id, "title": media["title"], "rating": value or None}}
    return render(request, "partials/rating.html", "partials/rating.html", context)


@router.post("/media/{media_id}/notes", response_class=HTMLResponse)
async def set_notes(request: Request, media_id: int) -> HTMLResponse:
    db = _db(request)
    form = await request.form()
    review = str(form.get("review") or "").strip() or None

    async with db.write() as w:
        await w.execute(
            """
            INSERT INTO ratings (media_id, review) VALUES (?, ?)
            ON CONFLICT (media_id) DO UPDATE SET
                review = excluded.review, updated_at = datetime('now')
            """,
            (media_id, review),
        )

    return render(request, "partials/notes.html", "partials/notes.html", {"media": {"id": media_id, "review": review}})


@router.post("/media/{media_id}/dismiss", response_class=HTMLResponse)
async def dismiss_media(request: Request, media_id: int) -> HTMLResponse:
    """Stop asking about this one."""
    db = _db(request)
    async with db.write() as w:
        await w.execute(
            """
            INSERT INTO ratings (media_id, dismissed_at) VALUES (?, datetime('now'))
            ON CONFLICT (media_id) DO UPDATE SET dismissed_at = datetime('now')
            """,
            (media_id,),
        )

    from .pages import _unrated

    media, remaining = await _unrated(db)
    return render(
        request, "partials/rate_card.html", "partials/rate_card.html", {"media": media, "remaining": remaining}
    )


@router.post("/media/{media_id}/price", response_class=HTMLResponse)
async def set_price(request: Request, media_id: int) -> HTMLResponse:
    """Enter a price by hand. Always beats anything a provider reported."""
    from ...metadata.pricing import Price, PriceBasis, store_price

    db = _db(request)
    form = await request.form()
    raw = str(form.get("price") or "").strip().replace("\u20ac", "").replace(".", "").replace(",", ".")
    try:
        cents = round(float(raw) * 100)
    except ValueError:
        raise HTTPException(status_code=400, detail="price must be a number") from None
    if cents < 0:
        raise HTTPException(status_code=400, detail="price must not be negative")

    media = await db.fetch_one("SELECT id, media_class FROM media WHERE id = ?", (media_id,))
    if media is None:
        raise HTTPException(status_code=404, detail=f"No media {media_id}")

    await store_price(db, media_id, Price(cents, PriceBasis.MANUAL), source="manual", note="von Hand")

    updated = await db.fetch_one(
        "SELECT id, media_class, effective_price_cents, price_basis FROM media WHERE id = ?",
        (media_id,),
    )
    if updated is None:  # pragma: no cover - the row was there a moment ago
        raise HTTPException(status_code=404, detail=f"No media {media_id}")
    return render(request, "partials/price.html", "partials/price.html", {"media": dict(updated)})


def _renewals(request: Request) -> RenewalService:
    db = _db(request)
    return RenewalService(db, request.app.state.settings, _service(request))


@router.post("/loans/{loan_key}/renew", response_class=HTMLResponse)
async def renew_loan(request: Request, loan_key: str) -> HTMLResponse:
    """Renew one loan, from the row menu."""
    try:
        outcome = await _renewals(request).renew_loan(loan_key)
    except KeyError as err:
        raise HTTPException(status_code=404, detail=str(err)) from err

    return await _loans_fragment(request, [outcome])


@router.post("/accounts/{name}/renew-due", response_class=HTMLResponse)
async def renew_due(request: Request, name: str, days: int | None = None) -> HTMLResponse:
    """Renew everything falling due soon, then show what happened per item.

    Never a bare "done": a library can refuse an individual renewal, and the
    only useful answer says which ones it refused.
    """
    try:
        outcomes = await _renewals(request).renew_due(name, days)
    except KeyError as err:
        raise HTTPException(status_code=404, detail=str(err)) from err

    return await _loans_fragment(request, outcomes)


async def _loans_fragment(request: Request, outcomes: list[Any]) -> HTMLResponse:
    from .pages import loans_context

    context = await loans_context(request)
    context["renewals"] = outcomes
    return render(request, "partials/loans_table.html", "partials/loans_table.html", context)


@router.get("/stats")
async def stats_json(request: Request) -> JSONResponse:
    """Statistics as JSON. Money always carries its basis, never a bare total."""
    from ... import stats as stats_module

    db = _db(request)
    data = await stats_module.overview(db, request.app.state.settings)
    durations = data["durations"]
    return JSONResponse(
        {
            "money_saved": data["money"],
            "durations": {
                "median_days": durations.median_days,
                "mean_days": durations.mean_days,
                "p25_days": durations.p25_days,
                "p75_days": durations.p75_days,
                "counted": durations.counted,
                "excluded_unknown_start": durations.excluded,
                "by_class": durations.by_class,
            },
            "media_mix": data["media_mix"],
            "renewals": data["renewals"],
            "overdue": data["overdue"],
            "quality": data["quality"],
        }
    )


@router.get("/accounts")
async def list_accounts(request: Request) -> JSONResponse:
    rows = await _db(request).fetch_all(
        "SELECT name, library_type, username, enabled, last_success_at, removed_at FROM accounts ORDER BY name"
    )
    return JSONResponse({"accounts": [dict(row) for row in rows]})
