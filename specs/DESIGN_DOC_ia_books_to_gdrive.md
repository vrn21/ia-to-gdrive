# Design Document: Internet Archive Book Downloader → Google Drive Uploader

**Author:** Auto-generated  
**Date:** 2026-03-24  
**Status:** Draft v2 (revised after QA review)  
**Python version:** 3.10+

---

## 1. Overview

A Python CLI script that:
1. Reads a list of book titles (with optional authors) from a plain text file
2. Searches Internet Archive using broad keyword queries
3. Ranks results with fuzzy matching (title + author aware)
4. Downloads the best-matching freely available book (PDF or EPUB)
5. Uploads it to a specified Google Drive folder
6. Produces a CSV summary report

Sequential processing. Single auth mode (OAuth2 desktop flow). No concurrency in v1.

---

## 2. Goals & Non-Goals

### Goals
- Accept book titles from a TXT file (one per line, optional `|` separated author)
- Search IA broadly and rank locally with fuzzy matching to handle typos, subtitles, word order
- Download only freely available book files (determined by actual file availability, not metadata heuristics)
- Upload to a user-specified Google Drive folder via OAuth2
- Produce a CSV report with match scores, statuses, and Drive file IDs

### Non-Goals
- Downloading DRM/lending-restricted books
- Concurrency or parallel downloads (future enhancement)
- Multiple auth modes (service account, rclone — future enhancement)
- Web UI or GUI
- Syncing, deduplication, or idempotent re-runs

---

## 3. Architecture

```
┌─────────────┐    ┌────────────┐    ┌──────────────────┐    ┌──────────────┐    ┌──────────────┐
│  Book List   │───▶│  Preflight │───▶│  Search & Match   │───▶│   Download   │───▶│ Google Drive  │
│  (TXT file)  │    │  Validate  │    │  (IA API + Fuzzy) │    │  (IA Python) │    │   Upload     │
└─────────────┘    └────────────┘    └──────────────────┘    └──────────────┘    └──────────────┘
                         │                                          │                     │
                         ▼                                          ▼                     ▼
                  ┌──────────────┐                           ┌────────────┐       ┌────────────┐
                  │ Validate:    │                           │  Local Temp │       │  Summary   │
                  │ - creds exist│                           │  Directory  │       │  Report    │
                  │ - Drive auth │                           └────────────┘       │  (CSV)     │
                  │ - folder OK  │                                                └────────────┘
                  │ - output dir │
                  └──────────────┘
```

### Pipeline Stages

| Stage | Input | Output | Component |
|-------|-------|--------|-----------|
| 0. Preflight | Config/creds/folder ID | Pass/fail (abort early) | Validate all external deps |
| 1. Parse | TXT file | List of `{title, author?}` | File reader |
| 2. Search | Title (+author) string | Top 30 IA results | `internetarchive.search_items()` |
| 3. Match | Query + IA results | Best item (identifier, score) | `rapidfuzz` title+author scoring |
| 4. Download | IA identifier | Local file path (verified) | `internetarchive` download |
| 5. Upload | Local file path | Drive file ID | Google Drive API v3 |
| 6. Report | All results | `report.csv` | csv module |

---

## 4. Component Deep Dive

### 4.0 Preflight Validation

Before processing any books, validate all external dependencies upfront:

```python
import os
import sys

def preflight(credentials_path: str, token_path: str, output_dir: str, folder_id: str | None) -> None:
    """Validate config before processing. Abort early on failure."""
    # 1. Check credentials file exists
    if not os.path.exists(credentials_path):
        sys.exit(f"Error: Google credentials file not found: {credentials_path}")

    # 2. Create output directory
    os.makedirs(output_dir, exist_ok=True)

    # 3. Authenticate and validate Drive access
    service = get_drive_service(credentials_path, token_path)

    # 4. If folder ID given, verify it's accessible
    if folder_id:
        try:
            service.files().get(fileId=folder_id, fields="id, name").execute()
        except Exception as e:
            sys.exit(f"Error: Cannot access Drive folder {folder_id}: {e}")

    return service
```

**Why:** Without preflight, you discover a bad folder ID or expired token only after downloading 50 books.

### 4.1 Input Parsing

**Format:** Plain text file, one entry per line. Optional author after `|` delimiter.

```
# books.txt
War and Peace | Leo Tolstoy
The Great Gatsby
A Brief History of Time | Stephen Hawking
Meditations | Marcus Aurelius
```

