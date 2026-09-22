"""수집 → 변환 → 검증을 한 번에 실행한다.

단계마다 트랜잭션을 나눈다. 검증이 실패해도 이미 끝난 변환 결과는 남아야 하고,
변환이 실패해도 수집한 raw는 남아야 다시 수집하지 않고 변환만 재실행할 수 있다.

종료 코드로 결과를 알린다. 스케줄러나 CI가 로그를 읽지 않고도
실패와 데이터 오류를 구분할 수 있게 하기 위해서다.

    0: 정상 완료
    1: 실행 중 예외 발생
    2: 실행은 끝났으나 error 등급 검증 결과가 있음

Examples:
    전체 실행::

        python -m src.pipeline

    수집 없이 저장된 raw로 변환과 검증만 다시 실행::

        python -m src.pipeline --skip-ingest
"""

import argparse
import logging
import sys

import psycopg

from src.config import config
from src.load.db import get_connection
from src.load.raw_loader import ingest_sources
from src.transform.transform import latest_batch, run_transform
from src.validate.validate import run_validate

logger = logging.getLogger("pipeline")

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_DATA_ERROR = 2


def count_errors(conn: psycopg.Connection) -> int:
    """소스별 최신 성공 배치에 기록된 error 등급 검증 결과를 센다.

    Args:
        conn (psycopg.Connection): 열린 DB 커넥션.

    Returns:
        int: error 등급 기록 수.
    """
    batch_ids = [b for b in (latest_batch(conn, s) for s in ("permit", "recall")) if b]
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM validation_log WHERE batch_id = ANY(%s) AND severity = 'error'",
            (batch_ids,),
        )
        return cur.fetchone()[0]


def run(skip_ingest: bool = False) -> int:
    """파이프라인 전체를 실행하고 종료 코드를 돌려준다.

    Args:
        skip_ingest (bool): True면 수집을 건너뛰고 저장된 최신 배치로 변환과 검증만 한다.

    Returns:
        int: 종료 코드. 모듈 설명 참고.
    """
    try:
        if skip_ingest:
            logger.info("수집 건너뜀")
        else:
            config.validate()
            logger.info("수집 완료 %s", ingest_sources())

        with get_connection() as conn:
            logger.info("변환 완료 %s", run_transform(conn))

        with get_connection() as conn:
            logger.info("검증 완료 %s", run_validate(conn))
            errors = count_errors(conn)
    except Exception:
        logger.exception("파이프라인 실패")
        return EXIT_FAILED

    if errors:
        logger.error("error 등급 검증 결과 %d건", errors)
        return EXIT_DATA_ERROR
    return EXIT_OK


def main() -> None:
    """명령행 인자를 읽어 파이프라인을 실행하고 종료 코드로 끝낸다."""
    parser = argparse.ArgumentParser(description="의약품 허가·회수 정보 수집 파이프라인")
    parser.add_argument("--skip-ingest", action="store_true",
                        help="수집을 건너뛰고 저장된 최신 배치로 변환과 검증만 실행")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")
    sys.exit(run(skip_ingest=args.skip_ingest))


if __name__ == "__main__":
    main()
