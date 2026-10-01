import unittest

from processing.cleaners import (
    Block,
    apply_ocr_merge_fixes,
    clean_blocks,
    drop_reference_section,
    html_table_to_markdown,
    is_reference_section_heading,
    is_noise_line,
    normalize_text,
    render_governed_markdown,
)


class CleanerTests(unittest.TestCase):
    def test_html_table_becomes_markdown_with_header_separator(self):
        html = (
            "<table><tr><td>No.</td><td>Criteria</td></tr>"
            "<tr><td>1.</td><td>Listed companies</td></tr></table>"
        )
        markdown = html_table_to_markdown(html)

        self.assertIn("| No. | Criteria |", markdown)
        self.assertIn("| --- | --- |", markdown)
        self.assertIn("| 1. | Listed companies |", markdown)

    def test_header_footer_and_number_blocks_are_dropped(self):
        blocks = [
            Block(0, 0, "header", "noise", "Journal name"),
            Block(0, 1, "number", "noise", "12"),
            Block(0, 2, "doc_title", "heading", "ESG Report"),
            Block(0, 3, "text", "paragraph", "Firm value improves."),
            Block(0, 4, "footer", "noise", "Page 1"),
        ]

        result = clean_blocks(blocks)

        self.assertEqual([block.block_label for block in result.blocks], ["doc_title", "text"])
        self.assertEqual(result.stats["dropped_by_label"], 3)

    def test_unicode_whitespace_and_ocr_merges_are_normalized(self):
        text = "ESG\u200b performance\u00a0of\u200b the firm\u3000 and\u3000 governance"
        self.assertEqual(normalize_text(text), "ESG performance of the firm and governance")
        self.assertEqual(
            apply_ocr_merge_fixes("guidanceof managementofthe"),
            "guidance of management of the",
        )

    def test_repeated_paragraphs_are_deduplicated(self):
        text = "The same sentence appears twice."
        blocks = [
            Block(0, 0, "text", "paragraph", text),
            Block(0, 1, "text", "paragraph", text),
            Block(0, 2, "paragraph_title", "heading", "Conclusion"),
        ]

        result = clean_blocks(blocks)

        self.assertEqual(result.stats["duplicate_removed"], 1)
        self.assertEqual([block.text for block in result.blocks].count(text), 1)

    def test_governed_markdown_adds_heading_levels(self):
        blocks = [
            Block(0, 0, "doc_title", "heading", "Paper Title"),
            Block(0, 1, "paragraph_title", "heading", "1. Introduction"),
            Block(0, 2, "text", "paragraph", "Body text."),
        ]

        markdown = render_governed_markdown(blocks)

        self.assertIn("# Paper Title", markdown)
        self.assertIn("### 1. Introduction", markdown)
        self.assertIn("Body text.", markdown)

    def test_long_ocr_gibberish_lines_are_detected(self):
        self.assertTrue(is_noise_line("an sch enviofl dive Bosidenghaitwchaitabeouatioanluervicam"))
        self.assertTrue(
            is_noise_line("Boid com.Thisindgoocvavrctlornctlnacr perforl performance.")
        )
        self.assertFalse(is_noise_line("ESG performance improves firm value."))

    def test_markdown_tables_are_kept_even_when_alnum_ratio_is_low(self):
        result = clean_blocks(
            [
                Block(
                    0,
                    0,
                    "table",
                    "table",
                    "| A | B |\n| --- | --- |\n| 1 | 2 |",
                )
            ]
        )

        self.assertEqual(len(result.blocks), 1)

    def test_trailing_reference_section_is_removed_by_heading_not_block_label(self):
        blocks = [
            Block(0, 0, "paragraph_title", "heading", "Conclusion"),
            Block(0, 1, "text", "paragraph", "The governed body remains."),
            Block(1, 2, "text", "paragraph", "## 7. References"),
            Block(1, 3, "text", "paragraph", "[1] Source material."),
            Block(1, 4, "reference", "reference", "[2] Another source."),
        ]

        result = drop_reference_section(blocks)

        self.assertTrue(result.detected)
        self.assertEqual(result.removed_blocks, 3)
        self.assertEqual([block.text for block in result.blocks], ["Conclusion", "The governed body remains."])
        self.assertTrue(is_reference_section_heading("参考资料："))
        self.assertTrue(is_reference_section_heading("Bibliography"))
        self.assertFalse(is_reference_section_heading("References to data sources are provided below."))

    def test_reference_block_label_is_a_fallback_when_ocr_misses_heading(self):
        result = drop_reference_section(
            [
                Block(0, 0, "text", "paragraph", "Body content."),
                Block(1, 1, "reference", "reference", "[1] Source without a title."),
                Block(1, 2, "reference", "reference", "[2] Another source."),
            ]
        )

        self.assertTrue(result.detected)
        self.assertEqual(result.removed_blocks, 2)
        self.assertEqual([block.text for block in result.blocks], ["Body content."])


if __name__ == "__main__":
    unittest.main()
