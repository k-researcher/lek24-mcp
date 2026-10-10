"""Keep tests independent of runtime caches on the developer's machine."""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_runtime_caches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEK24_COVERAGE_DIR", str(tmp_path / "coverage"))
    monkeypatch.setenv("LEK24_GEO_CACHE", str(tmp_path / "geocode-v1.json"))