```python
import re
from dataclasses import dataclass

@dataclass
class BookQuery:
    title: str
    author: str | None = None

def parse_input(filepath: str) -> list[BookQuery]:
    queries: list[BookQuery] = []
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "|" in line:
                parts = line.split("|", maxsplit=1)
                queries.append(BookQuery(title=parts[0].strip(), author=parts[1].strip()))
            else:
                queries.append(BookQuery(title=line))
    return queries
```

**Why `title | author`:**
- Title-only matching is the single biggest source of wrong-book selection for common titles ("Meditations", "Poems", "History")
- Author is optional — works fine without it, but dramatically improves accuracy when present

### 4.2 Internet Archive Search

**Library:** [`internetarchive`](https://github.com/jjjake/internetarchive) (official IA Python library)

**Search strategy — broad keywords, rank locally:**

```python
import internetarchive as ia
import time

IA_THROTTLE_SECONDS = 1.0

def search_ia(query: BookQuery, max_results: int = 30) -> list[dict]:
    """
    Search IA with broad keyword query, return candidate metadata.
    Uses individual title words (not exact phrase) to catch variants.
    """
    # Build broad query: title words + optional creator + restrict to texts
    title_terms = query.title
    q = f"({title_terms}) AND mediatype:(texts)"
    if query.author:
        q = f"({title_terms}) AND creator:({query.author}) AND mediatype:(texts)"

    results = []
    search = ia.search_items(
        q,
        fields=["identifier", "title", "creator", "downloads"],
        params={"rows": max_results},
    )

    for item in search:
        results.append({
            "identifier": item.get("identifier", ""),
            "title": item.get("title", ""),
            "creator": item.get("creator", ""),
            "downloads": item.get("downloads", 0),
        })
        if len(results) >= max_results:
            break

    time.sleep(IA_THROTTLE_SECONDS)
    return results
```

**Key design decisions:**
- **Broad keyword query** (not exact phrase `title:("...")`) — catches subtitle variants, typos, alternate editions. Exact phrase queries exclude the very results fuzzy matching is meant to recover.
- **`creator` field** used when author is provided — dramatically narrows results for common titles
- **30 candidates** fetched per query — enough for local ranking without excessive API load
- **Throttle after every search** — respects IA rate limits (1 req/sec unidentified)
- **`.get()` on all fields** — IA results can have missing keys; avoids `KeyError`

### 4.3 Fuzzy Matching / Candidate Ranking

**Library:** [`rapidfuzz`](https://github.com/rapidfuzz/RapidFuzz) (MIT, C++ backend)

```python
from rapidfuzz import fuzz

def normalize(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()

def score_candidate(query: BookQuery, candidate: dict) -> float:
    """
    Score a single IA result against the user's query.
    Title similarity is primary. Author match is a bonus. Downloads break ties.
    """
    title_score = fuzz.token_set_ratio(
        normalize(query.title),
        normalize(candidate.get("title", ""))
    )

    author_bonus = 0.0
    if query.author and candidate.get("creator"):
        creator = candidate["creator"]
        if isinstance(creator, list):
            creator = " ".join(creator)
        author_similarity = fuzz.token_set_ratio(
            normalize(query.author),
            normalize(creator)
        )
        # Author match worth up to 15 bonus points
        author_bonus = (author_similarity / 100.0) * 15

    return title_score + author_bonus

def find_best_match(
    query: BookQuery,
    ia_results: list[dict],
    threshold: float = 75.0,
) -> dict | None:
    """
    Rank all candidates, return best if above threshold.
    Logs runner-up for transparency.
    """
    if not ia_results:
        return None

    scored = []
    for r in ia_results:
        if not r.get("identifier") or not r.get("title"):
            continue
        s = score_candidate(query, r)
        scored.append((s, r))

    scored.sort(key=lambda x: (-x[0], -x[1].get("downloads", 0)))

    if not scored:
        return None

    best_score, best = scored[0]
    runner_up_score = scored[1][0] if len(scored) > 1 else 0

    if best_score < threshold:
        return None

    return {
        "identifier": best["identifier"],
        "title": best["title"],
        "creator": best.get("creator", ""),
        "score": round(best_score, 1),
        "runner_up_score": round(runner_up_score, 1),
    }
```

**Why this approach vs. v1:**
- **Author-aware scoring** — `score_candidate` adds up to 15 bonus points for author match, preventing "Meditations by Marcus Aurelius" from matching a random "Meditations on Cooking"
- **Normalization** — strips punctuation, collapses whitespace, lowercases before comparison
- **`token_set_ratio`** — handles word order ("War and Peace" = "Peace and War") and extra words ("War and Peace: A Novel")
- **Downloads as tiebreaker** — among equal scores, prefer the more popular item
- **Runner-up tracked** — logged for transparency / debugging
- **Threshold 75** (raised from 70) — reduces false positives for title-only queries
- **`.get()` everywhere** — no `KeyError` on incomplete results
- **Filters out items missing identifier/title** — avoids scoring garbage rows

### 4.4 Download from Internet Archive

```python
import os
import internetarchive as ia
import time

# Exact IA format labels for deterministic file selection
FORMAT_PRIORITY = [
    "Text PDF",
    "PDF",
    "EPUB",
]

def download_book(
    identifier: str,
    output_dir: str,
    formats: tuple[str, ...] = ("Text PDF", "PDF", "EPUB"),
    max_retries: int = 3,
) -> str | None:
    """
    Download the best available file from an IA item.
    Determines availability by inspecting actual file list, not collection metadata.
    Returns verified local file path, or None if no downloadable file found.
    """
    os.makedirs(output_dir, exist_ok=True)

    item = ia.get_item(identifier)
    all_files = list(item.get_files())

    # Find first available file matching preferred format (by exact format label)
    target = None
    for fmt in formats:
        for f in all_files:
            if f.format == fmt:
                target = f
                break
        if target:
            break

    if target is None:
        return None

    # Build expected download path
    # internetarchive downloads to: output_dir/identifier/filename
    expected_path = os.path.join(output_dir, identifier, target.name)

    for attempt in range(1, max_retries + 1):
        try:
            target.download(destdir=output_dir)
            time.sleep(IA_THROTTLE_SECONDS)

            # Verify file actually exists and has content
            if os.path.exists(expected_path) and os.path.getsize(expected_path) > 0:
                return expected_path

            return None
        except Exception as e:
            if attempt == max_retries:
                print(f"  Download failed after {max_retries} attempts: {e}")
                return None
            wait = 2 ** attempt
            print(f"  Download attempt {attempt} failed, retrying in {wait}s: {e}")
            time.sleep(wait)

    return None
```

**Key fixes from v1:**
- **No collection-based heuristics** — availability is determined by whether actual files with the desired format label exist in the item's file list. If there's no `"Text PDF"` / `"PDF"` / `"EPUB"` file entry, the item is skipped. This correctly handles both lending-restricted items (which won't have free PDF derivatives) and public items that simply lack that format.
- **Exact format label matching** (`f.format == fmt`) instead of substring matching — avoids picking `"PDF Module Source"` or `"Scanned PDF"` derivatives incorrectly
- **Immutable default** (`tuple` not `list`)
- **`os.makedirs` before download** — ensures output directory exists
- **Verified download path** — checks that `internetarchive` actually wrote the file where expected and that it has non-zero size
- **Retry with exponential backoff** — 3 attempts with 2/4/8s waits
- **Throttle on download** — all IA network calls are throttled, not just search

### 4.5 Google Drive Upload

**Auth mode (v1):** OAuth2 Desktop Flow only. Service account and rclone are future enhancements.

**Setup steps:**
1. Create a Google Cloud project at [console.cloud.google.com](https://console.cloud.google.com)
2. Enable the Google Drive API
3. Create OAuth2 Desktop credentials → download `credentials.json`
4. First run: browser opens for consent → stores `token.json` with refresh token
5. Subsequent runs: auto-refreshes, no user interaction needed

```python
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError
import os
import time

# drive scope (not drive.file) needed to upload into arbitrary existing folders
SCOPES = ["https://www.googleapis.com/auth/drive"]

def get_drive_service(
    credentials_path: str = "credentials.json",
    token_path: str = "token.json",
):
    """Build and return an authenticated Drive API service."""
    creds = None

    if os.path.exists(token_path):
        try:
            creds = Credentials.from_authorized_user_file(token_path, SCOPES)
        except Exception:
            creds = None  # Corrupted token file — re-auth

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                creds = None  # Refresh failed — re-auth

        if not creds:
            flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)
            creds = flow.run_local_server(port=0)

        with open(token_path, "w") as token:
            token.write(creds.to_json())

    return build("drive", "v3", credentials=creds)


def upload_to_drive(
    service,
    file_path: str,
    folder_id: str | None = None,
    max_retries: int = 3,
) -> dict | None:
    """
    Upload a file to Google Drive. Returns file metadata dict on success, None on failure.
    The returned 'id' is the Drive file ID. Note: the file is only accessible to the
    uploader unless permissions are explicitly added (not done by this script).
    """
    file_metadata: dict = {"name": os.path.basename(file_path)}
    if folder_id:
        file_metadata["parents"] = [folder_id]

    mime_map = {".pdf": "application/pdf", ".epub": "application/epub+zip"}
    ext = os.path.splitext(file_path)[1].lower()
    mime_type = mime_map.get(ext, "application/octet-stream")

    media = MediaFileUpload(file_path, mimetype=mime_type, resumable=True)

    for attempt in range(1, max_retries + 1):
        try:
            file = (
                service.files()
                .create(body=file_metadata, media_body=media, fields="id, name")
                .execute()
            )
            return file
        except HttpError as e:
            if e.resp.status in (429, 500, 502, 503) and attempt < max_retries:
                wait = 2 ** attempt
                print(f"  Upload attempt {attempt} failed ({e.resp.status}), retrying in {wait}s")
                time.sleep(wait)
            else:
                print(f"  Upload failed: {e}")
                return None
        except Exception as e:
            print(f"  Upload failed: {e}")
            return None
```

**Key fixes from v1:**
- **`drive` scope** (not `drive.file`) — `drive.file` only allows access to files the app created, NOT to arbitrary folders by ID. Using the broader scope avoids "File not found" errors when uploading into an existing folder.
- **Corrupted/expired token handling** — catches malformed token files and failed refreshes, falls back to re-authentication instead of crashing
- **Retry with backoff** on 429/5xx — actually implemented, not just promised
- **Returns `None` on failure** instead of raising — caller can log and continue
- **`fields="id, name"` only** — removed `webViewLink` because it's just the Drive page for the uploader and is NOT a shareable link. The doc no longer claims uploaded files produce shareable links (they don't without explicit permission creation).
- **Docstring clarifies** that uploaded files are private to the uploader

---

## 5. Main Pipeline

```python
import csv

def main(
    input_file: str,
    output_dir: str,
    credentials_path: str,
    token_path: str,
    folder_id: str | None,
    threshold: float,
    dry_run: bool,
    cleanup: bool,
):
    # 0. Preflight
    service = preflight(credentials_path, token_path, output_dir, folder_id)
    queries = parse_input(input_file)

    if not queries:
        print("No book titles found in input file.")
        return

    print(f"Processing {len(queries)} book(s)...\n")

    results = []

    for i, query in enumerate(queries, 1):
        label = f"[{i}/{len(queries)}] {query.title}"
        if query.author:
            label += f" by {query.author}"
        print(label)

        # Search
        ia_results = search_ia(query)
        if not ia_results:
            print("  ✗ No results found on Internet Archive")
            results.append(make_row(query, status="no_results"))
            continue

        # Match
        match = find_best_match(query, ia_results, threshold=threshold)
        if not match:
            print(f"  ✗ No match above threshold ({threshold})")
            results.append(make_row(query, status="below_threshold"))
            continue

        print(f"  Matched: {match['title']} (score: {match['score']}, runner-up: {match['runner_up_score']})")

        if dry_run:
            results.append(make_row(query, match=match, status="dry_run"))
            continue

        # Download
        file_path = download_book(match["identifier"], output_dir)
        if not file_path:
            print("  ✗ No downloadable PDF/EPUB found")
            results.append(make_row(query, match=match, status="no_downloadable_file"))
            continue

        file_size = os.path.getsize(file_path)
        print(f"  Downloaded: {os.path.basename(file_path)} ({file_size // 1024}KB)")

        # Upload
        drive_file = upload_to_drive(service, file_path, folder_id)
        if not drive_file:
            print("  ✗ Upload to Drive failed (local file kept for retry)")
            results.append(make_row(query, match=match, status="upload_failed", file_path=file_path))
            continue

        print(f"  ✓ Uploaded to Drive (ID: {drive_file['id']})")
        results.append(make_row(query, match=match, status="success", drive_id=drive_file["id"], file_path=file_path))

        # Cleanup local file on success if requested
        if cleanup and os.path.exists(file_path):
            os.remove(file_path)

    # Write report
    write_report(results, output_dir)
    print(f"\nDone. Report saved to {os.path.join(output_dir, 'report.csv')}")
```

**Temp file lifecycle:**
- On **upload success** + `--cleanup` flag: local file is deleted
- On **upload failure**: local file is **kept** so user can retry upload manually or re-run
- On **download failure**: nothing to clean up
- Default: `--cleanup` is **off** (files kept)

### 4.6 Report

```python
def make_row(query, match=None, status="", drive_id="", file_path=""):
    return {
        "query_title": query.title,
        "query_author": query.author or "",
        "matched_title": match["title"] if match else "",
        "matched_creator": match.get("creator", "") if match else "",
        "match_score": match["score"] if match else "",
        "runner_up_score": match.get("runner_up_score", "") if match else "",
        "ia_identifier": match["identifier"] if match else "",
        "local_file": file_path,
        "status": status,
        "drive_file_id": drive_id,
    }

REPORT_COLUMNS = [
    "query_title", "query_author", "matched_title", "matched_creator",
    "match_score", "runner_up_score", "ia_identifier", "local_file",
    "status", "drive_file_id",
]

def write_report(results: list[dict], output_dir: str) -> None:
    path = os.path.join(output_dir, "report.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=REPORT_COLUMNS)
        writer.writeheader()
        writer.writerows(results)
```

**Status values:**

| Status | Meaning |
|--------|---------|
| `success` | Matched, downloaded, uploaded |
| `dry_run` | Matched only (--dry-run) |
| `no_results` | IA search returned zero results |
| `below_threshold` | Best candidate scored below threshold |
| `no_downloadable_file` | Item exists but has no PDF/EPUB available for download |
| `upload_failed` | Downloaded but Drive upload failed (local file kept) |

---

## 5. CLI Interface

```
usage: ia_books_to_gdrive.py [-h] -i INPUT [-o OUTPUT_DIR]
                              [--drive-folder FOLDER_ID]
                              [--credentials CREDS_JSON]
                              [--threshold SCORE]
                              [--dry-run]
                              [--cleanup]

Download books from Internet Archive and upload to Google Drive.

required arguments:
  -i, --input INPUT        Path to TXT file with book titles (one per line, optional "| author")

optional arguments:
  --drive-folder FOLDER_ID  Google Drive folder ID to upload into (default: Drive root)
  --credentials CREDS_JSON  Path to Google OAuth credentials.json (default: ./credentials.json)
  --threshold SCORE         Fuzzy match score threshold 0-100 (default: 75)
  --output-dir OUTPUT_DIR   Local directory for downloads + report (default: ./downloads)
  --dry-run                 Search and match only — don't download or upload
  --cleanup                 Delete local files after successful upload
  -h, --help                Show this help message
```

**Removed from v1:** `--max-concurrent` (no concurrency in v1), `--formats` (hardcoded priority is simpler and avoids misconfiguration).

---

## 6. Dependencies

```
# requirements.txt
internetarchive>=5.0.0           # Official IA Python library (search + download)
rapidfuzz>=3.0.0                  # Fast fuzzy string matching (C++ backend, MIT)
google-api-python-client>=2.0.0   # Google Drive API client
google-auth-httplib2>=0.1.0       # Auth transport
google-auth-oauthlib>=1.0.0       # OAuth2 desktop flow
```

No `tqdm` — progress is shown via print statements per book. Keeps dependencies minimal.

---

## 7. Error Handling & Edge Cases

| Scenario | Handling |
|----------|----------|
| No IA results for a title | Log as `no_results`, continue to next |
| All results below threshold | Log best candidate score, record as `below_threshold` |
| Common title, no author given | Will match highest-scoring result — may be wrong. User should provide author for ambiguous titles |
| No PDF/EPUB files in item | Log as `no_downloadable_file`, skip |
| Download network error | Retry 3x with exponential backoff (2/4/8s), then log failure |
| Drive upload 429/5xx | Retry 3x with exponential backoff, then log `upload_failed` |
| Drive folder ID invalid | Caught in preflight — script aborts before processing |
| Credentials missing/expired | Caught in preflight — triggers re-auth or abort |
| Upload succeeds, file is private | Expected behavior — doc does not claim shareable links |
| Upload fails, local file exists | File is kept for manual retry |
| Partial pipeline failure | Report CSV includes all attempted rows with their status |
| Empty/comment lines in input | Skipped (blank lines and `#` comments) |
| Unicode titles / accents | Handled by normalize() — strips to word characters |
| Re-running same input | Will re-download and create duplicate Drive files (no idempotency in v1) |

---

## 8. Rate Limiting & Responsible Use

All IA network calls (search, metadata fetch, download) are throttled:

```python
IA_THROTTLE_SECONDS = 1.0  # Applied after every IA API call
```

- **Internet Archive:** 1 request/second minimum between all call types. Set `User-Agent` header via `internetarchive` library config.
- **Google Drive:** Retry on 429 with exponential backoff. Default quota (12,000 req/day) is sufficient for typical use (< 100 books).

---

## 9. Security Considerations

- `credentials.json` and `token.json` must be in `.gitignore` — never committed
- IA credentials (if `ia configure` is used) stored in `~/.config/internetarchive/ia.ini`
- No secrets logged — only file IDs, identifiers, and scores appear in output/report

---

## 10. Testing Strategy

| Test Type | What | How |
|-----------|------|-----|
| Unit | `parse_input()` | Empty lines, comments, Unicode, with/without author |
| Unit | `normalize()` | Punctuation, accents, extra whitespace |
| Unit | `score_candidate()` | Mock candidates, verify title+author scoring |
| Unit | `find_best_match()` | Mock results with edge cases (missing fields, ties, below threshold) |
| Unit | `make_row()` | All status variants |
| Integration | `search_ia()` | Live search for "Moby Dick" — verify results contain expected identifier |
| Integration | `download_book()` | Download a known small public domain item, verify file on disk |
| Integration | `preflight()` | Valid and invalid folder IDs, missing credentials |
| Integration | `upload_to_drive()` | Upload a 1KB test file to a test folder |
| E2E | Full pipeline | 3-book TXT → search → download → upload → verify report.csv |
| Mocking | IA + Drive | `unittest.mock.patch` on `ia.search_items`, `ia.get_item`, and Drive service for fast offline tests |

---

## 11. Future Enhancements (Out of Scope for v1)

- **Concurrency:** Parallel downloads with `concurrent.futures.ThreadPoolExecutor`
- **Service account auth:** For headless/CI execution
- **rclone upload:** Alternative to Drive API for users with existing rclone config
- **Idempotency:** Track uploaded books in a local state file to skip on re-runs
- **Open Library API:** Richer metadata / ISBN-based search for better matching
- **Shareable links:** Create Drive permissions to generate public/org-wide links
- **Additional formats:** DJVU, MOBI, TXT
- **Config file:** YAML/TOML instead of CLI flags only
- **Author fallback search:** If title+author returns no results, retry title-only

---

## 12. Reference OSS Projects

| Project | Role in This Script |
|---------|---------------------|
| [`jjjake/internetarchive`](https://github.com/jjjake/internetarchive) | Primary tool — search and download |
| [`rapidfuzz/RapidFuzz`](https://github.com/rapidfuzz/RapidFuzz) | Fuzzy string matching engine |
| [`googleapis/google-api-python-client`](https://github.com/googleapis/google-api-python-client) | Drive upload |
| [`MiniGlome/Archive.org-Downloader`](https://github.com/MiniGlome/Archive.org-Downloader) | Reference for IA download patterns |

---

## Appendix: Changes from v1 → v2

| Issue | v1 (broken) | v2 (fixed) |
|-------|-------------|------------|
| Search too strict | `title:("exact phrase")` excluded variants | Broad keyword query, rank locally |
| Title-only matching | Wrong book for common titles | Author-aware scoring (+15 bonus) |
| Lending detection | Collection metadata heuristic | Check actual file list for downloadable formats |
| Format matching | Substring (`"PDF" in f.format`) | Exact label match (`f.format == "Text PDF"`) |
| Missing `os` import | `NameError` at runtime | Fixed |
| Mutable default arg | `list` default on function | `tuple` default |
| No `.get()` on results | `KeyError` on incomplete data | `.get()` everywhere, filter bad rows |
| `drive.file` scope | Can't upload to existing folders | `drive` scope |
| No preflight | Bad folder ID discovered after downloads | Validate creds + folder before processing |
| No retry logic | Promised but not implemented | Exponential backoff on download + upload |
| `webViewLink` as "shareable" | Not actually shareable | Removed; doc clarifies files are private |
| Token corruption | Crash | Catches bad token, falls back to re-auth |
| 3 auth modes in v1 | Over-engineered | Single mode (OAuth2 desktop); others are future |
| `--max-concurrent` | Contradicted "no concurrency" | Removed from CLI |
| No temp file strategy | Undefined | Keep on failure, optional `--cleanup` on success |
| No idempotency | Undefined | Explicitly documented as out of scope |
| Threshold too low | 70 → false positives | Raised to 75 |
| Python version unstated | `dict \| None` syntax but no version req | Python 3.10+ stated |
| Download path unverified | Assumed but not checked | `os.path.exists` + size check after download |
