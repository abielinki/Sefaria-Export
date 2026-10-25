"""Sefaria MCP server.

Gives Claude read access to the Sefaria library: texts, commentaries,
search, cross references, book metadata, and the daily learning calendar.
No API key needed. Uses Sefaria's public API at https://www.sefaria.org.

Run:  python sefaria_mcp.py
"""

import html
import json
import os
import re
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import httpx
try:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP
except ModuleNotFoundError:  # mcp 2.x renamed it
    from mcp.server.mcpserver import MCPServer as FastMCP

BASE = "https://www.sefaria.org"
TIMEOUT = 30.0
MAX_CHARS = 12000  # cap per text response so one call doesn't flood the context

mcp = FastMCP("sefaria")

# ---- local library mode (Sefaria-Export) ----
BUCKET = "https://storage.googleapis.com/sefaria-export"
UPSTREAM_BOOKS = "https://raw.githubusercontent.com/Sefaria/Sefaria-Export/master/books.json"
CACHE_DIR = Path(os.environ.get("SEFARIA_CACHE", Path.home() / ".cache" / "sefaria-mcp"))
# Optional: a folder you downloaded with gcloud, same layout as the bucket (has a json/ subfolder).
EXPORT_DIR = os.environ.get("SEFARIA_EXPORT_DIR")
# Optional: path to books.json. Defaults to the copy in this repo, else downloads it once.
BOOKS_JSON = os.environ.get("SEFARIA_BOOKS_JSON")

_client = httpx.Client(
    base_url=BASE,
    timeout=TIMEOUT,
    headers={"User-Agent": "sefaria-mcp/0.1 (personal research)"},
    follow_redirects=True,
)

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"[ \t\u200e\u200f]+")


# ---------- helpers ----------

def _clean(s: str) -> str:
    """Strip HTML tags and entities, tidy whitespace."""
    s = TAG_RE.sub("", s or "")
    s = html.unescape(s)
    s = WS_RE.sub(" ", s)
    return s.strip()


def _get(path: str, params: Optional[dict] = None):
    r = _client.get(path, params=params)
    r.raise_for_status()
    return r.json()


def _post(path: str, body: dict):
    r = _client.post(path, json=body)
    r.raise_for_status()
    return r.json()


def _flatten(text, prefix: str = "") -> list[str]:
    """Flatten a nested text array into numbered lines like '3. text'."""
    out: list[str] = []
    if isinstance(text, str):
        c = _clean(text)
        if c:
            out.append(f"{prefix}{c}" if not prefix else f"{prefix} {c}")
    elif isinstance(text, list):
        for i, item in enumerate(text, start=1):
            label = f"{prefix}{i}" if not prefix else f"{prefix}.{i}"
            if isinstance(item, str):
                c = _clean(item)
                if c:
                    out.append(f"[{label}] {c}")
            elif isinstance(item, list):
                out.extend(_flatten(item, label))
    return out


def _truncate(s: str, limit: int = MAX_CHARS) -> str:
    if len(s) <= limit:
        return s
    return s[:limit] + f"\n\n[truncated at {limit} characters. Ask for a narrower ref or use the next ref.]"


