#!/usr/bin/env python3
"""Unit tests for ia_books_to_gdrive.py — all offline, no external calls."""

from __future__ import annotations

import csv
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch, PropertyMock

from ia_books_to_gdrive import (
    BookQuery,
    find_best_match,
    make_row,
    normalize,
    parse_input,
    score_candidate,
    write_report,
    download_book,
    search_ia,
    _sanitize_csv,
    REPORT_COLUMNS,
)


# ===========================================================================
# normalize()
# ===========================================================================


class TestNormalize(unittest.TestCase):
    def test_lowercase(self):
        self.assertEqual(normalize("Hello World"), "hello world")

    def test_strip_punctuation(self):
        self.assertEqual(normalize("War & Peace: A Novel"), "war peace a novel")

    def test_collapse_whitespace(self):
        self.assertEqual(normalize("  lots   of   space  "), "lots of space")

    def test_unicode_accents(self):
        result = normalize("Les Misérables")
        self.assertIn("misérables", result)

    def test_empty_string(self):
        self.assertEqual(normalize(""), "")

    def test_numbers_preserved(self):
        self.assertEqual(normalize("Chapter 1: Intro"), "chapter 1 intro")


# ===========================================================================
# parse_input()
# ===========================================================================


class TestParseInput(unittest.TestCase):
    def _write_and_parse(self, content: str) -> list[BookQuery]:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8"
        ) as f:
            f.write(content)
            f.flush()
            path = f.name
        try:
            return parse_input(path)
        finally:
            os.unlink(path)

    def test_simple_titles(self):
        queries = self._write_and_parse("Moby Dick\nWar and Peace\n")
        self.assertEqual(len(queries), 2)
        self.assertEqual(queries[0].title, "Moby Dick")
        self.assertIsNone(queries[0].author)

    def test_title_with_author(self):
        queries = self._write_and_parse("Moby Dick | Herman Melville\n")
        self.assertEqual(queries[0].title, "Moby Dick")
        self.assertEqual(queries[0].author, "Herman Melville")

    def test_blank_lines_skipped(self):
        queries = self._write_and_parse("Title One\n\n\nTitle Two\n")
        self.assertEqual(len(queries), 2)

    def test_comments_skipped(self):
        queries = self._write_and_parse("# This is a comment\nReal Title\n")
        self.assertEqual(len(queries), 1)
        self.assertEqual(queries[0].title, "Real Title")

    def test_empty_file(self):
        queries = self._write_and_parse("")
        self.assertEqual(len(queries), 0)

    def test_pipe_with_empty_author(self):
        queries = self._write_and_parse("Some Title | \n")
        self.assertEqual(queries[0].title, "Some Title")
        self.assertIsNone(queries[0].author)

    def test_unicode_title(self):
        queries = self._write_and_parse("Les Misérables | Victor Hugo\n")
        self.assertEqual(queries[0].title, "Les Misérables")
        self.assertEqual(queries[0].author, "Victor Hugo")

    def test_multiple_pipes(self):
        queries = self._write_and_parse("Title | Author | Extra\n")
        self.assertEqual(queries[0].title, "Title")
        self.assertEqual(queries[0].author, "Author | Extra")

    def test_empty_title_skipped(self):
        """'| Author' with no title should be skipped."""
        queries = self._write_and_parse("| Some Author\n")
        self.assertEqual(len(queries), 0)

    def test_only_pipe_skipped(self):
        queries = self._write_and_parse(" | \n")
        self.assertEqual(len(queries), 0)

    def test_bom_handled(self):
        """UTF-8 BOM should be handled transparently."""
        with tempfile.NamedTemporaryFile(
            mode="wb", suffix=".txt", delete=False
        ) as f:
            f.write(b"\xef\xbb\xbfMoby Dick\n")
            path = f.name
        try:
            queries = parse_input(path)
            self.assertEqual(len(queries), 1)
            self.assertEqual(queries[0].title, "Moby Dick")
        finally:
            os.unlink(path)

    def test_nonexistent_file_exits(self):
        with self.assertRaises(SystemExit):
            parse_input("/nonexistent/path/file.txt")


# ===========================================================================
# score_candidate()
# ===========================================================================


