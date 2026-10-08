import json
import secrets
import sqlite3
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from config import (
    FIRST_WITHDRAWAL_VERIFICATION_AMOUNT,
    PAYMENT_CURRENCY,
    PAYMENT_PROVIDER,
    PAYSTACK_ALLOWED_CHANNELS,
    PAYSTACK_CALLBACK_URL,
    PAYSTACK_PUBLIC_KEY,
    PAYSTACK_SECRET_KEY,
    PAYSTACK_SUBUNIT_MULTIPLIER,
    PAYSTACK_PAYMENT_EMAIL_POOL,
)
from services.db_service import fetch_one, has_column, now_iso
from services.level_service import (
    get_level_by_id,
    get_user_level,
    mark_final_stage_unlocked,
    mark_level_unlocked,
    unlock_level_with_account_balance,
)
from utils.enums import PaymentStatus, PaymentType, UserLevelStatus

PAYSTACK_BASE_URL = "https://api.paystack.co"
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
REFERENCE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _ensure_paystack_keys() -> None:
    if not PAYSTACK_SECRET_KEY:
        raise ValueError("Missing Paystack secret key in environment.")
    if not PAYSTACK_PUBLIC_KEY:
        raise ValueError("Missing Paystack public key in environment.")


def _request_json(
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    _ensure_paystack_keys()

    url = f"{PAYSTACK_BASE_URL}{path}"
    data = None
    headers = {
        "Authorization": f"Bearer {PAYSTACK_SECRET_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
    }

    if payload is not None:
        data = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(
        url=url,
        data=data,
        headers=headers,
        method=method.upper(),
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            body = response.read().decode("utf-8")
            return json.loads(body)

    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")
        try:
            parsed = json.loads(body)
            return {
                "status": False,
                "message": parsed.get("message", f"HTTP Error {exc.code}"),
                "raw": parsed,
            }
        except Exception:
            return {
                "status": False,
                "message": f"HTTP Error {exc.code}: {exc.reason}",
                "raw": body,
            }

    except Exception as exc:
        return {
            "status": False,
            "message": str(exc),
            "raw": None,
        }


def get_payment_config() -> dict[str, Any]:
    _ensure_paystack_keys()
    return {
        "provider": PAYMENT_PROVIDER,
        "currency": PAYMENT_CURRENCY,
        "public_key": PAYSTACK_PUBLIC_KEY,
        "channels": PAYSTACK_ALLOWED_CHANNELS,
        "checkout_mode": "hosted_checkout",
    }


def capture_user_email_once(
    conn: sqlite3.Connection,
    user_id: str,
    email: str,
) -> str:
    existing = get_user_email(conn, user_id)
    if existing:
        return existing

    clean_email = (email or "").strip().lower()
    if not EMAIL_PATTERN.fullmatch(clean_email):
        raise ValueError("Enter a valid email address to continue.")

    conn.execute(
        "UPDATE users SET email=? WHERE user_id=? AND (email IS NULL OR TRIM(email)='')",
        (clean_email, user_id),
    )
    conn.commit()
    return get_user_email(conn, user_id) or clean_email


def get_or_assign_payment_email(conn: sqlite3.Connection, user_id: str) -> str:
    row = fetch_one(conn, "SELECT payment_email FROM users WHERE user_id=?", (user_id,))
    if not row:
        raise ValueError("User not found.")
    payment_email = (row.get("payment_email") or "").strip().lower()
    if payment_email:
        return payment_email

    payment_email = secrets.choice(PAYSTACK_PAYMENT_EMAIL_POOL)
    conn.execute(
        "UPDATE users SET payment_email=? WHERE user_id=? AND (payment_email IS NULL OR TRIM(payment_email)='')",
        (payment_email, user_id),
    )
    conn.commit()
    return get_or_assign_payment_email(conn, user_id)


def get_user_email(
    conn: sqlite3.Connection,
    user_id: str,
) -> str | None:
    row = fetch_one(
        conn,
        """
        SELECT email
        FROM users
        WHERE user_id = ?
        """,
        (user_id,),
    )
    if not row:
        return None
    return (row.get("email") or "").strip().lower() or None


def _make_reference(
    user_id: str,
    level_number: int,
    payment_type: str,
) -> str:
    prefix = {
        PaymentType.LEVEL_UNLOCK.value: "LV",
        PaymentType.FINAL_STAGE_UNLOCK.value: "FS",
        PaymentType.FIRST_WITHDRAWAL_VERIFICATION.value: "WV",
    }.get(payment_type, "PY")
    safe_user = "".join(ch for ch in str(user_id or "").upper() if ch.isalnum())[:16] or "USER"
    random_part = "".join(secrets.choice(REFERENCE_ALPHABET) for _ in range(6))
    return f"{prefix}{int(level_number)}-{safe_user}-{random_part}"


def _amount_to_subunit(amount: float) -> int:
    return int(round(float(amount) * PAYSTACK_SUBUNIT_MULTIPLIER))


def initialize_paystack_transaction(
    *,
    email: str,
    amount: float,
    reference: str,
    callback_url: str | None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    clean_email = (email or "").strip().lower()
    if not clean_email:
        raise ValueError("Email is required to initialize payment.")

    payload: dict[str, Any] = {
        "email": clean_email,
        "amount": str(_amount_to_subunit(amount)),
        "currency": PAYMENT_CURRENCY,
        "reference": reference,
    }

    clean_callback = (callback_url or PAYSTACK_CALLBACK_URL or "").strip()
    if clean_callback:
        payload["callback_url"] = clean_callback

    if metadata is not None:
        payload["metadata"] = metadata

    if PAYSTACK_ALLOWED_CHANNELS:
        payload["channels"] = PAYSTACK_ALLOWED_CHANNELS

    return _request_json("POST", "/transaction/initialize", payload)


def verify_paystack_transaction(reference: str) -> dict[str, Any]:
    safe_reference = urllib.parse.quote(reference, safe="")
    return _request_json("GET", f"/transaction/verify/{safe_reference}")


def _insert_payment_intent(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    level_id: int,
    payment_type: str,
    amount: float,
    reference: str,
    provider_access_code: str | None,
    provider_response_raw: dict[str, Any],
    status: str,
) -> dict[str, Any]:
    timestamp = now_iso()

    conn.execute(
        """
        INSERT INTO payment_intents (
            user_id,
            level_id,
            payment_type,
            amount,
            currency,
            reference,
            provider,
            provider_access_code,
            status,
            provider_response_raw,
            verified_at,
            expires_at,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            level_id,
            payment_type,
            float(amount),
            PAYMENT_CURRENCY,
            reference,
            PAYMENT_PROVIDER,
            provider_access_code,
            status,
            json.dumps(provider_response_raw),
            None,
            None,
            timestamp,
            timestamp,
        ),
    )
    conn.commit()

    row = fetch_one(
        conn,
        """
        SELECT *
        FROM payment_intents
        WHERE reference = ?
        """,
        (reference,),
    )
    if not row:
        raise ValueError("Failed to create payment intent.")
    return row


def initialize_level_unlock_payment(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    level_id: int,
    email: str | None = None,
    callback_url: str | None = None,
    preflight: bool = False,
) -> dict[str, Any]:
    level = get_level_by_id(conn, level_id)
    if not level:
        raise ValueError("Level not found.")

    user_level = get_user_level(conn, user_id, level_id)
    if user_level and user_level["status"] != UserLevelStatus.LOCKED.value:
        raise ValueError("This level is already unlocked or completed.")

    balance_result = None
    required_balance_tables = {"users", "user_levels", "payment_intents"}
    existing_tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN (?, ?, ?)",
            tuple(required_balance_tables),
        ).fetchall()
    }
    if required_balance_tables.issubset(existing_tables) and has_column(conn, "users", "balance"):
        balance_result = unlock_level_with_account_balance(conn, user_id, level_id)
    if balance_result:
        if balance_result.get("mode") == "already_unlocked":
            raise ValueError("This level is already unlocked or completed.")
        return {
            **balance_result,
            "level_id": level_id,
            "level_number": int(level["level_number"]),
            "payment_type": PaymentType.LEVEL_UNLOCK.value,
        }

    # A preflight request has already made the server-side payment decision.
    # Do not initialize Paystack until the user has supplied the existing
    # checkout details.
    if preflight or not (email or get_user_email(conn, user_id)):
        return {
            "mode": "paystack",
            "requires_paystack": True,
            "amount": float(level["unlock_fee"]),
            "level_number": int(level["level_number"]),
            "payment_type": PaymentType.LEVEL_UNLOCK.value,
        }

    if email or get_user_email(conn, user_id):
        capture_user_email_once(conn, user_id, email or "")
    payment_email = get_or_assign_payment_email(conn, user_id)

    reference = _make_reference(
        user_id=user_id,
        level_number=int(level["level_number"]),
        payment_type=PaymentType.LEVEL_UNLOCK.value,
    )

    metadata = {
        "user_id": user_id,
        "level_id": level_id,
        "level_number": int(level["level_number"]),
        "payment_type": PaymentType.LEVEL_UNLOCK.value,
    }

    response = initialize_paystack_transaction(
        email=payment_email,
        amount=float(level["unlock_fee"]),
        reference=reference,
        callback_url=callback_url,
        metadata=metadata,
    )

    if not response.get("status"):
        raise ValueError(response.get("message", "Paystack initialization failed."))

    data = response.get("data") or {}

    intent = _insert_payment_intent(
        conn,
        user_id=user_id,
        level_id=level_id,
        payment_type=PaymentType.LEVEL_UNLOCK.value,
        amount=float(level["unlock_fee"]),
        reference=reference,
        provider_access_code=data.get("access_code"),
        provider_response_raw=response,
        status=PaymentStatus.PENDING.value,
    )

    return {
        "mode": "paystack",
        "requires_paystack": True,
        "payment_intent": {key: value for key, value in intent.items() if key != "provider_response_raw"},
        "authorization_url": data.get("authorization_url"),
        "access_code": data.get("access_code"),
        "reference": data.get("reference") or reference,
        "amount": float(level["unlock_fee"]),
        "level_number": int(level["level_number"]),
        "public_key": PAYSTACK_PUBLIC_KEY,
        "payment_status": data.get("status"),
        "payment_channel": data.get("channel"),
    }


def initialize_withdrawal_verification_payment(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    original_amount: float,
    network: str,
    number: str,
    account_name: str,
    method_id: str | None = None,
    email: str | None = None,
    callback_url: str | None = None,
) -> dict[str, Any]:
    from services.withdrawal_service import first_three_levels_completed

    if not first_three_levels_completed(conn, user_id):
        raise ValueError(
            "Before you can make your first withdrawal, you must successfully complete Levels 1, 2, and 3."
        )

    user = fetch_one(
        conn,
        "SELECT first_withdrawal_verified FROM users WHERE user_id = ?",
        (user_id,),
    )
    if not user:
        raise ValueError("User not found.")
    if int(user.get("first_withdrawal_verified") or 0) == 1:
        raise ValueError("First-withdrawal verification has already been completed.")

    existing = fetch_one(
        conn,
        """
        SELECT *
        FROM withdrawal_verification_intents
        WHERE user_id = ? AND status = 'pending'
        ORDER BY id DESC
        LIMIT 1
        """,
        (user_id,),
    )
    if existing:
        try:
            stored = json.loads(existing.get("provider_response_raw") or "{}")
        except (TypeError, ValueError):
            stored = {}
        data = stored.get("data") or {}
        return {
            "mode": "paystack",
            "requires_verification": True,
            "amount": FIRST_WITHDRAWAL_VERIFICATION_AMOUNT,
            "reference": existing["reference"],
            "authorization_url": data.get("authorization_url"),
            "access_code": data.get("access_code"),
            "payment_type": PaymentType.FIRST_WITHDRAWAL_VERIFICATION.value,
            "original_amount": float(existing["original_amount"]),
            "final_amount": float(existing["final_amount"]),
        }

    clean_amount = round(float(original_amount), 2)
    if email or get_user_email(conn, user_id):
        capture_user_email_once(conn, user_id, email or "")
    payment_email = get_or_assign_payment_email(conn, user_id)
    reference = _make_reference(
        user_id=user_id,
        level_number=0,
        payment_type=PaymentType.FIRST_WITHDRAWAL_VERIFICATION.value,
    )
    response = initialize_paystack_transaction(
        email=payment_email,
        amount=FIRST_WITHDRAWAL_VERIFICATION_AMOUNT,
        reference=reference,
        callback_url=callback_url,
        metadata={
            "user_id": user_id,
            "payment_type": PaymentType.FIRST_WITHDRAWAL_VERIFICATION.value,
            "original_withdrawal_amount": clean_amount,
            "final_withdrawal_amount": round(clean_amount + FIRST_WITHDRAWAL_VERIFICATION_AMOUNT, 2),
        },
    )
    if not response.get("status"):
        raise ValueError(response.get("message", "Paystack initialization failed."))

    final_amount = round(clean_amount + FIRST_WITHDRAWAL_VERIFICATION_AMOUNT, 2)
    timestamp = now_iso()
    conn.execute(
        """
        INSERT INTO withdrawal_verification_intents (
            user_id, reference, original_amount, verification_amount, final_amount,
            network, number, account_name, method_id, provider, status,
            provider_response_raw, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'paystack', 'pending', ?, ?, ?)
        """,
        (
            user_id,
            reference,
            clean_amount,
            FIRST_WITHDRAWAL_VERIFICATION_AMOUNT,
            final_amount,
            network,
            number,
            account_name,
            method_id,
            json.dumps(response),
            timestamp,
            timestamp,
        ),
    )
    conn.commit()
    data = response.get("data") or {}
    return {
        "mode": "paystack",
        "requires_verification": True,
        "amount": FIRST_WITHDRAWAL_VERIFICATION_AMOUNT,
        "reference": data.get("reference") or reference,
        "authorization_url": data.get("authorization_url"),
        "access_code": data.get("access_code"),
        "payment_type": PaymentType.FIRST_WITHDRAWAL_VERIFICATION.value,
        "original_amount": clean_amount,
        "final_amount": final_amount,
    }


def initialize_final_stage_payment(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    level_id: int,
    email: str | None = None,
    callback_url: str | None = None,
) -> dict[str, Any]:
    level = get_level_by_id(conn, level_id)
    if not level:
        raise ValueError("Level not found.")

    if int(level["final_stage_enabled"] or 0) != 1:
        raise ValueError("This level does not support final-stage unlock.")

    user_level = get_user_level(conn, user_id, level_id)
    if not user_level:
        raise ValueError("You have not unlocked this level yet.")

    if user_level["status"] != UserLevelStatus.ACTIVE_FINAL_STAGE_PENDING.value:
        raise ValueError("Final stage is not available for payment yet.")

    capture_user_email_once(conn, user_id, email or "")
    payment_email = get_or_assign_payment_email(conn, user_id)

    reference = _make_reference(
        user_id=user_id,
        level_number=int(level["level_number"]),
        payment_type=PaymentType.FINAL_STAGE_UNLOCK.value,
    )

    metadata = {
        "user_id": user_id,
        "level_id": level_id,
        "level_number": int(level["level_number"]),
        "payment_type": PaymentType.FINAL_STAGE_UNLOCK.value,
    }

    response = initialize_paystack_transaction(
        email=payment_email,
        amount=float(level["final_stage_fee"]),
        reference=reference,
        callback_url=callback_url,
        metadata=metadata,
    )

    if not response.get("status"):
        raise ValueError(response.get("message", "Paystack initialization failed."))

    data = response.get("data") or {}

    intent = _insert_payment_intent(
        conn,
        user_id=user_id,
        level_id=level_id,
        payment_type=PaymentType.FINAL_STAGE_UNLOCK.value,
        amount=float(level["final_stage_fee"]),
        reference=reference,
        provider_access_code=data.get("access_code"),
        provider_response_raw=response,
        status=PaymentStatus.PENDING.value,
    )

    return {
        "payment_intent": {key: value for key, value in intent.items() if key != "provider_response_raw"},
        "authorization_url": data.get("authorization_url"),
        "access_code": data.get("access_code"),
        "reference": data.get("reference") or reference,
        "amount": float(level["final_stage_fee"]),
        "level_number": int(level["level_number"]),
        "public_key": PAYSTACK_PUBLIC_KEY,
        "payment_status": data.get("status"),
        "payment_channel": data.get("channel"),
    }


def _update_payment_intent_status(
    conn: sqlite3.Connection,
    *,
    reference: str,
    status: str,
    provider_response_raw: dict[str, Any],
    verified_at: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE payment_intents
        SET
            status = ?,
            provider_response_raw = ?,
            verified_at = COALESCE(?, verified_at),
            updated_at = ?
        WHERE reference = ?
        """,
        (
            status,
            json.dumps(provider_response_raw),
            verified_at,
            now_iso(),
            reference,
        ),
    )
    conn.commit()


def verify_and_apply_payment(
    conn: sqlite3.Connection,
    reference: str,
) -> dict[str, Any]:
    if not fetch_one(
        conn,
        "SELECT reference FROM payment_intents WHERE reference = ?",
        (reference,),
    ):
        verification_table = fetch_one(
            conn,
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'withdrawal_verification_intents'",
        )
        if verification_table and fetch_one(
            conn,
            "SELECT reference FROM withdrawal_verification_intents WHERE reference = ?",
            (reference,),
        ):
            return verify_and_apply_withdrawal_verification(conn, reference)

    intent = fetch_one(
        conn,
        """
        SELECT *
        FROM payment_intents
        WHERE reference = ?
        """,
        (reference,),
    )
    if not intent:
        raise ValueError("Payment intent not found.")

    if intent["status"] == PaymentStatus.SUCCESS.value:
        return {
            "success": True,
            "message": "Payment already verified.",
            "reference": reference,
            "payment_type": intent["payment_type"],
            "level_id": intent["level_id"],
            "already_verified": True,
        }

    verification = verify_paystack_transaction(reference)
    if not verification.get("status"):
        _update_payment_intent_status(
            conn,
            reference=reference,
            status=PaymentStatus.FAILED.value,
            provider_response_raw=verification,
        )
        raise ValueError(verification.get("message", "Payment verification failed."))

    data = verification.get("data") or {}
    paystack_status = (data.get("status") or "").strip().lower()

    if paystack_status != "success":
        mapped_status = {
            "abandoned": PaymentStatus.ABANDONED.value,
            "failed": PaymentStatus.FAILED.value,
        }.get(paystack_status, PaymentStatus.PENDING.value)

        _update_payment_intent_status(
            conn,
            reference=reference,
            status=mapped_status,
            provider_response_raw=verification,
        )

        failure_reason = (
            data.get("message")
            or data.get("gateway_response")
            or f"Payment is not successful yet. Current status: {paystack_status or 'unknown'}"
        )

        return {
            "success": False,
            "message": failure_reason,
            "reference": reference,
            "payment_status": mapped_status,
        }

    verified_amount = float(data.get("amount", 0)) / float(PAYSTACK_SUBUNIT_MULTIPLIER)
    stored_amount = float(intent["amount"] or 0)

    if round(verified_amount, 2) != round(stored_amount, 2):
        _update_payment_intent_status(
            conn,
            reference=reference,
            status=PaymentStatus.FAILED.value,
            provider_response_raw=verification,
        )
        raise ValueError("Verified amount does not match expected payment amount.")

    timestamp = now_iso()
    _update_payment_intent_status(
        conn,
        reference=reference,
        status=PaymentStatus.SUCCESS.value,
        provider_response_raw=verification,
        verified_at=timestamp,
    )

    if intent["payment_type"] == PaymentType.LEVEL_UNLOCK.value:
        user_level = mark_level_unlocked(conn, intent["user_id"], int(intent["level_id"]))
        return {
            "success": True,
            "message": "Level unlock payment verified successfully.",
            "reference": reference,
            "payment_type": intent["payment_type"],
            "level_id": int(intent["level_id"]),
            "user_level": user_level,
            "payment_status": PaymentStatus.SUCCESS.value,
        }

    if intent["payment_type"] == PaymentType.FINAL_STAGE_UNLOCK.value:
        user_level = mark_final_stage_unlocked(conn, intent["user_id"], int(intent["level_id"]))
        return {
            "success": True,
            "message": "Final-stage payment verified successfully.",
            "reference": reference,
            "payment_type": intent["payment_type"],
            "level_id": int(intent["level_id"]),
            "user_level": user_level,
            "payment_status": PaymentStatus.SUCCESS.value,
        }

    raise ValueError("Unsupported payment type.")


def verify_and_apply_withdrawal_verification(
    conn: sqlite3.Connection,
    reference: str,
    expected_user_id: str | None = None,
) -> dict[str, Any]:
    from services.withdrawal_service import create_withdrawal_request

    intent = fetch_one(
        conn,
        "SELECT * FROM withdrawal_verification_intents WHERE reference = ?",
        (reference,),
    )
    if not intent:
        raise ValueError("Withdrawal verification payment not found.")
    if expected_user_id and str(intent["user_id"]) != str(expected_user_id):
        raise ValueError("This verification payment belongs to a different account.")

    if intent["status"] == PaymentStatus.SUCCESS.value and intent.get("withdrawal_request_id"):
        request = fetch_one(
            conn,
            "SELECT * FROM requests WHERE id = ?",
            (intent["withdrawal_request_id"],),
        )
        return {
            "success": True,
            "message": "Verification already completed and withdrawal submitted.",
            "reference": reference,
            "payment_type": PaymentType.FIRST_WITHDRAWAL_VERIFICATION.value,
            "payment_status": PaymentStatus.SUCCESS.value,
            "request": request,
            "already_verified": True,
        }

    verification = verify_paystack_transaction(reference)
    if not verification.get("status"):
        conn.execute(
            "UPDATE withdrawal_verification_intents SET status = ?, provider_response_raw = ?, updated_at = ? WHERE reference = ?",
            (PaymentStatus.FAILED.value, json.dumps(verification), now_iso(), reference),
        )
        conn.commit()
        raise ValueError(verification.get("message", "Payment verification failed."))

    data = verification.get("data") or {}
    paystack_status = str(data.get("status") or "").strip().lower()
    if paystack_status != "success":
        mapped_status = {
            "abandoned": PaymentStatus.ABANDONED.value,
            "failed": PaymentStatus.FAILED.value,
        }.get(paystack_status, PaymentStatus.PENDING.value)
        conn.execute(
            "UPDATE withdrawal_verification_intents SET status = ?, provider_response_raw = ?, updated_at = ? WHERE reference = ?",
            (mapped_status, json.dumps(verification), now_iso(), reference),
        )
        conn.commit()
        return {
            "success": False,
            "message": data.get("message") or data.get("gateway_response") or "Verification payment is not successful yet.",
            "reference": reference,
            "payment_type": PaymentType.FIRST_WITHDRAWAL_VERIFICATION.value,
            "payment_status": mapped_status,
        }

    verified_amount = float(data.get("amount", 0)) / float(PAYSTACK_SUBUNIT_MULTIPLIER)
    expected_amount = float(intent["verification_amount"] or FIRST_WITHDRAWAL_VERIFICATION_AMOUNT)
    if round(verified_amount, 2) != round(expected_amount, 2):
        conn.execute(
            "UPDATE withdrawal_verification_intents SET status = ?, provider_response_raw = ?, updated_at = ? WHERE reference = ?",
            (PaymentStatus.FAILED.value, json.dumps(verification), now_iso(), reference),
        )
        conn.commit()
        raise ValueError("Verified amount does not match the GHS 70.00 verification amount.")

    timestamp = now_iso()
    conn.execute(
        """
        UPDATE withdrawal_verification_intents
        SET status = 'success', provider_response_raw = ?, verified_at = ?, updated_at = ?
        WHERE reference = ?
        """,
        (json.dumps(verification), timestamp, timestamp, reference),
    )
    conn.execute(
        "UPDATE users SET first_withdrawal_verified = 1 WHERE user_id = ?",
        (intent["user_id"],),
    )
    conn.commit()

    request_result = create_withdrawal_request(
        conn,
        user_id=intent["user_id"],
        amount=float(intent["original_amount"]),
        network=intent["network"],
        number=intent["number"],
        name=intent["account_name"],
        method_id=intent["method_id"],
        verification_reference=reference,
        verification_amount=expected_amount,
    )
    conn.execute(
        "UPDATE withdrawal_verification_intents SET withdrawal_request_id = ?, updated_at = ? WHERE reference = ?",
        (request_result["request_id"], now_iso(), reference),
    )
    conn.commit()

    return {
        "success": True,
        "message": "Verification payment verified and withdrawal submitted successfully.",
        "reference": reference,
        "payment_type": PaymentType.FIRST_WITHDRAWAL_VERIFICATION.value,
        "payment_status": PaymentStatus.SUCCESS.value,
        "request": request_result,
    }
