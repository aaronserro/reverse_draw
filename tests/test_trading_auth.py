import json
import os
import tempfile
import unittest
from http.cookies import SimpleCookie

from fastapi import HTTPException, Response
from starlette.requests import Request

from app import main
from app.db import SQLiteStore
from app.draw import ReverseDraw


def request(ip="127.0.0.1", cookie=""):
    headers = []
    if cookie:
        headers.append((b"cookie", cookie.encode()))
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": headers,
            "client": (ip, 12345),
            "server": ("testserver", 80),
            "scheme": "http",
            "query_string": b"",
        }
    )


class TradingAuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.previous_store = main.store
        self.store = SQLiteStore(os.path.join(self.directory.name, "state.db"))
        main.store = self.store
        main._failures.clear()

    def tearDown(self):
        main._failures.clear()
        self.store.close()
        main.store = self.previous_store
        self.directory.cleanup()

    def assign(self, name="Jane Doe", start=1, end=2):
        return main.assign_block(main.BlockIn(name=name, start=start, end=end))

    def login(self, name, code, ip="127.0.0.1"):
        response = Response()
        payload = main.trading_login(
            main.TraderLoginIn(name=name, code=code), request(ip), response
        )
        cookie = SimpleCookie()
        cookie.load(response.headers["set-cookie"])
        return payload, cookie[main.TRADER_COOKIE]

    def test_code_generated_once_per_normalized_holder_and_not_persisted(self):
        first = self.assign("  Jane   Doe  ", 1, 2)
        code = first["new_trading_credentials"][0]["code"]
        second = self.assign("jane doe", 3, 3)
        saved = ReverseDraw(self.store.read())

        self.assertEqual(len(first["new_trading_credentials"]), 1)
        self.assertEqual(second["new_trading_credentials"], [])
        self.assertEqual(len(saved.holder_credentials), 1)
        self.assertNotIn(code, json.dumps(saved.to_dict()))
        self.assertEqual(sorted(saved.owners), [1, 2, 3])
        admin_person = main.admin_payload(saved)["summary"][0]
        self.assertEqual(admin_person["trading_code"], code)

    def test_existing_digest_credentials_migrate_to_viewable_codes(self):
        draw = ReverseDraw()
        draw.set_owners({1: "Jane Doe"})
        draw.holder_credentials = {
            "jane doe": {
                "id": "existing-id",
                "name": "Jane Doe",
                "digest": "legacy-digest",
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        }

        main._sync_holder_credentials(draw)
        person = main.admin_payload(draw)["summary"][0]

        self.assertRegex(person["trading_code"], r"^\d{6}$")
        self.assertEqual(
            draw.holder_credentials["jane doe"]["code_scheme"],
            "derived-v1",
        )
        self.assertNotIn(person["trading_code"], json.dumps(draw.to_dict()))

    def test_valid_login_returns_only_the_authenticated_holders_tickets(self):
        jane = self.assign("Jane Doe", 1, 2)["new_trading_credentials"][0]
        self.assign("Bob Smith", 3, 3)

        payload, cookie = self.login(" jane   DOE ", jane["code"])

        self.assertTrue(payload["authenticated"])
        self.assertEqual(payload["name"], "Jane Doe")
        self.assertEqual(
            [item["ticket"] for item in payload["tickets"]], [1, 2]
        )
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "strict")
        self.assertEqual(cookie["path"], "/")

    def test_wrong_name_and_wrong_code_use_the_same_error(self):
        credential = self.assign()["new_trading_credentials"][0]
        messages = []
        for index, (name, code) in enumerate(
            (("Unknown Person", credential["code"]), ("Jane Doe", "000000"))
        ):
            with self.assertRaises(HTTPException) as caught:
                main.trading_login(
                    main.TraderLoginIn(name=name, code=code),
                    request(f"127.0.0.{index + 1}"),
                    Response(),
                )
            self.assertEqual(caught.exception.status_code, 401)
            messages.append(caught.exception.detail)
        self.assertEqual(messages[0], messages[1])

    def test_reset_invalidates_the_old_code_and_active_session(self):
        credential = self.assign()["new_trading_credentials"][0]
        _, cookie = self.login("Jane Doe", credential["code"])
        cookie_header = f"{main.TRADER_COOKIE}={cookie.value}"
        session = main.trading_session(request(cookie=cookie_header))
        self.assertTrue(session["authenticated"])

        reset = main.reset_holder_credential(
            main.HolderCredentialIn(name="Jane Doe")
        )
        new_code = reset["new_trading_credentials"][0]["code"]

        session = main.trading_session(request(cookie=cookie_header))
        self.assertFalse(session["authenticated"])
        with self.assertRaises(HTTPException):
            self.login("Jane Doe", credential["code"])
        self.assertTrue(self.login("Jane Doe", new_code)[0]["authenticated"])

    def test_deallocating_every_ticket_prunes_the_credential(self):
        self.assign()
        result = main.unassign_holders(main.UnassignIn(name=" jane doe "))

        self.assertEqual(result["unassigned"], 2)
        self.assertEqual(ReverseDraw(self.store.read()).holder_credentials, {})

    def test_draw_reset_preserves_or_clears_credentials_with_holders(self):
        credential = self.assign()["new_trading_credentials"][0]
        main.reset(main.ResetIn(keep_holders=True, confirm="RESET"))
        kept = ReverseDraw(self.store.read())
        self.assertEqual(len(kept.holder_credentials), 1)
        self.assertNotIn(credential["code"], json.dumps(kept.to_dict()))

        main.reset(main.ResetIn(keep_holders=False, confirm="RESET"))
        cleared = ReverseDraw(self.store.read())
        self.assertEqual(cleared.holder_credentials, {})
        self.assertEqual(cleared.owners, {})

    def test_repeated_failures_are_rate_limited(self):
        self.assign()
        for _ in range(5):
            with self.assertRaises(HTTPException) as caught:
                main.trading_login(
                    main.TraderLoginIn(name="Jane Doe", code="999999"),
                    request("10.0.0.5"),
                    Response(),
                )
            self.assertEqual(caught.exception.status_code, 401)

        with self.assertRaises(HTTPException) as caught:
            main.trading_login(
                main.TraderLoginIn(name="Jane Doe", code="999999"),
                request("10.0.0.5"),
                Response(),
            )
        self.assertEqual(caught.exception.status_code, 429)


if __name__ == "__main__":
    unittest.main()
