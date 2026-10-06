# Changelog

## v0.3.0 — 2026-10-06

Prices and covers now come from the shops that actually stock the work, and
the operator gets a surface to drive the backfills that a long-lived database
inevitably needs.

- Six shop front-ends — Buchkatalog.de, Thalia, Amazon.de, buch7.de,
  Lehmanns.de, eBook.de — join the price ladder beside the catalogues
  (VLB/DNB/Google Books). Each is queried by ISBN and by title, every answer
  is stored, and the price field names its source ("Listenpreis von
  buch7.de"); the alternatives show beside it, subdued. A bot wall is its own
  outcome, so "Thalia blocks us" and "Thalia has nothing" stay distinguishable.
- `lookup_probes`: one row per (work, provider, purpose) is the no-retry
  mechanism — a probed source is not asked again, a blocked or errored one
  retries after `price_retry_days`. When nothing answered at all, the UI says
  *keine Quelle gefunden* instead of pretending.
- Covers fall back to a prioritised image-provider list when the OPAC gave
  none; shops never fill bibliographic fields, only price and cover.
- `bib-tracker-lookup ISBN`: the same provider code as a CLI probe tool
  (`--json`, `--provider`, `--fresh`), no database writes.
- `/settings`: database size, completeness, HTTP cache, the probe ledger by
  outcome, and every configured provider marked by whether it can answer —
  a missing key or cookie is shown, not silently skipped. The Wartung card
  runs the backfills one task at a time: re-search prices/covers, re-enrich,
  reload covers after a parser fix, rebuild the history, VACUUM. A work's
  page carries the same actions at one-work scope.
- A failed HTMX request surfaces as a toast; a second maintenance press while
  busy is refused, visibly.
- The version lives in `bib_tracker.__version__`; pyproject, package.nix,
  `/healthz` and the footer read the same line.
- Version formatting follows German rules (`233,5 kB`, dates `06.10.2026`).

Pins ha_stadtbibliothek v1.4.2. Gate: 311 unit tests, Playwright e2e suite,
ruff, mypy strict, NixOS VM test — all green.

## v0.2.0 — 2026-10-05

Stuttgart login works against the current aDIS. Pins ha_stadtbibliothek
v1.4.2 (button clicks by label, JS-driven Ausleihen navigation) and syncs the
recorded OPAC fixtures to the 2026-10-05 captures.
