#!/usr/bin/env python3
"""
Download books from Internet Archive and upload to Google Drive.

Usage:
    python ia_books_to_gdrive.py -i books.txt --dry-run
    python ia_books_to_gdrive.py -i books.txt --drive-folder <FOLDER_ID>
    python ia_books_to_gdrive.py -i books.txt --drive-folder <FOLDER_ID> --cleanup

Input file format (one per line, optional "| author"):
    Moby Dick | Herman Melville
    The Great Gatsby
    Meditations | Marcus Aurelius
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import time
from dataclasses import dataclass

import internetarchive as ia
from rapidfuzz import fuzz

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

IA_THROTTLE_SECONDS = 1.0
IA_MAX_RESULTS = 30
DEFAULT_THRESHOLD = 75.0
MAX_RETRIES = 3

FORMAT_PRIORITY = ("Text PDF", "PDF", "EPUB")

REPORT_COLUMNS = [
    "query_title",
    "query_author",
    "matched_title",
    "matched_creator",
    "match_score",
    "runner_up_score",
    "ia_identifier",
    "local_file",
    "status",
    "drive_file_id",
]

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class BookQuery:
    title: str
    author: str | None = None


# ---------------------------------------------------------------------------
# Input parsing
# ---------------------------------------------------------------------------


def parse_input(filepath: str) -> list[BookQuery]:
    """Parse a TXT file into BookQuery objects. Skips blanks, # comments, and empty titles."""
    queries: list[BookQuery] = []
    try:
        with open(filepath, "r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "|" in line:
                    parts = line.split("|", maxsplit=1)
                    title = parts[0].strip()
                    author = parts[1].strip() or None
                else:
                    title = line
                    author = None
                if not title:
                    continue
                queries.append(BookQuery(title=title, author=author))
    except OSError as e:
        sys.exit(f"Error: Cannot read input file: {e}")
    return queries


# ---------------------------------------------------------------------------
# Text normalization
# ---------------------------------------------------------------------------


def normalize(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# Internet Archive search
# ---------------------------------------------------------------------------


def search_ia(
    query: BookQuery, max_results: int = IA_MAX_RESULTS
) -> tuple[list[dict], bool]:
    """
    Search IA with broad keyword query, return (candidate list, success bool).
    Returns ([], False) on API failure vs ([], True) for genuinely no results.
    """
    title_terms = query.title
    if query.author:
        q = f"({title_terms}) AND creator:({query.author}) AND mediatype:(texts)"
    else:
        q = f"({title_terms}) AND mediatype:(texts)"

    results: list[dict] = []
    try:
        search = ia.search_items(
            q,
            fields=["identifier", "title", "creator", "downloads"],
            params={"rows": max_results},
        )
        for item in search:
            downloads = item.get("downloads", 0)
            try:
                downloads = int(downloads)
            except (TypeError, ValueError):
                downloads = 0
            results.append(
                {
                    "identifier": item.get("identifier", ""),
                    "title": item.get("title", ""),
                    "creator": item.get("creator", ""),
                    "downloads": downloads,
                }
            )
            if len(results) >= max_results:
                break
    except Exception as e:
        print(f"  Warning: IA search failed: {e}")
        time.sleep(IA_THROTTLE_SECONDS)
        return [], False

    time.sleep(IA_THROTTLE_SECONDS)
    return results, True


# ---------------------------------------------------------------------------
# Fuzzy matching / candidate ranking
# ---------------------------------------------------------------------------


def score_candidate(query: BookQuery, candidate: dict) -> float:
    """
    Score a single IA result against the user's query.
    Title similarity is primary (0-100). Author match adds up to 15 bonus.
    """
    title_score = fuzz.token_set_ratio(
        normalize(query.title),
        normalize(candidate.get("title", "")),
    )

    author_bonus = 0.0
    if query.author and candidate.get("creator"):
        creator = candidate["creator"]
        if isinstance(creator, list):
            creator = " ".join(creator)
        author_similarity = fuzz.token_set_ratio(
            normalize(query.author),
            normalize(creator),
        )
        author_bonus = (author_similarity / 100.0) * 15

    return title_score + author_bonus


def find_best_match(
    query: BookQuery,
    ia_results: list[dict],
    threshold: float = DEFAULT_THRESHOLD,
) -> dict | None:
    """Rank all candidates by score, return best if above threshold."""
    if not ia_results:
        return None

    scored: list[tuple[float, dict]] = []
    for r in ia_results:
        if not r.get("identifier") or not r.get("title"):
            continue
        s = score_candidate(query, r)
        scored.append((s, r))

    if not scored:
        return None

    # Primary: highest score. Tiebreaker: most downloads.
    def _sort_key(item: tuple[float, dict]) -> tuple[float, float]:
        score, cand = item
        dl = cand.get("downloads", 0)
        try:
            dl = int(dl)
        except (TypeError, ValueError):
            dl = 0
        return (-score, -dl)

    scored.sort(key=_sort_key)

    best_score, best = scored[0]
    runner_up_score = scored[1][0] if len(scored) > 1 else 0.0

    if best_score < threshold:
        return None

    creator = best.get("creator", "")
    if isinstance(creator, list):
        creator = ", ".join(creator)

    return {
        "identifier": best["identifier"],
        "title": best["title"],
        "creator": creator,
        "score": round(best_score, 1),
        "runner_up_score": round(runner_up_score, 1),
    }


# ---------------------------------------------------------------------------
# Internet Archive download
# ---------------------------------------------------------------------------


def download_book(
    identifier: str,
    output_dir: str,
    formats: tuple[str, ...] = FORMAT_PRIORITY,
    max_retries: int = MAX_RETRIES,
) -> tuple[str | None, str]:
    """
    Download the best available file from an IA item.
    Returns (file_path, status) where status is one of:
      "ok"                  — downloaded and verified
      "no_downloadable_file" — item has no file in preferred formats
      "download_failed"     — network/IO error after retries
    """
    os.makedirs(output_dir, exist_ok=True)

    try:
        item = ia.get_item(identifier)
        all_files = list(item.get_files())
    except Exception as e:
        print(f"  Warning: Could not fetch item {identifier}: {e}")
        return None, "download_failed"

    # Find first file matching preferred format by exact label
    target = None
    for fmt in formats:
        for f in all_files:
            if f.format == fmt:
                target = f
                break
        if target:
            break

    if target is None:
        return None, "no_downloadable_file"

    # internetarchive downloads to: output_dir/identifier/filename
    expected_path = os.path.join(output_dir, identifier, target.name)

    for attempt in range(1, max_retries + 1):
        try:
            target.download(destdir=output_dir)
            time.sleep(IA_THROTTLE_SECONDS)

            # Verify file actually landed
            if os.path.exists(expected_path) and os.path.getsize(expected_path) > 0:
                return expected_path, "ok"

            # File didn't land where expected — check alternative path
            alt_path = os.path.join(output_dir, target.name)
            if os.path.exists(alt_path) and os.path.getsize(alt_path) > 0:
                return alt_path, "ok"

            # File missing/empty — retry
            if attempt < max_retries:
                wait = 2**attempt
                print(f"  Download produced empty/missing file, retrying in {wait}s")
                time.sleep(wait)
                continue
            print("  Download produced empty/missing file after all attempts")
            return None, "download_failed"
        except Exception as e:
            if attempt == max_retries:
                print(f"  Download failed after {max_retries} attempts: {e}")
                return None, "download_failed"
            wait = 2**attempt
            print(f"  Download attempt {attempt} failed, retrying in {wait}s: {e}")
            time.sleep(wait)

    return None, "download_failed"


# ---------------------------------------------------------------------------
# Google Drive auth + upload
# ---------------------------------------------------------------------------

# drive scope (not drive.file) needed to upload into arbitrary existing folders
SCOPES = ["https://www.googleapis.com/auth/drive"]


def get_drive_service(
    credentials_path: str = "credentials.json",
    token_path: str = "token.json",
):
    """Build and return an authenticated Drive API v3 service."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None

    if os.path.exists(token_path):
        try:
            creds = Credentials.from_authorized_user_file(token_path, SCOPES)
        except Exception:
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                creds = None

        if not creds:
            flow = InstalledAppFlow.from_client_secrets_file(
                credentials_path, SCOPES
            )
            creds = flow.run_local_server(port=0)

        with open(token_path, "w") as token:
            token.write(creds.to_json())

    return build("drive", "v3", credentials=creds)


def upload_to_drive(
    service,
    file_path: str,
    folder_id: str | None = None,
    max_retries: int = MAX_RETRIES,
) -> dict | None:
    """
    Upload a file to Google Drive. Returns file metadata dict on success.
    The file is private to the uploader — no shareable link is created.
    """
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    file_metadata: dict = {"name": os.path.basename(file_path)}
    if folder_id:
        file_metadata["parents"] = [folder_id]

    mime_map = {".pdf": "application/pdf", ".epub": "application/epub+zip"}
    ext = os.path.splitext(file_path)[1].lower()
    mime_type = mime_map.get(ext, "application/octet-stream")

    media = MediaFileUpload(file_path, mimetype=mime_type, resumable=True)

    for attempt in range(1, max_retries + 1):
        try:
            result = (
                service.files()
                .create(
                    body=file_metadata,
                    media_body=media,
                    fields="id, name",
                    supportsAllDrives=True,
                )
                .execute()
            )
            return result
        except HttpError as e:
            status = e.resp.status if hasattr(e, "resp") else 0
            if status in (429, 500, 502, 503) and attempt < max_retries:
                wait = 2**attempt
                print(f"  Upload attempt {attempt} failed ({status}), retrying in {wait}s")
                time.sleep(wait)
            else:
                print(f"  Upload failed: {e}")
                return None
        except Exception as e:
            print(f"  Upload failed: {e}")
            return None

    return None


# ---------------------------------------------------------------------------
# Preflight validation
# ---------------------------------------------------------------------------


def preflight(
    credentials_path: str,
    token_path: str,
    output_dir: str,
    folder_id: str | None,
    dry_run: bool,
):
    """Validate config before processing. Returns Drive service or None for dry-run."""
    os.makedirs(output_dir, exist_ok=True)

    if dry_run:
        return None

    if not os.path.exists(credentials_path):
        sys.exit(f"Error: Google credentials file not found: {credentials_path}")

    try:
        service = get_drive_service(credentials_path, token_path)
    except Exception as e:
        sys.exit(f"Error: Google Drive authentication failed: {e}")

    if folder_id:
        try:
            meta = (
                service.files()
                .get(fileId=folder_id, fields="id, name, mimeType", supportsAllDrives=True)
                .execute()
            )
            if meta.get("mimeType") != "application/vnd.google-apps.folder":
                sys.exit(
                    f"Error: Drive ID '{folder_id}' is not a folder "
                    f"(type: {meta.get('mimeType')})"
                )
            print(f"Drive folder verified: {meta.get('name', folder_id)}")
        except SystemExit:
            raise
        except Exception as e:
            sys.exit(f"Error: Cannot access Drive folder '{folder_id}': {e}")

    return service


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def _sanitize_csv(value: str) -> str:
    """Prevent CSV formula injection by prefixing dangerous leading chars."""
    if value and value[0] in ("=", "+", "-", "@"):
        return "'" + value
    return value


def make_row(
    query: BookQuery,
    match: dict | None = None,
    status: str = "",
    drive_id: str = "",
    file_path: str = "",
) -> dict:
    creator = ""
    if match:
        creator = match.get("creator", "")
        if isinstance(creator, list):
            creator = ", ".join(creator)

    return {
        "query_title": _sanitize_csv(query.title),
        "query_author": _sanitize_csv(query.author or ""),
        "matched_title": _sanitize_csv(match["title"] if match else ""),
        "matched_creator": _sanitize_csv(creator),
        "match_score": match["score"] if match else "",
        "runner_up_score": match.get("runner_up_score", "") if match else "",
        "ia_identifier": match["identifier"] if match else "",
        "local_file": file_path,
        "status": status,
        "drive_file_id": drive_id,
    }


def write_report(results: list[dict], output_dir: str) -> str:
    path = os.path.join(output_dir, "report.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=REPORT_COLUMNS)
        writer.writeheader()
        writer.writerows(results)
    return path


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


def run(
    input_file: str,
    output_dir: str,
    credentials_path: str,
    token_path: str,
    folder_id: str | None,
    threshold: float,
    dry_run: bool,
    cleanup: bool,
) -> None:
    # 0. Preflight
    service = preflight(credentials_path, token_path, output_dir, folder_id, dry_run)

    # 1. Parse input
    queries = parse_input(input_file)
    if not queries:
        print("No book titles found in input file.")
        write_report([], output_dir)
        return

    mode = "DRY RUN — search & match only" if dry_run else "full pipeline"
    print(f"Processing {len(queries)} book(s) [{mode}]\n")

    results: list[dict] = []

    for i, query in enumerate(queries, 1):
        label = f"[{i}/{len(queries)}] \"{query.title}\""
        if query.author:
            label += f" by {query.author}"
        print(label)

        # 2. Search
        ia_results, search_ok = search_ia(query)
        if not search_ok:
            print("  ✗ Search failed (API error)")
            results.append(make_row(query, status="search_failed"))
            continue
        if not ia_results:
            print("  ✗ No results found on Internet Archive")
            results.append(make_row(query, status="no_results"))
            continue

        # 3. Match
        match = find_best_match(query, ia_results, threshold=threshold)
        if not match:
            print(f"  ✗ No match above threshold ({threshold})")
            results.append(make_row(query, status="below_threshold"))
            continue

        print(
            f"  Matched: \"{match['title']}\" "
            f"(score: {match['score']}, runner-up: {match['runner_up_score']})"
        )

        if dry_run:
            results.append(make_row(query, match=match, status="dry_run"))
            continue

        # 4. Download
        file_path, dl_status = download_book(match["identifier"], output_dir)
        if file_path is None:
            msg = (
                "No downloadable PDF/EPUB found"
                if dl_status == "no_downloadable_file"
                else "Download failed (network/IO error)"
            )
            print(f"  ✗ {msg}")
            results.append(make_row(query, match=match, status=dl_status))
            continue

        file_size = os.path.getsize(file_path)
        print(f"  Downloaded: {os.path.basename(file_path)} ({file_size // 1024}KB)")

        # 5. Upload
        drive_file = upload_to_drive(service, file_path, folder_id)
        if not drive_file:
            print("  ✗ Upload to Drive failed (local file kept for retry)")
            results.append(
                make_row(
                    query, match=match, status="upload_failed", file_path=file_path
                )
            )
            continue

        print(f"  ✓ Uploaded to Drive (ID: {drive_file['id']})")
        results.append(
            make_row(
                query,
                match=match,
                status="success",
                drive_id=drive_file["id"],
                file_path=file_path,
            )
        )

        if cleanup:
            try:
                if os.path.exists(file_path):
                    os.remove(file_path)
                    print("  Cleaned up local file")
            except OSError as e:
                print(f"  Warning: Could not clean up {file_path}: {e}")

    # 6. Report
    try:
        report_path = write_report(results, output_dir)
    except OSError as e:
        print(f"\nError: Could not write report: {e}")
        report_path = "(failed to write)"

    # Summary
    print("\n" + "=" * 60)
    success = sum(1 for r in results if r["status"] == "success")
    matched = sum(1 for r in results if r["status"] in ("success", "dry_run"))
    failed = len(results) - matched
    print(f"Done. {matched} matched, {success} uploaded, {failed} skipped/failed.")
    print(f"Report: {report_path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download books from Internet Archive and upload to Google Drive.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            'Input file format (one per line, optional "| author"):\n'
            "  Moby Dick | Herman Melville\n"
            "  The Great Gatsby\n"
            "  Meditations | Marcus Aurelius\n"
        ),
    )
    parser.add_argument(
        "-i",
        "--input",
        required=True,
        help="Path to TXT file with book titles",
    )
    parser.add_argument(
        "--drive-folder",
        default=None,
        help="Google Drive folder ID to upload into (default: Drive root)",
    )
    parser.add_argument(
        "--credentials",
        default="credentials.json",
        help="Path to Google OAuth credentials.json (default: ./credentials.json)",
    )
    parser.add_argument(
        "--token",
        default="token.json",
        help="Path to store OAuth token (default: ./token.json)",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_THRESHOLD,
        help=f"Fuzzy match score threshold 0-100 (default: {DEFAULT_THRESHOLD})",
    )
    parser.add_argument(
        "--output-dir",
        default="./downloads",
        help="Local directory for downloads + report (default: ./downloads)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Search and match only — don't download or upload",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="Delete local files after successful upload",
    )

    args = parser.parse_args()

    if not os.path.isfile(args.input):
        sys.exit(f"Error: Input file not found or is not a file: {args.input}")

    run(
        input_file=args.input,
        output_dir=args.output_dir,
        credentials_path=args.credentials,
        token_path=args.token,
        folder_id=args.drive_folder,
        threshold=args.threshold,
        dry_run=args.dry_run,
        cleanup=args.cleanup,
    )


if __name__ == "__main__":
    main()
