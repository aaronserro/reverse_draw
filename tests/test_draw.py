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
        ):
            draw = ReverseDraw(stored)

        self.assertEqual(draw.schedule, stored["schedule"])
        self.assertTrue(draw.schedule_pending)


if __name__ == "__main__":
    unittest.main()
