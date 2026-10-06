# bib-tracker

Tracks what a household borrows from German public libraries, and turns that
into a lending history the libraries themselves do not keep.

Stuttgart (aDIS/BMS) and Remseck (Koha/LMSCloud) are supported today; the
scraping comes from [ha_stadtbibliothek](https://github.com/makefu/ha_stadtbibliothek),
whose backends work without Home Assistant.

Release notes live in [CHANGELOG.md](CHANGELOG.md).

## Why

The library OPACs only show what is on loan *right now*. There is no history,
no ratings, no prices: once an item goes back it vanishes without a trace. So
bib-tracker polls each account on a schedule and derives the history from those
snapshots — an item appearing means borrowed, an item disappearing means
returned — then enriches each work from external sources and lets you rate it.

## What it is careful about

Most of what this application knows is inferred, and it tries hard not to
pretend otherwise.

- **Dates carry their provenance.** Every lend and return date is stored with a
  source (`exact`, `due_minus_period`, `first_seen`, `before_tracking`,
  `manual`) and a pair of bounds. The interface marks an inferred date, and a
  loan that was already running when tracking began is shown as unknown rather
  than guessed at.
- **A failed poll never rewrites history.** Both scrapers return an empty list
  when their table selector misses, so an authentication failure or a changed
  page would otherwise look exactly like an emptied account. Failures are
  classified and store nothing; a sudden emptiness is held back until a second
  poll confirms it.
- **Money is never one number.** Prices come from a configurable ladder
  (see below) and fall back to per-media-type defaults, so a good share of any
  total is a guess. "Money saved" therefore always reports the split between
  real prices and estimates, and the estimated share is drawn hatched in the
  chart rather than hidden in a footnote. A price you enter by hand always wins.
- **History can be recomputed.** The polls are stored verbatim and the derived
  tables can be dropped and rebuilt from them, so a reconciler bug found next
  year is still fixable for history already recorded. Manual corrections
  survive that rebuild.

## Running it

```nix
{
  inputs.bib-tracker.url = "github:makefu/bib-tracker";

  # ...
  imports = [ inputs.bib-tracker.nixosModules.default ];
  nixpkgs.overlays = [ inputs.bib-tracker.overlays.default ];

  services.bib-tracker = {
    enable = true;
    port = 8099;

    accounts.stuttgart = {
      libraryType = "stuttgart";
      username = "123456";
      passwordFile = "/run/secrets/bib-stuttgart";
    };

    accounts.remseck = {
      libraryType = "remseck";
      username = "654321";
      passwordFile = "/run/secrets/bib-remseck";
    };
  };
}
```

There is no authentication: put it behind a reverse proxy. Passwords are read
through systemd's credential store and never enter the Nix store.

### Config files

Outside NixOS the settings come from YAML config files. Every key of the
settings model may appear in a file; several files are merged, later ones
winning key by key, which is how the open configuration and the account
credentials are kept apart:

```yaml
# config.yaml — safe to commit
db_path: /var/lib/bib-tracker/bib-tracker.db
host: 127.0.0.1
port: 8099
log_level: info

poll_interval_minutes: 360
metadata_providers: [openlibrary, dnb, wikidata]
price_providers: [vlb, dnb, googlebooks, buchkatalog, thalia, amazon, buch7, lehmanns, ebookde]
image_providers: [library, openlibrary, thalia, googlebooks, buchkatalog, buch7, ebookde, lehmanns, amazon]
default_prices:
  book: 15.0
  game: 35.0

accounts:
  stuttgart:
    library_type: stuttgart
    username: "123456"
  remseck:
    library_type: remseck
    username: "654321"
```

```yaml
# .secrets.yml — git-ignored, merged second
accounts:
  stuttgart:
    password: "hunter2"
  remseck:
    password: "hunter3"

metadata_api_keys:
  bgg: "…"

# A Thalia session that gets past its Cloudflare check; without it the
# thalia probe is marked blocked in the UI. Files work too
# (metadata_cookie_files), same precedence as the keys above.
metadata_cookies:
  thalia: "session-xy=…"
```

Accounts merge *per account*: the second file completes the account the
first one declared instead of replacing it, and an account accepts
`password` (inline), `password_file` (path), or `password_credential` (the
name of a systemd credential, which is how the NixOS unit passes secrets
without ever putting them in a file). Provider tokens work the same way:
`metadata_api_keys` holds them inline, `metadata_api_key_files` points at
files, and a file wins when both name the same provider.

```sh
bib-tracker config.yaml .secrets.yml
```

The files are found in this order:

1. `CONFIG ...` — config files as plain arguments, merged left to right,
  accepted by every command (`bib-tracker`, `-migrate`, `-poll`,
  `-rebuild`); a named file must exist.
2. `BIB_TRACKER_CONFIG_FILES` — `os.pathsep`-separated, for setups that
  cannot pass arguments (systemd units, containers); every named file must
  exist. The NixOS module generates a public and a secrets file and passes
  exactly this pair.
3. `$XDG_CONFIG_HOME/bib-tracker/config.yaml` (`~/.config/...` when unset);
   this default need not exist.

`BIB_TRACKER_*` environment variables always beat every file. Files carry
values in YAML; env vars carry complex values as JSON or, for lists, a
comma-separated string.

### Metadata providers

| Provider | Needs a key | Gives |
|---|---|---|
| `openlibrary` | no | covers, bibliographic data, community ratings |
| `dnb` | no | the best coverage for German titles |
| `wikidata` | no | board games: publisher, year, player counts |
| `googlebooks` | in practice | descriptions, page counts, list prices |
| `bgg` | yes | board-game ratings and weights |
| `vlb` | yes (contract) | the authoritative *gebundener Ladenpreis* |

Google Books works anonymously only until its per-address daily quota runs out,
and BoardGameGeek returns 401 to every anonymous request as of 2026. Both are
therefore left out of the defaults; add them with a credential:

```nix
services.bib-tracker.metadata = {
  providers = [ "openlibrary" "dnb" "wikidata" "googlebooks" "bgg" ];
  apiKeyFiles = {
    googlebooks = "/run/secrets/google-books-key";
    bgg = "/run/secrets/bgg-token";
  };
};
```

Without them nothing breaks: board games fall back to Wikidata, and prices fall
back to the configured defaults, labelled as estimates.

### Prices

Germany has a fixed book price, so "what would this have cost" has a real
answer — if you can get at it. Sources are tried in a configurable order:

```nix
services.bib-tracker.metadata.priceProviders = [ "vlb" "dnb" "googlebooks" ];
```

- **VLB** — since 2011 the reference database for the *gebundener Ladenpreis*
  under the Börsenverein's Verkehrsordnung, and the OLG Frankfurt has held that
  a price recorded there beats a differing one on a publisher's own site. It
  needs a contract with MVB, so most installations will not have it.
- **DNB** — the price as catalogued, in MARC 020 $c, free and with good German
  coverage. It is the price at cataloguing time, so a later change or a lifted
  price binding is not reflected: right for "what would this have cost", not a
  statement about today's price.
- **Google Books** — the fallback, and rarely knows German titles at all.

Whatever the ladder finds, a price typed on a work's page overrides it: you can
see the book, and no database outranks that.

When nothing is found, the media-class default applies and every figure resting
on it says so:

```nix
services.bib-tracker.defaultPrices = {
  book = 15.0;
  game = 35.0;
  # audiobook, music, movie, magazine, other
};
```

### The settings page

`/settings` is the operator's surface: database size and completeness, the
HTTP cache, every configured provider marked by whether it can actually
answer (a missing key or cookie is shown, not silently skipped), and the
probe ledger summarised by outcome. Its Wartung card runs the backfills one
task at a time — re-search prices or covers (with the choice to forget the
probe table first), re-enrich works from scratch, reload every cover after a
parser fix, rebuild the history, or VACUUM. A task in flight is visible in
the card, and a second press is refused rather than doubling the requests.
Each work's page carries the same idea at one-work scope: *Preis neu suchen*
/ *Cover neu laden* beside the price field.

## Commands

| Command | What it does |
|---|---|
| `bib-tracker` | the web server |
| `bib-tracker-migrate` | apply migrations and exit (runs as `ExecStartPre`) |
| `bib-tracker-poll` | poll every account once, non-zero exit on failure |
| `bib-tracker-rebuild` | recompute the history from the stored observations |
| `bib-tracker-lookup ISBN` | ask every price/image source for one work and print the answers side by side (`--json`, `--provider`, `--fresh`) |

## Development

```sh
nix develop
pytest
ruff check . && ruff format --check .
mypy

nix flake check      # adds the NixOS VM test
```

The VM test is the real gate: it boots the service on a NixOS machine against a
fake Koha OPAC serving recorded fixtures, with outbound traffic blocked at the
unit level so an accidental call to a real library fails the test rather than
passing quietly.

### Live checks

Some things only the real services can answer — whether the DNB actually has a
price for the books this household borrows, whether a BoardGameGeek token still
works. Those live in `tests/integration`, are marked `live`, and are excluded
from every automated run:

```sh
nix run .#integration       # or: pytest -m live
```

They read credentials from a git-ignored `.secrets.yml`:

```yaml
remseck_username: "..."
remseck_password: "..."
stuttgart_username: "..."
stuttgart_password: "..."
bgg_api_key: "..."
# vlb_api_key: "..."   # needs an MVB contract
```

A test whose credential is missing skips rather than fails.

Live checks against the real services are marked `live` and excluded from every
build. They need credentials in a git-ignored `.secrets.yml`, and each one
skips rather than fails when its credential is missing:

```sh
pytest -m live                       # all of them
pytest -m live tests/integration     # explicit
```

They exist for the questions unit tests cannot answer — whether the DNB really
has a price for the books this household borrows, and whether a scraper's idea
of "logged in" still matches the live OPAC. Both have already caught real bugs
that fixture-based tests happily passed.
