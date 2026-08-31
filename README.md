# bib-tracker

Tracks what a household borrows from German public libraries, and turns that
into a lending history the libraries themselves do not keep.

Stuttgart (aDIS/BMS) and Remseck (Koha/LMSCloud) are supported today; the
scraping comes from [ha_stadtbibliothek](https://github.com/makefu/ha_stadtbibliothek),
whose backends work without Home Assistant.

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
- **Money is never one number.** The only free source of list prices is Google
  Books, and it has nothing for most German library holdings, so most prices
  come from configured per-media-type defaults. "Money saved" therefore always
  reports the split between real prices and estimates, and the estimated share
  is drawn hatched in the chart rather than hidden in a footnote. A price you
  enter by hand always wins.
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

### Metadata providers

| Provider | Needs a key | Gives |
|---|---|---|
| `openlibrary` | no | covers, bibliographic data, community ratings |
| `dnb` | no | the best coverage for German titles |
| `wikidata` | no | board games: publisher, year, player counts |
| `googlebooks` | in practice | descriptions, page counts, **list prices** |
| `bgg` | yes | board-game ratings and weights |

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

```nix
services.bib-tracker.defaultPrices = {
  book = 15.0;
  game = 35.0;
  # audiobook, music, movie, magazine, other
};
```

These are only used when no real price could be found, and any figure resting
on them says so. A price entered on a work's page overrides everything.

## Commands

| Command | What it does |
|---|---|
| `bib-tracker` | the web server |
| `bib-tracker-migrate` | apply migrations and exit (runs as `ExecStartPre`) |
| `bib-tracker-poll` | poll every account once, non-zero exit on failure |
| `bib-tracker-rebuild` | recompute the history from the stored observations |

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
