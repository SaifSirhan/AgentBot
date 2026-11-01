"""Tests for hybrid RAG search: SQLite FTS5 indexing and RRF fusion.

Run: python -m unittest tests.test_rag_hybrid -v
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import rag_tool  # noqa: E402


class RRFTests(unittest.TestCase):
    def test_default_k_is_sixty(self):
        self.assertEqual(rag_tool.RRF_K, 60)

    def test_single_list_scores_follow_rank(self):
        scores = rag_tool.rrf_scores([["a", "b", "c"]])
        self.assertAlmostEqual(scores["a"], 1 / 61)
        self.assertAlmostEqual(scores["b"], 1 / 62)
        self.assertAlmostEqual(scores["c"], 1 / 63)

    def test_document_in_both_lists_sums_both_contributions(self):
        scores = rag_tool.rrf_scores([["a", "b"], ["b", "c"]])
        self.assertAlmostEqual(scores["a"], 1 / 61)
        self.assertAlmostEqual(scores["b"], 1 / 62 + 1 / 61)
        self.assertAlmostEqual(scores["c"], 1 / 62)

    def test_both_list_hit_outranks_every_single_hit(self):
        scores = rag_tool.rrf_scores([["a", "b"], ["b", "c"]])
        self.assertGreater(scores["b"], scores["a"])
        self.assertGreater(scores["b"], scores["c"])

    def test_custom_k_changes_the_weights(self):
        scores = rag_tool.rrf_scores([["a"], ["a"]], k=10)
        self.assertAlmostEqual(scores["a"], 2 / 11)

    def test_empty_rankings_score_nothing(self):
        self.assertEqual(rag_tool.rrf_scores([[], []]), {})


class FTSIndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rag_fts_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.fts = rag_tool.FTSIndex(str(self.tmp / "fts.sqlite3"))
        self.addCleanup(self.fts.close)

    def test_indexes_and_returns_exact_ids(self):
        source = r"C:\docs\invoice.txt"
        self.fts.replace_source(
            source, [("invoice::0", "Invoice number ABC-123 is due Friday")])

        hits = self.fts.search("ABC-123")

        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["id"], "invoice::0")
        self.assertEqual(hits[0]["source"], source)
        self.assertIn("Invoice number", hits[0]["text"])

    def test_searches_filenames_as_well_as_chunks(self):
        self.fts.replace_source(
            r"C:\docs\Q4_report_2026.pdf",
            [("report::0", "Revenue grew twelve percent")])
        self.fts.replace_source(
            r"C:\docs\notes.txt",
            [("notes::0", "Unrelated grocery list")])

        hits = self.fts.search("Q4_report_2026.pdf")

        self.assertTrue(hits)
        self.assertEqual(hits[0]["id"], "report::0")

    def test_replacing_a_source_drops_stale_chunks(self):
        self.fts.replace_source("doc.txt", [("old::0", "alpha uniqueterm")])
        self.fts.replace_source("doc.txt", [("new::0", "beta otherterm")])

        self.assertEqual(self.fts.search("uniqueterm"), [])
        hits = self.fts.search("otherterm")
        self.assertEqual([hit["id"] for hit in hits], ["new::0"])
        self.assertEqual(self.fts.count(), 1)

    def test_delete_source_removes_only_that_file(self):
        self.fts.replace_source("a.txt", [("a::0", "alpha marker")])
        self.fts.replace_source("b.txt", [("b::0", "beta marker")])

        self.fts.delete_source("a.txt")

        self.assertEqual(self.fts.search("alpha"), [])
        self.assertEqual(
            [hit["id"] for hit in self.fts.search("beta")], ["b::0"])
        self.assertEqual(self.fts.count(), 1)

    def test_query_operators_are_treated_as_plain_words(self):
        self.fts.replace_source("a.txt", [("a::0", "safe content")])

        hits = self.fts.search('"; DROP TABLE chunks; --')

        self.assertIsInstance(hits, list)
        self.assertEqual(self.fts.count(), 1)

    def test_empty_query_returns_nothing(self):
        self.fts.replace_source("a.txt", [("a::0", "alpha")])
        self.assertEqual(self.fts.search("   !!!   "), [])

    def test_count_and_bulk_insert(self):
        rows = [
            ("a::0", "a.txt", "alpha content"),
            ("b::0", "b.txt", "beta content"),
        ]
        self.fts.bulk_insert(rows)
        self.assertEqual(self.fts.count(), 2)
        self.assertEqual(
            [hit["id"] for hit in self.fts.search("beta")], ["b::0"])


class FakeEncoder:
    def encode(self, texts, show_progress_bar=False):
        return _VectorList([[0.1, 0.2] for _ in texts])


class _VectorList(list):
    def tolist(self):
        return list(self)


class FakeCollection:
    def __init__(self, ids, documents, sources, count=None):
        self.ids = ids
        self.documents = documents
        self.sources = sources
        self.count_value = len(ids) if count is None else count
        self.query_calls = []

    def count(self):
        return self.count_value

    def query(self, query_embeddings, n_results):
        self.query_calls.append(n_results)
        return {
            "ids": [self.ids],
            "documents": [self.documents],
            "metadatas": [[{"source": s} for s in self.sources]],
        }


class RecordingCollection(FakeCollection):
    def __init__(self):
        super().__init__([], [], [])
        self.upserts = []
        self.deletes = []

    def delete(self, where=None):
        self.deletes.append(where)

    def upsert(self, ids, documents, embeddings, metadatas):
        self.ids = list(ids)
        self.documents = list(documents)
        self.sources = [m["source"] for m in metadatas]
        self.count_value = len(ids)
        self.upserts.append({
            "ids": list(ids),
            "documents": list(documents),
            "embeddings": embeddings,
            "metadatas": list(metadatas),
        })


class HybridSearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rag_hybrid_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.fts = rag_tool.FTSIndex(str(self.tmp / "fts.sqlite3"))
        self.addCleanup(self.fts.close)

    def make_rag(self, ids, documents, sources, count=None):
        rag = rag_tool.RAGTool.__new__(rag_tool.RAGTool)
        rag.model = FakeEncoder()
        rag.collection = FakeCollection(
            ids, documents, sources, count=count)
        rag.fts = self.fts
        return rag

    def test_chunk_in_both_lists_is_boosted_and_deduplicated(self):
        rag = self.make_rag(
            ["a", "b"], ["alpha text", "shared text"], ["a.txt", "b.txt"])
        self.fts.replace_source(
            "b.txt", [("b", "shared keyword chunk")])
        self.fts.replace_source(
            "c.txt", [("c", "keyword chunk three")])

        output = rag.search("keyword", top_k=3)

        self.assertLess(output.index("[From: b.txt]"), output.index("[From: a.txt]"))
        self.assertLess(output.index("[From: a.txt]"), output.index("[From: c.txt]"))
        self.assertEqual(output.count("shared text"), 1)

    def test_keyword_only_hits_are_returned(self):
        rag = self.make_rag(["a"], ["alpha text"], ["a.txt"])
        self.fts.replace_source("z.txt", [("z", "exact ID ZX-99")])

        output = rag.search("ZX-99", top_k=8)

        self.assertIn("[From: z.txt]", output)
        self.assertIn("exact ID ZX-99", output)

    def test_search_documents_returns_the_fused_results(self):
        rag = self.make_rag(
            ["a", "b"], ["alpha text", "shared text"], ["a.txt", "b.txt"])
        self.fts.replace_source("b.txt", [("b", "shared keyword chunk")])

        with mock.patch.object(rag_tool, "get_rag", return_value=rag):
            output = rag_tool.search_documents("keyword")

        self.assertLess(
            output.index("[From: b.txt]"), output.index("[From: a.txt]"))

    def test_no_documents_indexed_message(self):
        rag = self.make_rag([], [], [], count=0)

        self.assertEqual(
            rag.search("anything"), "No documents indexed yet.")

    def test_no_hits_message(self):
        rag = self.make_rag([], [], [], count=1)

        self.assertEqual(
            rag.search("missingterm"), "No relevant information found.")

    def test_index_file_writes_the_same_chunks_to_chroma_and_fts(self):
        rag = rag_tool.RAGTool.__new__(rag_tool.RAGTool)
        rag.model = FakeEncoder()
        rag.collection = RecordingCollection()
        rag.fts = self.fts
        path = self.tmp / "report_ABC.txt"
        path.write_text(
            "Project codename NEEDLE42 is due Friday. " * 4,
            encoding="utf-8")

        result = rag.index_file(str(path))

        self.assertTrue(result.startswith("Indexed"), result)
        self.assertEqual(len(rag.collection.upserts), 1)
        upsert = rag.collection.upserts[0]
        self.assertEqual(self.fts.count(), len(upsert["ids"]))

        hits = self.fts.search("NEEDLE42")
        self.assertEqual([hit["id"] for hit in hits], upsert["ids"])
        self.assertIn(hits[0]["text"], upsert["documents"])

    def test_filename_is_searchable_after_indexing(self):
        rag = rag_tool.RAGTool.__new__(rag_tool.RAGTool)
        rag.model = FakeEncoder()
        rag.collection = RecordingCollection()
        rag.fts = self.fts
        path = self.tmp / "budget_2026_final.txt"
        path.write_text("Quarterly numbers are attached.", encoding="utf-8")

        rag.index_file(str(path))
        hits = self.fts.search("budget_2026_final.txt")

        self.assertTrue(hits)
        self.assertIn("budget_2026_final.txt", hits[0]["source"])

    def test_default_chunks_are_wide_with_a_500_char_overlap(self):
        rag = rag_tool.RAGTool.__new__(rag_tool.RAGTool)
        text = "".join(chr(ord("a") + index % 26) for index in range(7000))

        chunks = rag._chunk(text)

        self.assertEqual(len(chunks), 3)
        self.assertEqual([len(chunk) for chunk in chunks], [3000, 3000, 2000])
        self.assertEqual(chunks[0][-500:], chunks[1][:500])
        self.assertEqual(chunks[1][-500:], chunks[2][:500])

    def test_chat_export_uses_the_wide_chunks_too(self):
        rag = rag_tool.RAGTool.__new__(rag_tool.RAGTool)
        rag.model = FakeEncoder()
        rag.collection = RecordingCollection()
        rag.fts = self.fts
        path = self.tmp / "PrivateExport" / "messages.html"
        path.parent.mkdir(parents=True)
        path.write_text("love you " * 500, encoding="utf-8")

        result = rag.index_file(str(path))

        self.assertTrue(result.startswith("Indexed"), result)
        documents = rag.collection.upserts[0]["documents"]
        self.assertEqual(len(documents), 2)
        self.assertTrue(all(len(chunk) <= 3000 for chunk in documents))
        self.assertTrue(all("love you" in chunk for chunk in documents))

    def test_version_bump_reindexes_unchanged_files_once(self):
        folder = self.tmp / "docs"
        folder.mkdir()
        path = folder / "note.txt"
        path.write_text("alpha memory", encoding="utf-8")
        state_path = self.tmp / "state.json"
        stat = path.stat()
        state_path.write_text(json.dumps({
            str(path): {"mtime": stat.st_mtime, "size": stat.st_size},
            "__index_version__": 1,
        }), encoding="utf-8")

        rag = rag_tool.RAGTool.__new__(rag_tool.RAGTool)
        rag.model = FakeEncoder()
        rag.collection = RecordingCollection()
        rag.fts = self.fts

        with mock.patch.object(rag_tool, "STATE_FILE", str(state_path)):
            first = rag.index_folder(str(folder))
            second = rag.index_folder(str(folder))

        self.assertIn("Indexed: 1", first)
        self.assertIn("Indexed: 0", second)
        self.assertEqual(len(rag.collection.upserts), 1)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(
            state["__index_version__"], rag_tool.INDEX_VERSION)

    def test_search_documents_defaults_to_five_results(self):
        rag = self.make_rag(
            [f"a{index}" for index in range(8)],
            [f"keyword text {index}" for index in range(8)],
            [f"doc{index}.txt" for index in range(8)])

        with mock.patch.object(rag_tool, "get_rag", return_value=rag):
            output = rag_tool.search_documents("keyword")

        self.assertEqual(output.count("[From: "), 5)
        self.assertEqual(rag.collection.query_calls, [10])

    def test_search_documents_accepts_an_explicit_top_k(self):
        rag = self.make_rag(
            [f"a{index}" for index in range(8)],
            [f"keyword text {index}" for index in range(8)],
            [f"doc{index}.txt" for index in range(8)])

        with mock.patch.object(rag_tool, "get_rag", return_value=rag):
            output = rag_tool.search_documents("keyword", top_k=2)

        self.assertEqual(output.count("[From: "), 2)
        self.assertEqual(rag.collection.query_calls, [4])


class LargeExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rag_large_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.fts = rag_tool.FTSIndex(str(self.tmp / "fts.sqlite3"))
        self.addCleanup(self.fts.close)

    def make_rag(self):
        rag = rag_tool.RAGTool.__new__(rag_tool.RAGTool)
        rag.model = FakeEncoder()
        rag.collection = RecordingCollection()
        rag.fts = self.fts
        return rag

    def test_five_megabyte_telegram_json_is_streamed_not_skipped(self):
        path = self.tmp / "result.json"
        payload = "love you " * 55
        messages = [{"from": "Moss", "text": "ZXOPENINGMARKER"}]
        for index in range(12000):
            messages.append({
                "from": f"User{index % 20}",
                "text": [{"type": "plain", "text": f"{payload}{index}"}],
            })
        messages.append({"from": "Moss", "text": "ZXCLOSINGMARKER"})
        path.write_text(
            json.dumps({"messages": messages}), encoding="utf-8")
        self.assertGreater(path.stat().st_size, 5_000_000)

        rag = self.make_rag()
        result = rag.index_file(str(path))

        self.assertTrue(result.startswith("Indexed"), result)
        self.assertGreater(len(rag.collection.upserts), 1)
        total = sum(len(upsert["ids"]) for upsert in rag.collection.upserts)
        self.assertGreater(total, 100)
        self.assertEqual(total, self.fts.count())
        self.assertEqual(
            len({chunk_id
                 for upsert in rag.collection.upserts
                 for chunk_id in upsert["ids"]}),
            total)
        self.assertTrue(
            all(len(chunk) <= rag_tool.CHUNK_SIZE
                for upsert in rag.collection.upserts
                for chunk in upsert["documents"]))

        first_hits = self.fts.search("ZXOPENINGMARKER")
        last_hits = self.fts.search("ZXCLOSINGMARKER")
        self.assertTrue(first_hits)
        self.assertTrue(last_hits)
        self.assertIn("Moss: ZXOPENINGMARKER", first_hits[0]["text"])
        self.assertIn("Moss: ZXCLOSINGMARKER", last_hits[0]["text"])

    def test_telegram_text_accepts_strings_and_entity_lists(self):
        path = self.tmp / "result.json"
        path.write_text(json.dumps({"messages": [
            {"from": "Moss", "text": "plain hello"},
            {"from": "Ash", "text": [
                {"type": "plain", "text": "hello "},
                {"type": "bold", "text": "world"},
            ]},
            {"from": "Ash", "text": ["chunk one", " chunk two"]},
            {"type": "service", "text": ""},
            {"text": "no sender"},
        ]}), encoding="utf-8")

        rag = self.make_rag()
        result = rag.index_file(str(path))

        self.assertTrue(result.startswith("Indexed"), result)
        documents = "\n".join(rag.collection.upserts[0]["documents"])
        self.assertIn("Moss: plain hello", documents)
        self.assertIn("Ash: hello world", documents)
        self.assertIn("Ash: chunk one chunk two", documents)
        self.assertIn("unknown: no sender", documents)

    def test_large_html_export_is_parsed_and_indexed(self):
        path = self.tmp / "messages.html"
        paragraph = "<p>" + ("love you " * 220) + "</p>\n"
        repeats = (rag_tool.LARGE_HTML_BYTES // len(paragraph)) + 1
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("<html><body>")
            for _ in range(repeats):
                handle.write(paragraph)
            handle.write("<p>ZXHTMLENDMARKER</p></body></html>")
        self.assertGreater(path.stat().st_size, rag_tool.LARGE_HTML_BYTES)

        rag = self.make_rag()
        result = rag.index_file(str(path))

        self.assertTrue(result.startswith("Indexed"), result)
        self.assertGreater(len(rag.collection.upserts), 0)
        self.assertTrue(self.fts.search("ZXHTMLENDMARKER"))

    def test_folder_index_records_the_telegram_export_in_state(self):
        folder = self.tmp / "export"
        folder.mkdir()
        path = folder / "result.json"
        messages = [
            {"from": "Moss", "text": "ZXFOLDERMARKER " * 40}
            for _ in range(900)
        ]
        path.write_text(
            json.dumps({"messages": messages}), encoding="utf-8")
        self.assertGreater(len(path.read_text(encoding="utf-8")),
                           rag_tool.MAX_TEXT_CHARS)
        state_path = self.tmp / "state.json"

        rag = self.make_rag()
        with mock.patch.object(rag_tool, "STATE_FILE", str(state_path)):
            first = rag.index_folder(str(folder))
            second = rag.index_folder(str(folder))

        self.assertIn("Indexed: 1", first)
        self.assertIn("Indexed: 0", second)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(
            state["__index_version__"], rag_tool.INDEX_VERSION)
        self.assertIn(str(path), state)

    def test_malformed_telegram_json_skips_cleanly(self):
        path = self.tmp / "result.json"
        path.write_text("{not valid json", encoding="utf-8")

        rag = self.make_rag()
        result = rag.index_file(str(path))

        self.assertTrue(result.startswith("Skipped"), result)
        self.assertIn("JSON parse failed", result)
        self.assertEqual(rag.collection.upserts, [])

    def test_reindexing_replaces_the_previous_telegram_chunks(self):
        path = self.tmp / "result.json"
        path.write_text(json.dumps({"messages": [
            {"from": "A", "text": "ZXOLDMARKER"}]}), encoding="utf-8")
        rag = self.make_rag()
        rag.index_file(str(path))

        path.write_text(json.dumps({"messages": [
            {"from": "A", "text": "ZXNEWMARKER"}]}), encoding="utf-8")
        rag.index_file(str(path))

        self.assertEqual(self.fts.search("ZXOLDMARKER"), [])
        self.assertTrue(self.fts.search("ZXNEWMARKER"))
        self.assertEqual(self.fts.count(), 1)


class BackfillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rag_backfill_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.fts = rag_tool.FTSIndex(str(self.tmp / "fts.sqlite3"))
        self.addCleanup(self.fts.close)

    def test_existing_chroma_chunks_backfill_into_fts(self):
        class BackfillCollection:
            def count(self):
                return 2

            def get(self, include=None):
                return {
                    "ids": ["a::0", "b::0"],
                    "documents": ["alpha memory", "beta memory"],
                    "metadatas": [
                        {"source": "a.txt"}, {"source": "b.txt"}],
                }

        rag = rag_tool.RAGTool.__new__(rag_tool.RAGTool)
        rag.fts = self.fts
        rag.collection = BackfillCollection()
        rag._backfill_fts()

        self.assertEqual(self.fts.count(), 2)
        self.assertEqual(
            [hit["id"] for hit in self.fts.search("beta")], ["b::0"])

    def test_nonempty_fts_is_not_rebuilt(self):
        self.fts.replace_source("keep.txt", [("keep::0", "keep me")])

        class FailingCollection:
            def count(self):
                raise AssertionError("should not inspect Chroma when FTS has data")

            def get(self, include=None):
                raise AssertionError("should not backfill")

        rag = rag_tool.RAGTool.__new__(rag_tool.RAGTool)
        rag.fts = self.fts
        rag.collection = FailingCollection()
        rag._backfill_fts()

        self.assertEqual(self.fts.count(), 1)


if __name__ == "__main__":
    unittest.main()
