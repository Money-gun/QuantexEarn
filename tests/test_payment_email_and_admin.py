import sqlite3
import unittest
from unittest.mock import patch

from config import PAYSTACK_PAYMENT_EMAIL_POOL
from services import paystack_service


class PaymentEmailTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("CREATE TABLE users (user_id TEXT PRIMARY KEY, email TEXT, payment_email TEXT)")
        self.conn.execute("INSERT INTO users VALUES ('EMP1', NULL, NULL)")

    def tearDown(self):
        self.conn.close()

    def test_first_payment_email_is_saved_once_and_internal_email_is_assigned(self):
        self.assertEqual(
            paystack_service.capture_user_email_once(self.conn, "EMP1", "person@example.com"),
            "person@example.com",
        )
        self.assertEqual(
            paystack_service.capture_user_email_once(self.conn, "EMP1", "changed@example.com"),
            "person@example.com",
        )
        self.assertIn(
            paystack_service.get_or_assign_payment_email(self.conn, "EMP1"),
            PAYSTACK_PAYMENT_EMAIL_POOL,
        )

    def test_level_initialization_sends_only_assigned_payment_email_to_paystack(self):
        self.conn.execute("CREATE TABLE payment_intents (id INTEGER PRIMARY KEY, user_id TEXT, level_id INTEGER, payment_type TEXT, amount REAL, currency TEXT, reference TEXT, provider TEXT, provider_access_code TEXT, status TEXT, provider_response_raw TEXT, verified_at TEXT, expires_at TEXT, created_at TEXT, updated_at TEXT)")
        self.conn.execute("UPDATE users SET payment_email=? WHERE user_id='EMP1'", (PAYSTACK_PAYMENT_EMAIL_POOL[0],))
        captured = {}

        def fake_initialize(**kwargs):
            captured.update(kwargs)
            return {"status": True, "data": {"access_code": "access", "authorization_url": "https://checkout.example", "reference": kwargs["reference"]}}

        with patch.object(paystack_service, "get_level_by_id", return_value={"level_number": 1, "unlock_fee": 50}), patch.object(paystack_service, "get_user_level", return_value=None), patch.object(paystack_service, "initialize_paystack_transaction", side_effect=fake_initialize):
            paystack_service.initialize_level_unlock_payment(conn=self.conn, user_id="EMP1", level_id=1, email="real.user@example.com")

        self.assertEqual(captured["email"], PAYSTACK_PAYMENT_EMAIL_POOL[0])
        self.assertEqual(self.conn.execute("SELECT email FROM users WHERE user_id='EMP1'").fetchone()[0], "real.user@example.com")


class AdminAssetsTests(unittest.TestCase):
    def test_admin_uses_name_columns_and_readable_dates(self):
        with open("templates/admin.html", encoding="utf-8") as template, open("static/js/admin-shell.js", encoding="utf-8") as script:
            self.assertGreaterEqual(template.read().count("<div>Name</div>"), 3)
            contents = script.read()
            self.assertIn("Intl.DateTimeFormat", contents)
            self.assertIn("data-date-range", contents)


if __name__ == "__main__":
    unittest.main()
