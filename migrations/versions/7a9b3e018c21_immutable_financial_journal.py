"""Protect the financial journal at database level."""

from alembic import op

revision = "7a9b3e018c21"
down_revision = "039a510de629"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE FUNCTION reject_journal_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION 'Financial journal is append-only; use compensating entries';
    END;
    $$;
    """)
    for table in ("wallet_transactions", "payment_events", "admin_actions"):
        op.execute(
            f"CREATE TRIGGER immutable_{table} BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION reject_journal_mutation()"
        )


def downgrade():
    op.execute("DROP TRIGGER immutable_wallet_transactions ON wallet_transactions")
    op.execute("DROP TRIGGER immutable_payment_events ON payment_events")
    op.execute("DROP TRIGGER immutable_admin_actions ON admin_actions")
    op.execute("DROP FUNCTION reject_journal_mutation()")
