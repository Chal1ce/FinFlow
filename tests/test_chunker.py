import unittest

from processing.cleaners import Block
from processing.chunker import chunk_blocks


def block(label, text, page=0, index=0, content_type="paragraph"):
    return Block(page, index, label, content_type, text)


class ChunkerTests(unittest.TestCase):
    lineage = {
        "governed_uid": "governed-1",
        "document_uid": "doc-1",
        "work_uid": "work-1",
        "asset_uid": "asset-1",
        "backend": "local",
    }

    def chunk(self, blocks):
        return chunk_blocks(
            blocks,
            governed_uid=self.lineage["governed_uid"],
            document_uid=self.lineage["document_uid"],
            work_uid=self.lineage["work_uid"],
            asset_uid=self.lineage["asset_uid"],
            backend=self.lineage["backend"],
            max_chars=2000,
            min_chars=80,
        )

    def test_heading_context_and_table_are_separate_chunks(self):
        blocks = [
            block("doc_title", "ESG and Firm Value", 0, 0, "heading"),
            block("text", "ESG improves firm value.", 0, 1),
            block("paragraph_title", "1. Introduction", 1, 2, "heading"),
            block("text", "This section reviews prior evidence.", 1, 3),
            block("table", "| A | B |\n| --- | --- |\n| 1 | 2 |", 2, 4, "table"),
            block("text", "The table shows the sample.", 2, 5),
        ]

        chunks = self.chunk(blocks)

        self.assertEqual(len(chunks), 4)
        self.assertEqual(chunks[0].content_type, "section")
        self.assertIn("ESG and Firm Value", chunks[0].title_context)
        self.assertEqual(chunks[1].title_context, "ESG and Firm Value > 1. Introduction")
        self.assertEqual(chunks[2].content_type, "table")
        self.assertEqual(chunks[2].page, 2)
        self.assertEqual(chunks[3].content_type, "section")
        self.assertEqual(chunks[3].title_context, "ESG and Firm Value > 1. Introduction")

    def test_long_section_is_split_at_sentence_boundaries(self):
        sentence = "Firm value increases when governance quality improves. "
        blocks = [
            block("doc_title", "Governance", 0, 0, "heading"),
            block("paragraph_title", "1. Analysis", 0, 1, "heading"),
            block("text", sentence * 45, 0, 2),
        ]

        chunks = self.chunk(blocks)

        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk.text) <= 2000 for chunk in chunks))
        self.assertTrue(all(chunk.title_context.endswith("1. Analysis") for chunk in chunks))

    def test_chunk_ids_and_offsets_are_stable(self):
        blocks = [
            block("doc_title", "Title", 0, 0, "heading"),
            block("text", "Body text.", 0, 1),
        ]

        chunks = self.chunk(blocks)
        repeated = self.chunk(blocks)

        self.assertEqual(chunks[0].chunk_uid, repeated[0].chunk_uid)
        self.assertEqual(chunks[0].char_start, 0)
        self.assertGreater(chunks[0].char_end, chunks[0].char_start)
        self.assertEqual(chunks[0].pages, (0,))

    def test_bbox_provenance_is_kept_on_chunks(self):
        blocks = [
            Block(0, 0, "doc_title", "heading", "Title", (0.0, 0.0, 10.0, 10.0)),
            Block(
                0,
                1,
                "table",
                "table",
                "| A | B |\n| --- | --- |\n| 1 | 2 |",
                (1.0, 2.0, 9.0, 8.0),
            ),
        ]

        chunks = self.chunk(blocks)

        self.assertEqual(chunks[0].bboxes, ((0.0, 0.0, 10.0, 10.0),))
        self.assertEqual(chunks[1].bboxes, ((1.0, 2.0, 9.0, 8.0),))

    def test_logical_chunk_id_survives_governed_version_change(self):
        blocks = [
            block("doc_title", "Title", 0, 0, "heading"),
            block("text", "Body text.", 0, 1),
        ]

        first = self.chunk(blocks)
        second = chunk_blocks(
            blocks,
            governed_uid="governed-2",
            document_uid=self.lineage["document_uid"],
            work_uid=self.lineage["work_uid"],
            asset_uid=self.lineage["asset_uid"],
            backend=self.lineage["backend"],
            max_chars=2000,
            min_chars=80,
        )

        self.assertEqual(first[0].chunk_id, second[0].chunk_id)
        self.assertNotEqual(first[0].chunk_version_uid, second[0].chunk_version_uid)
        self.assertEqual(first[0].chunk_uid, first[0].chunk_version_uid)


if __name__ == "__main__":
    unittest.main()
