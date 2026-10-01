import json
import unittest

from spiders.source_adapters import CNInfoAdapter, CollectionTarget, SSEAdapter


class FakeHttpClient:
    def __init__(self, payload: bytes):
        self.payload = payload
        self.calls = []

    def post_form(self, url, form, *, referer=None):
        self.calls.append((url, form, referer))
        return self.payload


class SourceAdapterTests(unittest.TestCase):
    target = CollectionTarget("600519", 2023, 2023)

    def test_cninfo_filters_summary_and_english_versions(self):
        payload = {
            "hasMore": False,
            "announcements": [
                {
                    "announcementId": "1219506510",
                    "announcementTitle": "<em>贵州茅台</em>2023年年度报告",
                    "announcementTime": 1712073600000,
                    "adjunctUrl": "finalpage/2024-04-03/1219506510.PDF",
                },
                {
                    "announcementId": "1219506504",
                    "announcementTitle": "贵州茅台2023年年度报告摘要",
                    "announcementTime": 1712073600000,
                    "adjunctUrl": "finalpage/2024-04-03/1219506504.PDF",
                },
                {
                    "announcementId": "1219506497",
                    "announcementTitle": "贵州茅台2023年年度报告（英文版）",
                    "announcementTime": 1712073600000,
                    "adjunctUrl": "finalpage/2024-04-03/1219506497.PDF",
                },
            ],
        }
        client = FakeHttpClient(json.dumps(payload).encode())

        candidates = CNInfoAdapter(client).discover(self.target)

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].source_id, "1219506510")
        self.assertEqual(candidates[0].report_year, 2023)
        self.assertEqual(
            candidates[0].source_url,
            "https://static.cninfo.com.cn/finalpage/2024-04-03/1219506510.PDF",
        )

    def test_sse_parses_jsonp_and_filters_summary(self):
        payload = {
            "pageHelp": {"pageCount": 1},
            "result": [
                {
                    "BULLETIN_TYPE": "年报",
                    "TITLE": "贵州茅台2023年年度报告",
                    "SSEDATE": "2024-04-03",
                    "URL": "/disclosure/listedinfo/announcement/c/new/2024-04-03/600519_20240403_W0YD.pdf",
                },
                {
                    "BULLETIN_TYPE": "年报摘要",
                    "TITLE": "贵州茅台2023年年度报告摘要",
                    "SSEDATE": "2024-04-03",
                    "URL": "/disclosure/listedinfo/announcement/c/new/2024-04-03/600519_20240403_IU3U.pdf",
                },
            ],
        }
        client = FakeHttpClient(f"finDocGovCallback({json.dumps(payload)})".encode())

        candidates = SSEAdapter(client).discover(self.target)

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].source_name, "sse")
        self.assertEqual(candidates[0].publish_date, "2024-04-03")
        self.assertTrue(candidates[0].source_url.startswith("https://www.sse.com.cn/"))

    def test_cninfo_org_id_matches_market_specific_seven_digit_format(self):
        self.assertEqual(CNInfoAdapter._org_id("600519"), "gssh0600519")
        self.assertEqual(CNInfoAdapter._org_id("000858"), "gssz0000858")
        self.assertEqual(CNInfoAdapter._org_id("300750"), "gssz0300750")

    def test_auto_end_year_tracks_previous_report_year(self):
        target = CollectionTarget.from_mapping(
            {"stock_code": "600519", "start_year": 2022, "end_year": "auto"}
        )
        self.assertEqual(target.end_year, __import__("datetime").datetime.now().year - 1)


if __name__ == "__main__":
    unittest.main()
