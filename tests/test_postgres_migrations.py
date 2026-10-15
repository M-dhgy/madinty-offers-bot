"""Safety tests for upgrading a legacy schema with ordered migrations.

Only run against a dedicated PostgreSQL database whose name ends in ``_test``.
The test creates and drops random temporary schemas inside that database.
"""

import asyncio
import os
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit
from uuid import uuid4

import asyncpg
import pytest
from dotenv import load_dotenv

import db

ROOT = Path(__file__).resolve().parents[1]


def _test_database_url():
    load_dotenv(ROOT / ".env")
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        pytest.skip("Set DATABASE_URL to a dedicated local PostgreSQL test database")
    parsed = urlsplit(dsn)
    database_name = unquote(parsed.path.rsplit("/", 1)[-1])
    if not database_name.endswith("_test"):
        pytest.fail("Migration tests refuse databases not ending in '_test'")
    if parsed.hostname not in {None, "", "localhost", "127.0.0.1", "::1"}:
        pytest.fail("This migration test is restricted to a local PostgreSQL server")
    return dsn


def _legacy_schema_sql(duplicate_codes=False):
    duplicate = """
        INSERT INTO discount_codes (campaign_id, user_id, code)
        VALUES (1, 1, 'LEGACY-DUPLICATE');
    """ if duplicate_codes else ""
    return """
        CREATE TABLE cities (
            id SERIAL PRIMARY KEY, name TEXT NOT NULL, code TEXT UNIQUE NOT NULL,
            status TEXT NOT NULL DEFAULT 'active'
        );
        CREATE TABLE categories (
            id SERIAL PRIMARY KEY, name TEXT NOT NULL, code TEXT UNIQUE NOT NULL,
            status TEXT NOT NULL DEFAULT 'active'
        );
        CREATE TABLE users (
            id SERIAL PRIMARY KEY, telegram_id BIGINT UNIQUE NOT NULL,
            username TEXT, first_name TEXT, phone TEXT, city_id INTEGER,
            account_type TEXT, status TEXT NOT NULL DEFAULT 'active',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_active_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE TABLE businesses (
            id SERIAL PRIMARY KEY, user_id INTEGER, business_name TEXT NOT NULL,
            business_type TEXT, city_id INTEGER, phone TEXT, address TEXT,
            description TEXT, status TEXT NOT NULL DEFAULT 'pending',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE TABLE campaigns (
            id SERIAL PRIMARY KEY, business_id INTEGER, title TEXT, description TEXT,
            raw_input TEXT, category_id INTEGER, city_id INTEGER, target_audience TEXT,
            discount_percent INTEGER, image_url TEXT, start_date DATE, end_date DATE,
            status TEXT NOT NULL DEFAULT 'draft', created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            approved_at TIMESTAMPTZ, published_at TIMESTAMPTZ
        );
        CREATE TABLE discount_codes (
            id SERIAL PRIMARY KEY, campaign_id INTEGER, user_id INTEGER,
            code TEXT UNIQUE NOT NULL, status TEXT NOT NULL DEFAULT 'unused',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(), redeemed_at TIMESTAMPTZ
        );
        CREATE TABLE campaign_events (
            id SERIAL PRIMARY KEY, campaign_id INTEGER, user_id INTEGER,
            event_type TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        INSERT INTO cities (id, name, code) VALUES (1, 'الخرطوم', 'KHARTOUM');
        INSERT INTO categories (id, name, code) VALUES (1, 'مطاعم وكافيهات', 'FOOD');
        INSERT INTO users (id, telegram_id, first_name) VALUES (1, 700001, 'قديم');
        INSERT INTO businesses (id, user_id, business_name, city_id, status)
        VALUES (1, 1, 'نشاط قديم', 1, 'approved');
        INSERT INTO campaigns (id, business_id, title, city_id, category_id, status)
        VALUES (1, 1, 'حملة قديمة', 1, 1, 'active');
        INSERT INTO discount_codes (campaign_id, user_id, code)
        VALUES (1, 1, 'LEGACY-CODE');
    """ + duplicate


