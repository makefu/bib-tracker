# Recorded OPAC pages (copied from ha_stadtbibliothek)

Verbatim captures of the two live installations, run through `record_fixtures.py`
in the repository root. Nothing in this directory is hand-written or trimmed.

That is the point. The fixtures one directory up are constructed: they tell a
small, controlled story (four loans, some fees, one renewal) that the unit
tests assert against line by line. Constructed fixtures drift. Twice already a
backend has been "fixed" against markup that only ever existed in a fixture --
`#useraccount` on every Koha tab, `section#results` on the aDIS loan page --
and both times the live installations disagreed and every loan vanished. These
recordings are the counterweight: whatever the real servers send, they say so.

## Provenance

Captured 2026-10-05 from the accounts in `.secrets.yml`. The previous set
came from 2026-08-31/2026-09-28 and described an older aDIS: the live site
since moved the session from `jsessionid=` into the URL path
(`/aDISWeb/_<sid>/app`) and renamed the form submit fields, which broke the
backend against a fixture set that had stopped matching the server.

| File | Request | Notes |
| --- | --- | --- |
| `remseck_checkouts.html` | `GET /cgi-bin/koha/opac-user.pl` | Koha 22.11, 5 loans, two of them renewable (`input[name="item"]`). Also the body the login `POST` returns. |
| `remseck_account.html` | `GET /cgi-bin/koha/opac-account.pl` | `#finestable` present with an empty `tbody`: no fees owed. |
| `remseck_logged_out.html` | same URL, no session | `form#auth`, none of the masthead markers. |
| `remseck_detail.html` | `GET /cgi-bin/koha/opac-detail.pl?biblionumber=…` | Carries `span[property="isbn"]`. |
| `remseck_detail_ean.html` | same, different record | A tiptoi puzzle: no ISBN, and an `ean` of `4005556001385`. |
| `stuttgart_home.html` | `GET /?service=…&sp=SOPAC` | Search mask; step 1 of the aDIS login. |
| `stuttgart_login_form.html` | `POST` of step 1 | The credentials form. |
| `stuttgart_account.html` | `POST` of the credentials | "Mein Konto", carrying `div#konto-services`. The service links are `href="#"`; a page script wires each id to `top.htmlOnLink("*SZA")`-style clicks. |
| `stuttgart_ausleihen.html` | `POST` of the account page's form (the JS-nav click) | aDIS/BMS, 49 loans. The row checkboxes all share `name="$RTable_checkbox[]"` and are told apart by value (`CheckCell_N`). |
| `stuttgart_search_00..04.html` | `POST` of the start-page search form, one query per loan title of the *constructed* `../stuttgart_ausleihen.html`, `SRCHAW=Katalog` | The cover route: result rows (`li.rList_li`) with lazily-loaded `api.vlb.de` jackets in `data-src` (`img-delayed` rows have no `src` at all). `00`/`04` answer with rows that match no title; `00` even landed on the home page. |
| `stuttgart_search_05.html` | same, query "Der Koboldmaki und der große Sturm" | A query the OPAC answers with a single-hit Vollanzeige (`div.show-full-basics`, no result rows): the cover sits in the detail block, and `p.info` echoes the query. |

No renewal response is recorded: renewing is a real change to a real account.
The renewal fixtures one directory up stay constructed, and the constructed
Stuttgart listing mirrors the live one's checkbox naming so a rename still
fails loudly here first.

The search fixtures are keyed to the constructed fixture's titles, not to
whatever was out on the live account, because `test_stuttgart.py` routes its
search mock on those titles; the recorder reads `../stuttgart_ausleihen.html`
to build the queries. Refreshing them re-records the responses to those exact
queries, so a catalogue retitle can still break the pinned cover URLs -- the
tests then fail loudly with the expected-URL assertion, which is the intent.

## What was removed

Identity, and nothing else:

- the account holder's name, in every greeting and breadcrumb;
- the Koha `borrowernumber`, rewritten to `10000001`;
- the aDIS `jsessionid` and its per-session object handles, remapped so that
  distinct handles stay distinct and the Ausleihen link still matches the page
  it fetches;
- the counter pickup code aDIS prints beside the loan list;
- Koha's `csrf_token`.

Titles, authors, call numbers, branches, due dates, barcodes, biblionumbers and
cover URLs are all as recorded. They are the data under test -- inventing them
is exactly how the constructed fixtures came to describe markup that does not
exist -- and they are catalogue records, not personal data. What they reveal
about the account holder is which children's books were on loan on one day.

## Refreshing

```sh
# in the ha_stadtbibliothek repository
nix run .#record-fixtures -- /path/to/.secrets.yml
```

The recorder writes here directly and refuses to write anything it can still
find identifying data in. Expect the loan counts and dates in
`test_recorded_pages.py` to move with the recording; that test asserts the
shape of the markup and the corner cases, not the borrowing.
