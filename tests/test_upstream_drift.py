"""Contract tests for the scheduled upstream drift probe."""

import importlib.util
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "upstream_drift_check", ROOT / "scripts" / "upstream-drift-check.py"
)
assert SPEC is not None and SPEC.loader is not None
DRIFT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DRIFT)


def test_provider_drift_reports_sync_errors_instead_of_a_false_match(tmp_path):
    wheel = tmp_path / "provider.whl"
    package = tmp_path / "hermes_memory_provider"
    package.mkdir()
    result = SimpleNamespace(
        returncode=2,
        stdout="partial comparison output\n",
        stderr="invalid extraction\n",
    )
    with (
        patch.object(DRIFT, "download_wheel", return_value=wheel),
        patch.object(DRIFT, "extract_provider", return_value=package),
        patch.object(DRIFT.subprocess, "run", return_value=result),
        pytest.raises(RuntimeError, match="vendor sync failed with exit 2"),
    ):
        DRIFT.provider_drift("https://example.invalid/provider.whl")


def test_provider_drift_preserves_match_and_difference_status(tmp_path):
    wheel = tmp_path / "provider.whl"
    package = tmp_path / "hermes_memory_provider"
    package.mkdir()
    for code, expected in ((0, False), (1, True)):
        result = SimpleNamespace(returncode=code, stdout=f"status {code}\n", stderr="")
        with (
            patch.object(DRIFT, "download_wheel", return_value=wheel),
            patch.object(DRIFT, "extract_provider", return_value=package),
            patch.object(DRIFT.subprocess, "run", return_value=result),
        ):
            drift, report = DRIFT.provider_drift("https://example.invalid/provider.whl")
        assert drift is expected
        assert report == f"status {code}"


def test_download_wheel_rejects_non_pypi_urls(tmp_path):
    with (
        patch.object(DRIFT.urllib.request, "urlopen") as open_url,
        pytest.raises(RuntimeError, match="untrusted wheel URL"),
    ):
        DRIFT.download_wheel("https://attacker.example/provider.whl", tmp_path)
    open_url.assert_not_called()


def test_download_wheel_enforces_size_limit(tmp_path, monkeypatch):
    class Response:
        headers = {}
        received = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def geturl(self):
            return "https://files.pythonhosted.org/packages/provider.whl"

        def read(self, _size=-1):
            if self.received:
                return b""
            self.received = True
            return b"12345"

    monkeypatch.setattr(DRIFT, "MAX_WHEEL_BYTES", 4, raising=False)
    with (
        patch.object(DRIFT.urllib.request, "urlopen", return_value=Response()),
        pytest.raises(RuntimeError, match="wheel exceeds the size limit"),
    ):
        DRIFT.download_wheel("https://files.pythonhosted.org/packages/provider.whl", tmp_path)


def test_download_wheel_preserves_existing_path(tmp_path):
    wheel = tmp_path / "provider.whl"
    wheel.write_text("keep", encoding="utf-8")

    class Response:
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def geturl(self):
            return "https://files.pythonhosted.org/packages/provider.whl"

    with (
        patch.object(DRIFT.urllib.request, "urlopen", return_value=Response()),
        pytest.raises(FileExistsError),
    ):
        DRIFT.download_wheel("https://files.pythonhosted.org/packages/provider.whl", tmp_path)
    assert wheel.read_text(encoding="utf-8") == "keep"


def test_extract_provider_rejects_zip_traversal(tmp_path):
    wheel = tmp_path / "unsafe.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("hermes_memory_provider/../../escaped.py", "payload")

    with pytest.raises(RuntimeError, match="unsafe provider path"):
        DRIFT.extract_provider(wheel, tmp_path / "extract")
    assert not (tmp_path / "escaped.py").exists()


def test_extract_provider_bounds_source_size(tmp_path, monkeypatch):
    wheel = tmp_path / "large.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("hermes_memory_provider/provider.py", "12345")
    monkeypatch.setattr(DRIFT, "MAX_PROVIDER_BYTES", 4, raising=False)

    with pytest.raises(RuntimeError, match="provider source exceeds the size limit"):
        DRIFT.extract_provider(wheel, tmp_path / "extract")


def test_hermes_range_does_not_accept_future_contract_versions():
    assert DRIFT.hermes_range_status("0.19.0")[0] is True
    assert DRIFT.hermes_range_status("0.20.0")[0] is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
