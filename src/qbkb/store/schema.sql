-- 状态库（design.md 第 5 节）。connect() 每次都会执行，所以全部用 IF NOT EXISTS。

CREATE TABLE IF NOT EXISTS articles (
  url            TEXT PRIMARY KEY,
  site           TEXT NOT NULL,
  slug           TEXT NOT NULL,
  status         TEXT NOT NULL,
  lastmod        TEXT,
  etag           TEXT,
  http_status    INTEGER,
  raw_path       TEXT,
  body_hash      TEXT,
  title          TEXT,
  title_zh       TEXT,
  author         TEXT,
  published      TEXT,
  category       TEXT,
  value_score    REAL,
  rubric_version TEXT,
  md_path        TEXT,
  error          TEXT,
  retries        INTEGER NOT NULL DEFAULT 0,
  updated_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_articles_status ON articles (status, site);

CREATE TABLE IF NOT EXISTS segments (
  article_url TEXT NOT NULL REFERENCES articles (url) ON DELETE CASCADE,
  idx         INTEGER NOT NULL,
  src_hash    TEXT NOT NULL,
  PRIMARY KEY (article_url, idx)
);

CREATE TABLE IF NOT EXISTS translation_memory (
  src_hash         TEXT NOT NULL,
  backend          TEXT NOT NULL,
  src_html         TEXT NOT NULL,
  tgt_html         TEXT NOT NULL,
  glossary_version TEXT,
  created_at       TEXT NOT NULL,
  PRIMARY KEY (src_hash, backend)
);
