"""API 응답을 raw 테이블에 그대로 적재한다.

정제 없이 원본을 보관하는 이유는 재처리 때문이다.
정제 규칙은 데이터를 보면서 계속 바뀌는데, 원본이 없으면 규칙을 고칠 때마다
API를 다시 전량 호출해야 한다. 일일 트래픽 한도가 있는 공공 API에서는
이 비용이 크고, 그 사이 원본이 바뀌면 이전 결과와 비교할 수도 없다.
"""

import json
import logging
import uuid
from datetime import datetime, timezone

import psycopg

from src.extract.api_client import PAGE_SIZE, extract_items, fetch_page, fetch_total_count

logger = logging.getLogger(__name__)

def start_batch(conn: psycopg.Connection, source: str, total_count: int) -> uuid.UUID:
    """수집 배치를 시작하고 이력 행을 남긴다.

    수집 시작 시점에 먼저 기록하는 이유는 중단된 배치를 식별하기 위해서다.
    완료 후에만 기록하면 실패한 실행은 흔적 없이 사라진다.

    Args:
        conn (psycopg.Connection): autocommit 커넥션. 적재 트랜잭션과
            분리되어 있어야 실패 시에도 이 행이 남는다.
        source (str): 데이터 소스 구분자. permit 또는 recall.
        total_count (int): API가 보고한 전체 레코드 수.

    Returns:
        uuid.UUID: 생성된 배치 식별자.
    """
    batch_id = uuid.uuid4()

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ingest_log (batch_id, source, started_at, status, total_count)
            VALUES (%s, %s, %s, 'running', %s)
            """,
            (batch_id, source, datetime.now(timezone.utc), total_count),
        )
    return batch_id

def finish_batch(
        conn: psycopg.Connection,
        batch_id: uuid.UUID,
        status: str,
        fetched_count: int,
        error_message: str | None = None,
) -> None:
    """배치 종료 상태를 기록한다.

    Args:
        conn (psycopg.Connection): autocommit 커넥션.
        batch_id (uuid.UUID): 대상 배치 식별자.
        status (str): success 또는 failed.
        fetched_count (int): 실제로 수집한 레코드 수.
        error_message (str | None): 실패 시 오류 내용.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE ingest_log
               SET finished_at = %s, status = %s,
                   fetched_count = %s, error_message = %s
             WHERE batch_id = %s
            """,
            (datetime.now(timezone.utc), status, fetched_count, error_message, batch_id),
        )

def save_page(
        conn: psycopg.Connection,
        batch_id: uuid.UUID,
        page_no: int,
        items: list[dict],
) -> int:
    """한 페이지 분량의 레코드를 raw 테이블에 적재한다.
    
    레코드 단위로 저장하는 이유는 이후 정제 단계에서 행 단위로 다루기 때문이다.
    페이지 전체를 한 행에 넣으면 개별 레코드를 조회할 때마다 JSON을 풀어야 한다.

    Args:
        conn (psycopg.Connection): 열린 DB 커넥션.
        batch_id (uuid.UUID): 소속 배치 식별자.
        page_no (int): 원본 페이지 번호. 수집 누락 추적에 사용한다.
        items (list[dict]): 해당 페이지의 레코드 목록.

    Returns:
        int: 적재한 레코드 수.
    """
    if not items:
        return 0

    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO raw_permit (payload, batch_id, page_no)
            VALUES (%s, %s, %s)
            """,
            [(json.dumps(item, ensure_ascii=False), batch_id, page_no) for item in items],
        )
    return len(items)

def ingest_all(
        conn: psycopg.Connection,
        log_conn: psycopg.Connection,
        base_url: str,
        source: str,
) -> uuid.UUID:
    """전체 페이지를 순회하며 raw 테이블에 적재한다.

    페이지마다 커밋하지 않고 배치 전체를 하나의 트랜잭션으로 처리한다.
    중간에 실패하면 부분 적재분이 남지 않으므로, 재실행 시 중복을 걱정할 필요가 없다.

    적재와 이력은 서로 다른 연결로 쓴다. 적재는 전부 아니면 전무여야 하지만
    이력은 실패했을 때 더더욱 남아야 하기 때문이다.
    success는 적재 커밋이 끝난 뒤에 기록한다. 순서가 반대면 이력은 성공인데
    데이터는 없는 상태가 생길 수 있다.

    Args:
        conn (psycopg.Connection): 적재용 커넥션. 이 함수가 커밋 시점을 정한다.
        log_conn (psycopg.Connection): 이력 기록용 autocommit 커넥션.
        base_url (str): API 요청주소.
        source (str): 데이터 소스 구분자.

    Returns:
        uuid.UUID: 이번 배치의 식별자.

    Raises:
        Exception: 수집 중 발생한 예외를 이력에 기록한 뒤 그대로 전파한다.
    """
    total_count = fetch_total_count(base_url)
    total_pages = -(-total_count // PAGE_SIZE)
    batch_id = start_batch(log_conn, source, total_count)

    logger.info("배치 시작 (source=%s, total=%d, pages=%d)", source, total_count, total_pages)

    fetched = 0
    try:
        for page_no in range(1, total_pages + 1):
            payload = fetch_page(base_url, page_no)
            fetched += save_page(conn, batch_id, page_no, extract_items(payload))

            if page_no % 50 == 0 or page_no == total_pages:
                logger.info("진행 %d/%d (%d건)", page_no, total_pages, fetched)
        conn.commit()
    except Exception as exc:
        conn.rollback()
        finish_batch(log_conn, batch_id, "failed", fetched, str(exc))
        raise

    finish_batch(log_conn, batch_id, "success", fetched)

    # API가 보고한 건수와 실제 수집 건수가 다르면 페이지 누락을 의심해야 한다.
    # 수집 도중 원본에 레코드가 추가되는 경우에도 발생할 수 있다.
    if fetched != total_count:
        logger.warning("건수 불일치: 예상 %d, 실제 %d", total_count, fetched)

    return batch_id
