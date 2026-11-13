"""控制台的 HTTP 服务（只监听 127.0.0.1）和 JSON 接口。

安全措施：
- 只绑定 127.0.0.1；每个请求检查 Host 头只能是 127.0.0.1:<端口> 或 localhost:<端口>
  （防 DNS 重绑定）；
- 会改状态的接口（POST）必须带每次启动随机生成的 token（`X-QBKB-Token` 头，页面从 index.html
  的 meta 里读），Content-Type 必须是 application/json，有 Origin 头时必须是本服务自己：
  别的网站发不出合格的跨站请求，也读不到 token；
- 响应都带 CSP（脚本只能来自本服务，不允许内联脚本）、nosniff、禁止被嵌进别的页面；
- 文章 HTML 在服务端用 markdown-it（关掉原始 HTML）渲染，见 render.py；
- key 只写进 .env，接口和页面只给"是否已配置 + 后 4 位"；日志里的 key 换成 ***。

App 是不依赖 HTTP 的业务层（测试直接调用），Handler 只负责路由、校验和编码。
"""

from __future__ import annotations

import contextlib
import hmac
import html
import json
import mimetypes
import os
import secrets
import sys
import threading
import time
import webbrowser
from collections.abc import Callable, Mapping
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
import yaml

from qbkb import config, store
from qbkb.config import CONFIG_DIR
from qbkb.console import browse, demo, jobs, sections_ui
from qbkb.console.net import LocalServer
from qbkb.console.render import render_markdown
from qbkb.console.schedule import ScheduleError, Scheduler, TaskState
from qbkb.console.workspace import (
    DEEPL_KEY,
    DEFAULT_ROOT,
    LLM_KEY,
    Mode,
    Workspace,
    check_key_value,
    check_limit,
    check_time,
    keys_configured,
    load_settings,
    mask,
    read_json,
    read_keys,
    resolve_mode,
    save_settings,
    set_key,
    state_dir,
    workspace,
    write_json,
)
from qbkb.models import QuotaExceededError, Status, Translator
from qbkb.sections import SectionRules, load_section_rules, survey
from qbkb.translate import DeepLTranslator
from qbkb.translate.base import TranslatorUnavailableError
from qbkb.translate.glossary import Glossary, load_glossary
from qbkb.translate.pipeline import title_unit, translated_title
from qbkb.translate.structure import plain_text

# DeepLTranslator 默认 target_lang 对应的翻译记忆后端名：真实模式只读查询用这个稳定标识，
# 不必为读翻译记忆建带凭据的客户端
REAL_BACKEND = "deepl:ZH-HANS"

DEFAULT_PORT = 8790
STATIC_DIR = Path(__file__).parent / "static"
QUOTA_FILE = "quota.json"
MAX_BODY = 64 * 1024
IMAGE_TYPES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".svg", ".img"}
KEY_FIELDS = {"deepl": DEEPL_KEY, "llm": LLM_KEY}
CHECK_TIMEOUT_S = 10.0
APP_ID = "qbkb-console"

CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' http: https: data:; "
    "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _bad(message: str) -> ApiError:
    return ApiError(HTTPStatus.BAD_REQUEST, message)


def _now() -> str:
    from datetime import datetime

    return datetime.now().isoformat(timespec="seconds")


