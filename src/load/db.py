"""PostgreSQL 연결을 제공한다.

psycopg 연결을 컨텍스트 매니저로 감싸 트랜잭션과 자원 해제를 위임한다.
호출부마다 connect와 close를 반복하면 예외 발생 시 연결이 남고,
커밋 누락으로 적재가 조용히 사라지는 실수가 생긴다.
with 블록을 벗어날 때 정상 종료면 커밋, 예외면 롤백이 자동으로 수행된다.
"""

from contextlib import contextmanager
from typing import Iterator

import psycopg

from src.config import config

@contextmanager
def get_connection(autocommit: bool = False) -> Iterator[psycopg.Connection]:
    """DB 연결을 열고 블록 종료 시 정리한다.

    autocommit 연결을 따로 둘 수 있게 한 이유는 이력 기록 때문이다.
    적재 트랜잭션과 같은 연결로 이력을 쓰면, 적재가 실패해 롤백될 때
    실패했다는 기록까지 함께 사라진다.

    Args:
        autocommit (bool): True면 문장마다 즉시 커밋한다. 적재 트랜잭션과
            무관하게 남아야 하는 이력 기록용이다. 기본값은 False.

    Yields:
        psycopg.Connection: 열린 커넥션. autocommit이 False이면 블록을
            정상적으로 벗어날 때 커밋되고, 예외가 발생하면 롤백된다.
    """
    conn = psycopg.connect(config.db_dsn(), autocommit=autocommit)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

# ================
# 실행부
# ================

if __name__ == "__main__":
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT version()")
            print(cur.fetchone()[0])