class TestScoreCandidate(unittest.TestCase):
    def test_exact_title_match(self):
        query = BookQuery(title="War and Peace")
        candidate = {"title": "War and Peace", "creator": ""}
        score = score_candidate(query, candidate)
        self.assertEqual(score, 100.0)

    def test_partial_title(self):
        query = BookQuery(title="War and Peace")
        candidate = {"title": "War and Peace: A Novel by Tolstoy", "creator": ""}
        score = score_candidate(query, candidate)
        self.assertGreater(score, 80)

    def test_author_bonus(self):
        query = BookQuery(title="Meditations", author="Marcus Aurelius")
        candidate_right = {"title": "Meditations", "creator": "Marcus Aurelius"}
        candidate_wrong = {"title": "Meditations", "creator": "John Smith"}
        score_right = score_candidate(query, candidate_right)
        score_wrong = score_candidate(query, candidate_wrong)
        self.assertGreater(score_right, score_wrong)
        self.assertLessEqual(score_right, 115.0)

    def test_no_author_no_bonus(self):
        query = BookQuery(title="Meditations")
        candidate = {"title": "Meditations", "creator": "Marcus Aurelius"}
        score = score_candidate(query, candidate)
        self.assertEqual(score, 100.0)

    def test_creator_as_list(self):
        query = BookQuery(title="Some Book", author="Alice")
        candidate = {"title": "Some Book", "creator": ["Alice", "Bob"]}
        score = score_candidate(query, candidate)
        self.assertGreater(score, 100)

    def test_missing_title(self):
        query = BookQuery(title="War and Peace")
        candidate = {"title": "", "creator": ""}
        score = score_candidate(query, candidate)
        self.assertEqual(score, 0.0)

    def test_missing_creator_key(self):
        query = BookQuery(title="Test", author="Author")
        candidate = {"title": "Test"}
        score = score_candidate(query, candidate)
        self.assertEqual(score, 100.0)


# ===========================================================================
# find_best_match()
# ===========================================================================


class TestFindBestMatch(unittest.TestCase):
    def test_selects_best(self):
        query = BookQuery(title="Moby Dick")
        results = [
            {"identifier": "mobydick01", "title": "Moby Dick", "creator": "Melville", "downloads": 100},
            {"identifier": "mobydick02", "title": "Moby Dick or The Whale", "creator": "Melville", "downloads": 50},
            {"identifier": "random", "title": "Some Other Book", "creator": "Nobody", "downloads": 10},
        ]
        match = find_best_match(query, results, threshold=75)
        self.assertIsNotNone(match)
        self.assertEqual(match["identifier"], "mobydick01")

    def test_below_threshold(self):
        query = BookQuery(title="Nonexistent Book Title XYZ123")
        results = [
            {"identifier": "abc", "title": "Totally Different", "creator": "", "downloads": 0},
        ]
        match = find_best_match(query, results, threshold=75)
        self.assertIsNone(match)

    def test_empty_results(self):
        match = find_best_match(BookQuery(title="Anything"), [], threshold=75)
        self.assertIsNone(match)

    def test_filters_missing_identifier(self):
        query = BookQuery(title="Test")
        results = [
            {"identifier": "", "title": "Test", "creator": "", "downloads": 0},
            {"identifier": "valid", "title": "Test", "creator": "", "downloads": 0},
        ]
        match = find_best_match(query, results, threshold=50)
        self.assertIsNotNone(match)
        self.assertEqual(match["identifier"], "valid")

    def test_filters_missing_title(self):
        query = BookQuery(title="Test")
        results = [{"identifier": "id1", "title": "", "creator": "", "downloads": 0}]
        match = find_best_match(query, results, threshold=50)
        self.assertIsNone(match)

    def test_runner_up_tracked(self):
        query = BookQuery(title="Moby Dick")
        results = [
            {"identifier": "a", "title": "Moby Dick", "creator": "", "downloads": 100},
            {"identifier": "b", "title": "Moby Dick Illustrated", "creator": "", "downloads": 50},
        ]
        match = find_best_match(query, results, threshold=50)
        self.assertIsNotNone(match)
        self.assertIn("runner_up_score", match)
        self.assertGreater(match["runner_up_score"], 0)

    def test_tiebreaker_by_downloads(self):
        query = BookQuery(title="Test Book")
        results = [
            {"identifier": "low", "title": "Test Book", "creator": "", "downloads": 10},
            {"identifier": "high", "title": "Test Book", "creator": "", "downloads": 1000},
        ]
        match = find_best_match(query, results, threshold=50)
        self.assertIsNotNone(match)
        self.assertEqual(match["identifier"], "high")

    def test_author_aware_disambiguation(self):
        query = BookQuery(title="Meditations", author="Marcus Aurelius")
        results = [
            {"identifier": "wrong", "title": "Meditations on Cooking", "creator": "Chef Bob", "downloads": 5000},
            {"identifier": "right", "title": "Meditations", "creator": "Marcus Aurelius", "downloads": 100},
        ]
        match = find_best_match(query, results, threshold=50)
        self.assertIsNotNone(match)
        self.assertEqual(match["identifier"], "right")

    def test_creator_list_in_result(self):
        query = BookQuery(title="Test")
        results = [
            {"identifier": "id1", "title": "Test", "creator": ["Author A", "Author B"], "downloads": 0},
        ]
        match = find_best_match(query, results, threshold=50)
        self.assertIsNotNone(match)
        self.assertIn("Author A", match["creator"])

    def test_string_downloads_no_crash(self):
        """downloads returned as string should not crash the sort."""
        query = BookQuery(title="Test")
        results = [
            {"identifier": "id1", "title": "Test", "creator": "", "downloads": "123"},
            {"identifier": "id2", "title": "Test", "creator": "", "downloads": None},
        ]
        match = find_best_match(query, results, threshold=50)
        self.assertIsNotNone(match)