class App:
    """控制台的业务逻辑。所有外部依赖都可以替换：子进程、任务计划、HTTP（测 key 用）。"""

    def __init__(
        self,
        root: Path,
        *,
        token: str | None = None,
        popen: jobs.Popen | None = None,
        scheduler: Scheduler | None = None,
        transport: httpx.BaseTransport | None = None,
        environ: Mapping[str, str] | None = None,
        is_windows: bool | None = None,
        kill: Callable[[jobs.Proc], None] | None = None,
        translator_factory: Callable[[Workspace], Translator] | None = None,
    ) -> None:
        self.root = root.resolve()
        self.token = token or secrets.token_urlsafe(32)
        self._translator_factory = translator_factory
        self._translator_obj: Translator | None = None
        self._translator_mode: str | None = None
        self._translator_key: str | None = None
        self._translating = False  # 整个控制台同一时间只允许一个栏目在翻译（防并发重复计费）
        self._translating_lock = threading.Lock()
        kwargs: dict[str, Any] = {}
        if popen is not None:
            kwargs["popen"] = popen
        if kill is not None:
            kwargs["kill"] = kill
        self.jobs = jobs.JobManager(
            self.root, secrets=self._secrets, on_finish=self._job_done, **kwargs
        )
        self.scheduler = scheduler or Scheduler()
        self.transport = transport
        self.environ = dict(os.environ if environ is None else environ)
        self.is_windows = os.name == "nt" if is_windows is None else is_windows
        self._demo_site: demo.DemoSite | None = None
        self._demo_lock = threading.Lock()

    # ---- 基础 ----

    def keys(self) -> dict[str, str]:
        return read_keys(self.root)

    def _secrets(self) -> list[str]:
        return [v for v in self.keys().values() if v]

    def mode(self) -> Mode:
        return resolve_mode(load_settings(self.root).mode, self.keys())

    def ws(self) -> Workspace:
        return workspace(self.root, self.mode())

    def demo_site(self) -> demo.DemoSite:
        with self._demo_lock:
            if self._demo_site is None:
                self._demo_site = demo.DemoSite(workspace(self.root, "demo"))
            return self._demo_site

    def close(self) -> None:
        self.jobs.stop()
        self.jobs.wait(10)
        if self._demo_site is not None:
            self._demo_site.stop()

    def _conn(self, ws: Workspace) -> Any:
        return store.connect(ws.db) if ws.db.is_file() else None

    def _job_done(self, job: jobs.Job) -> None:
        if job.quota and job.mode == "real":
            write_json(state_dir(self.root) / QUOTA_FILE, {**job.quota, "at": job.finished})

    def _categories(self, ws: Workspace) -> list[str]:
        path = (ws.config_dir or CONFIG_DIR) / "filters.yaml"
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            return []
        cats = data.get("include_categories") if isinstance(data, dict) else None
        return [str(c) for c in cats] if isinstance(cats, list) else []

    def _section_names(self, ws: Workspace) -> dict[str, str]:
        """栏目 id → 栏目名（按站点配置的顺序）。配置读不出来就只显示 id。"""
        from qbkb.sections import load_section_rules

        cfg = ws.config_dir or CONFIG_DIR
        names: dict[str, str] = {}
        try:
            for name in config.list_sites(cfg):
                if ws.site and name != ws.site:
                    continue
                for s in load_section_rules(config.load_site(name, cfg)).sections:
                    names.setdefault(s.id, s.name)
        except (OSError, ValueError, yaml.YAMLError):
            return names
        return names

    # ---- 读 ----

    def state(self) -> dict[str, object]:
        settings = load_settings(self.root)
        keys = self.keys()
        ws = self.ws()
        counts: dict[str, int] = {}
        conn = self._conn(ws)
        if conn is not None:
            try:
                counts = {s.value: n for s, n in store.count_by_status(conn, ws.site).items()}
            finally:
                conn.close()
        job = self.jobs.job
        return {
            "app": APP_ID,
            "mode": ws.mode,
            "mode_setting": settings.mode,
            "keys": {"deepl": mask(keys[DEEPL_KEY]), "llm": mask(keys[LLM_KEY])},
            "keys_ready": keys_configured(keys),
            "counts": counts,
            "job": None if job is None else {**job.summary(), "lines": []},
            "schedule": {
                "enabled": settings.schedule.enabled,
                "time": settings.schedule.time,
                "limit": settings.schedule.limit,
                "supported": self.is_windows,
            },
            "task": self._task_state(),
            "last_auto": read_json(state_dir(self.root) / "last_auto.json"),
            "quota": read_json(state_dir(self.root) / QUOTA_FILE),
            "data_dir": str(ws.base),
        }

    def _task_state(self) -> dict[str, bool] | None:
        if not self.is_windows:
            return None
        try:
            st: TaskState = self.scheduler.state()
        except (OSError, ScheduleError):
            return None
        return {"exists": st.exists, "ours": st.ours}

    def articles(self) -> dict[str, object]:
        ws = self.ws()
        conn = self._conn(ws)
        rows = []
        if conn is not None:
            try:
                rows = store.list_rows(conn, [Status.PUBLISHED], ws.site)
            finally:
                conn.close()
        # v2：按栏目分组；没有栏目的老数据按 v1 的类别
        names = self._section_names(ws)
        groups: dict[str, list[dict[str, object]]] = {}
        for r in rows:
            sec = r["section"]
            label = names.get(str(sec), str(sec)) if sec else str(r["category"] or "未分类")
            groups.setdefault(label, []).append(
                {
                    "url": r["url"],
                    "title": r["title_zh"] or r["title"] or r["slug"],
                    "title_en": r["title"],
                    "date": (r["published"] or "")[:10] or None,
                }
            )
        known = list(names.values()) + self._categories(ws)
        order = [c for c in dict.fromkeys(known) if c in groups]
        order += sorted(c for c in groups if c not in order)
        return {
            "mode": ws.mode,
            "total": len(rows),
            "sites": self._sites_view(ws, rows),
            "groups": [
                {
                    "category": c,
                    "articles": sorted(
                        groups[c], key=lambda a: (str(a["date"] or ""), str(a["url"])), reverse=True
                    ),
                }
                for c in order
            ],
        }

    def _site_layout(self, ws: Workspace) -> list[tuple[str, str, list[tuple[str, str]]]]:
        """按配置顺序的 (站点 id, 站点显示名, [(栏目 id, 栏目名)])。配置读不出来的站点只给 id。"""
        cfg = ws.config_dir or CONFIG_DIR
        out: list[tuple[str, str, list[tuple[str, str]]]] = []
        try:
            names = config.list_sites(cfg)
        except OSError:
            return out
        for name in names:
            if ws.site and name != ws.site:
                continue
            display = name
            secs: list[tuple[str, str]] = []
            try:
                site = config.load_site(name, cfg)
                display = browse.site_display_name(name, site.raw.get("firm"))
                secs = [(s.id, s.name) for s in load_section_rules(site).sections]
            except (OSError, ValueError, yaml.YAMLError):
                pass
            out.append((name, display, secs))
        return out

    def _sites_view(self, ws: Workspace, rows: list[Any]) -> list[dict[str, object]]:
        """站点 → 栏目 → 文章 的分组（浏览页 v2）。站点、栏目按配置顺序，文章按日期倒序。"""
        layout = self._site_layout(ws)
        by_site: dict[str, list[Any]] = {}
        for r in rows:
            by_site.setdefault(str(r["site"]), []).append(r)
        known = {site for site, _, _ in layout}
        layout += [(s, s, []) for s in sorted(by_site) if s not in known]
        sites: list[dict[str, object]] = []
        for site, display, secs in layout:
            site_rows = by_site.get(site)
            if not site_rows:
                continue
            labels = dict(secs)
            groups: dict[str, dict[str, Any]] = {}
            for r in site_rows:
                sid = str(r["section"]) if r["section"] else ""
                if sid:
                    key, label = sid, labels.get(sid, sid)
                else:
                    label = str(r["category"] or "未分类")
                    key = f"category:{label}"
                g = groups.setdefault(key, {"id": key, "name": label, "articles": []})
                g["articles"].append(browse.article_card(r))
            order = [sid for sid, _ in secs if sid in groups]
            order += sorted(k for k in groups if k not in order)
            sections: list[dict[str, object]] = []
            for key in order:
                g = groups[key]
                arts = sorted(
                    g["articles"], key=lambda a: (str(a["date"] or ""), str(a["url"])), reverse=True
                )
                sections.append(
                    {"id": g["id"], "name": g["name"], "total": len(arts), "articles": arts}
                )
            sites.append(
                {"site": site, "name": display, "total": len(site_rows), "sections": sections}
            )
        return sites

    def article(self, url: str, view: str) -> dict[str, object]:
        from qbkb.export import article_paths

        if view not in ("zh", "bi"):
            raise _bad("view 只能是 zh 或 bi")
        ws = self.ws()
        conn = self._conn(ws)
        if conn is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "还没有文章")
        try:
            row = store.get_article(conn, url)
        finally:
            conn.close()
        if row is None or row["status"] != Status.PUBLISHED or (ws.site and row["site"] != ws.site):
            raise ApiError(HTTPStatus.NOT_FOUND, "没有这篇已发布的文章")
        main, bilingual = article_paths(
            ws.out, str(row["site"]), row["published"], str(row["slug"])
        )
        path = main if view == "zh" else bilingual
        if not path.is_file():
            raise ApiError(
                HTTPStatus.NOT_FOUND,
                f"找不到文件 {path.name}：可能被移动或删除了，重新运行一次即可",
            )
        body = render_markdown(path.read_text(encoding="utf-8"), path, ws.out)
        src = str(row["url"])
        return {
            "url": src,
            "source_url": src if urlsplit(src).scheme in ("http", "https") else None,
            "title": row["title_zh"] or row["title"],
            "title_en": row["title"],
            "author": row["author"],
            "published": row["published"],
            "section": self._section_names(ws).get(str(row["section"]), row["section"])
            if row["section"]
            else None,
            "translated_at": row["translated_at"],
            "translator": row["translator"],
            "summary": row["summary_zh"] or None,
            "translated": bool(row["translated_at"]),
            "has_pdf": bool(row["pdf_path"]),
            "view": view,
            "html": body,
        }

    def review(self) -> dict[str, object]:
        ws = self.ws()
        conn = self._conn(ws)
        items: list[dict[str, object]] = []
        if conn is not None:
            try:
                rows = store.list_rows(conn, [Status.NEEDS_REVIEW, Status.FAILED], ws.site)
            finally:
                conn.close()
            for r in rows:
                items.append(
                    {
                        "url": r["url"],
                        "site": r["site"],
                        "title": r["title_zh"] or r["title"] or r["slug"],
                        "status": r["status"],
                        "reason": r["error"] or "",
                        "retries": r["retries"],
                        "can_retranslate": r["status"] == Status.NEEDS_REVIEW,
                    }
                )
        return {"mode": ws.mode, "items": items}

    def job(self, since: int) -> dict[str, object]:
        job = self.jobs.job
        return {"job": None if job is None else job.summary(since)}

    def out_file(self, rel: str) -> Path:
        ws = self.ws()
        root = ws.out.resolve()
        try:
            path = (root / unquote(rel)).resolve()
            path.relative_to(root)
        except (OSError, ValueError) as e:
            raise ApiError(HTTPStatus.NOT_FOUND, "没有这个文件") from e
        if path.suffix.lower() not in IMAGE_TYPES or not path.is_file():
            raise ApiError(HTTPStatus.NOT_FOUND, "没有这个文件")
        return path

    # ---- 栏目抽样 ----

    def _section_cfg(self, ws: Workspace) -> Path:
        return ws.config_dir or CONFIG_DIR

    def _section_sites(self, ws: Workspace) -> list[str]:
        """可以抽样的站点：演示模式就是 demo；真实模式是所有配了 sections 的站点。"""
        if ws.demo:
            return ["demo"]
        cfg = self._section_cfg(ws)
        names: list[str] = []
        for name in config.list_sites(cfg):
            try:
                if load_section_rules(config.load_site(name, cfg)).enabled:
                    names.append(name)
            except (OSError, ValueError, yaml.YAMLError):
                continue
        return names

    def _resolve_section_site(self, ws: Workspace, site: object) -> str:
        if not isinstance(site, str) or not site:
            raise _bad("要指定站点")
        if site not in self._section_sites(ws):
            raise _bad(f"没有这个站点：{site}")
        return site

    def _section_rules(self, ws: Workspace, site: str) -> SectionRules:
        cfg = self._section_cfg(ws)
        return load_section_rules(config.load_site(site, cfg))

    def _csv_path(self, ws: Workspace, site: str) -> Path:
        return ws.out / "sections" / f"{site}.csv"

    def _translator(self, ws: Workspace) -> Translator:
        key = "" if ws.demo else self.keys().get(DEEPL_KEY, "")
        if (
            self._translator_obj is None
            or self._translator_mode != ws.mode
            or self._translator_key != key
        ):
            if self._translator_factory is not None:
                self._translator_obj = self._translator_factory(ws)
            elif ws.demo:
                self._translator_obj = demo.DemoTranslator(ws.base)
            else:
                if not key:
                    raise RuntimeError("没有找到 DEEPL_API_KEY：请在「设置」页填写")
                import deepl

                # 显式用刚读到的 key 建客户端，不用 load_dotenv（它不覆盖 os.environ 里的旧值）
                self._translator_obj = DeepLTranslator(deepl.DeepLClient(key, max_retries=0))
            self._translator_mode = ws.mode
            self._translator_key = key
        return self._translator_obj

    def _translator_name(self, ws: Workspace) -> str | None:
        """只读翻译记忆时用的后端名。

        测试注入后端时用它的真实名字；生产按模式用稳定标识（真实模式不建带凭据的客户端）。
        """
        if self._translator_factory is not None:
            try:
                return self._translator(ws).name
            except Exception:
                return None
        return "demo" if ws.demo else REAL_BACKEND

    def _glossary(self, ws: Workspace) -> Glossary:
        path = (ws.config_dir or CONFIG_DIR) / "glossary.yaml"
        try:
            return load_glossary(path)
        except (OSError, ValueError, yaml.YAMLError):
            return Glossary("", {}, {})

    def sections_view(self, site: str) -> dict[str, object]:
        ws = self.ws()
        if not site:
            sites = self._section_sites(ws)
            if not sites:
                return {
                    "mode": ws.mode,
                    "sites": [],
                    "site": None,
                    "has_csv": False,
                    "summary": [],
                    "sections": [],
                }
            site = sites[0]
        site = self._resolve_section_site(ws, site)
        try:
            rules = self._section_rules(ws, site)
        except (OSError, ValueError, yaml.YAMLError) as e:
            raise ApiError(HTTPStatus.NOT_FOUND, f"读不到站点 {site} 的配置：{e}") from e
        path = self._csv_path(ws, site)
        rows = survey.read_csv(path) if path.is_file() else []
        conn = self._conn(ws)
        tallies = []
        try:
            if conn is not None:
                tallies = survey.tally(conn, site, rules)
        finally:
            if conn is not None:
                conn.close()
        vs, _bad = survey.verdicts(rows)
        sampled: dict[str, int] = {}
        for r in rows:
            sid = r.get("栏目id", "")
            sampled[sid] = sampled.get(sid, 0) + 1
        summary = [
            dict(zip(survey.SUMMARY_COLUMNS, row, strict=True))
            for row in survey.summary_rows(tallies, vs, sampled)
        ]
        return {
            "mode": ws.mode,
            "sites": self._section_sites(ws),
            "site": site,
            "has_csv": path.is_file(),
            "summary": summary,
            "sections": self._section_articles(ws, rows),
        }

    def _section_articles(
        self, ws: Workspace, rows: list[dict[str, str]]
    ) -> list[dict[str, object]]:
        """按抽样表的栏目分组，逐篇补上标题 / 日期 / 摘要和缓存的中文。"""
        groups: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for r in rows:
            sid = r.get("栏目id", "")
            if sid not in groups:
                groups[sid] = {
                    "id": sid,
                    "name": r.get("栏目", sid),
                    "collect": r.get("是否采集", "—"),
                    "rows": [],
                }
                order.append(sid)
            groups[sid]["rows"].append(r)
        conn = self._conn(ws)
        backend = self._translator_name(ws)
        glossary = self._glossary(ws) if backend else None
        out: list[dict[str, object]] = []
        try:
            for sid in order:
                g = groups[sid]
                arts: list[dict[str, object]] = []
                units: list[str] = []
                for r in g["rows"]:
                    a = self._article_info(conn, r, backend, glossary)
                    if a["title_en"]:
                        units.append(title_unit(str(a["title_en"])))
                    if a["summary"]:
                        units.append(sections_ui.summary_unit(str(a["summary"])))
                    arts.append(a)
                est = 0
                if conn is not None and backend is not None and glossary is not None:
                    est = sections_ui.estimate_units(conn, units, backend, glossary)
                out.append(
                    {
                        "id": sid,
                        "name": g["name"],
                        "collect": g["collect"],
                        "estimate_chars": est,
                        "articles": arts,
                    }
                )
        finally:
            if conn is not None:
                conn.close()
        return out

    def _article_info(
        self,
        conn: Any,
        r: dict[str, str],
        backend: str | None,
        glossary: Glossary | None,
    ) -> dict[str, object]:
        url = r.get("网址", "")
        a: dict[str, object] = {
            "url": url,
            "title_en": r.get("英文标题", ""),
            "title_zh": "",
            "date": r.get("发布日期", ""),
            "summary": "",
            "summary_zh": "",
            "mark": r.get("要不要", ""),
            "note": r.get("备注", ""),
        }
        if conn is None:
            return a
        row = store.get_article(conn, url)
        if row is not None:
            a["title_en"] = str(row["title"] or a["title_en"])
            a["date"] = (str(row["published"] or "")[:10]) or a["date"]
            a["title_zh"] = str(row["title_zh"] or "")
            a["summary"] = sections_ui.summary_text(conn, url)
        if backend is not None and glossary is not None:
            if not a["title_zh"] and a["title_en"]:
                a["title_zh"] = translated_title(conn, str(a["title_en"]), backend, glossary) or ""
            if a["summary"]:
                a["summary_zh"] = (
                    sections_ui.translated_summary(conn, str(a["summary"]), backend, glossary) or ""
                )
        return a

    def generate_sections(self, body: Mapping[str, Any]) -> dict[str, object]:
        ws = self.ws()
        site = self._resolve_section_site(ws, body.get("site"))
        try:
            rules = self._section_rules(ws, site)
        except (OSError, ValueError, yaml.YAMLError) as e:
            raise ApiError(HTTPStatus.NOT_FOUND, f"读不到站点 {site} 的配置：{e}") from e
        conn = self._conn(ws)
        tallies = []
        try:
            if conn is not None:
                tallies = survey.tally(conn, site, rules)
        finally:
            if conn is not None:
                conn.close()
        path = self._csv_path(ws, site)
        with sections_ui.csv_lock(path):
            old = survey.read_csv(path) if path.is_file() else []
            survey.write_samples(path, tallies, survey.DEFAULT_SAMPLE, survey.DEFAULT_SEED, old)
        return self.sections_view(site)

    def mark_section(self, body: Mapping[str, Any]) -> dict[str, object]:
        ws = self.ws()
        site = self._resolve_section_site(ws, body.get("site"))
        url = body.get("url")
        if not isinstance(url, str) or not url:
            raise _bad("要给出网址")
        mark = body.get("mark")  # None = 不改；"" = 清除；要 / 不要 = 设置
        if mark is not None and mark not in ("要", "不要", ""):
            raise _bad("标记只能是「要」「不要」或空（清除）")
        note = body.get("note")
        if note is not None and not isinstance(note, str):
            raise _bad("备注要是文字")
        path = self._csv_path(ws, site)
        if not path.is_file():
            raise _bad("还没有抽样表，先生成一张")
        try:
            sections_ui.update_marks(path, {url: (mark, note)})
        except KeyError:
            raise _bad("这个网址不在抽样表里") from None
        except sections_ui.SamplingCsvFormatError as e:
            raise _bad(str(e)) from e
        except sections_ui.SamplingCsvError as e:
            raise ApiError(HTTPStatus.CONFLICT, str(e)) from e
        return self.sections_view(site)

    def translate_section(self, body: Mapping[str, Any]) -> dict[str, object]:
        ws = self.ws()
        site = self._resolve_section_site(ws, body.get("site"))
        section = body.get("section")
        if not isinstance(section, str) or not section:
            raise _bad("要指定栏目")
        path = self._csv_path(ws, site)
        rows = survey.read_csv(path) if path.is_file() else []
        sec_rows = [r for r in rows if r.get("栏目id") == section]
        if not sec_rows:
            raise _bad("这个栏目没有抽样的文章")
        # 互斥：翻译记忆按文本 hash 共享，不同栏目可能撞同一段文本；只按栏目拦重入挡不住两个标签页
        # 同时翻不同栏目各自重复计费。所以用一把 App 级锁：整个控制台同一时间只允许一个栏目在翻译。
        # 另外还要拿任务锁（见下），和后台任务、每天的定时任务互斥。
        with self._translating_lock:
            if self._translating:
                raise ApiError(HTTPStatus.CONFLICT, "已有栏目正在翻译，等它完成再点")
            self._translating = True
        done = 0
        failed_plain: list[str] = []
        try:
            # 和 /api/run、/api/translate、重翻共用同一把任务锁：原子拿到后才读翻译记忆、
            # 构造待翻单元，一直持有到调用和翻译记忆落库结束，期间别的翻译入口都返回 409
            with self.jobs.exclusive("栏目翻译"):
                done, failed_plain = self._translate_section_units(ws, sec_rows)
        except jobs.Busy as e:
            raise ApiError(HTTPStatus.CONFLICT, str(e)) from e
        finally:
            with self._translating_lock:
                self._translating = False
        view = self.sections_view(site)
        view["translated"] = done
        view["failed"] = failed_plain
        return view

    def _translate_section_units(
        self, ws: Workspace, sec_rows: list[dict[str, str]]
    ) -> tuple[int, list[str]]:
        """翻一个栏目抽样文章的标题 + 摘要（调用方已持有任务锁）。

        返回 (新译条数, 校验失败的原文)。"""
        try:
            translator = self._translator(ws)
        except Exception as e:
            raise ApiError(HTTPStatus.BAD_REQUEST, f"翻译后端起不来：{e}") from e
        glossary = self._glossary(ws)
        conn = self._conn(ws)
        if conn is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "还没有文章")
        try:
            units: list[str] = []
            title_rows: list[tuple[str, str]] = []
            for r in sec_rows:
                url = r.get("网址", "")
                row = store.get_article(conn, url)
                if row is None:
                    continue
                title = str(row["title"] or "")
                if title and not row["title_zh"]:
                    units.append(title_unit(title))
                    title_rows.append((url, title))
                summary = sections_ui.summary_text(conn, url)
                if summary:
                    units.append(sections_ui.summary_unit(summary))
            try:
                done, failed = sections_ui.translate_units(conn, units, translator, glossary)
            except QuotaExceededError as e:
                raise ApiError(HTTPStatus.TOO_MANY_REQUESTS, f"翻译额度用完，没翻完：{e}") from e
            except TranslatorUnavailableError as e:
                raise ApiError(HTTPStatus.BAD_GATEWAY, f"翻译后端暂时不可用：{e}") from e
            for url, title in title_rows:
                zh = translated_title(conn, title, translator.name, glossary)
                if zh:
                    store.set_fields(conn, url, title_zh=zh)
            return done, [plain_text(u) for u in failed]
        finally:
            conn.close()

    # ---- 写 ----

    def _env_for(self, ws: Workspace) -> dict[str, str]:
        # .env 里的 key 优先：控制台启动后才保存的 key 也能用上（规则见 jobs.job_env）
        return jobs.job_env(ws, self.environ, self.keys())

    def _prepare(self, ws: Workspace) -> None:
        if self.jobs.running():
            raise ApiError(HTTPStatus.CONFLICT, "已经有任务在运行，等它跑完再开始新的")
        if ws.demo:
            self.demo_site().start()
        elif not keys_configured(self.keys()):
            raise _bad("真实模式需要先在「设置」页填写 DeepL 的 key（或切回演示模式）")

    def start_run(self, body: Mapping[str, Any]) -> dict[str, object]:
        try:
            limit = check_limit(body.get("limit"))
        except ValueError as e:
            raise _bad(str(e)) from e
        ws = self.ws()
        self._prepare(ws)
        title = f"运行（最多 {limit} 篇，{'演示' if ws.demo else '真实'}模式）"
        return self._start(ws, "run", title, [jobs.run_command(ws, limit)])

    def retranslate(self, body: Mapping[str, Any]) -> dict[str, object]:
        url = body.get("url")
        ws = self.ws()
        conn = self._conn(ws)
        if conn is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "还没有文章")
        try:
            if isinstance(url, str) and url:
                row = store.get_article(conn, url)
                if row is None or row["status"] != Status.NEEDS_REVIEW:
                    raise _bad("这篇文章不在待复查列表里")
                site = str(row["site"])
            else:
                site = body.get("site")
                if not isinstance(site, str) or not site:
                    raise _bad("要给出 url 或 site")
                if not store.list_by_status(conn, Status.NEEDS_REVIEW, site):
                    raise _bad(f"站点 {site} 没有待复查的文章")
                url = None
        finally:
            conn.close()
        self._prepare(ws)
        title = f"重翻 {url}" if url else f"重翻站点 {site} 的待复查文章"
        return self._start(ws, "retranslate", title, jobs.retranslate_commands(ws, url, site))

    def translate_fulltext(self, body: Mapping[str, Any]) -> dict[str, object]:
        """翻选中文章的全文。dry_run=true 只估算（不调外部服务）；false 才开后台任务。"""
        dry_run = body.get("dry_run")
        if not isinstance(dry_run, bool):
            raise _bad("dry_run 必须是 true / false")
        raw = body.get("urls")
        if not isinstance(raw, list) or any(not isinstance(u, str) or not u for u in raw):
            raise _bad("urls 必须是网址列表")
        for u in cast("list[str]", raw):
            if (problem := browse.url_problem(u)) is not None:
                raise _bad(problem)
        urls = list(dict.fromkeys(cast("list[str]", raw)))
        if not urls:
            raise _bad("请先选择要翻译的文章")
        if len(urls) > browse.MAX_TRANSLATE_URLS:
            raise _bad(f"一次最多选 {browse.MAX_TRANSLATE_URLS} 篇")
        ws = self.ws()
        conn = self._conn(ws)
        if conn is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "还没有文章")
        try:
            todo: list[str] = []
            for url in urls:
                row = store.get_article(conn, url)
                if (
                    row is None
                    or row["status"] != Status.PUBLISHED
                    or (ws.site and row["site"] != ws.site)
                ):
                    raise _bad(f"只能选已发布的文章，这篇不是：{url}")
                if not row["translated_at"]:
                    todo.append(url)
            already = len(urls) - len(todo)
            if dry_run:
                return self._translate_estimate(ws, conn, todo, already)
        finally:
            conn.close()
        if not todo:
            raise _bad("选中的文章都已经翻过全文了")
        # 同 _prepare 的检查，但不必启动演示站点：演示译者自带中文，translate 不联网
        if self.jobs.running():
            raise ApiError(HTTPStatus.CONFLICT, "已经有任务在运行，等它跑完再开始新的")
        if not ws.demo and not keys_configured(self.keys()):
            raise _bad("真实模式需要先在「设置」页填写 DeepL 的 key（或切回演示模式）")
        path = self._write_url_list(todo)
        try:
            title = f"翻译全文（{len(todo)} 篇，{'演示' if ws.demo else '真实'}模式）"
            self._start(ws, "translate", title, jobs.translate_urls_commands(ws, path))
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return {"started": True, "count": len(todo)}

    def _translate_estimate(
        self, ws: Workspace, conn: Any, todo: list[str], already: int
    ) -> dict[str, object]:
        """dry_run：估算待翻字符数。只读状态库和 quota.json，不建翻译客户端、不联网。"""
        name = self._translator_name(ws)
        est = browse.FulltextEstimate(0, 0)
        if name is not None and todo:
            est = browse.estimate_fulltext(conn, todo, name, self._glossary(ws))
        quota = None if ws.demo else read_json(state_dir(self.root) / QUOTA_FILE)
        left = quota.get("remaining") if quota else None
        has_key = (
            ws.demo or self._translator_factory is not None or bool(self.keys().get(DEEPL_KEY))
        )
        return {
            "count": len(todo),
            "already": already,
            # chars 已扣掉翻译记忆命中的段落，并把结构校验失败后的重翻算作最坏情况（上限）；
            # chars_base 是不含重翻的基础估算；没有待翻正文段落（只剩标题）时两者相等、不是上限
            "chars": est.maximum,
            "chars_base": est.base,
            "chars_upper_bound": not est.exact,
            "backend": name.split(":")[0] if name and has_key else None,
            "quota_left": left if isinstance(left, int) else None,
        }

    def _write_url_list(self, urls: list[str]) -> Path:
        """把要翻的网址写进临时清单（每行一个）。顺手清掉一天前留下的旧清单。"""
        folder = state_dir(self.root)
        folder.mkdir(parents=True, exist_ok=True)
        cutoff = time.time() - 86400
        for old in folder.glob("translate-urls-*.txt"):
            with contextlib.suppress(OSError):
                if old.stat().st_mtime < cutoff:
                    old.unlink()
        path = folder / f"translate-urls-{secrets.token_hex(6)}.txt"
        text = "\n".join(urls) + "\n"
        path.write_bytes(text.encode("utf-8"))
        # 消费方（qbkb translate --url-file）按行拆：确认每一行正好是选中的那一个网址
        if text.splitlines() != urls:
            path.unlink(missing_ok=True)
            raise _bad("网址里含有换行等特殊字符，拒绝翻译")
        return path

    def _start(
        self, ws: Workspace, kind: str, title: str, commands: list[list[str]]
    ) -> dict[str, object]:
        try:
            job = self.jobs.start(ws, kind, title, commands, self._env_for(ws))
        except jobs.Busy as e:
            raise ApiError(HTTPStatus.CONFLICT, str(e)) from e
        return {"job": job.summary()}

    def stop(self) -> dict[str, object]:
        return {"stopped": self.jobs.stop()}

    def save_keys(self, body: Mapping[str, Any]) -> dict[str, object]:
        updates: dict[str, str] = {}
        for field, name in KEY_FIELDS.items():
            value = body.get(field)
            if value is None or value == "":
                continue
            if not isinstance(value, str):
                raise _bad("key 必须是文字")
            try:
                updates[name] = check_key_value(value)
            except ValueError as e:
                raise _bad(f"{'DeepL' if field == 'deepl' else 'DeepSeek'} {e}") from e
        clear = body.get("clear") or []
        if not isinstance(clear, list) or any(c not in KEY_FIELDS for c in clear):
            raise _bad("clear 只能是 deepl / llm")
        if not updates and not clear:
            raise _bad("没有要保存的 key")
        for name, value in updates.items():
            set_key(self.root, name, value)
        for c in clear:
            set_key(self.root, KEY_FIELDS[str(c)], "")
        keys = self.keys()
        return {"keys": {"deepl": mask(keys[DEEPL_KEY]), "llm": mask(keys[LLM_KEY])}}

    def test_key(self, body: Mapping[str, Any]) -> dict[str, object]:
        which = body.get("which")
        if which not in KEY_FIELDS:
            raise _bad("which 只能是 deepl 或 llm")
        typed = body.get("key")
        if isinstance(typed, str) and typed.strip():
            try:
                key = check_key_value(typed)
            except ValueError as e:
                raise _bad(str(e)) from e
        else:
            key = self.keys()[KEY_FIELDS[str(which)]]
        if not key:
            return {"ok": False, "message": "还没有填写这个 key"}
        try:
            with httpx.Client(timeout=CHECK_TIMEOUT_S, transport=self.transport) as client:
                if which == "deepl":
                    return self._check_deepl(client, key)
                return self._check_llm(client, key)
        except httpx.HTTPError as e:
            # httpx 的错误信息里只有地址，没有请求头，不会带出 key
            return {"ok": False, "message": f"连不上：{type(e).__name__}（检查网络或代理）"}

    def _check_deepl(self, client: httpx.Client, key: str) -> dict[str, object]:
        host = "https://api-free.deepl.com" if key.endswith(":fx") else "https://api.deepl.com"
        r = client.get(f"{host}/v2/usage", headers={"Authorization": f"DeepL-Auth-Key {key}"})
        if r.status_code in (401, 403):
            return {"ok": False, "message": f"key 无效或没有权限（HTTP {r.status_code}）"}
        if r.status_code != 200:
            return {"ok": False, "message": f"DeepL 返回 HTTP {r.status_code}"}
        try:
            data = r.json()
            used, limit = int(data["character_count"]), int(data["character_limit"])
        except (ValueError, KeyError, TypeError):
            return {"ok": False, "message": "DeepL 的返回看不懂（不是 usage 格式）"}
        left = max(limit - used, 0)
        quota = {"used": used, "limit": limit, "remaining": left, "at": _now()}
        write_json(state_dir(self.root) / QUOTA_FILE, quota)
        return {
            "ok": True,
            "message": f"连接正常：本月已用 {used:,} / 上限 {limit:,} 字符（剩余 {left:,}）",
            "quota": quota,
        }

    def _llm_base(self) -> str:
        from dotenv import dotenv_values

        env = dotenv_values(self.root / ".env") if (self.root / ".env").is_file() else {}
        base = env.get("LLM_BASE_URL") or self.environ.get("LLM_BASE_URL")
        if not base:
            try:
                data = yaml.safe_load((CONFIG_DIR / "filters.yaml").read_text(encoding="utf-8"))
                base = str(data["l2"]["base_url"])
            except (OSError, KeyError, TypeError, yaml.YAMLError):
                base = "https://api.deepseek.com"
        return base.rstrip("/")

    def _check_llm(self, client: httpx.Client, key: str) -> dict[str, object]:
        base = self._llm_base()
        if urlsplit(base).scheme != "https":
            return {"ok": False, "message": "LLM_BASE_URL 必须是 https 地址"}
        r = client.get(f"{base}/models", headers={"Authorization": f"Bearer {key}"})
        if r.status_code in (401, 403):
            return {"ok": False, "message": f"key 无效或没有权限（HTTP {r.status_code}）"}
        if r.status_code != 200:
            return {"ok": False, "message": f"{urlsplit(base).netloc} 返回 HTTP {r.status_code}"}
        try:
            models = [str(m["id"]) for m in r.json()["data"]]
        except (ValueError, KeyError, TypeError):
            models = []
        shown = "、".join(models[:5]) or "（列表为空）"
        return {"ok": True, "message": f"连接正常：可用模型 {len(models)} 个：{shown}"}

    def set_mode(self, body: Mapping[str, Any]) -> dict[str, object]:
        value = body.get("mode")
        if value not in ("auto", "demo", "real"):
            raise _bad("mode 只能是 auto / demo / real")
        if self.jobs.running():
            raise ApiError(HTTPStatus.CONFLICT, "任务运行中不能切换模式")
        s = load_settings(self.root)
        s.mode = value
        save_settings(self.root, s)
        return self.state()

    def set_schedule(self, body: Mapping[str, Any]) -> dict[str, object]:
        enabled = body.get("enabled")
        if not isinstance(enabled, bool):
            raise _bad("enabled 必须是 true / false")
        s = load_settings(self.root)
        try:
            at = check_time(body.get("time", s.schedule.time))
            limit = check_limit(body.get("limit", s.schedule.limit))
        except ValueError as e:
            raise _bad(str(e)) from e
        if not self.is_windows:
            raise _bad("每天自动更新目前只支持 Windows 的任务计划")
        try:
            if enabled:
                self.scheduler.enable(self.root, at)
            else:
                self.scheduler.disable()
        except (ScheduleError, OSError) as e:
            raise ApiError(HTTPStatus.INTERNAL_SERVER_ERROR, str(e)) from e
        s.schedule.enabled, s.schedule.time, s.schedule.limit = enabled, at, limit
        save_settings(self.root, s)
        return self.state()

    def reset_demo(self) -> dict[str, object]:
        if self.jobs.running():
            raise ApiError(HTTPStatus.CONFLICT, "任务运行中不能清空演示数据")
        demo.reset_data(workspace(self.root, "demo"))
        return self.state()