async def _run_migration_checks(dsn):
    admin = await asyncpg.connect(dsn)
    schemas = [
        f"mig_audit_{uuid4().hex}", f"mig_duplicate_{uuid4().hex}",
        f"mig_concurrent_{uuid4().hex}", f"mig_permission_{uuid4().hex}",
    ]
    try:
        for schema in schemas:
            await admin.execute(f'CREATE SCHEMA "{schema}"')

        upgraded = await asyncpg.connect(dsn, server_settings={"search_path": schemas[0]})
        try:
            await upgraded.execute(_legacy_schema_sql())
            applied = await db.apply_migrations(upgraded)
            assert applied == ["001", "002", "003", "004"]
            assert await upgraded.fetchval("SELECT count(*) FROM users WHERE first_name='قديم'") == 1
            assert await upgraded.fetchval("SELECT count(*) FROM campaigns WHERE title='حملة قديمة'") == 1
            assert await upgraded.fetchval("SELECT code FROM discount_codes WHERE code='LEGACY-CODE'") == "LEGACY-CODE"
            assert await upgraded.fetchval(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_schema=current_schema() AND table_name='campaigns' AND column_name='submission_key'"
            ) == 1
            assert await upgraded.fetchval(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_schema=current_schema() AND table_name='listings' AND column_name='submission_key'"
            ) == 1
            assert await upgraded.fetchval(
                "SELECT count(*) FROM pg_indexes WHERE schemaname=current_schema() "
                "AND indexname IN ('idx_campaigns_submission_key','idx_listings_submission_key')"
            ) == 2
            assert await upgraded.fetchval(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema=current_schema() AND table_name='telegram_update_receipts'"
            ) == 1
            assert await upgraded.fetchval(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema=current_schema() AND table_name='campaign_recipient_deliveries'"
            ) == 1
            assert await upgraded.fetchval(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_schema=current_schema() AND table_name='telegram_update_receipts' "
                "AND column_name IN ('status','stage','lease_until','attempts')"
            ) == 4
            assert await db.apply_migrations(upgraded) == []
            assert await upgraded.fetchval("SELECT count(*) FROM schema_migrations") == 4
        finally:
            await upgraded.close()

        duplicate = await asyncpg.connect(dsn, server_settings={"search_path": schemas[1]})
        try:
            await duplicate.execute(_legacy_schema_sql(duplicate_codes=True))
            with pytest.raises(asyncpg.exceptions.RaiseError, match="duplicate campaign/user codes"):
                await db.apply_migrations(duplicate)
            assert await duplicate.fetchval("SELECT count(*) FROM discount_codes") == 2
            assert await duplicate.fetchval(
                "SELECT count(*) FROM pg_constraint "
                "WHERE conname='uq_discount_campaign_user' "
                "AND conrelid='discount_codes'::regclass"
            ) == 0
            assert await duplicate.fetchval("SELECT count(*) FROM schema_migrations") == 0
            assert await duplicate.fetchval("SELECT to_regclass('listings')") is None
            assert await duplicate.fetchval(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_schema=current_schema() AND table_name='businesses' AND column_name='is_paid'"
            ) == 0
        finally:
            await duplicate.close()

        concurrent_admin = await asyncpg.connect(dsn, server_settings={"search_path": schemas[2]})
        try:
            await concurrent_admin.execute(_legacy_schema_sql())
        finally:
            await concurrent_admin.close()

        class DelayedCreateConnection:
            def __init__(self, connection):
                self._connection = connection

            def __getattr__(self, name):
                return getattr(self._connection, name)

            async def execute(self, query, *args):
                if "CREATE TABLE IF NOT EXISTS schema_migrations" in query:
                    await asyncio.sleep(0.05)
                return await self._connection.execute(query, *args)

        concurrent_connections = [
            await asyncpg.connect(dsn, server_settings={"search_path": schemas[2]})
            for _ in range(8)
        ]
        try:
            results = await asyncio.gather(*(
                db.apply_migrations(DelayedCreateConnection(connection))
                for connection in concurrent_connections
            ))
            flattened = [version for result in results for version in result]
            assert sorted(flattened) == ["001", "002", "003", "004"]
            assert await concurrent_connections[0].fetchval("SELECT count(*) FROM schema_migrations") == 4
            assert await concurrent_connections[0].fetchval(
                "SELECT count(*) FROM campaign_recipient_deliveries"
            ) == 0
        finally:
            for connection in concurrent_connections:
                await connection.close()

        restricted_role = os.environ.get("MIGRATION_TEST_ROLE")
        if restricted_role:
            if not re.fullmatch(r"[A-Za-z0-9_]+", restricted_role):
                pytest.fail("MIGRATION_TEST_ROLE must be a simple local role name")
            permission_admin = await asyncpg.connect(
                dsn, server_settings={"search_path": schemas[3]}
            )
            restricted = None
            try:
                await permission_admin.execute(_legacy_schema_sql())
                await permission_admin.execute(
                    f'REVOKE ALL ON SCHEMA "{schemas[3]}" FROM "{restricted_role}"'
                )
                await permission_admin.execute(
                    f'GRANT USAGE ON SCHEMA "{schemas[3]}" TO "{restricted_role}"'
                )
                restricted = await asyncpg.connect(
                    dsn, server_settings={"search_path": schemas[3]}
                )
                await restricted.execute(f'SET ROLE "{restricted_role}"')
                with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                    await db.apply_migrations(restricted)
                assert await permission_admin.fetchval("SELECT to_regclass('schema_migrations')") is None
                assert await permission_admin.fetchval("SELECT count(*) FROM campaigns") == 1
            finally:
                if restricted:
                    await restricted.close()
                await permission_admin.close()
    finally:
        for schema in schemas:
            await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.close()


def test_versioned_migration_upgrades_legacy_schema_safely_and_repeats():
    asyncio.run(_run_migration_checks(_test_database_url()))
