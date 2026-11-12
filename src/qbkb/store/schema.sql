-- 状态库（design.md 第 5 节）。connect() 每次都会执行，所以全部用 IF NOT EXISTS。

CREATE TABLE IF NOT EXISTS articles (
  url            TEXT PRIMARY KEY,
  site           TEXT NOT NULL,
  slug           TEXT NOT NULL,
  status         TEXT NOT NULL,
  lastmod        TEXT,
  etag           TEXT,
  last_modified  TEXT,     -- 响应头 Last-Modified，用于 If-Modified-Since
  http_status    INTEGER,
  raw_path       TEXT,
  body_hash      TEXT,
  body_segments  INTEGER,  -- 前多少段是正文，其后是版权声明等附录（keep）；NULL 表示全是正文
  title          TEXT,
  title_zh       TEXT,
  author         TEXT,
  published      TEXT,
  tags           TEXT,     -- JSON 数组字符串
  category       TEXT,
  value_score    REAL,
  evergreen      INTEGER,  -- 0 / 1
  score_reason   TEXT,
  rubric_version TEXT,
  scored_body_hash TEXT,  -- 打分时的 body_hash；和当前 body_hash 相同就复用打分
  translator     TEXT,
  translated_at  TEXT,
  md_path        TEXT,
  qc_issues      TEXT,     -- extract 质检问题，JSON 数组；没有问题为 NULL
  section_key    TEXT,     -- 页面上读到的栏目标识（如 "ARTICLE | VIEWS FROM THE FLOOR"、"url:/insights/x"）；
                           -- "" 表示读不到；NULL 表示还没按 v2 规则判定过（老数据）
  section        TEXT,     -- 对应站点配置 sections 里的栏目 id；NULL 表示配置里没有这个栏目
  summary_zh TEXT,
  summary_body_hash TEXT,
  summary_version TEXT,
  summary_error TEXT,
  summary_at TEXT,
  fetched_at TEXT,       -- 成功保存HTML的UTC时间；旧行不推断回填，304不刷新
  summary_truncated INTEGER,
  error          TEXT,
  retries        INTEGER NOT NULL DEFAULT 0,
  updated_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_articles_status ON articles (status, site);

-- 文章由哪些段落组成。存 HTML，translate 直接从这里取要翻的正文，不用回头调 extract。
CREATE TABLE IF NOT EXISTS segments (
  article_url TEXT NOT NULL REFERENCES articles (url) ON DELETE CASCADE,
  idx         INTEGER NOT NULL,
  src_hash    TEXT NOT NULL,
  src_html    TEXT NOT NULL,
  PRIMARY KEY (article_url, idx)
);

-- 段落级翻译记忆。glossary_version 不同视为未命中（术语表升级后要重翻含术语的段落）；
-- tm_get 只返回 quality = 'ok' 的译文，rejected 的只能通过 tm_lookup 看到。
CREATE TABLE IF NOT EXISTS translation_memory (
  src_hash         TEXT NOT NULL,
  backend          TEXT NOT NULL,
  src_html         TEXT NOT NULL,
  tgt_html         TEXT NOT NULL,
  glossary_version TEXT,
  quality          TEXT NOT NULL DEFAULT 'ok',  -- ok | rejected（等人工复查）
  created_at       TEXT NOT NULL,
  PRIMARY KEY (src_hash, backend)
);