# ---- HTTP --------------------------------------------------------------------


def _index_html(token: str) -> bytes:
    text = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    return text.replace("{{TOKEN}}", html.escape(token, quote=True)).encode("utf-8")


def make_handler(app: App, port_box: list[int]) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "qbkb-console"
        sys_version = ""

        def log_message(self, format: str, *args: object) -> None:
            pass  # 不打印每个请求（地址里可能有文章 URL，没有 key；只是太吵）

        # -- 通用 --

        def _allowed_hosts(self) -> set[str]:
            port = port_box[0]
            return {f"127.0.0.1:{port}", f"localhost:{port}"}

        def _send(self, status: int, body: bytes, ctype: str, extra: dict[str, str] | None = None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, data: object) -> None:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self._send(status, body, "application/json; charset=utf-8")

        def _error(self, status: int, message: str) -> None:
            self._json(status, {"error": message})

        def _host_ok(self) -> bool:
            return self.headers.get("Host", "") in self._allowed_hosts()

        def _handle(self, fn: Callable[[], None]) -> None:
            if not self._host_ok():
                self._error(HTTPStatus.FORBIDDEN, "只接受来自本机控制台页面的请求")
                return
            try:
                fn()
            except ApiError as e:
                self._error(e.status, e.message)
            except Exception as e:  # 兜底：不把堆栈或内部细节发给页面
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"控制台内部错误：{type(e).__name__}")

        # -- GET --

        def do_GET(self) -> None:
            self._handle(self._get)

        def _get(self) -> None:
            parts = urlsplit(self.path)
            q = {k: v[0] for k, v in parse_qs(parts.query).items()}
            path = parts.path
            if path == "/":
                self._send(HTTPStatus.OK, _index_html(app.token), "text/html; charset=utf-8")
            elif path in ("/static/app.js", "/static/app.css"):
                name = path.rsplit("/", 1)[1]
                ctype = "text/javascript" if name.endswith(".js") else "text/css"
                body = (STATIC_DIR / name).read_bytes()
                self._send(HTTPStatus.OK, body, f"{ctype}; charset=utf-8")
            elif path == "/api/ping":
                self._json(HTTPStatus.OK, {"app": APP_ID})
            elif path == "/api/state":
                self._json(HTTPStatus.OK, app.state())
            elif path == "/api/articles":
                self._json(HTTPStatus.OK, app.articles())
            elif path == "/api/article":
                self._json(HTTPStatus.OK, app.article(q.get("url", ""), q.get("view", "zh")))
            elif path == "/api/review":
                self._json(HTTPStatus.OK, app.review())
            elif path == "/api/sections":
                self._json(HTTPStatus.OK, app.sections_view(q.get("site", "")))
            elif path == "/api/job":
                try:
                    since = max(int(q.get("since", "0")), 0)
                except ValueError:
                    since = 0
                self._json(HTTPStatus.OK, app.job(since))
            elif path.startswith("/out/"):
                f = app.out_file(path[len("/out/") :])
                ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
                if f.suffix.lower() == ".img":
                    ctype = "application/octet-stream"
                self._send(HTTPStatus.OK, f.read_bytes(), ctype)
            else:
                self._error(HTTPStatus.NOT_FOUND, "没有这个页面")

        # -- POST --

        def do_POST(self) -> None:
            self._handle(self._post)

        def _post(self) -> None:
            token = self.headers.get("X-QBKB-Token", "")
            if not hmac.compare_digest(token.encode("utf-8"), app.token.encode("utf-8")):
                raise ApiError(HTTPStatus.FORBIDDEN, "token 不对：请刷新控制台页面")
            origin = self.headers.get("Origin")
            if origin is not None and urlsplit(origin).netloc not in self._allowed_hosts():
                raise ApiError(HTTPStatus.FORBIDDEN, "不接受来自别的网站的请求")
            ctype = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
            if ctype != "application/json":
                raise ApiError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "只接受 JSON")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as e:
                raise _bad("Content-Length 不对") from e
            if not 0 <= length <= MAX_BODY:
                raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "请求太大")
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError as e:
                raise _bad("请求不是合法的 JSON") from e
            if not isinstance(body, dict):
                raise _bad("请求必须是 JSON 对象")
            routes: dict[str, Callable[[dict[str, Any]], dict[str, object]]] = {
                "/api/run": app.start_run,
                "/api/retranslate": app.retranslate,
                "/api/translate": app.translate_fulltext,
                "/api/stop": lambda b: app.stop(),
                "/api/keys": app.save_keys,
                "/api/keys/test": app.test_key,
                "/api/mode": app.set_mode,
                "/api/schedule": app.set_schedule,
                "/api/demo/reset": lambda b: app.reset_demo(),
                "/api/shutdown": lambda b: self._shutdown(),
                "/api/sections/generate": app.generate_sections,
                "/api/sections/mark": app.mark_section,
                "/api/sections/translate": app.translate_section,
            }
            fn = routes.get(urlsplit(self.path).path)
            if fn is None:
                raise ApiError(HTTPStatus.NOT_FOUND, "没有这个接口")
            self._json(HTTPStatus.OK, fn(body))

        def _shutdown(self) -> dict[str, object]:
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return {"bye": True}

    return Handler


