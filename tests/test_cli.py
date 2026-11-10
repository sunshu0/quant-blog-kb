from pathlib import Path

import pytest

from qbkb import cli, store


def test_init_db(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "state.db"
    assert cli.main(["init-db", "--db", str(db)]) == 0
    assert db.exists()
    store.connect(db).close()
    assert "状态库就绪" in capsys.readouterr().out


def test_sites_lists_man(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["sites"]) == 0
    assert "man\tallowed" in capsys.readouterr().out
