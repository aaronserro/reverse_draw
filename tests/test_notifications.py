import os
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from app.draw import ReverseDraw
from app.db import SQLiteStore
from app.email_service import (
    SMTPEmailConfig,
    email_config,
    render_ticket_email,
)
from app import main
from app.main import (
    _notification_preview,
    _process_notification_batch,
    _source_fingerprint,
)


class NotificationPreviewTests(unittest.TestCase):
    def setUp(self):
        self.previous_env = {
            name: os.environ.get(name)
            for name in (
                "EMAIL_PROVIDER",
                "MS_GRAPH_TENANT_ID",
                "MS_GRAPH_CLIENT_ID",
                "MS_GRAPH_CLIENT_SECRET",
                "EMAIL_SENDER_ADDRESS",
                "PUBLIC_APP_URL",
            )
        }
        os.environ.update(
            {
                "EMAIL_PROVIDER": "graph",
                "MS_GRAPH_TENANT_ID": "tenant",
                "MS_GRAPH_CLIENT_ID": "client",
                "MS_GRAPH_CLIENT_SECRET": "secret",
                "EMAIL_SENDER_ADDRESS": "draw@example.com",
                "PUBLIC_APP_URL": "https://draw.example.com",
            }
        )

    def tearDown(self):
        for name, value in self.previous_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def make_draw(self):
        frame = pd.DataFrame(
            {
                "Full name": ["Jane Doe", "Bob Smith"],
                "HOOPP email address": [
                    "Jane@example.com",
                    "bob@example.com",
                ],
            }
        )
        draw = ReverseDraw()
        draw.source_dataframe = {
            "filename": "orders.xlsx",
            "uploaded_at": "2026-01-01T00:00:00+00:00",
            "json": frame.to_json(orient="split", date_format="iso"),
        }
        draw.source_dataframe["fingerprint"] = _source_fingerprint(
            draw.source_dataframe
        )
        draw.allocation_source_fingerprint = draw.source_dataframe["fingerprint"]
        draw.set_owners({1: "Jane Doe", 2: "Jane Doe", 3: "Bob Smith"})
        return draw

    def test_first_batch_contains_every_current_ticket(self):
        preview = _notification_preview(self.make_draw())
        self.assertTrue(preview["ready"])
        self.assertEqual(preview["pending_people"], 2)
        self.assertEqual(preview["pending_tickets"], 3)
        self.assertEqual(preview["recipients"][1]["new_tickets"], [1, 2])

    def test_successful_pairs_are_not_resent_but_new_ticket_is(self):
        draw = self.make_draw()
        draw.notification_batches.append(
            {
                "status": "completed",
                "jobs": [
                    {
                        "email": "jane@example.com",
                        "status": "sent",
                        "new_tickets": [1, 2],
                    },
                    {
                        "email": "bob@example.com",
                        "status": "sent",
                        "new_tickets": [3],
                    },
                ],
            }
        )
        self.assertEqual(_notification_preview(draw)["pending_people"], 0)

        draw.set_owners({**draw.owners, 4: "Jane Doe"})
        preview = _notification_preview(draw)
        self.assertEqual(preview["pending_people"], 1)
        self.assertEqual(preview["recipients"][0]["new_tickets"], [4])
        self.assertEqual(preview["recipients"][0]["all_tickets"], [1, 2, 4])

    def test_unknown_submission_is_blocked_from_retry(self):
        draw = self.make_draw()
        draw.notification_batches.append(
            {
                "status": "attention",
                "jobs": [
                    {
                        "email": "jane@example.com",
                        "status": "unknown",
                        "new_tickets": [1, 2],
                    }
                ],
            }
        )
        preview = _notification_preview(draw)
        self.assertFalse(preview["ready"])
        self.assertTrue(
            any("uncertain" in item["reason"] for item in preview["blocked"])
        )
        self.assertNotIn(
            "jane@example.com",
            {item["email"] for item in preview["recipients"]},
        )

    def test_source_replacement_requires_allocation_review(self):
        draw = self.make_draw()
        draw.source_dataframe["json"] += " "
        draw.source_dataframe.pop("fingerprint")
        preview = _notification_preview(draw)
        self.assertFalse(preview["source_matches_allocation"])
        self.assertFalse(preview["ready"])

    def test_deallocate_then_allocate_new_ticket_creates_one_pending_ticket(self):
        draw = self.make_draw()
        draw.notification_batches.append(
            {
                "status": "completed",
                "jobs": [
                    {
                        "email": "jane@example.com",
                        "status": "sent",
                        "new_tickets": [1, 2],
                    }
                ],
            }
        )
        self.assertEqual(draw.unassign_block(1, 2), 2)
        draw.assign_block("Jane Doe", 4, 4)
        preview = _notification_preview(draw)
        jane = next(
            item
            for item in preview["recipients"]
            if item["email"] == "jane@example.com"
        )
        self.assertEqual(jane["new_tickets"], [4])
        self.assertEqual(jane["all_tickets"], [4])

    def test_deallocate_holder_removes_only_matching_person(self):
        draw = self.make_draw()
        self.assertEqual(draw.unassign_holder("  JANE   DOE "), 2)
        self.assertEqual(draw.owners, {3: "Bob Smith"})