def make_server(app: App, port: int) -> ThreadingHTTPServer:
    """只绑定 127.0.0.1。port 为 0 时由系统分配。"""
    box = [port]
    srv = LocalServer(("127.0.0.1", port), make_handler(app, box))
    box[0] = srv.server_address[1]
    srv.daemon_threads = True
    return srv


def _existing(port: int) -> bool:
    """这个端口上是不是已经有一个控制台在跑（双击两次的情况）。"""
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/api/ping", timeout=1.0, trust_env=False)
        return r.status_code == 200 and r.json().get("app") == APP_ID
    except (httpx.HTTPError, ValueError):
        return False


def _bind(app: App, port: int | None) -> ThreadingHTTPServer:
    candidates = [port] if port is not None else [*range(DEFAULT_PORT, DEFAULT_PORT + 10), 0]
    last: OSError | None = None
    for p in candidates:
        try:
            return make_server(app, p)
        except OSError as e:
            last = e
    raise RuntimeError(f"端口都被占用了：{last}")


def run_console(
    root: Path | None = None, port: int | None = None, open_browser: bool = True
) -> int:
    root = (root or DEFAULT_ROOT).resolve()
    first = port or DEFAULT_PORT
    if _existing(first):
        url = f"http://127.0.0.1:{first}/"
        print(f"控制台已经在运行：{url}")
        if open_browser:
            webbrowser.open(url)
        return 0
    app = App(root)
    srv = _bind(app, port)
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    mode = app.mode()
    print(f"qbkb 控制台已启动：{url}")
    print(f"  项目目录：{root}")
    print(f"  当前模式：{'演示模式（不联网、不需要 key）' if mode == 'demo' else '真实模式'}")
    print("  关闭这个窗口（或按 Ctrl+C、或在页面右上角点「退出」）就会停止。")
    sys.stdout.flush()
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        with contextlib.suppress(Exception):
            app.close()
        srv.server_close()
    print("控制台已停止。")
    return 0
