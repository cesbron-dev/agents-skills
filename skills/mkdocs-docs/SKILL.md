---
name: mkdocs-docs
description: |
  Search and query a MkDocs documentation site using a local git cache.
  Use when: the user asks questions about a project that has MkDocs documentation;
  the user wants to find guides, API references, or configuration docs;
  the user types "how do I …" or "what is …" about a documented project.
  Clones the docs repo once, checks for updates periodically, and returns only
  relevant excerpts — not the full docs — to keep token usage low.
  Always ends responses with a Sources section linking to the online docs.
---

# MkDocs Documentation Search

Token-efficient documentation assistant. The local git clone is used as a RAG source:
only the most relevant markdown excerpts are loaded into context, not the full site.

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

| Field | Required | Description |
|---|---|---|
| `repo_url` | yes | Git clone URL of the MkDocs repository |
| `base_url` | yes | Root URL of the published documentation site |
| `repo_name` | yes | Short slug used as the cache directory name |
| `docs_path` | no | Subdirectory inside the repo that contains `.md` files (default: `docs`) |
| `update_interval_hours` | no | How often to `git pull` the cache (default: `24`) |

## Workflow

### Step 1 — Load config

Read `.claude/mkdocs-docs.json`. If it does not exist, tell the user and stop.
Extract all fields, applying defaults for optional ones.

### Step 2 — Search the local cache

Run the search script with the user's question as the query:

```bash
python3 "$(dirname "$0")/../bin/doc-search.py" \
  --repo-url   "<repo_url>" \
  --repo-name  "<repo_name>" \
  --base-url   "<base_url>" \
  --docs-path  "<docs_path>" \
  --update-interval <update_interval_hours> \
  --query      "<user question or key terms>" \
  --max-results 5
```

The script will:
- Clone the repo on first run (`~/.cache/agents-skills/docs/<repo_name>/`)
- Automatically pull if the cache is older than `update_interval_hours`
- Score every `.md` file by term frequency and extract the top matching sections
- Print excerpts with source URLs

### Step 3 — If no results, list sections

If the search returns "No relevant documentation found", run with `--list-sections`
to discover what topics the docs cover:

```bash
python3 "$(dirname "$0")/../bin/doc-search.py" \
  --repo-url  "<repo_url>" \
  --repo-name "<repo_name>" \
  --base-url  "<base_url>" \
  --docs-path "<docs_path>" \
  --list-sections
```

Then re-run the search with adjusted keywords, or tell the user which sections exist.

### Step 4 — Answer from excerpts only

- Use **only** the returned excerpts to answer; do not guess or hallucinate.
- Keep the answer concise — quote the relevant part of the excerpt.
- If the question spans multiple pages, synthesise across sources.
- If the answer is genuinely not in the docs, say so explicitly.

### Step 5 — Always append a Sources section

End every response with:

```
**Sources**
- [Page title](https://docs.example.com/section/page/) — one-line summary of what this page covers
```

List every page referenced, even if only partially used.

## Cache details

| Item | Value |
|---|---|
| Cache root | `~/.cache/agents-skills/docs/` |
| Clone flags | `--depth=1` (shallow, saves disk) |
| Update method | `git fetch --depth=1` + `git reset --hard origin/HEAD` |
| Offline behaviour | Silently continues with existing cache; prints a warning to stderr |
| URL mismatch | Re-clones if `repo_url` differs from the cached remote |
