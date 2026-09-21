from tools.issue_invitation import _psycopg_dsn


def test_issue_invitation_accepts_sqlalchemy_psycopg_dsn() -> None:
    assert (
        _psycopg_dsn("postgresql+psycopg://user:secret@127.0.0.1:5432/db")
        == "postgresql://user:secret@127.0.0.1:5432/db"
    )


def test_issue_invitation_keeps_libpq_dsn_unchanged() -> None:
    dsn = "postgresql://user:secret@127.0.0.1:5432/db"
    assert _psycopg_dsn(dsn) == dsn