# ===========================================================================
# search_ia() — mocked
# ===========================================================================


class TestSearchIA(unittest.TestCase):
    @patch("ia_books_to_gdrive.ia.search_items")
    @patch("ia_books_to_gdrive.time.sleep")
    def test_returns_results_on_success(self, mock_sleep, mock_search):
        mock_search.return_value = iter([
            {"identifier": "book1", "title": "Test Book", "creator": "Author", "downloads": 42},
        ])
        results, ok = search_ia(BookQuery(title="Test Book"))
        self.assertTrue(ok)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["identifier"], "book1")
        self.assertEqual(results[0]["downloads"], 42)

    @patch("ia_books_to_gdrive.ia.search_items")
    @patch("ia_books_to_gdrive.time.sleep")
    def test_returns_false_on_exception(self, mock_sleep, mock_search):
        mock_search.side_effect = Exception("API down")
        results, ok = search_ia(BookQuery(title="Test"))
        self.assertFalse(ok)
        self.assertEqual(results, [])

    @patch("ia_books_to_gdrive.ia.search_items")
    @patch("ia_books_to_gdrive.time.sleep")
    def test_coerces_string_downloads(self, mock_sleep, mock_search):
        mock_search.return_value = iter([
            {"identifier": "b1", "title": "T", "creator": "", "downloads": "999"},
        ])
        results, ok = search_ia(BookQuery(title="T"))
        self.assertTrue(ok)
        self.assertEqual(results[0]["downloads"], 999)

    @patch("ia_books_to_gdrive.ia.search_items")
    @patch("ia_books_to_gdrive.time.sleep")
    def test_coerces_none_downloads(self, mock_sleep, mock_search):
        mock_search.return_value = iter([
            {"identifier": "b1", "title": "T", "creator": "", "downloads": None},
        ])
        results, ok = search_ia(BookQuery(title="T"))
        self.assertTrue(ok)
        self.assertEqual(results[0]["downloads"], 0)


# ===========================================================================
# download_book() — mocked
# ===========================================================================


class TestDownloadBook(unittest.TestCase):
    def _make_mock_file(self, name="book.pdf", fmt="Text PDF"):
        mock_file = MagicMock()
        mock_file.format = fmt
        mock_file.name = name
        mock_file.download = MagicMock()
        return mock_file

    @patch("ia_books_to_gdrive.ia.get_item")
    @patch("ia_books_to_gdrive.time.sleep")
    def test_no_matching_format(self, mock_sleep, mock_get_item):
        mock_item = MagicMock()
        mock_file = self._make_mock_file(fmt="DjVu")
        mock_item.get_files.return_value = [mock_file]
        mock_get_item.return_value = mock_item

        path, status = download_book("test_id", "/tmp/test_dl")
        self.assertIsNone(path)
        self.assertEqual(status, "no_downloadable_file")

    @patch("ia_books_to_gdrive.ia.get_item")
    @patch("ia_books_to_gdrive.time.sleep")
    def test_get_item_raises(self, mock_sleep, mock_get_item):
        mock_get_item.side_effect = Exception("Network error")

        path, status = download_book("bad_id", "/tmp/test_dl")
        self.assertIsNone(path)
        self.assertEqual(status, "download_failed")

    @patch("ia_books_to_gdrive.ia.get_item")
    @patch("ia_books_to_gdrive.time.sleep")
    def test_successful_download(self, mock_sleep, mock_get_item):
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_file = self._make_mock_file()
            mock_item = MagicMock()
            mock_item.get_files.return_value = [mock_file]
            mock_get_item.return_value = mock_item

            # Simulate file being written by download()
            def fake_download(destdir=None):
                dl_dir = os.path.join(destdir, "test_id")
                os.makedirs(dl_dir, exist_ok=True)
                with open(os.path.join(dl_dir, "book.pdf"), "w") as f:
                    f.write("fake pdf content")

            mock_file.download = fake_download

            path, status = download_book("test_id", tmpdir, max_retries=1)
            self.assertIsNotNone(path)
            self.assertEqual(status, "ok")
            self.assertTrue(os.path.exists(path))

    @patch("ia_books_to_gdrive.ia.get_item")
    @patch("ia_books_to_gdrive.time.sleep")
    def test_download_exception_retries(self, mock_sleep, mock_get_item):
        mock_file = self._make_mock_file()
        mock_file.download = MagicMock(side_effect=Exception("timeout"))
        mock_item = MagicMock()
        mock_item.get_files.return_value = [mock_file]
        mock_get_item.return_value = mock_item

        path, status = download_book("test_id", "/tmp/test_dl", max_retries=2)
        self.assertIsNone(path)
        self.assertEqual(status, "download_failed")
        self.assertEqual(mock_file.download.call_count, 2)


