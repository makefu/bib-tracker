{
  lib,
  python313Packages,
}:

python313Packages.buildPythonApplication {
  pname = "bib-tracker";
  version = "0.1.0";

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
  ];

  # Live tests hit the real OPACs and metadata APIs; the build has no network.
  disabledTestMarks = [ "live" ];

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
