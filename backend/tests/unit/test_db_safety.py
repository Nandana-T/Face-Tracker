from __future__ import annotations

import pytest

from tests.db_safety import check_test_database

DEV = "postgresql://postgres:secret@db.abcdwxyz.supabase.co:5432/postgres?sslmode=require"
LOCAL = "postgresql://postgres:@127.0.0.1:54844/postgres"


def test_local_database_is_allowed() -> None:
    check_test_database(LOCAL, DEV)


def test_the_development_database_is_refused_however_it_is_spelled() -> None:
    for same in (
        DEV,
        DEV.replace("postgresql://", "postgresql+psycopg://"),
        DEV.split("?")[0],
        DEV.replace("secret", "another-password"),  # credentials are irrelevant to identity
    ):
        with pytest.raises(RuntimeError, match="same database as DATABASE_URL"):
            check_test_database(same, DEV)


def test_hosted_providers_need_an_explicit_opt_in() -> None:
    other = "postgresql://postgres:x@db.zzzzzzzz.supabase.co:5432/postgres"
    with pytest.raises(RuntimeError, match="hosted provider"):
        check_test_database(other, DEV)
    check_test_database(other, DEV, allow_hosted=True)  # dedicated test project, explicitly allowed
    with pytest.raises(RuntimeError, match="same database"):
        check_test_database(DEV, DEV, allow_hosted=True)  # ...but never the dev database itself


def test_a_different_database_name_on_the_same_server_is_a_different_database() -> None:
    check_test_database(
        "postgresql://u:p@localhost:5432/katomaran_test",
        "postgresql://u:p@localhost:5432/katomaran",
    )
