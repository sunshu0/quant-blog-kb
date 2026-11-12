"""控制台工作区：两种模式的数据目录隔离、设置文件、.env 里的 key 写入与掩码。"""

from pathlib import Path

import pytest

from qbkb.console.workspace import (
    DEEPL_KEY,
    LLM_KEY,
    Settings,
    load_settings,
    mask,
    read_keys,
    redact,
    resolve_mode,
    save_settings,
    set_key,
    workspace,
)

DEEPL = "0123abcd-1111-2222-3333-444455556666:fx"
LLM = "sk-abcdefghijklmnop9876"


def test_demo_and_real_data_are_separate(tmp_path: Path) -> None:
    real, demo = workspace(tmp_path, "real"), workspace(tmp_path, "demo")
    assert real.db == tmp_path / "state.db" and real.out == tmp_path / "output"
    assert demo.base == tmp_path / ".qbkb" / "demo"
    for a, b in ((real.db, demo.db), (real.raw, demo.raw), (real.out, demo.out)):
        assert a != b and not b.is_relative_to(real.raw) and not b.is_relative_to(real.out)
    assert demo.config_dir == demo.base / "config" and real.config_dir is None
    assert demo.site == "demo" and real.site is None


def test_set_key_replaces_appends_and_keeps_other_lines(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("# 注释\nDEEPL_API_KEY=old-old-old\nLLM_BASE_URL=https://x.test\n", "utf-8")
    set_key(tmp_path, DEEPL_KEY, DEEPL)
    set_key(tmp_path, LLM_KEY, LLM)
    text = env.read_text("utf-8")
    assert text == (
        f"# 注释\nDEEPL_API_KEY={DEEPL}\nLLM_BASE_URL=https://x.test\nLLM_API_KEY={LLM}\n"
    )
    assert read_keys(tmp_path) == {DEEPL_KEY: DEEPL, LLM_KEY: LLM}
    set_key(tmp_path, DEEPL_KEY, "")  # 清除
    assert "DEEPL_API_KEY" not in env.read_text("utf-8")
    assert b"\r" not in env.read_bytes()


def test_set_key_creates_env_and_handles_export_and_duplicates(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("export LLM_API_KEY=a-a-a-a-a\nLLM_API_KEY=b-b-b-b-b\n", "utf-8")
    set_key(tmp_path, LLM_KEY, LLM)
    assert (tmp_path / ".env").read_text("utf-8") == f"LLM_API_KEY={LLM}\n"
    other = tmp_path / "new"
    other.mkdir()
    set_key(other, DEEPL_KEY, DEEPL)
    assert read_keys(other)[DEEPL_KEY] == DEEPL


@pytest.mark.parametrize(
    "bad",
    ["short", "abc def ghi jkl", "abcdefgh\nEVIL=1", 'abcdefgh"x', "abcdefgh#x", "x" * 201],
)
def test_set_key_rejects_values_that_could_inject(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ValueError):
        set_key(tmp_path, DEEPL_KEY, bad)
    assert not (tmp_path / ".env").exists()


def test_set_key_rejects_unknown_names(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        set_key(tmp_path, "PATH", DEEPL)


def test_mask_never_returns_the_key() -> None:
    m = mask(DEEPL)
    assert m == {"configured": True, "last4": "6:fx"}
    assert DEEPL not in str(m)
    assert mask("") == {"configured": False, "last4": ""}


def test_resolve_mode() -> None:
    both = {DEEPL_KEY: DEEPL, LLM_KEY: LLM}
    assert resolve_mode("auto", both) == "real"
    assert resolve_mode("auto", {DEEPL_KEY: DEEPL, LLM_KEY: ""}) == "demo"
    assert resolve_mode("demo", both) == "demo"
    assert resolve_mode("real", {DEEPL_KEY: "", LLM_KEY: ""}) == "real"


def test_settings_roundtrip_and_bad_values(tmp_path: Path) -> None:
    assert load_settings(tmp_path) == Settings()
    s = Settings(mode="demo")
    s.schedule.enabled, s.schedule.time, s.schedule.limit = True, "07:30", 12
    save_settings(tmp_path, s)
    assert load_settings(tmp_path) == s
    (tmp_path / ".qbkb" / "console.json").write_text(
        '{"mode": "x", "schedule": {"enabled": "yes", "time": "25:00", "limit": 999}}', "utf-8"
    )
    assert load_settings(tmp_path) == Settings()


def test_redact() -> None:
    assert redact(f"error with {DEEPL} inside", [DEEPL, ""]) == "error with *** inside"