# ===========================================================================
# _sanitize_csv()
# ===========================================================================


class TestSanitizeCSV(unittest.TestCase):
    def test_normal_string_unchanged(self):
        self.assertEqual(_sanitize_csv("Hello"), "Hello")

    def test_equals_prefixed(self):
        self.assertEqual(_sanitize_csv("=cmd()"), "'=cmd()")

    def test_plus_prefixed(self):
        self.assertEqual(_sanitize_csv("+1234"), "'+1234")

    def test_minus_prefixed(self):
        self.assertEqual(_sanitize_csv("-formula"), "'-formula")

    def test_at_prefixed(self):
        self.assertEqual(_sanitize_csv("@import"), "'@import")

    def test_empty_string(self):
        self.assertEqual(_sanitize_csv(""), "")


# ===========================================================================
# make_row()
# ===========================================================================


class TestMakeRow(unittest.TestCase):
    def test_success_row(self):
        query = BookQuery(title="War and Peace", author="Tolstoy")
        match = {
            "identifier": "wap01",
            "title": "War and Peace",
            "creator": "Leo Tolstoy",
            "score": 95.0,
            "runner_up_score": 60.0,
        }
        row = make_row(query, match=match, status="success", drive_id="xyz123", file_path="/tmp/wap.pdf")
        self.assertEqual(row["query_title"], "War and Peace")
        self.assertEqual(row["query_author"], "Tolstoy")
        self.assertEqual(row["status"], "success")
        self.assertEqual(row["drive_file_id"], "xyz123")

    def test_no_match_row(self):
        query = BookQuery(title="Unknown Book")
        row = make_row(query, status="no_results")
        self.assertEqual(row["matched_title"], "")
        self.assertEqual(row["status"], "no_results")

    def test_all_report_columns_present(self):
        query = BookQuery(title="Test")
        row = make_row(query, status="test")
        for col in REPORT_COLUMNS:
            self.assertIn(col, row)

    def test_formula_injection_sanitized(self):
        query = BookQuery(title="=HYPERLINK()")
        row = make_row(query, status="test")
        self.assertTrue(row["query_title"].startswith("'"))


# ===========================================================================
# write_report()
# ===========================================================================


class TestWriteReport(unittest.TestCase):
    def test_writes_csv(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            results = [
                {
                    "query_title": "Test",
                    "query_author": "",
                    "matched_title": "Test Book",
                    "matched_creator": "Author",
                    "match_score": 95,
                    "runner_up_score": 60,
                    "ia_identifier": "test01",
                    "local_file": "/tmp/test.pdf",
                    "status": "success",
                    "drive_file_id": "abc123",
                },
            ]
            path = write_report(results, tmpdir)
            self.assertTrue(os.path.exists(path))

            with open(path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                rows = list(reader)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["query_title"], "Test")

    def test_empty_results(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = write_report([], tmpdir)
            self.assertTrue(os.path.exists(path))
            with open(path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                rows = list(reader)
            self.assertEqual(len(rows), 0)


# ===========================================================================
# preflight()
# ===========================================================================


class TestPreflight(unittest.TestCase):
    def test_dry_run_skips_auth(self):
        from ia_books_to_gdrive import preflight

        with tempfile.TemporaryDirectory() as tmpdir:
            result = preflight(
                credentials_path="nonexistent.json",
                token_path="nonexistent_token.json",
                output_dir=tmpdir,
                folder_id=None,
                dry_run=True,
            )
            self.assertIsNone(result)

    def test_non_dry_run_requires_credentials(self):
        from ia_books_to_gdrive import preflight

        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(SystemExit):
                preflight(
                    credentials_path="definitely_nonexistent_creds.json",
                    token_path="token.json",
                    output_dir=tmpdir,
                    folder_id=None,
                    dry_run=False,
                )


if __name__ == "__main__":
    unittest.main()
