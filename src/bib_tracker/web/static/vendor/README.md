# Vendored assets

Committed rather than fetched at build time, so `nix build` needs no network
and the NixOS VM test can run fully offline.

| File            | Version | Licence | Source |
|-----------------|---------|---------|--------|
| `htmx.min.js`   | 2.0.4   | BSD-2-Clause | https://cdnjs.cloudflare.com/ajax/libs/htmx/2.0.4/htmx.min.js |
| `alpine.min.js` | 3.14.9  | MIT     | https://cdnjs.cloudflare.com/ajax/libs/alpinejs/3.14.9/cdn.min.js |

To update, download the new file, bump the version here, and check the page
still works. Nothing else references the version.
