import unittest
from unittest.mock import patch

import pandas as pd

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


if __name__ == "__main__":
    unittest.main()
