"""변환이 끝난 테이블을 검사해 결과를 validation_log에 기록한다.

transform이 레코드 하나를 바꾸면서 알 수 있는 문제를 기록한다면,
이 모듈은 테이블 전체나 테이블 사이를 봐야 알 수 있는 문제를 기록한다.

검사는 모두 SQL의 INSERT ... SELECT로 수행한다. 대상이 수만 건이라
파이썬으로 행을 끌어와 비교하는 것보다 DB 안에서 한 번에 처리하는 편이 빠르고,
규칙 하나가 쿼리 하나로 읽혀 무엇을 검사하는지 코드에서 바로 보인다.

현재 위반이 0건인 규칙도 둔다. 지금 깨끗하다는 것은 이번 배치의 사실일 뿐이고,
원천이 바뀌는 순간을 잡으려면 매 배치 검사해야 한다.
"""

import logging
import uuid
from dataclasses import dataclass

import psycopg

from src.load.db import get_connection
from src.transform.transform import latest_batch

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Rule:
    """검증 규칙 하나.

    Attributes:
        code (str): 규칙 식별자. validation_log.rule_code에 기록된다.
        severity (str): error, warning, info 중 하나.
        source (str): 기준 배치를 고를 소스. permit 또는 recall.
        sql (str): 위반 행을 validation_log에 넣는 쿼리.
            %(batch_id)s, %(rule_code)s, %(severity)s 자리표시자를 쓴다.
    """

    code: str
    severity: str
    source: str
    sql: str


RULES = (
    Rule(
        code="CANCEL_STATE_MISMATCH",
        severity="error",
        source="permit",
        # 정상인데 취소일이 있거나, 정상이 아닌데 취소일이 없으면 상태와 날짜 중 하나가 틀렸다.
        sql="""
            INSERT INTO validation_log (batch_id, rule_code, severity, target_key, detail)
            SELECT %(batch_id)s, %(rule_code)s, %(severity)s, item_seq,
                   jsonb_build_object('cancel_name', cancel_name, 'cancel_date', cancel_date)
              FROM permit
             WHERE (cancel_name = '정상' AND cancel_date IS NOT NULL)
                OR (cancel_name IS DISTINCT FROM '정상' AND cancel_date IS NULL)
        """,
    ),
    Rule(
        code="STDR_CODE_MISMATCH",
        severity="warning",
        source="permit",
        # PRDLST_STDR_CODE를 ITEM_SEQ와 같다고 보고 저장하지 않으므로 그 가정을 원본에서 확인한다.
        sql="""
            INSERT INTO validation_log (batch_id, rule_code, severity, target_key, detail)
            SELECT %(batch_id)s, %(rule_code)s, %(severity)s, payload->>'ITEM_SEQ',
                   jsonb_build_object('prdlst_stdr_code', payload->>'PRDLST_STDR_CODE')
              FROM raw_record
             WHERE batch_id = %(batch_id)s
               AND payload->>'PRDLST_STDR_CODE' IS DISTINCT FROM payload->>'ITEM_SEQ'
        """,
    ),
    Rule(
        code="INGR_CNT_MISMATCH",
        severity="warning",
        source="permit",
        # 원본 성분 수와 성분명을 분해한 개수를 대조한다. 둘 다 비어 있으면 일치로 본다.
        # 어긋나면 구분자가 성분명 안에 섞였거나 원본의 두 필드가 서로 다르다는 뜻이다.
        sql="""
            INSERT INTO validation_log (batch_id, rule_code, severity, target_key, detail)
            SELECT %(batch_id)s, %(rule_code)s, %(severity)s, p.item_seq,
                   jsonb_build_object('ingr_cnt_raw', p.ingr_cnt_raw, 'parsed', coalesce(pi.n, 0))
              FROM permit p
              LEFT JOIN (SELECT item_seq, count(*) AS n
                           FROM permit_ingredient GROUP BY item_seq) pi USING (item_seq)
             WHERE coalesce(p.ingr_cnt_raw, 0) <> coalesce(pi.n, 0)
        """,
    ),
    Rule(
        code="RECALL_UNMATCHED_PERMIT",
        severity="info",
        source="recall",
        # 회수 API는 허가 API보다 품목 범위가 넓어 미연결은 예상되는 현상이다.
        # 경고로 두면 매 배치 수백 건이 쌓여 정작 중요한 경고가 묻히므로 info로 둔다.
        # 비율이 크게 바뀌면 원천 변화의 신호이므로 건수는 계속 기록한다.
        sql="""
            INSERT INTO validation_log (batch_id, rule_code, severity, target_key, detail)
            SELECT %(batch_id)s, %(rule_code)s, %(severity)s,
                   r.item_seq || '|' || r.recall_command_date,
                   jsonb_build_object('product_name', r.product_name_raw)
              FROM recall r
             WHERE r.batch_id = %(batch_id)s
               AND NOT EXISTS (SELECT 1 FROM permit p WHERE p.item_seq = r.item_seq)
        """,
    ),
)


def run_rule(conn: psycopg.Connection, rule: Rule, batch_id: uuid.UUID) -> int:
    """규칙 하나를 실행하고 위반 건수를 돌려준다.

    같은 배치를 다시 검사하면 같은 위반이 또 나오므로 이 규칙의 이전 기록을 먼저 지운다.

    Args:
        conn (psycopg.Connection): 열린 DB 커넥션.
        rule (Rule): 실행할 규칙.
        batch_id (uuid.UUID): 기준 배치.

    Returns:
        int: 기록한 위반 건수.
    """
    params = {"batch_id": batch_id, "rule_code": rule.code, "severity": rule.severity}
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM validation_log WHERE batch_id = %(batch_id)s AND rule_code = %(rule_code)s",
            params,
        )
        cur.execute(rule.sql, params)
        return cur.rowcount


def run_validate(conn: psycopg.Connection) -> dict[str, int]:
    """모든 규칙을 소스별 최신 성공 배치 기준으로 실행한다.

    기준 배치가 없는 소스의 규칙은 건너뛴다.

    Args:
        conn (psycopg.Connection): 열린 DB 커넥션.

    Returns:
        dict[str, int]: 규칙별 위반 건수. 건너뛴 규칙은 포함하지 않는다.
    """
    batches = {source: latest_batch(conn, source) for source in {r.source for r in RULES}}
    summary = {}

    for rule in RULES:
        batch_id = batches[rule.source]
        if batch_id is None:
            logger.warning("기준 배치가 없어 건너뜀 (rule=%s, source=%s)", rule.code, rule.source)
            continue
        summary[rule.code] = run_rule(conn, rule, batch_id)
        logger.info("%s [%s] %d건", rule.code, rule.severity, summary[rule.code])

    return summary


# ================
# 실행부
# ================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    with get_connection() as conn:
        print(run_validate(conn))
