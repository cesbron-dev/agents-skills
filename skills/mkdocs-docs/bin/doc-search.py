#!/usr/bin/env python3
"""
MkDocs local RAG search — agents-skills.

SQLite FTS5 (BM25) index + mkdocs.yml nav + shallow git cache.
No external dependencies beyond pyyaml (optional, enables nav-based URLs).
"""

import argparse
import json
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

CACHE_BASE = Path.home() / ".cache" / "agents-skills" / "docs"

try:
    import yaml as _yaml
except ImportError:
    _yaml = None

# ---------------------------------------------------------------------------
# Cache management
# ---------------------------------------------------------------------------


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def _git_head(d: Path) -> str | None:
    r = _run(["git", "-C", str(d), "rev-parse", "HEAD"])
    return r.stdout.strip() if r.returncode == 0 else None


def _remote_url(d: Path) -> str | None:
    r = _run(["git", "-C", str(d), "remote", "get-url", "origin"])
    return r.stdout.strip() if r.returncode == 0 else None


def _stale(d: Path, hours: int) -> bool:
    m = d / ".last-update"
    if not m.exists():
        return True
    return datetime.now() - datetime.fromtimestamp(m.stat().st_mtime) > timedelta(hours=hours)


def ensure_cache(repo_url: str, cache_dir: Path, interval_h: int) -> bool:
    """Clone if missing, pull if stale. Returns True when HEAD changed."""
    cache_dir.mkdir(parents=True, exist_ok=True)

    if (cache_dir / ".git").exists() and _remote_url(cache_dir) != repo_url:
        print("[mkdocs-docs] Remote URL changed — re-cloning", file=sys.stderr)
        shutil.rmtree(cache_dir)
        cache_dir.mkdir(parents=True)

    head_before = _git_head(cache_dir)

    if not (cache_dir / ".git").exists():
        print(f"[mkdocs-docs] Cloning {repo_url} …", file=sys.stderr)
        r = _run(["git", "clone", "--depth=1", "--quiet", repo_url, str(cache_dir)])
        if r.returncode != 0:
            print(f"[mkdocs-docs] Clone failed: {r.stderr.strip()}", file=sys.stderr)
            return False
        (cache_dir / ".last-update").touch()
        return True

    if _stale(cache_dir, interval_h):
        print("[mkdocs-docs] Refreshing cache …", file=sys.stderr)
        r1 = _run(["git", "-C", str(cache_dir), "fetch", "--depth=1", "--quiet", "origin"])
        r2 = _run(["git", "-C", str(cache_dir), "reset", "--hard", "origin/HEAD"])
        if r1.returncode == 0 and r2.returncode == 0:
            (cache_dir / ".last-update").touch()
        else:
            print("[mkdocs-docs] Update failed (offline?). Using cached version.", file=sys.stderr)

    return _git_head(cache_dir) != head_before


# ---------------------------------------------------------------------------
# mkdocs.yml — nav → URL map
# ---------------------------------------------------------------------------


def _file_to_url(file_path: str, base_url: str) -> str:
    parts = Path(file_path).parts
    url_parts = list(parts[:-1]) + ([] if parts[-1] == "index.md" else [parts[-1][:-3]])
    slug = "/".join(url_parts)
    return f"{base_url.rstrip('/')}/{slug}/" if slug else f"{base_url.rstrip('/')}/"


def _walk_nav(nav, base_url: str, out: dict) -> None:
    """Recursively walk a mkdocs nav list and populate out[rel_path] = url."""
    for item in nav:
        if isinstance(item, str):
            out[item] = _file_to_url(item, base_url)
        elif isinstance(item, dict):
            for _title, value in item.items():
                if isinstance(value, str):
                    out[value] = _file_to_url(value, base_url)
                elif isinstance(value, list):
                    _walk_nav(value, base_url, out)


