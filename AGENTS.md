# Working on bib-tracker

## Conventions

- Python 3.13, `ruff format`, `ruff check`, `mypy --strict` over `src/`.
- TDD. A bug fix starts with the failing test that reproduces it.
- Tests use realistic inputs: real recorded OPAC HTML and real recorded
  provider payloads, under `tests/fixtures/`. Never hand-written JSON standing
  in for an API response — record it.
- Commit messages in Linux-kernel style, explaining *why*.
- `git add -AN` before any `nix build`, and run long builds through `pueue`.

## Architecture

Two layers, and most of the design follows from the split:

- **Observation** — `poll_runs` and `snapshot_items`. Append-only, never
  edited. What each OPAC said, and when.
- **Derived** — `copies`, `loans`, `renewals`. Recomputable from the
  observations by `rebuild_history()`.

Manual corrections live in `loan_overrides`, outside the derived layer, keyed
by a `loan_key` built from the opening run id — immutable observation data, so
corrections survive a rebuild.

**Nothing may write to the derived layer except the reconciler.** If you find
yourself wanting to, the fact belongs in the observation layer instead; that is
what the synthetic `import` runs behind manual entry are for.

## Rules that exist for a reason

- A poll that failed for *any* reason stores no snapshot. Both scrapers return
  `[]` when their selector misses, so "the site changed" and "nothing is
  borrowed" are indistinguishable from the outside. `ParseError` upstream
  exists precisely to tell them apart.
- Import runs are additive: they may open a loan, never close one. A one-item
  synthetic snapshot otherwise looks exactly like an emptied account.
- `was_overdue` is latched when observed. Renewing moves the due date, so
  recomputing it later erases that a loan ever ran late.
- Ratings from different providers are never averaged. 7.8/10 and 4.1/5 are
  different claims.
- Any money figure must carry its basis. `stats.money_saved()` returns the
  split, not a total, on purpose — do not add a convenience that drops it.

## Upstream

Scraping belongs in `ha_stadtbibliothek`, not here. If a field is missing or a
parser is wrong, fix it there with a test and a version bump, and bump the
flake input. Things that are *not* upstream's business: media-class
normalisation, retry policy, history, fingerprinting, price lookup.

## The gate

`nix flake check` runs ruff, mypy, the unit suite and a NixOS VM test. The VM
test boots the service against a fake Koha OPAC built from the recorded
fixtures with their dates shifted relative to today, and blocks outbound
traffic at the unit level. Keep it green; it is the only thing that exercises
the module, the sandbox and the real backends together.
