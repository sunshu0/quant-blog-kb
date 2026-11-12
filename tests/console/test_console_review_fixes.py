"""PR #12 审查修复：pythonw 找不到时报错、渲染（代码里的上标、站内链接）、自动更新的 key 优先级。
任务锁、停止按钮、代理、术语表的用例分别在 test_console_jobs.py 和 test_translate_deepl.py。"""

import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from qbkb.console import auto, schedule
from qbkb.console.render import render_markdown
from qbkb.console.schedule import ScheduleError, Scheduler
from qbkb.console.server import ApiError, App
from qbkb.console.workspace import Settings, save_settings

# ---- 7. 找不到 pythonw ----


def test_task_python_without_venv_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ScheduleError) as info:
        schedule.task_python(tmp_path)
    assert "uv sync" in str(info.value)


def test_enable_without_venv_never_touches_task_scheduler(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        return subprocess.CompletedProcess(list(args), 1, "", "")

    with pytest.raises(ScheduleError):
        Scheduler(runner).enable(tmp_path, "09:00")
    assert calls == []


def test_settings_page_shows_the_error(tmp_path: Path) -> None:
    app = App(tmp_path, token="t", scheduler=Scheduler(lambda a: None), is_windows=True)  # type: ignore[arg-type]
    with pytest.raises(ApiError) as info:
        app.set_schedule({"enabled": True, "time": "09:00", "limit": 5})
    assert "uv sync" in info.value.message
    from qbkb.console.workspace import load_settings

    assert load_settings(tmp_path).schedule.enabled is False  # 失败时设置不变


# ---- 8. 渲染 ----


def _render(tmp_path: Path, text: str) -> str:
    out = tmp_path / "output"
    (out / "man").mkdir(parents=True, exist_ok=True)
    return render_markdown(text, out / "man" / "a.md", out)


def test_sup_inside_code_is_not_turned_into_superscript(tmp_path: Path) -> None:
    html = _render(
        tmp_path,
        "正文<sup>1</sup>，代码 `x<sup>2</sup>` 结束\n\n"
        "```\ny<sup>3</sup>\n```\n\n    z<sub>4</sub>\n",
    )
    assert "正文<sup>1</sup>" in html
    assert "<code>x&lt;sup&gt;2&lt;/sup&gt;</code>" in html
    assert "y&lt;sup&gt;3&lt;/sup&gt;" in html and "<sup>3</sup>" not in html
    assert "z&lt;sub&gt;4&lt;/sub&gt;" in html


def test_links_never_navigate_away_from_console(tmp_path: Path) -> None:
    html = _render(
        tmp_path,
        "[站内](man/other.md) [根](/api/state) [协议相对](//evil.test/x) "
        "[外部](https://example.test/a) [锚点](#fn1) [邮件](mailto:a@example.test)",
    )
    assert '<a class="link-disabled"' in html and 'href="man/other.md"' not in html
    assert 'href="/api/state"' not in html
    assert 'href="https://evil.test/x" target="_blank" rel="noopener noreferrer"' in html
    assert 'href="https://example.test/a" target="_blank"' in html
    assert '<a href="#fn1">锚点</a>' in html
    assert 'href="mailto:a@example.test" target="_blank"' in html


# ---- 6. 自动更新的 key 以 .env 为准 ----


class EnvSpy:
    def __init__(self) -> None:
        self.env: dict[str, str] = {}

    def __call__(self, cmd: list[str], **kwargs: Any) -> Any:
        self.env = kwargs["env"]

        class P:
            stdout: Any = iter(["[export] man: 发布 0（内容没变 0）\n"])
            pid = 1

            def wait(self) -> int:
                return 0

            def poll(self) -> int:
                return 0

            def terminate(self) -> None:
                pass

        return P()


def test_auto_update_uses_dotenv_keys_over_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text(
        "DEEPL_API_KEY=new-deepl-key:fx\nLLM_API_KEY=sk-new-llm-key\n", "utf-8"
    )
    s = Settings(mode="real")
    s.schedule.enabled = True
    save_settings(tmp_path, s)
    monkeypatch.setenv("DEEPL_API_KEY", "stale-shell-key")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.test:8080")
    spy = EnvSpy()
    assert auto.run_auto(tmp_path, popen=spy) == 0
    assert spy.env["DEEPL_API_KEY"] == "new-deepl-key:fx"
    assert spy.env["LLM_API_KEY"] == "sk-new-llm-key"
    assert "127.0.0.1" in spy.env["NO_PROXY"] and "localhost" in spy.env["no_proxy"]
    log = (tmp_path / "logs" / auto.LOG_NAME).read_text("utf-8")
    assert "new-deepl-key" not in log
