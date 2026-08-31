# A stand-in Koha OPAC for the VM test.
#
# Remseck rather than Stuttgart on purpose: the Koha flow is one POST to log in
# and one GET to read the account, whereas aDIS needs a three-step stateful
# jsessionid form dance that would take more code to fake than to use.
#
# It serves the real recorded fixtures from ha_stadtbibliothek, so the test
# exercises the actual parser against actual markup, and rewrites the dates in
# them relative to today so the test cannot rot as those due dates recede into
# the past.
{ pkgs, fixtures }:

pkgs.writers.writePython3Bin "fake-library" { libraries = [ ]; } ''
  """Minimal Koha OPAC stand-in backed by recorded fixtures."""

  import http.cookies
  import re
  from datetime import date, timedelta
  from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
  from pathlib import Path
  from urllib.parse import parse_qs, urlparse

  FIXTURES = Path("${fixtures}")
  SESSION_COOKIE = "CGISESSID"
  SESSION_VALUE = "faketestsession"

  # Scenario 1 is the baseline; 2 drops one loan (a return); 3 is logged out.
  SCENARIOS = {
      1: "remseck_checkouts.html",
      2: "remseck_checkouts_returned.html",
      3: "remseck_login.html",
  }
  state = {"scenario": 1}


  def shift_dates(html: str) -> str:
      """Move the fixture's fixed dates to a window around today.

      The recorded due dates are already in the past, which would make every
      loan overdue and the assertions meaningless a year from now.
      """
      today = date.today()
      offsets = {
          "2026-04-15": 14,
          "2026-04-20": 19,
          "2026-04-08": 7,
          "2026-04-25": 24,
          "2026-04-11": 10,
      }
      for iso, delta in offsets.items():
          target = today + timedelta(days=delta)
          html = html.replace(iso, target.isoformat())
          german_old = ".".join(reversed(iso.split("-")))
          html = html.replace(german_old, target.strftime("%d.%m.%Y"))
      return html


  def fixture(name: str) -> bytes:
      return shift_dates((FIXTURES / name).read_text(encoding="utf-8")).encode("utf-8")


  class Handler(BaseHTTPRequestHandler):
      protocol_version = "HTTP/1.1"

      def log_message(self, fmt, *args):  # noqa: A002 - quiet by default
          pass

      def _send(self, body: bytes, status: int = 200, headers=()):
          self.send_response(status)
          self.send_header("Content-Type", "text/html; charset=utf-8")
          self.send_header("Content-Length", str(len(body)))
          for key, value in headers:
              self.send_header(key, value)
          self.end_headers()
          self.wfile.write(body)

      def _authenticated(self) -> bool:
          raw = self.headers.get("Cookie", "")
          cookie = http.cookies.SimpleCookie(raw)
          return SESSION_COOKIE in cookie and cookie[SESSION_COOKIE].value == SESSION_VALUE

      def do_POST(self):
          path = urlparse(self.path).path
          length = int(self.headers.get("Content-Length", 0))
          body = self.rfile.read(length).decode("utf-8") if length else ""

          scenario_match = re.match(r"^/__scenario__/(\d+)$", path)
          if scenario_match:
              state["scenario"] = int(scenario_match.group(1))
              self._send(b"ok")
              return

          if path == "/cgi-bin/koha/opac-user.pl":
              fields = parse_qs(body)
              user = (fields.get("userid") or [""])[0]
              password = (fields.get("password") or [""])[0]
              if state["scenario"] == 3 or not user or not password:
                  self._send(fixture("remseck_login.html"))
                  return
              cookie = f"{SESSION_COOKIE}={SESSION_VALUE}; Path=/"
              self._send(fixture(SCENARIOS[state["scenario"]]), headers=[("Set-Cookie", cookie)])
              return

          if path == "/cgi-bin/koha/opac-renew.pl":
              self._send(b"<html><body>renewed</body></html>")
              return

          self._send(b"not found", status=404)

      def do_GET(self):
          parsed = urlparse(self.path)
          path = parsed.path

          if path == "/__health__":
              self._send(b"ok")
              return

          # An expired session sends the login page back, which is what the
          # backend must recognise as "not an account page".
          if state["scenario"] == 3 or not self._authenticated():
              self._send(fixture("remseck_login.html"))
              return

          if path == "/cgi-bin/koha/opac-user.pl":
              self._send(fixture(SCENARIOS[state["scenario"]]))
              return

          if path == "/cgi-bin/koha/opac-account.pl":
              self._send(fixture("remseck_no_fees.html"))
              return

          if path == "/cgi-bin/koha/opac-detail.pl":
              self._send(fixture("remseck_detail.html"))
              return

          self._send(b"not found", status=404)


  def main() -> None:
      server = ThreadingHTTPServer(("127.0.0.1", 8081), Handler)
      server.serve_forever()


  if __name__ == "__main__":
      main()
''
