import unittest
from unittest.mock import patch

from app.draw import ReverseDraw


class DynamicScheduleTests(unittest.TestCase):
    def test_future_rounds_follow_current_configuration(self):
        stored = {
            "schedule": {
                "total": 1000,
                "survivors": [500, 250, 100, 1],
                "labels": ["Old 1", "Old 2", "Old 3", "Old Final"],
            },
            "rounds": [
                {
                    "round": 1,
                    "label": "Old 1",
                    "timestamp": "2026-01-01T00:00:00+00:00",
                    "seed": "1",
                    "started_with": 1000,
                    "survivors": 500,
                    "eliminated": [],
                }
            ],
        }
        with (
            patch("app.config.TOTAL_TICKETS", 1000),
            patch("app.config.ROUND_SURVIVORS", [500, 301, 102, 89, 3]),
            patch(
                "app.config.ROUND_LABELS",
                ["Round 1", "Round 2", "Round 3", "Round 4", "Grand Prize"],
            ),
            patch("app.config.ROUND_KINDS", ["elimination"] * 5),
            patch("app.config.ROUND_PRIZES", [""] * 5),
        ):
            draw = ReverseDraw(stored)

        self.assertEqual(draw.survivors, [500, 301, 102, 89, 3])
        self.assertEqual(
            draw.labels,
            ["Old 1", "Round 2", "Round 3", "Round 4", "Grand Prize"],
        )
        self.assertFalse(draw.schedule_pending)

    def test_completed_target_mismatch_keeps_audited_schedule(self):
        stored = {
            "schedule": {
                "total": 1000,
                "survivors": [500, 250, 1],
                "labels": ["Round 1", "Round 2", "Final"],
            },
            "rounds": [
                {
                    "round": 1,
                    "label": "Round 1",
                    "timestamp": "2026-01-01T00:00:00+00:00",
                    "seed": "1",
                    "started_with": 1000,
                    "survivors": 500,
                    "eliminated": [],
                }
            ],
        }
        with (
            patch("app.config.TOTAL_TICKETS", 1000),
            patch("app.config.ROUND_SURVIVORS", [400, 200, 1]),
            patch("app.config.ROUND_LABELS", ["Round 1", "Round 2", "Final"]),
            patch("app.config.ROUND_KINDS", ["elimination"] * 3),
            patch("app.config.ROUND_PRIZES", [""] * 3),
        ):
            draw = ReverseDraw(stored)

        self.assertEqual(draw.survivors, stored["schedule"]["survivors"])
        self.assertEqual(draw.labels, stored["schedule"]["labels"])
        self.assertTrue(draw.schedule_pending)

    def test_configured_draw_stops_with_ten_finalists(self):
        draw = ReverseDraw()
        records = [draw.run_next_round() for _ in draw.survivors]

        self.assertEqual(
            [
                record["started_with"] - record["survivors"]
                for record in records
            ],
            [499, 1, 199, 1, 199, 1, 89, 1],
        )
        self.assertEqual(
            [record["kind"] for record in records],
            [
                "elimination",
                "prize",
                "elimination",
                "prize",
                "elimination",
                "prize",
                "elimination",
                "prize",
            ],
        )
        prize_records = [
            record for record in records if record["kind"] == "prize"
        ]
        self.assertTrue(
            all(len(record["eliminated"]) == 1 for record in prize_records)
        )
        self.assertEqual(len(draw.active()), 10)
        self.assertTrue(draw.finished)
        self.assertTrue(
            all(draw.status(ticket) == "FINALIST" for ticket in draw.active())
        )
        first_gift_winner = prize_records[0]["eliminated"][0]
        self.assertEqual(draw.status(first_gift_winner), "Gift Card 1 winner")

    def test_undo_gift_card_stage_restores_selected_ticket(self):
        draw = ReverseDraw()
        draw.run_next_round()
        gift_record = draw.run_next_round()
        winning_ticket = gift_record["eliminated"][0]

        undone = draw.undo_last_round()

        self.assertEqual(undone["kind"], "prize")
        self.assertEqual(undone["prize"], "Gift Card 1")
        self.assertIn(winning_ticket, draw.active())
        self.assertEqual(draw.status(winning_ticket), "Still in")
        self.assertEqual(len(draw.active()), 501)


if __name__ == "__main__":
    unittest.main()