class EmailTemplateTests(unittest.TestCase):
    def test_template_escapes_user_values_and_lists_tickets(self):
        subject, html_body, text_body = render_ticket_email(
            name="<Jane>",
            new_tickets=[7],
            all_tickets=[7, 9],
            app_url="https://draw.example.com",
            trading_code="042731",
            site_access_code="654321",
        )
        self.assertIn("ticket numbers", subject.lower())
        self.assertNotIn("<Jane>", html_body)
        self.assertIn("&lt;Jane&gt;", html_body)
        self.assertIn("#7, #9", text_body)
        self.assertIn("@keyframes ticketReveal", html_body)
        self.assertIn("Why did I receive this?", html_body)
        self.assertIn("Reply to this email", text_body)
        self.assertIn("042731", html_body)
        self.assertIn("042731", text_body)
        self.assertIn("Live draw board access code", html_body)
        self.assertIn("654321", html_body)
        self.assertIn("Live draw board access code: 654321", text_body)
        self.assertIn("https://draw.example.com/trading/login", text_body)

    def test_template_omits_site_code_when_public_board_is_open(self):
        _, html_body, text_body = render_ticket_email(
            name="Jane",
            new_tickets=[7],
            all_tickets=[7],
            app_url="https://draw.example.com",
            trading_code="042731",
            site_access_code="",
        )
        self.assertNotIn("Live draw board access code", html_body)
        self.assertNotIn("Live draw board access code", text_body)
        self.assertIn("View the live draw board", text_body)

    def test_free_smtp_configuration_uses_authenticated_address_by_default(self):
        values = {
            "EMAIL_PROVIDER": "smtp",
            "SMTP_HOST": "smtp.gmail.com",
            "SMTP_PORT": "587",
            "SMTP_SECURITY": "starttls",
            "SMTP_USERNAME": "draw.sender@gmail.com",
            "SMTP_PASSWORD": "abcd efgh ijkl mnop",
            "EMAIL_SENDER_NAME": "Fundraiser Draw",
            "EMAIL_SENDER_ADDRESS": "",
            "PUBLIC_APP_URL": "https://draw.example.com",
        }
        with patch.dict(os.environ, values, clear=False):
            settings = email_config()
        self.assertIsInstance(settings, SMTPEmailConfig)
        self.assertTrue(settings.configured)
        self.assertEqual(settings.sender, "draw.sender@gmail.com")
        self.assertEqual(settings.sender_name, "Fundraiser Draw")
        self.assertEqual(settings.password, "abcdefghijklmnop")


class BatchProcessingTests(unittest.TestCase):
    def test_clear_history_makes_current_tickets_pending_again(self):
        with tempfile.TemporaryDirectory() as directory:
            previous_store = main.store
            test_store = SQLiteStore(os.path.join(directory, "state.db"))
            main.store = test_store
            try:
                draw = NotificationPreviewTests().make_draw()
                draw.notification_batches = [
                    {
                        "id": "batch-1",
                        "status": "completed",
                        "jobs": [
                            {
                                "email": "jane@example.com",
                                "status": "sent",
                                "new_tickets": [1, 2],
                            },
                            {
                                "email": "bob@example.com",
                                "status": "sent",
                                "new_tickets": [3],
                            },
                        ],
                    }
                ]
                with test_store.transaction() as box:
                    box.data = draw.to_dict()

                result = main.clear_notification_history()

                saved = ReverseDraw(test_store.read())
                self.assertEqual(result["cleared_batches"], 1)
                self.assertEqual(saved.notification_batches, [])
                self.assertEqual(result["preview"]["pending_tickets"], 3)
            finally:
                test_store.close()
                main.store = previous_store

    def test_successful_graph_response_is_persisted(self):
        class FakeClient:
            message = None

            def send_ticket_email(self, **message):
                self.message = message
                return {"status_code": 202, "request_id": "request-123"}

        with tempfile.TemporaryDirectory() as directory:
            previous_store = main.store
            test_store = SQLiteStore(os.path.join(directory, "state.db"))
            main.store = test_store
            try:
                draw = ReverseDraw()
                draw.set_owners({1: "Jane Doe"})
                draw.notification_batches = [
                    {
                        "id": "batch-1",
                        "status": "sending",
                        "jobs": [
                            {
                                "email": "jane@example.com",
                                "name": "Jane Doe",
                                "new_tickets": [1],
                                "all_tickets": [1],
                                "status": "pending",
                            }
                        ],
                    }
                ]
                with test_store.transaction() as box:
                    box.data = draw.to_dict()

                client = FakeClient()
                with patch("app.main.build_email_client", return_value=client):
                    _process_notification_batch(
                        "batch-1",
                        [
                            {
                                "email": "jane@example.com",
                                "name": "Jane Doe",
                                "new_tickets": [1],
                                "all_tickets": [1],
                            }
                        ],
                    )

                saved = ReverseDraw(test_store.read()).notification_batches[0]
                self.assertRegex(client.message["trading_code"], r"^\d{6}$")
                self.assertEqual(saved["status"], "completed")
                self.assertEqual(saved["jobs"][0]["status"], "sent")
                self.assertEqual(
                    saved["jobs"][0]["provider_request_id"], "request-123"
                )
            finally:
                test_store.close()
                main.store = previous_store


if __name__ == "__main__":
    unittest.main()
