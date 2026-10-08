-- The application migration/backfill in database/apply_schema.py is idempotent.
ALTER TABLE users ADD COLUMN payment_email TEXT;