def parse_nav(cache_dir: Path, base_url: str) -> dict[str, str]:
    """Return {relative_md_path: url}. Empty dict if no yaml / no nav."""
    if _yaml is None:
        return {}
    for name in ("mkdocs.yml", "mkdocs.yaml"):
        p = cache_dir / name
        if p.exists():
            try:
                cfg = _yaml.safe_load(p.read_text(errors="ignore"))
                nav = cfg.get("nav") if isinstance(cfg, dict) else None
                if nav:
                    result: dict[str, str] = {}
                    _walk_nav(nav, base_url, result)
                    return result
            except Exception as e:
                print(f"[mkdocs-docs] mkdocs.yml parse error: {e}", file=sys.stderr)
    return {}


# ---------------------------------------------------------------------------
# Markdown → chunks
# ---------------------------------------------------------------------------

_HEADING = re.compile(r"^(#{1,4})\s+(.+)")
_FRONT_MATTER = re.compile(r"^---\s*\n.*?\n---\s*\n", re.DOTALL)


def _strip_frontmatter(text: str) -> str:
    return _FRONT_MATTER.sub("", text, count=1)


def chunk_markdown(content: str, max_words: int = 350) -> list[tuple[str, str]]:
    """
    Split content at headings into (section_title, body) pairs.
    Large sections are further split by word count to cap token use.
    """
    text = _strip_frontmatter(content)
    lines = text.splitlines()

    sections: list[tuple[str, list[str]]] = []
    cur_title = ""
    cur_lines: list[str] = []

    for line in lines:
        m = _HEADING.match(line)
        if m:
            if cur_lines:
                sections.append((cur_title, cur_lines))
            cur_title = m.group(2).strip()
            cur_lines = [line]
        else:
            cur_lines.append(line)
    if cur_lines:
        sections.append((cur_title, cur_lines))

    result: list[tuple[str, str]] = []
    for title, sec_lines in sections:
        body = "\n".join(sec_lines).strip()
        if not body:
            continue
        words = body.split()
        if len(words) <= max_words:
            result.append((title, body))
        else:
            for i in range(0, len(words), max_words):
                result.append((title, " ".join(words[i : i + max_words])))
    return result


def page_title(content: str, fallback: str) -> str:
    for ln in content.splitlines()[:20]:
        if ln.startswith("# "):
            return ln[2:].strip()
    return fallback


# ---------------------------------------------------------------------------
# SQLite FTS5 index
# ---------------------------------------------------------------------------

_SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS _meta (key TEXT PRIMARY KEY, value TEXT);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(
    path   UNINDEXED,
    title,
    section,
    url    UNINDEXED,
    body,
    tokenize = 'unicode61 remove_diacritics 2'
);
"""


def open_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def _meta_get(conn: sqlite3.Connection, key: str) -> str | None:
    r = conn.execute("SELECT value FROM _meta WHERE key=?", (key,)).fetchone()
    return r["value"] if r else None


def _meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO _meta VALUES (?,?)", (key, value))


def build_index(
    conn: sqlite3.Connection,
    cache_dir: Path,
    docs_path: str,
    base_url: str,
) -> None:
    print("[mkdocs-docs] Indexing docs …", file=sys.stderr)

    docs_dir = cache_dir / docs_path
    if not docs_dir.exists():
        docs_dir = cache_dir

    nav_map = parse_nav(cache_dir, base_url)
    nav_source = "mkdocs.yml" if nav_map else "file paths"
    print(f"[mkdocs-docs] URL source: {nav_source}", file=sys.stderr)

    conn.execute("DELETE FROM chunks")

    rows = []
    for md in sorted(docs_dir.rglob("*.md")):
        try:
            content = md.read_text(errors="ignore")
        except OSError:
            continue

        rel = str(md.relative_to(docs_dir))
        url = nav_map.get(rel) or _file_to_url(rel, base_url)
        ptitle = page_title(content, md.stem.replace("-", " ").replace("_", " ").title())

        for sec_title, body in chunk_markdown(content):
            rows.append((rel, ptitle, sec_title, url, body))

    conn.executemany("INSERT INTO chunks VALUES (?,?,?,?,?)", rows)
    _meta_set(conn, "git_head", _git_head(cache_dir) or "unknown")
    _meta_set(conn, "base_url", base_url)
    conn.commit()
    print(f"[mkdocs-docs] Indexed {len(rows)} chunks from {docs_dir}.", file=sys.stderr)


def ensure_index(
    conn: sqlite3.Connection,
    cache_dir: Path,
    docs_path: str,
    base_url: str,
    force: bool = False,
) -> None:
    current = _git_head(cache_dir) or "unknown"
    if force or _meta_get(conn, "git_head") != current or _meta_get(conn, "base_url") != base_url:
        build_index(conn, cache_dir, docs_path, base_url)


# ---------------------------------------------------------------------------
# Code block extraction
# ---------------------------------------------------------------------------

_CODE_BLOCK = re.compile(r"```(\w*)\n(.*?)```", re.DOTALL)
# Config-related languages worth extracting
_CONFIG_LANGS = {"yaml", "yml", "json", "toml", "hcl", "tf", "ini", "env", "properties", "xml", ""}


def extract_code_blocks(text: str) -> list[dict]:
    """Return all fenced code blocks as {lang, code} dicts."""
    return [
        {"lang": m.group(1).lower() or "text", "code": m.group(2).strip()}
        for m in _CODE_BLOCK.finditer(text)
        if m.group(2).strip()
    ]


def config_code_blocks(text: str) -> list[dict]:
    """Return only code blocks whose language looks like a config format."""
    return [b for b in extract_code_blocks(text) if b["lang"] in _CONFIG_LANGS]


def _strip_code_blocks(text: str) -> str:
    return _CODE_BLOCK.sub("", text).strip()


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

# Extra terms appended to queries in configure mode to surface config sections
_CONFIG_BOOST = "configuration options parameters settings example"


def _fts_query(raw: str) -> str:
    """Build a safe FTS5 query: quoted phrase + individual terms as OR."""
    clean = re.sub(r"[^\w\s]", " ", raw)
    terms = [t for t in clean.split() if len(t) > 1]
    if not terms:
        return '""'
    phrase = f'"{" ".join(terms)}"'
    singles = " OR ".join(f'"{t}"' for t in terms)
    return f"{phrase} OR {singles}"


def search(
    conn: sqlite3.Connection,
    query: str,
    max_results: int = 5,
    snippet_tokens: int = 60,
) -> list[dict]:
    """Standard search: returns short BM25 snippets."""
    fts_q = _fts_query(query)

    rows = conn.execute(
        """
        SELECT path, title, section, url,
               snippet(chunks, 4, '<b>', '</b>', '…', ?) AS excerpt,
               rank
        FROM   chunks
        WHERE  chunks MATCH ?
        ORDER  BY rank
        LIMIT  ?
        """,
        (snippet_tokens, fts_q, max_results * 4),
    ).fetchall()

    pages: dict[str, dict] = {}
    order: list[str] = []

    for row in rows:
        url = row["url"]
        if url not in pages:
            if len(pages) >= max_results:
                continue
            pages[url] = {"title": row["title"], "url": url, "excerpts": []}
            order.append(url)
        entry = pages[url]
        if len(entry["excerpts"]) < 3:
            sec = row["section"]
            header = f"**{sec}**\n" if sec and sec != row["title"] else ""
            entry["excerpts"].append(header + row["excerpt"])

    return [pages[u] for u in order]


def search_configure(
    conn: sqlite3.Connection,
    query: str,
    max_results: int = 5,
    body_chars: int = 2000,
) -> list[dict]:
    """
    Configure mode: augments the query with config terms, returns full section
    bodies and extracts code blocks (YAML / HCL / JSON / TOML / …).
    """
    augmented = f"{query} {_CONFIG_BOOST}"
    fts_q = _fts_query(augmented)

    rows = conn.execute(
        """
        SELECT path, title, section, url, body, rank
        FROM   chunks
        WHERE  chunks MATCH ?
        ORDER  BY rank
        LIMIT  ?
        """,
        (fts_q, max_results * 4),
    ).fetchall()

    pages: dict[str, dict] = {}
    order: list[str] = []

    for row in rows:
        url = row["url"]
        if url not in pages:
            if len(pages) >= max_results:
                continue
            pages[url] = {"title": row["title"], "url": url, "sections": []}
            order.append(url)
        entry = pages[url]
        if len(entry["sections"]) < 3:
            body = row["body"]
            blocks = config_code_blocks(body)
            description = _strip_code_blocks(body)[:body_chars]
            entry["sections"].append({
                "heading": row["section"],
                "description": description,
                "code_blocks": blocks,
            })

    return [pages[u] for u in order]


def list_pages(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT DISTINCT title, url FROM chunks ORDER BY title"
    ).fetchall()
    return [{"title": r["title"], "url": r["url"]} for r in rows]


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def print_text(results: list[dict]) -> None:
    if not results:
        print("No relevant documentation found.")
        return

    for i, r in enumerate(results, 1):
        print(f"### [{i}] {r['title']}")
        print(f"Source: {r['url']}\n")
        for excerpt in r["excerpts"]:
            print(excerpt)
            print()
        print("---\n")

    print("**Sources**")
    for i, r in enumerate(results, 1):
        print(f"[{i}] [{r['title']}]({r['url']})")


def print_configure(results: list[dict]) -> None:
    if not results:
        print("No configuration documentation found.")
        return

    for i, r in enumerate(results, 1):
        print(f"### [{i}] {r['title']}")
        print(f"Source: {r['url']}\n")
        for sec in r["sections"]:
            if sec["heading"]:
                print(f"#### {sec['heading']}\n")
            if sec["description"]:
                print(sec["description"])
                print()
            for block in sec["code_blocks"]:
                lang = block["lang"] or "text"
                print(f"```{lang}")
                print(block["code"])
                print("```\n")
        print("---\n")

    print("**Sources**")
    for i, r in enumerate(results, 1):
        print(f"[{i}] [{r['title']}]({r['url']})")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    p = argparse.ArgumentParser(
        description="MkDocs local RAG search — SQLite FTS5 + mkdocs.yml",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--repo-url", required=True, help="Git clone URL")
    p.add_argument("--repo-name", required=True, help="Cache directory name")
    p.add_argument("--base-url", required=True, help="Published docs root URL")
    p.add_argument("--docs-path", default="docs", help="Subdir inside repo with .md files")
    p.add_argument("--update-interval", type=int, default=24, help="Hours between git pulls")
    p.add_argument("--query", default="", help="Search query")
    p.add_argument("--max-results", type=int, default=5)
    p.add_argument(
        "--mode",
        choices=["search", "configure"],
        default="search",
        help=(
            "search: short BM25 snippets for Q&A. "
            "configure: full section bodies + code blocks for config generation."
        ),
    )
    p.add_argument("--reindex", action="store_true", help="Force full index rebuild")
    p.add_argument("--list-pages", action="store_true", help="List all indexed pages")
    p.add_argument("--json", action="store_true", help="JSON output")
    args = p.parse_args()

    cache_dir = CACHE_BASE / args.repo_name
    updated = ensure_cache(args.repo_url, cache_dir, args.update_interval)

    db_path = cache_dir / ".search.db"
    conn = open_db(db_path)
    ensure_index(conn, cache_dir, args.docs_path, args.base_url, force=args.reindex or updated)

    if args.list_pages:
        pages = list_pages(conn)
        if args.json:
            print(json.dumps(pages, ensure_ascii=False))
        else:
            print(f"Indexed pages ({len(pages)}):")
            for pg in pages:
                print(f"  {pg['title']:40s}  {pg['url']}")
        return

    if not args.query:
        p.error("--query is required unless --list-pages is used")

    if args.mode == "configure":
        results = search_configure(conn, args.query, args.max_results)
        if args.json:
            print(json.dumps(results, indent=2, ensure_ascii=False))
        else:
            print_configure(results)
    else:
        results = search(conn, args.query, args.max_results)
        if args.json:
            print(json.dumps(results, indent=2, ensure_ascii=False))
        else:
            print_text(results)


if __name__ == "__main__":
    main()