def _err(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        return f"Sefaria returned HTTP {e.response.status_code} for {e.request.url}"
    return f"Request failed: {e}"


# ---------- tools ----------

@mcp.tool()
def get_text(ref: str, language: str = "both", version_title: Optional[str] = None) -> str:
    """Fetch the text at a Sefaria ref.

    ref: a Sefaria reference such as "Berakhot 2a", "Genesis 1:1-3",
         "Rashi on Genesis 1:1", "Mishneh Torah, Shabbat 1:1",
         "Aruch HaShulchan, Orach Chayim 1:1". Section-level refs return the
         whole section. Use find_ref first if you are unsure of the spelling.
    language: "hebrew", "english", or "both" (default). Hebrew is the source
         text for most works. "english" returns the primary translation.
    version_title: optional exact versionTitle to request a specific edition
         (use list_versions to see what exists). Applies to the language chosen.
    """
    try:
        wanted = []
        if language in ("hebrew", "both"):
            wanted.append("hebrew")
        if language in ("english", "both"):
            wanted.append("english")
        if not wanted:
            return "language must be 'hebrew', 'english', or 'both'."

        params = []
        for lang in wanted:
            if version_title and len(wanted) == 1:
                params.append(("version", f"{lang}|{version_title}"))
            else:
                params.append(("version", lang))
        params.append(("return_format", "text_only"))

        data = _get(f"/api/v3/texts/{quote(ref, safe='')}", params)
        header = f"{data.get('ref', ref)}  ({data.get('heRef', '')})"
        parts = [header]
        for v in data.get("versions", []):
            lines = _flatten(v.get("text", []))
            if not lines:
                continue
            parts.append(f"\n--- {v.get('language')} | {v.get('versionTitle')} ---")
            parts.append("\n".join(lines))
        warnings = data.get("warnings") or []
        if len(parts) == 1:
            parts.append("No text returned for that ref/language.")
        if warnings:
            parts.append(f"\nWarnings: {warnings}")
        nxt, prv = data.get("next"), data.get("prev")
        if nxt or prv:
            parts.append(f"\nprev: {prv} | next: {nxt}")
        return _truncate("\n".join(parts))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool()
def list_versions(ref: str) -> str:
    """List the available editions (versions) of a text, by language and title.
    Use the exact versionTitle with get_text(version_title=...)."""
    try:
        data = _get(f"/api/v3/texts/{quote(ref, safe='')}", {"version": "primary", "return_format": "text_only"})
        rows = []
        for v in data.get("available_versions", []):
            rows.append(
                f"{v.get('languageFamilyName', v.get('language'))} | {v.get('versionTitle')} | license: {v.get('license') or 'n/a'}"
            )
        return f"Versions for {data.get('ref', ref)}:\n" + "\n".join(rows) if rows else "No versions found."
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool()
def search(
    query: str,
    scope: str = "text",
    categories: Optional[list[str]] = None,
    exact: bool = False,
    size: int = 10,
) -> str:
    """Full-text search across the Sefaria library (Hebrew or English).

    query: words or phrase. Hebrew works. Quote-free; slop allows nearby words.
    scope: "text" for library texts (default) or "sheet" for user source sheets.
    categories: optional path prefixes to narrow results, for example
         ["Talmud/Bavli"], ["Halakhah/Shulchan Arukh"], ["Tanakh/Torah"],
         ["Chasidut"]. Matches the start of the book's category path.
    exact: True searches the unlemmatized 'exact' field (spelling-sensitive).
    size: number of hits (max 30).
    Returns refs with highlighted snippets. Follow up with get_text on a ref.
    """
    try:
        size = max(1, min(size, 30))
        body = {
            "query": query,
            "type": scope if scope in ("text", "sheet") else "text",
            "field": "exact" if exact else "naive_lemmatizer",
            "size": size,
            "slop": 10,
            "sort_method": "score",
            "sort_fields": ["pagesheetrank"],
            "sort_reverse": False,
            "sort_score_missing": 0.04,
            "source_proj": True,
            "filters": categories or [],
            "filter_fields": [],
            "aggs": ["path"] if scope == "text" else [],
        }
        data = _post("/api/search-wrapper", body)
        hits = data.get("hits", {})
        total = hits.get("total")
        if isinstance(total, dict):
            total = total.get("value")
        out = [f"{total} total hits, showing up to {size}"]
        for h in hits.get("hits", []):
            src = h.get("_source", {})
            hl = h.get("highlight", {})
            snippet = ""
            for vals in hl.values():
                if vals:
                    snippet = _clean(vals[0])
                    break
            if not snippet:
                snippet = _clean(src.get("exact", ""))[:300]
            ref = src.get("ref") or src.get("title") or h.get("_id")
            ver = src.get("version", "")
            lang = src.get("lang", "")
            out.append(f"- {ref} [{lang}{' | ' + ver if ver else ''}]\n    {snippet}")
        return _truncate("\n".join(out))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool()
def related(ref: str, max_per_group: int = 12) -> str:
    """Show what connects to a ref: commentaries, Talmud and Midrash sources,
    halakhic works, and other cross references, grouped by work. Also lists
    topics. Use this to find "who comments on this" or "where else is this
    verse/sugya discussed", then call get_text on the refs you want."""
    try:
        data = _get(f"/api/related/{quote(ref, safe='')}")
        links = data.get("links", [])
        groups: dict[str, list[dict]] = {}
        for ln in links:
            cat = ln.get("category", "Other")
            title = (ln.get("collectiveTitle") or {}).get("en") or ln.get("index_title", "")
            groups.setdefault(f"{cat} / {title}", []).append(ln)
        out = [f"{len(links)} links for {ref}"]
        # commentary first, then the rest alphabetically
        for key in sorted(groups, key=lambda k: (not k.startswith("Commentary"), k)):
            items = groups[key]
            refs = [i.get("sourceRef") or i.get("ref") for i in items[:max_per_group]]
            more = f" (+{len(items) - max_per_group} more)" if len(items) > max_per_group else ""
            out.append(f"{key}: " + "; ".join(r for r in refs if r) + more)
        topics = data.get("topics", [])
        if topics:
            names = [(t.get("title") or {}).get("en") for t in topics[:15]]
            out.append("Topics: " + ", ".join(n for n in names if n))
        sheets = data.get("sheets", [])
        if sheets:
            out.append(f"{len(sheets)} public source sheets mention this ref.")
        return _truncate("\n".join(out))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool()
def find_ref(query: str) -> str:
    """Resolve a loose title or citation into a valid Sefaria ref, and suggest
    completions. Example: "bava metzia 2" or "sa orach chaim 1". Use this when
    get_text says a ref is invalid."""
    try:
        data = _get(f"/api/name/{quote(query, safe='')}", {"limit": 8})
        out = []
        if data.get("is_ref"):
            out.append(f"Valid ref: {data.get('ref')} (book: {data.get('book')}, type: {data.get('type')})")
        else:
            out.append("Not a complete ref.")
        comps = data.get("completion_objects") or []
        if comps:
            out.append("Suggestions: " + "; ".join(c.get("title", "") for c in comps[:8]))
        elif data.get("completions"):
            out.append("Suggestions: " + "; ".join(data["completions"][:8]))
        return "\n".join(out)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool()
def book_info(title: str) -> str:
    """Metadata for a book: Hebrew title, categories, structure (chapter/daf
    levels), author and composition dates, and whether it is a commentary."""
    try:
        data = _get(f"/api/v2/index/{quote(title, safe='')}")
        keys = [
            "title", "heTitle", "categories", "sectionNames", "authors",
            "compDate", "compPlace", "era", "base_text_titles",
            "collective_title", "heCollectiveTitle", "enDesc", "enShortDesc",
        ]
        out = []
        for k in keys:
            v = data.get(k)
            if v not in (None, "", [], {}):
                out.append(f"{k}: {v}")
        return "\n".join(out) or "No metadata found."
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool()
def learning_calendar(
    diaspora: bool = True,
    year: Optional[int] = None,
    month: Optional[int] = None,
    day: Optional[int] = None,
) -> str:
    """Today's learning schedule from Sefaria: parsha, Daf Yomi, Mishnah Yomit,
    Daily Rambam, Tanya, Aruch HaShulchan Yomi and others that Sefaria tracks.
    Leave date fields empty for today. diaspora=False for Israel."""
    try:
        params: dict = {"diaspora": 1 if diaspora else 0}
        if year and month and day:
            params.update({"year": year, "month": month, "day": day})
        data = _get("/api/calendars", params)
        items = data.get("calendar_items", [])
        out = [f"Calendar for {data.get('date', 'today')}"]
        for it in items:
            t = (it.get("title") or {}).get("en", "")
            dv = (it.get("displayValue") or {}).get("en", "")
            ref = it.get("ref", "")
            line = f"- {t}: {dv}"
            if ref:
                line += f"  -> {ref}"
            out.append(line)
        return "\n".join(out)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool()
def lexicon(word: str) -> str:
    """Look up a Hebrew or Aramaic word in Sefaria's dictionaries (BDB, Jastrow,
    Klein and others). Pass the word in Hebrew letters."""
    try:
        data = _get(f"/api/words/{quote(word, safe='')}")
        if not isinstance(data, list) or not data:
            return "No entries found."
        out = []
        for e in data[:6]:
            head = e.get("headword", "")
            lex = e.get("parent_lexicon", "")
            senses = e.get("content", {}).get("senses", [])
            defs = []
            for s in senses[:4]:
                d = s.get("definition")
                if isinstance(d, str):
                    defs.append(_clean(d))
            out.append(f"{head} [{lex}]: " + " | ".join(defs) if defs else f"{head} [{lex}]")
        return _truncate("\n".join(out))
    except Exception as e:  # noqa: BLE001
        return _err(e)


# ---------- local library (whole-book search from the Sefaria-Export dataset) ----------

_books_cache: Optional[list] = None
NIKUD_RE = re.compile("[\u0591-\u05bd\u05bf\u05c1\u05c2\u05c4\u05c5\u05c7]")


def _books() -> list:
    """Load the books.json index (local file, else cached download)."""
    global _books_cache
    if _books_cache is not None:
        return _books_cache
    candidates = []
    if BOOKS_JSON:
        candidates.append(Path(BOOKS_JSON))
    candidates.append(Path(__file__).resolve().parent.parent / "books.json")  # inside the fork
    candidates.append(CACHE_DIR / "books.json")
    for c in candidates:
        if c.is_file():
            _books_cache = json.loads(c.read_text(encoding="utf-8"))["books"]
            return _books_cache
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    r = httpx.get(UPSTREAM_BOOKS, timeout=120, follow_redirects=True)
    r.raise_for_status()
    (CACHE_DIR / "books.json").write_bytes(r.content)
    _books_cache = r.json()["books"]
    return _books_cache


def _pick_book(title: str, language: str, version_title: Optional[str]):
    lang = language.capitalize()
    want = version_title or "merged"
    pool = [b for b in _books() if b["title"].lower() == title.lower() and b["language"] == lang]
    for b in pool:
        if b["versionTitle"] == want:
            return b
    return pool[0] if pool and version_title is None else None


def _book_path(book: dict) -> str:
    """Path of the json file inside the export layout."""
    url = book["json_url"]
    return url.split("/sefaria-export/", 1)[1]


def _load_book(book: dict):
    """Return parsed book JSON from the local export, the cache, or the bucket."""
    rel = _book_path(book)
    from urllib.parse import unquote
    rel_local = unquote(rel)
    locations = []
    if EXPORT_DIR:
        locations.append(Path(EXPORT_DIR) / rel_local)
    locations.append(CACHE_DIR / "export" / rel_local)
    for loc in locations:
        if loc.is_file():
            return json.loads(loc.read_text(encoding="utf-8"))
    dest = CACHE_DIR / "export" / rel_local
    dest.parent.mkdir(parents=True, exist_ok=True)
    with httpx.stream("GET", book["json_url"], timeout=180, follow_redirects=True) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_bytes():
                f.write(chunk)
    return json.loads(dest.read_text(encoding="utf-8"))


def _walk(node, path=()):
    if isinstance(node, str):
        if node.strip():
            yield path, node
    elif isinstance(node, list):
        for i, item in enumerate(node):
            yield from _walk(item, path + (i,))
    elif isinstance(node, dict):
        for k, v in node.items():
            yield from _walk(v, path + (k,))


def _label(book: dict, path: tuple) -> str:
    """Best-effort Sefaria-style ref from a position in the text array."""
    title = book["title"]
    cats = book.get("categories", [])
    if "Bavli" in cats and path and all(isinstance(p, int) for p in path):
        # Sefaria's Talmud arrays start at 1a, so index 2 is daf 2a
        daf = path[0] // 2 + 1
        side = "ab"[path[0] % 2]
        rest = ":".join(str(p + 1) for p in path[1:])
        return f"{title} {daf}{side}" + (f":{rest}" if rest else "")
    names = [p for p in path if isinstance(p, str) and p]
    nums = [str(p + 1) for p in path if isinstance(p, int)]
    head = title + (", " + ", ".join(names) if names else "")
    return f"{head} {':'.join(nums)}".strip()


def _norm(s: str, keep_nikud: bool) -> str:
    s = _clean(s)
    if not keep_nikud:
        s = s.replace("\u05be", " ").replace("\u05c3", "")
        s = NIKUD_RE.sub("", s)
    return s


@mcp.tool()
def library_find(query: str, category: Optional[str] = None, language: Optional[str] = None) -> str:
    """Find books in the Sefaria-Export dataset by title words, to use with grep_book.

    query: words from the title, such as "arukh" or "mishneh torah".
    category: optional category word to narrow, such as "Halakhah" or "Talmud".
    language: optional, "Hebrew" or "English".
    Lists title, category path and the languages available."""
    try:
        q = query.lower()
        found: dict[str, dict] = {}
        for b in _books():
            if q not in b["title"].lower():
                continue
            if category and category.lower() not in " / ".join(b["categories"]).lower():
                continue
            if language and b["language"].lower() != language.lower():
                continue
            e = found.setdefault(b["title"], {"cats": " / ".join(b["categories"]), "langs": set()})
            e["langs"].add(b["language"])
        if not found:
            return "No matching books."
        rows = [f"{t} [{v['cats']}] ({', '.join(sorted(v['langs']))})" for t, v in sorted(found.items())[:30]]
        extra = f"\n(+{len(found) - 30} more, narrow the query)" if len(found) > 30 else ""
        return "\n".join(rows) + extra
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool()
def grep_book(
    titles: list[str],
    query: str,
    language: str = "Hebrew",
    regex: bool = False,
    keep_nikud: bool = False,
    version_title: Optional[str] = None,
    max_hits: int = 20,
) -> str:
    """Search inside one or more whole books for an exact word or phrase.

    Unlike search, this is a literal string match over the full text, and it
    ignores nikud and cantillation by default, so a plain Hebrew phrase finds
    vocalized text. Good for tracking an exact expression through a work.
    The first use of a book downloads it from the Sefaria-Export bucket and
    caches it, after that it works offline.

    titles: exact book titles, up to 15, such as ["Bava Metzia"] or
         ["Arukh HaShulchan"]. Use library_find if unsure of the title.
    query: Hebrew or English text. Quote-free. With regex=True, a Python regex.
    language: "Hebrew" (default) or "English".
    version_title: specific edition, default is the merged best-text version.
    Returns approximate refs. Confirm with get_text before quoting."""
    try:
        titles = titles[:15]
        max_hits = max(1, min(max_hits, 60))
        pat = re.compile(query if regex else re.escape(_norm(query, keep_nikud)))
        out, shown = [], 0
        for t in titles:
            book = _pick_book(t, language, version_title)
            if not book:
                out.append(f"## {t}: no {language} edition found (check library_find).")
                continue
            data = _load_book(book)
            total = 0
            hits = []
            for path, seg in _walk(data.get("text", data)):
                n = _norm(seg, keep_nikud)
                m = pat.search(n)
                if not m:
                    continue
                total += 1
                if shown + len(hits) < max_hits:
                    a, b = max(0, m.start() - 120), min(len(n), m.end() + 160)
                    hits.append(f"- {_label(book, path)}\n    ...{n[a:b]}...")
            shown += len(hits)
            out.append(f"## {t} ({book['language']}, {book['versionTitle']}): {total} matching segments")
            out.extend(hits)
        return _truncate("\n".join(out))
    except re.error as e:
        return f"Bad regex: {e}"
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool()
def library_status() -> str:
    """Show where the local library mode reads from and how much is cached."""
    try:
        cache = CACHE_DIR / "export"
        files = [p for p in cache.rglob("*.json")] if cache.exists() else []
        size = sum(p.stat().st_size for p in files) / 1e6
        return (
            f"books index: {len(_books())} entries\n"
            f"cache dir: {CACHE_DIR} ({len(files)} books, {size:.0f} MB)\n"
            f"SEFARIA_EXPORT_DIR: {EXPORT_DIR or 'not set'}"
        )
    except Exception as e:  # noqa: BLE001
        return _err(e)


if __name__ == "__main__":
    mcp.run()
