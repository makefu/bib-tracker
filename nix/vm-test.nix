# The final gate: boot the service on a real NixOS machine and curl it.
#
# Runs with no internet. Outbound traffic is blocked at the unit level rather
# than merely unused, so an accidental call to a real OPAC or metadata API
# fails the test instead of silently succeeding on a networked builder.
{ pkgs, self }:

pkgs.testers.nixosTest {
  name = "bib-tracker";

  nodes.machine =
    { ... }:
    {
      imports = [ self.nixosModules.default ];

      nixpkgs.overlays = [ self.overlays.default ];

      services.bib-tracker = {
        enable = true;
        port = 8099;
        # Nothing to talk to yet: the scraping path arrives with the poller.
        metadata.enable = false;
        poll.onStartup = false;
      };

      systemd.services.bib-tracker.serviceConfig = {
        IPAddressDeny = "any";
        IPAddressAllow = [ "localhost" ];
      };
    };

  testScript = ''
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

    with subtest("the database lives where the module said"):
        machine.succeed("test -f /var/lib/bib-tracker/bib-tracker.db")

    with subtest("the unit is sandboxed as configured"):
        machine.succeed("systemctl show bib-tracker.service -p DynamicUser | grep -q =yes")
        machine.succeed("systemctl show bib-tracker.service -p StateDirectory | grep -q bib-tracker")
        machine.succeed("systemctl show bib-tracker.service -p ProtectSystem | grep -q strict")

    with subtest("nothing crashed on the way up"):
        machine.fail("journalctl -u bib-tracker.service | grep -q Traceback")
  '';
}
