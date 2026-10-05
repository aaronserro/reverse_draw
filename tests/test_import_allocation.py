import unittest
from unittest.mock import patch

import pandas as pd

from app.draw import DrawError
from app.main import _holders_from_dataframe


class ImportAllocationTests(unittest.TestCase):
    @patch("app.main.secrets.SystemRandom")
    def test_orders_without_preferences_receive_randomized_tickets(
        self, system_random
    ):
        system_random.return_value.shuffle.side_effect = list.reverse
        source = pd.DataFrame(
            {
                "Full name": ["Jane Doe"],
                "Quantity": [3],
            }
        )

        holders = _holders_from_dataframe(source, 10)

        system_random.return_value.shuffle.assert_called_once()
        self.assertEqual(holders["ticket"].tolist(), [8, 9, 10])
        self.assertEqual(holders["name"].tolist(), ["Jane Doe"] * 3)

    @patch("app.main.secrets.SystemRandom")
    def test_preferred_ticket_is_preserved_and_remainder_is_random(
        self, system_random
    ):
        system_random.return_value.shuffle.side_effect = list.reverse
        source = pd.DataFrame(
            {
                "Full name": ["Jane Doe"],
                "Quantity": [2],
                "Preferred ticket choice": ["7"],
            }
        )

        holders = _holders_from_dataframe(source, 10)

        self.assertEqual(holders["ticket"].tolist(), [7, 10])
        shuffled = system_random.return_value.shuffle.call_args.args[0]
        self.assertNotIn(7, shuffled)

    @patch("app.main.secrets.SystemRandom")
    def test_default_limits_preferences_to_first_100_valid_orders(
        self, system_random
    ):
        system_random.return_value.shuffle.side_effect = list.reverse
        source = pd.DataFrame(
            {
                "Full name": [
                    f"Person {number:03d}" for number in range(1, 102)
                ],
                "Quantity": [1] * 101,
                "Preferred ticket choice": list(range(1, 102)),
            }
        )

        holders = _holders_from_dataframe(source, 200)
        owner_by_ticket = dict(zip(holders["ticket"], holders["name"]))

        self.assertEqual(owner_by_ticket[100], "Person 100")
        self.assertNotEqual(owner_by_ticket.get(101), "Person 101")
        self.assertEqual(owner_by_ticket[200], "Person 101")

    @patch("app.main.secrets.SystemRandom")
    def test_all_scope_honors_preference_after_order_100(
        self, system_random
    ):
        system_random.return_value.shuffle.side_effect = list.reverse
        source = pd.DataFrame(
            {
                "Full name": [
                    f"Person {number:03d}" for number in range(1, 102)
                ],
                "Quantity": [1] * 101,
                "Preferred ticket choice": list(range(1, 102)),
            }
        )

        holders = _holders_from_dataframe(
            source, 200, preferred_ticket_scope="all"
        )
        owner_by_ticket = dict(zip(holders["ticket"], holders["name"]))

        self.assertEqual(owner_by_ticket[101], "Person 101")

    @patch("app.main.secrets.SystemRandom")
    def test_blank_and_invalid_orders_do_not_consume_first_100_slots(
        self, system_random
    ):
        system_random.return_value.shuffle.side_effect = list.reverse
        source = pd.DataFrame(
            {
                "Full name": ["", "Invalid"]
                + [f"Person {number:03d}" for number in range(1, 101)],
                "Quantity": [1, 0] + [1] * 100,
                "Preferred ticket choice": [151, 152]
                + list(range(1, 100))
                + [150],
            }
        )

        holders = _holders_from_dataframe(source, 200)
        owner_by_ticket = dict(zip(holders["ticket"], holders["name"]))

        self.assertEqual(owner_by_ticket[150], "Person 100")

    @patch("app.main.secrets.SystemRandom")
    def test_same_person_orders_across_cutoff_keep_quantity_but_limit_choices(
        self, system_random
    ):
        system_random.return_value.shuffle.side_effect = list.reverse
        source = pd.DataFrame(
            {
                "Full name": ["Jane Doe"] * 101,
                "Quantity": [1] * 101,
                "Preferred ticket choice": [7] + [""] * 99 + [8],
            }
        )

        holders = _holders_from_dataframe(source, 200)
        shuffled = system_random.return_value.shuffle.call_args.args[0]

        self.assertEqual(len(holders), 101)
        self.assertNotIn(7, shuffled)
        self.assertIn(8, shuffled)

    def test_explicit_ticket_assignments_ignore_preference_scope(self):
        source = pd.DataFrame(
            {"ticket": [19, 27], "name": ["Jane Doe", "Bob Smith"]}
        )

        limited = _holders_from_dataframe(
            source, 100, preferred_ticket_scope="first_100"
        )
        unlimited = _holders_from_dataframe(
            source, 100, preferred_ticket_scope="all"
        )

        pd.testing.assert_frame_equal(limited, unlimited)

    def test_invalid_preference_scope_is_rejected(self):
        source = pd.DataFrame(
            {"Full name": ["Jane Doe"], "Quantity": [1]}
        )

        with self.assertRaisesRegex(DrawError, "first_100 or all"):
            _holders_from_dataframe(
                source, 100, preferred_ticket_scope="first_50"
            )


if __name__ == "__main__":
    unittest.main()
