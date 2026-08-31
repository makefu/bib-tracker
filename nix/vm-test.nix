# The final gate: boot the service on a real NixOS machine and curl it.
#
# Runs with no internet. Outbound traffic is blocked at the unit level rather
# than merely unused, so an accidental call to a real OPAC or metadata API
# fails the test instead of silently succeeding on a networked builder.
{ pkgs, self }:

let
  fakeLibrary = import ./fake-library.nix {
    inherit pkgs;
    fixtures = ../tests/fixtures/library;
  };
in
pkgs.testers.nixosTest {
  name = "bib-tracker";

  nodes.machine =
    { config, ... }:
    {
      imports = [ self.nixosModules.default ];

      nixpkgs.overlays = [ self.overlays.default ];

      # Stands in for the Remseck OPAC, serving the real recorded fixtures.
      systemd.services.fake-library = {
        description = "Fake Koha OPAC for the bib-tracker test";
        wantedBy = [ "multi-user.target" ];
        before = [ "bib-tracker.service" ];
        serviceConfig = {
          ExecStart = pkgs.lib.getExe fakeLibrary;
          DynamicUser = true;
          Restart = "on-failure";
        };
      };

      environment.etc."bib-tracker-password".text = "hunter2";
      # So the test can run bib-tracker-rebuild by hand.
      environment.systemPackages = [ config.services.bib-tracker.package ];

      services.bib-tracker = {
        enable = true;
        port = 8099;
        metadata.enable = false;
        poll.onStartup = false;
        accounts.test = {
          libraryType = "remseck";
          username = "testuser";
          passwordFile = "/etc/bib-tracker-password";
          baseUrl = "http://127.0.0.1:8081";
        };
      };

      systemd.services.bib-tracker.serviceConfig = {
        IPAddressDeny = "any";
        IPAddressAllow = [ "localhost" ];
      };
    };

  testScript = ''
    machine.wait_for_unit("fake-library.service")
    machine.wait_for_open_port(8081)
    machine.wait_for_unit("bib-tracker.service")
    machine.wait_for_open_port(8099)

    with subtest("the service is healthy and the schema was migrated"):
        machine.succeed("curl -fsS localhost:8099/healthz | grep -q '\"status\":\"ok\"'")
        machine.succeed("curl -fsS localhost:8099/healthz | grep -q '\"schema_version\":1'")
        machine.succeed("curl -fsS localhost:8099/readyz")

    with subtest("vendored assets ship in the closure, so no CDN is needed"):
        machine.succeed(
            "curl -fsS -o /dev/null -w '%{http_code}' localhost:8099/static/css/tokens.css | grep -q 200"
        )

    with subtest("the declared account was seeded from the module"):
        machine.succeed("curl -fsS localhost:8099/api/accounts | grep -q '\"name\":\"test\"'")

    with subtest("a manual poll scrapes the OPAC through the real backend"):
        machine.succeed("curl -fsS -X POST localhost:8099/api/accounts/test/poll")
        machine.wait_until_succeeds(
            "curl -fsS localhost:8099/api/runs/latest | grep -q '\"status\":\"success\"'"
        )

    with subtest("the loans landed, parsed out of the recorded markup"):
        machine.succeed("curl -fsS localhost:8099/api/loans | grep -q '\"open_loans\":4'")
        machine.succeed("curl -fsS localhost:8099/api/loans | grep -q 'Die unendliche Geschichte'")
        machine.succeed("curl -fsS localhost:8099/api/loans | grep -q 'Ende, Michael'")

    with subtest("an item disappearing is seen as a return"):
        machine.succeed("curl -fsS -X POST localhost:8081/__scenario__/2")
        machine.succeed("curl -fsS -X POST localhost:8099/api/accounts/test/poll")
        machine.wait_until_succeeds("curl -fsS localhost:8099/api/loans | grep -q '\"open_loans\":3'")
        machine.fail("curl -fsS localhost:8099/api/loans | grep -q 'Die unendliche Geschichte'")

    with subtest("the return became history, with its date bounded"):
        machine.succeed("curl -fsS localhost:8099/api/history | grep -q '\"count\":4'")
        machine.succeed(
            "curl -fsS 'localhost:8099/api/history?state=returned' | grep -q 'Die unendliche Geschichte'"
        )
        machine.succeed(
            "curl -fsS 'localhost:8099/api/history?state=returned' | grep -q '\"return_date_source\":\"last_seen\"'"
        )

    with subtest("an auth failure must NOT look like everything was returned"):
        machine.succeed("curl -fsS -X POST localhost:8081/__scenario__/3")
        machine.succeed("curl -fsS -X POST localhost:8099/api/accounts/test/poll")
        machine.wait_until_succeeds(
            "curl -fsS localhost:8099/api/runs/latest | grep -q '\"status\":\"auth_error\"'"
        )
        machine.succeed("curl -fsS localhost:8099/api/loans | grep -q '\"open_loans\":3'")

    with subtest("history can be recomputed from the stored observations"):
        before = machine.succeed("curl -fsS localhost:8099/api/history")
        # Reuse the unit's own environment so the rebuild sees the same
        # database and account settings the service does.
        machine.succeed(
            "export $(systemctl show bib-tracker.service -p Environment --value"
            " | xargs -n1 | grep -E '^BIB_TRACKER_(DB_PATH|ACCOUNTS_FILE)=' | xargs)"
            " && bib-tracker-rebuild"
        )
        after = machine.succeed("curl -fsS localhost:8099/api/history")
        assert before == after, "rebuilding from observations changed the history"

    with subtest("the database lives where the module said"):
        machine.succeed("test -f /var/lib/bib-tracker/bib-tracker.db")

    with subtest("the unit is sandboxed as configured"):
        machine.succeed("systemctl show bib-tracker.service -p DynamicUser | grep -q =yes")
        machine.succeed("systemctl show bib-tracker.service -p StateDirectory | grep -q bib-tracker")
        machine.succeed("systemctl show bib-tracker.service -p ProtectSystem | grep -q strict")

    with subtest("the password never entered the Nix store"):
        machine.fail("grep -rq hunter2 /nix/store/*bib-tracker*accounts.json")

    with subtest("nothing crashed along the way"):
        machine.fail("journalctl -u bib-tracker.service | grep -q Traceback")
  '';
}
