from pathlib import Path

import pytest

from qbkb import config


def _write_site(tmp_path: Path, name: str, body: str) -> Path:
    sites = tmp_path / "sites"
    sites.mkdir(exist_ok=True)
    (sites / f"{name}.yaml").write_text(body, encoding="utf-8")
    return tmp_path


def test_repo_sites_load() -> None:
    names = config.list_sites()
    assert {"man", "cfm"} <= set(names)
    for name in names:
        config.load_site(name)


def test_cfm_compliance() -> None:
    """CFM：2026-11-12 获书面许可（不含公开再分发），可以抓取。"""
    site = config.load_site("cfm")
    config.ensure_allowed(site)
    compliance = site.raw["compliance"]
    assert compliance["terms_url"] == "https://www.cfm.com/terms-of-use/"
    assert "2026-11-12" in compliance["permission"]
    assert "Crawl-delay" in compliance["robots_txt"]
    assert site.raw["base_url"] == "https://www.cfm.com"
    assert not config.is_local_site(site)


def test_allowed_site_passes_gate(tmp_path: Path) -> None:
    d = _write_site(tmp_path, "ok", "compliance:\n  decision: allowed\n")
    config.ensure_allowed(config.load_site("ok", d))


@pytest.mark.parametrize("decision", ["permission_needed", "blocked"])
def test_gate_refuses_non_allowed(tmp_path: Path, decision: str) -> None:
    d = _write_site(tmp_path, "no", f"compliance:\n  decision: {decision}\n")
    with pytest.raises(config.ComplianceError):
        config.ensure_allowed(config.load_site("no", d))


@pytest.mark.parametrize(
    "body",
    [
        "base_url: x\n",
        "compliance: {}\n",
        "compliance:\n  decision: maybe\n",
        "- not a mapping\n",
    ],
)
def test_invalid_compliance_is_rejected(tmp_path: Path, body: str) -> None:
    d = _write_site(tmp_path, "bad", body)
    with pytest.raises(ValueError):
        config.load_site("bad", d)


def test_config_dir_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib

    _write_site(tmp_path, "envsite", "compliance: {decision: allowed}")
    monkeypatch.setenv("QBKB_CONFIG_DIR", str(tmp_path))
    reloaded = importlib.reload(config)
    try:
        assert tmp_path == reloaded.CONFIG_DIR
        assert reloaded.list_sites() == ["envsite"]
    finally:
        monkeypatch.delenv("QBKB_CONFIG_DIR")
        importlib.reload(config)
