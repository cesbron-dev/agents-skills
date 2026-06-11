---
name: mkdocs-docs
description: |
  Search and query a MkDocs documentation site using a local git cache and SQLite FTS5 index.
  Use when: the user asks questions about a project that has MkDocs documentation;
  the user wants to find guides, API references, or configuration docs;
  the user types "how do I …" or "what is …" about a documented project.
  Clones the docs repo once, indexes content with BM25 (SQLite FTS5), checks for updates
  periodically, and returns only relevant excerpts — not the full docs — to keep token usage low.
  Always ends responses with a Sources section linking to the online docs.
---

# MkDocs Documentation Search

Token-efficient documentation assistant using a local SQLite FTS5 index built from a git clone.
Only BM25-ranked excerpts are loaded into context; full source URLs are always cited.

## How it works

```
git clone (shallow)  →  chunk .md files by heading
         ↓
  SQLite FTS5 index  ←  mkdocs.yml nav (accurate URLs)
         ↓
user query (any lang)  →  translate to docs_lang  →  BM25 search
         ↓
  top excerpts + source URLs  →  answer in user's language
```

The index is rebuilt automatically when `git HEAD` changes. The git clone is refreshed
on a configurable interval (default 24 h) using `fetch --depth=1 + reset --hard`.

## Prerequisites — project configuration

Create `.claude/mkdocs-docs.json` in the project root:

```json
{
  "repo_url": "https://github.com/org/project-docs.git",
  "base_url": "https://docs.example.com",
  "repo_name": "project-docs",
  "docs_path": "docs",
  "update_interval_hours": 24
}
```

| Field | Required | Default | Description |
|---|---|---|---|
| `repo_url` | yes | — | Git clone URL of the MkDocs repo |
| `base_url` | yes | — | Root URL of the published docs site |
| `repo_name` | yes | — | Short slug → cache dir name |
| `docs_path` | no | `docs` | Subdir inside the repo containing `.md` files |
| `docs_lang` | no | `en` | Language of the documentation (`en`, `fr`, …) |
| `update_interval_hours` | no | `24` | Hours between `git pull` checks |

**Optional:** install `pyyaml` so the skill reads `mkdocs.yml` nav for exact page URLs.
Without it, URLs are derived from file paths (usually correct but may differ for custom nav).
```bash
pip install pyyaml
```

## Workflow

### Step 1 — Load config

Read `.claude/mkdocs-docs.json`. If it does not exist, tell the user and stop.
Apply defaults for optional fields.

### Step 2 — Translate query to the docs language

FTS5 is a keyword index: a French query will not match English documentation.
Before calling the search script, translate the user's question into `docs_lang`.

Rules:
- Detect the user's query language from the conversation.
- If it differs from `docs_lang`, silently translate the query yourself — do not tell the user.
- Keep technical terms, identifiers, and code as-is (they are language-neutral).
- Prefer a concise keyword-style rephrasing over a literal translation, e.g.
  `"comment configurer l'authentification ?"` → `"configure authentication"`.
- If the user's language matches `docs_lang`, use the query as-is.

### Step 3 — Search the index

Run the search script with the user's question:

```bash
python3 ~/.claude/skills/mkdocs-docs/bin/doc-search.py \
  --repo-url          "<repo_url>" \
  --repo-name         "<repo_name>" \
  --base-url          "<base_url>" \
  --docs-path         "<docs_path>" \
  --update-interval   <update_interval_hours> \
  --query             "<user question or key terms>" \
  --max-results       5
```

The script handles everything automatically:
- Clones the repo if `~/.cache/agents-skills/docs/<repo_name>/` is missing
- Pulls and rebuilds the SQLite index when `git HEAD` changes
- Parses `mkdocs.yml` nav to produce accurate URLs (requires `pyyaml`)
- Returns BM25-ranked excerpts with `snippet()` highlighting

### Step 4 — If no results, discover available pages

```bash
python3 ~/.claude/skills/mkdocs-docs/bin/doc-search.py \
  --repo-url  "<repo_url>"  --repo-name "<repo_name>" \
  --base-url  "<base_url>"  --docs-path "<docs_path>" \
  --list-pages
```

Use the page list to suggest where to look or rephrase the query.

### Step 5 — Force index rebuild (when needed)

Add `--reindex` to force a full rebuild, e.g. after manually pulling new docs:

```bash
python3 ~/.claude/skills/mkdocs-docs/bin/doc-search.py ... --reindex
```

### Step 6 — Answer from excerpts only

- Use **only** the returned excerpts; do not hallucinate missing content.
- Quote the excerpt directly when relevant.
- Synthesise across multiple pages if the question spans topics.
- If the answer is not in the docs, say so and suggest the most related page.
- **Always reply in the user's language**, regardless of the docs language.

### Step 7 — Always append Sources

End every response with:

```
**Sources**
- [Page title](https://docs.example.com/section/page/) — what this page covers
```

## Index details

| Item | Value |
|---|---|
| Index location | `~/.cache/agents-skills/docs/<repo_name>/.search.db` |
| FTS engine | SQLite FTS5, BM25 ranking, unicode61 tokenizer |
| Chunk size | ≤ 350 words per section |
| Snippet | `snippet()` with `<b>` highlighting, ~60 tokens |
| Invalidation | Triggered by `git HEAD` change or `--reindex` flag |
| Clone mode | `--depth=1` shallow clone |
| Offline | Continues with existing cache; prints warning to stderr |
