import os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DATABASE_URL", "postgresql://lab:lab@localhost/lab_test")
os.environ.setdefault("LAB_API_KEY", "test-key")

import psycopg, pytest

@pytest.fixture(scope="session", autouse=True)
def fresh_schema():
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    yield
