{
  lib,
  pkgs,
  python313Packages,
  playwrightDriver ? null,
}:

python313Packages.buildPythonApplication {
  pname = "bib-tracker";
  # Single source: the version lives in bib_tracker.__version__ and pyproject
  # reads it dynamically; the derivation reads the same line so the two cannot
  # drift apart at release time. builtins.match has no (?s), so find the line
  # first and capture within it.
  version = let
    line = builtins.head (
      builtins.filter (l: builtins.match ''__version__ = "([^"]+)".*'' l != null) (
        lib.splitString "\n" (builtins.readFile ../src/bib_tracker/__init__.py)
      )
    );
  in
    builtins.elemAt (builtins.match ''__version__ = "([^"]+)".*'' line) 0;

  src = lib.cleanSource ../.;
  pyproject = true;

  build-system = with python313Packages; [ setuptools ];

  dependencies = with python313Packages; [
    fastapi
    uvicorn
    jinja2
    httpx
    apscheduler
    pydantic
    pydantic-settings
    pyyaml
    python-multipart
    pillow
    structlog
    ha-stadtbibliothek
  ];

  nativeCheckInputs = with python313Packages; [
    pytestCheckHook
    pytest-asyncio
    respx
    freezegun
    asgi-lifespan
  ] ++ lib.optionals (playwrightDriver != null) [ python313Packages.playwright python313Packages.pytest-playwright ];

  # Playwright's browsers come from the store rather than a network install,
  # so the e2e tests find them through PLAYWRIGHT_BROWSERS_PATH (an env
  # attribute: it is exported during build and check). Each browser component
  # is a derivation whose RUNPATH already names the libraries Chromium needs
  # (libglvnd, vulkan, alsa, ...), so putting the components in buildInputs is
  # what makes the binary launchable inside the build sandbox.
  buildInputs = lib.optionals (playwrightDriver != null) [
    playwrightDriver.components.chromium
    playwrightDriver.components.chromium-headless-shell
  ];
  PLAYWRIGHT_BROWSERS_PATH = lib.optionalString (playwrightDriver != null) "${playwrightDriver.browsers}";

  # The playwright browser components ship a wrapped `chrome` that sets
  # FONTCONFIG_FILE, but headless-shell is unwrapped and the build sandbox has
  # no system font config: Chromium dies on the first page render ("Cannot
  # load default config file", then a Skia font-manager FATAL). Give the
  # browser tests a real fonts.conf and a writable HOME for chrome's profile
  # (the sandbox HOME is not one). DejaVu matches what the widget tests'
  # geometry expectations were written against.
  FONTCONFIG_FILE = lib.optionalString (playwrightDriver != null) (
    "${pkgs.makeFontsConf { fontDirectories = [ pkgs.dejavu_fonts ]; }}"
  );
  HOME = lib.optionalString (playwrightDriver != null) "/tmp";

  # Mark selection is pyproject's addopts (`-m 'not live and not e2e'`): the
  # plain package build is the unit suite only. The flake's `e2e` check
  # rebuilds this derivation with PYTEST_ADDOPTS="-m e2e", which pytest ranks
  # above the ini addopts, so the browser tests get run by a gate of their own.

  pythonImportsCheck = [
    "bib_tracker"
    "bib_tracker.db.migrator"
  ];

  meta = {
    description = "Track what your household borrows from German public libraries";
    homepage = "https://github.com/makefu/bib-tracker";
    license = lib.licenses.mit;
    mainProgram = "bib-tracker";
  };
}
