# Sefaria MCP

An MCP server that lets Claude read the Sefaria library. It has two halves.

**Live tools** call Sefaria's public API (no key needed):

- get_text: text at any ref, Hebrew, English or both, optional specific edition
- list_versions: editions available for a ref
- search: Sefaria's full text search, with optional category filter like Talmud/Bavli
- related: commentaries and cross references for a ref, grouped by work
- find_ref: turn a loose title into a valid ref
- book_info: author, dates, structure
- learning_calendar: today's parsha, Daf Yomi, Mishnah Yomit, Rambam, Arukh HaShulchan Yomi and more
- lexicon: BDB, Jastrow, Klein

**Library tools** read whole books from the Sefaria-Export dataset in this repo's index (`../books.json`):

- library_find: find a book by title words
- grep_book: exact phrase search inside one or more whole books. Ignores nikud by default, supports regex, returns refs with snippets
- library_status: shows what is cached

The first time you grep a book, it downloads from Sefaria's public bucket and is cached on disk, so later searches are instant and work offline. Use grep_book when you need an exact expression traced through a work, and search when you want Sefaria's ranked, lemmatized results.

Refs from grep_book are worked out from position in the text, so confirm with get_text before quoting.

## Install

Python 3.10 or newer.

    pip install -r requirements.txt

## Connect to Claude

Claude Code:

    claude mcp add sefaria -- python /full/path/to/Sefaria-Export/sefaria-mcp/sefaria_mcp.py

Claude desktop app: Settings, Developer, Edit Config, then add under mcpServers:

    "sefaria": {
      "command": "python",
      "args": ["/full/path/to/Sefaria-Export/sefaria-mcp/sefaria_mcp.py"]
    }

Restart the app.

## Optional settings (environment variables)

- SEFARIA_CACHE: where downloaded books are cached. Default is ~/.cache/sefaria-mcp
- SEFARIA_EXPORT_DIR: a folder you copied from the bucket with gcloud (it should contain a json/ folder). Used before the cache and before downloading.
- SEFARIA_BOOKS_JSON: path to a books.json. Default is the one in this repo, or a one-time download.

To grab a whole category up front, for example all of Halakhah:

    gcloud storage cp -r "gs://sefaria-export/json/Halakhah/" ./sefaria-data/json/Halakhah/

then set SEFARIA_EXPORT_DIR to ./sefaria-data.

## Try it

- "What is today's Arukh HaShulchan Yomi? Pull the Hebrew and trace its sources."
- "Grep Bava Metzia and Arukh HaShulchan for שומר חנם and compare where each uses it."
- "Who comments on Genesis 18:1, and what does each one say?"

## Notes

Sefaria texts have per-edition licenses, which list_versions shows. This is for personal research use.
