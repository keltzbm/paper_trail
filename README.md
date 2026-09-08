# paper_trail — local library organizer

This covers six files only: `organizer.py`, `metadata.py`, `utils.py`,
`concurrency.py`, `json_store.py`, `notes.py`. They handle taking a folder of
PDFs you already have locally and filing them into a proper research
library — identifying each paper, renaming it, and writing a note next to
it. Nothing here downloads or scrapes anything; the only network calls are
read-only lookups against the public Crossref API to confirm a paper's
identity.

## What it does, in one pass

1. Scans an inbox folder of PDFs.
2. Finds duplicates by content (not filename) and skips them.
3. For each remaining PDF, tries several ways to identify it — always
   confirming the match against Crossref before trusting it (see
   [How matching works](#how-matching-works) below).
4. Renames and moves matched PDFs into `<year>_<author>_<title>/` folders in
   your library, with an Obsidian-ready `.md` note alongside each one.
5. Anything it can't confidently identify goes into `_unfiled/` instead of
   being guessed at.

---

## Commands

### Organize an inbox

```
python -m paper_trail.organizer [inbox] [output] [options]
```

| Argument / flag | Default | What it does |
|---|---|---|
| `inbox` (positional) | `downloaded_pdfs` | Folder of PDFs to sort through |
| `output` (positional) | your library root | Where organized folders get created |
| `-w`, `--workers N` | `8` | Concurrent worker threads |
| `--dry-run` | off | Shows what would happen; moves nothing, writes nothing |
| `-v`, `--verbose` | off | Also prints log lines to the terminal (not just `logs/organizer.log`) |
| `--file-timeout N` | `90` | Max seconds to wait on any single PDF before giving up on it and moving on. `0` waits forever |
| `--skip-ocr` | off | Skips the OCR step entirely — much faster on a big batch, at the cost of missing scanned PDFs with no embedded text |
| `--ocr-backend {auto,vision,tesseract}` | `auto` | `auto` uses Apple's Vision framework (GPU/Neural Engine) on macOS when installed, Tesseract (CPU) otherwise |
| `--revert-method METHOD` | off | Instead of organizing, scans `output` for papers already matched via `METHOD` and moves them back to `_unfiled/`. See [Auditing matches](#auditing-and-reverting-low-confidence-matches) |

**Examples**

```bash
# Basic run
python -m paper_trail.organizer "~/research/_unfiled" "~/research"

# Preview first, verbose, with more workers
python -m paper_trail.organizer "~/research/_unfiled" "~/research" -w 16 --dry-run -v

# Fast first pass, skipping the slow OCR step
python -m paper_trail.organizer "~/research/_unfiled" "~/research" -w 16 --skip-ocr

# Slower second pass with OCR, just on whatever's left in _unfiled
python -m paper_trail.organizer "~/research/_unfiled" "~/research" -w 8 --file-timeout 60

# Force Tesseract instead of Vision (e.g. to compare results)
python -m paper_trail.organizer "~/research/_unfiled" "~/research" --ocr-backend tesseract

# Preview, then actually revert every paper matched only by fuzzy title search
python -m paper_trail.organizer --revert-method fuzzy_title_search --dry-run "~/research"
python -m paper_trail.organizer --revert-method fuzzy_title_search "~/research"
```

> With `--revert-method`, pass the library path as a single argument (as
> above) — it's read from whichever positional you give it. If you're
> scripting this and want to be explicit, two positionals also work and the
> **second** one is used as the library: `... --revert-method X unused
> "~/research"`.

### Relocate an organized library

Moves everything out of one library folder into another (e.g. onto a
different drive), cleaning up the empty source folder when done.

```
python -m paper_trail.utils source target [options]
```

| Argument / flag | Default | What it does |
|---|---|---|
| `source` (positional) | — | Current library folder |
| `target` (positional) | — | Destination folder |
| `-w`, `--workers N` | `8` | Concurrent move workers |
| `--no-cleanup` | off | Keeps the (now-empty) source folder instead of removing it |

```bash
python -m paper_trail.utils "~/research" "/Volumes/Backup/research" -w 8
```

---

## How matching works

For each PDF, `get_metadata()` tries these in order and **stops at the
first one Crossref actually confirms** — nothing is accepted just because
it looks plausible:

1. **`isbn`** — an ISBN pattern (`978-...`) in the filename, mapped to a
   Springer DOI.
2. **`legacy_springer_doi`** — pre-2007 Springer titles used a short code
   (`b138671`, `BFb0063954`) as the literal DOI suffix; recognized directly.
3. **`pdf_text_doi`** — DOI-shaped strings found in the PDF's embedded text
   (first 3 pages). A page can mention several (its own DOI, plus ones it
   cites) — each is tried against Crossref in turn until one validates.
4. **`ocr_doi`** *(unless `--skip-ocr`)* — same idea, but for scanned pages
   with no embedded text: OCR one page at a time, try any DOI-shaped
   results, only rasterize/OCR the next page if nothing on this one
   validated.
5. **`fuzzy_title_search`** — last resort: cleans up the filename and asks
   Crossref's search API for the closest-matching title. This is a guess,
   not a validated identifier — see the warning below.

Note: a DOI simply appearing in the **filename** is **not** trusted as a
match on its own (removed deliberately — a filename is metadata someone
else assigned, and can be wrong). DOIs are only trusted when found in the
actual page content and confirmed against Crossref.

## Auditing and reverting low-confidence matches

Every organized note's frontmatter includes a `resolved_via:` field
recording which method above matched it. The end-of-run summary also
breaks down successes by method, and specifically flags `ocr_doi` and
`fuzzy_title_search` counts — those two are the ones most likely to be
wrong, so they're worth a spot-check.

To find them: search your notes/vault for `resolved_via: ocr_doi` or
`resolved_via: fuzzy_title_search`.

To undo them in bulk: `--revert-method` (see above) moves every paper
matched via a given method back into `_unfiled/`, so you can re-run
organize and let a different (more reliable) method have a shot, or
resolve them by hand.

---

## Cache and logs

- **`data/cache_index.json`** — remembers metadata already resolved for a
  given file path, so re-running organize on the same inbox doesn't
  re-query Crossref for files it's already identified. Loaded once per run,
  not once per file.
- **`logs/organizer.log`** — timestamped per-file log (`→ Starting: X`,
  `Organized: X → Y/`, `✗ No metadata found for X`, timeouts, errors).
  Always written, regardless of `-v`. Useful for checking whether a run is
  actually progressing: `tail -f logs/organizer.log` in a second terminal
  while a run is going.

## Performance notes

- **Duplicate scan** groups files by size before hashing anything — only
  files that share a size with another file get MD5'd, since files of
  different sizes can never be identical. Usually a small fraction of a
  large, varied library.
- **`--skip-ocr`** for a fast first pass across a big batch; OCR is by far
  the slowest step. Leftover `_unfiled/` files can get a slower second pass
  without it.
- **`--ocr-backend auto`** (default) uses Apple's Vision framework — GPU/
  Neural Engine accelerated — on macOS instead of CPU-bound Tesseract, when
  the `ocrmac` package is installed.
- **`--file-timeout`** keeps one pathological file (a slow network read, a
  scanned PDF that hangs in OCR) from freezing progress on the rest of a
  batch — it gets reported and left in place instead of blocked on forever.
- The Crossref HTTP session is reused across the whole run (connection
  keep-alive) instead of opening a fresh connection for every lookup.

## Dependencies

- **`tqdm`** — progress bars (optional; falls back to a plain counter if
  missing).
- **`pypdf`** — embedded PDF text extraction.
- **`pdf2image`** + **poppler** (system package, e.g. `brew install poppler`
  or `apt install poppler-utils`) — rasterizes scanned pages for OCR.
- **`pytesseract`** + the **Tesseract** binary — CPU OCR, works on
  macOS/Windows/Linux.
- **`ocrmac`** (`pip install ocrmac`) — macOS only. Enables the
  GPU/Neural-Engine-accelerated Vision OCR backend. Not required; without
  it, `--ocr-backend auto` just uses Tesseract everywhere.
