#!/usr/bin/env python3
"""
MkDocs local RAG search — agents-skills.

Clones a MkDocs documentation repo to a local cache, keeps it updated,
and returns relevant excerpts to minimise token usage in agent contexts.
"""

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

CACHE_BASE = Path.home() / ".cache" / "agents-skills" / "docs"


# ---------------------------------------------------------------------------
# Cache management
# ---------------------------------------------------------------------------

def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def _remote_url(cache_dir: Path) -> str | None:
    r = _run(["git", "-C", str(cache_dir), "remote", "get-url", "origin"])
    return r.stdout.strip() if r.returncode == 0 else None


def _needs_update(cache_dir: Path, interval_hours: int) -> bool:
    marker = cache_dir / ".last-update"
    if not marker.exists():
        return True
    age = datetime.now() - datetime.fromtimestamp(marker.stat().st_mtime)
    return age > timedelta(hours=interval_hours)


def ensure_cache(repo_url: str, cache_dir: Path, interval_hours: int) -> None:
    """Clone if missing, pull if stale. Silently continues when offline."""
    cache_dir.mkdir(parents=True, exist_ok=True)

    git_dir = cache_dir / ".git"

    # Re-clone if the cached remote URL no longer matches
    if git_dir.exists() and _remote_url(cache_dir) != repo_url:
        print(
            f"[mkdocs-docs] Remote URL changed — re-cloning into {cache_dir}",
            file=sys.stderr,
        )
        import shutil
        shutil.rmtree(cache_dir)
        cache_dir.mkdir(parents=True)
        git_dir = cache_dir / ".git"  # reset

    if not git_dir.exists():
        print(f"[mkdocs-docs] Cloning {repo_url} ...", file=sys.stderr)
        r = _run(["git", "clone", "--depth=1", "--quiet", repo_url, str(cache_dir)])
        if r.returncode != 0:
            print(f"[mkdocs-docs] Clone failed: {r.stderr.strip()}", file=sys.stderr)
            return
        (cache_dir / ".last-update").touch()
        return

    if _needs_update(cache_dir, interval_hours):
        print("[mkdocs-docs] Refreshing docs cache ...", file=sys.stderr)
        # fetch + hard reset is more reliable than pull on shallow clones
        r1 = _run(["git", "-C", str(cache_dir), "fetch", "--depth=1", "--quiet", "origin"])
        r2 = _run(["git", "-C", str(cache_dir), "reset", "--hard", "origin/HEAD"])
        if r1.returncode == 0 and r2.returncode == 0:
            (cache_dir / ".last-update").touch()
        else:
            print(
                "[mkdocs-docs] Could not update (offline?). Using cached version.",
                file=sys.stderr,
            )


# ---------------------------------------------------------------------------
# Search / extraction
# ---------------------------------------------------------------------------

_HEADING_RE = re.compile(r"^#{1,4}\s+.+")


def _extract_sections(content: str, terms: list[str], max_lines: int = 25) -> list[str]:
    """
    Split content at markdown headings and return the top-scored sections
    that contain at least one query term.
    """
    lines = content.splitlines()

    # Collect section boundaries (line indices where headings appear)
    boundaries = [i for i, ln in enumerate(lines) if _HEADING_RE.match(ln)]
    boundaries.append(len(lines))
    if not boundaries or boundaries[0] != 0:
        boundaries.insert(0, 0)

    scored: list[tuple[int, str]] = []
    for i in range(len(boundaries) - 1):
        start, end = boundaries[i], boundaries[i + 1]
        block_lines = lines[start:end]
        block = "\n".join(block_lines)
        block_lower = block.lower()
        score = sum(block_lower.count(t) for t in terms)
        if score > 0:
            excerpt = "\n".join(block_lines[:max_lines])
            if len(block_lines) > max_lines:
                excerpt += "\n…"
            scored.append((score, excerpt))

    scored.sort(reverse=True)
    return [text for _, text in scored[:3]]


def _path_to_url(rel: Path, base_url: str) -> str:
    """Convert a docs-relative path to a published URL."""
    parts = list(rel.parts)
    if parts[-1] == "index.md":
        url_parts = parts[:-1]
    else:
        url_parts = parts[:-1] + [parts[-1][:-3]]  # strip .md
    url_path = "/".join(url_parts)
    base = base_url.rstrip("/")
    return f"{base}/{url_path}/" if url_path else f"{base}/"


def search(
    cache_dir: Path,
    docs_path: str,
    query: str,
    base_url: str,
    max_results: int = 5,
) -> list[dict]:
    docs_dir = cache_dir / docs_path
    if not docs_dir.exists():
        docs_dir = cache_dir

    terms = [t.lower() for t in re.findall(r"\w+", query) if len(t) > 2]
    if not terms:
        return []

    # Score every markdown file
    scored: list[tuple[int, Path]] = []
    for md in docs_dir.rglob("*.md"):
        try:
            content = md.read_text(errors="ignore")
        except OSError:
            continue
        lower = content.lower()
        score = sum(lower.count(t) for t in terms)
        if score > 0:
            scored.append((score, md))

    scored.sort(reverse=True)

    results: list[dict] = []
    for _, md in scored[: max_results * 2]:
        if len(results) >= max_results:
            break
        try:
            content = md.read_text(errors="ignore")
        except OSError:
            continue

        excerpts = _extract_sections(content, terms)
        if not excerpts:
            continue

        # Page title: first H1 or prettified filename
        title = md.stem.replace("-", " ").replace("_", " ").title()
        for ln in content.splitlines()[:15]:
            if ln.startswith("# "):
                title = ln[2:].strip()
                break

        url = _path_to_url(md.relative_to(docs_dir), base_url)
        results.append({"title": title, "url": url, "excerpts": excerpts})

    return results


def list_sections(cache_dir: Path, docs_path: str) -> list[str]:
    docs_dir = cache_dir / docs_path
    if not docs_dir.exists():
        docs_dir = cache_dir
    return sorted(f.stem for f in docs_dir.glob("*.md"))


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def print_text(results: list[dict]) -> None:
    if not results:
        print("No relevant documentation found for this query.")
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


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(
        description="MkDocs local RAG search",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--repo-url", required=True, help="Git URL of the MkDocs repo")
    p.add_argument("--repo-name", required=True, help="Short name for the cache directory")
    p.add_argument("--base-url", required=True, help="Base URL of the published docs site")
    p.add_argument("--docs-path", default="docs", help="Path inside the repo for .md files")
    p.add_argument("--update-interval", type=int, default=24, help="Hours between git pull checks")
    p.add_argument("--query", default="", help="Search query")
    p.add_argument("--max-results", type=int, default=5)
    p.add_argument("--json", action="store_true", help="Output JSON")
    p.add_argument(
        "--list-sections",
        action="store_true",
        help="List top-level doc sections instead of searching",
    )
    args = p.parse_args()

    cache_dir = CACHE_BASE / args.repo_name
    ensure_cache(args.repo_url, cache_dir, args.update_interval)

    if args.list_sections:
        sections = list_sections(cache_dir, args.docs_path)
        if args.json:
            print(json.dumps(sections))
        else:
            print("Available documentation sections:")
            for s in sections:
                print(f"  - {s}")
        return

    if not args.query:
        p.error("--query is required unless --list-sections is set")

    results = search(cache_dir, args.docs_path, args.query, args.base_url, args.max_results)

    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
    else:
        print_text(results)


if __name__ == "__main__":
    main()
