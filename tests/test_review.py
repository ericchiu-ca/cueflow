import unittest
from pathlib import Path

from subflow.core import SubtitleSegment
from subflow.review import (
    audit_review_segments,
    audit_review_track,
    review_payload,
    review_summary,
    segments_from_review_payload,
)


class TestSubtitleReview(unittest.TestCase):
    def setUp(self):
        self.segments = [
            SubtitleSegment("0001", 0.0, 0.5, "Short", []),
            SubtitleSegment("0002", 0.4, 2.0, "Short", []),
            SubtitleSegment("0003", 2.1, 4.0, "A" * 95, []),
        ]

    def test_audit_flags_timing_length_and_repetition(self):
        issues = audit_review_segments(self.segments)
        self.assertIn("SHORT_DURATION", {issue.code for issue in issues["0001"]})
        self.assertIn("OVERLAP", {issue.code for issue in issues["0002"]})
        self.assertIn("REPEATED_TEXT", {issue.code for issue in issues["0002"]})
        self.assertIn("LONG_TEXT", {issue.code for issue in issues["0003"]})

    def test_editor_audit_is_lenient_and_positional(self):
        issues = audit_review_track(
            [
                {"id": "0001", "start": 0, "end": "", "text": "Hi"},
                {"id": "0001", "start": 1, "end": 2, "text": "  "},
                {"id": "0003", "start": 3, "end": 4, "text": "same  words"},
                {"id": "0004", "start": 5, "end": 6, "text": "Same words"},
                {"id": "0005", "start": 7, "end": 8, "text": "长" * 46},
            ],
            "chinese",
        )
        codes = [{issue["code"] for issue in item} for item in issues]
        self.assertIn("INVALID_TIME", codes[0])  # blank end is NaN, not a crash
        self.assertIn("EMPTY_TEXT", codes[1])  # duplicate ID keeps its own slot
        self.assertIn("REPEATED_TEXT", codes[3])  # whitespace-insensitive
        self.assertIn("LONG_TEXT", codes[4])  # Chinese budget is 45
        with self.assertRaisesRegex(ValueError, "source or chinese"):
            audit_review_track([], "other")

    def test_web_ui_does_not_reimplement_review_rules(self):
        html = (Path(__file__).resolve().parents[1] / "subflow" / "static" / "index.html").read_text(
            encoding="utf-8"
        )
        for code in ("EMPTY_TEXT", "INVALID_TIME", "SHORT_DURATION", "LONG_TEXT", "OVERLAP", "REPEATED_TEXT"):
            self.assertNotIn(code, html)
        self.assertIn("/api/review/audit", html)

    def test_review_payload_round_trip_preserves_ids_and_edits(self):
        payload = review_payload(self.segments, {"0001": True})
        payload[1]["text"] = "Revised subtitle"
        restored, reviewed = segments_from_review_payload(
            payload,
            [segment.id for segment in self.segments],
        )
        self.assertEqual(restored[1].text, "Revised subtitle")
        self.assertTrue(reviewed["0001"])
        self.assertEqual(review_summary(payload)["reviewed"], 1)

    def test_review_save_rejects_removed_or_empty_segments(self):
        payload = review_payload(self.segments)
        with self.assertRaisesRegex(ValueError, "count changed"):
            segments_from_review_payload(payload[:-1], [segment.id for segment in self.segments])
        payload[0]["text"] = ""
        with self.assertRaisesRegex(ValueError, "empty subtitle text"):
            segments_from_review_payload(payload, [segment.id for segment in self.segments])

    def test_review_structure_mode_allows_delete_and_new_stable_id(self):
        payload = review_payload(self.segments)
        payload.pop(1)
        payload.append(
            {
                "id": "0004",
                "start": 4.1,
                "end": 5.5,
                "text": "New subtitle",
                "reviewed": False,
            }
        )
        restored, _ = segments_from_review_payload(
            payload,
            [segment.id for segment in self.segments],
            allow_structure_changes=True,
        )
        self.assertEqual([segment.id for segment in restored], ["0001", "0003", "0004"])


if __name__ == "__main__":
    unittest.main()
