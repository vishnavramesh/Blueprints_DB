from b4p import doctor


def test_describe_target_hides_credentials():
    where, local = doctor.describe_target("postgresql://postgres.abc:s3cretPW@aws-0.pooler.example.com:6543/postgres")
    assert where == "aws-0.pooler.example.com:6543/postgres" and not local
    assert "s3cret" not in where and "postgres.abc" not in where
    assert doctor.describe_target("postgresql:///b4p_v2") == ("local socket/b4p_v2", True)
    assert doctor.describe_target("postgresql://u:p@localhost/x")[1] is True


def test_doctor_on_a_ready_database(conn, monkeypatch, capsys):
    from b4p import cli
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", '{"private_key": "TOPSECRET"}')
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "all 8 dashboard views exist" in out and "TOPSECRET" not in out and "local socket" in out


def test_doctor_is_read_only(conn):
    import psycopg
    import pytest
    from b4p import db
    with db.connect(options="-c default_transaction_read_only=on") as c:
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            c.execute("CREATE TABLE doctor_probe (x int)")
