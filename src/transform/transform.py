"""raw 레코드를 정규화 테이블로 변환한다.

소스별로 가장 최근에 성공한 배치 하나만 읽는다. 수집은 매번 전량이라
최신 성공 배치가 곧 원천의 현재 상태이고, 실패한 배치는 부분 데이터일 수 있어 쓰지 않는다.

모든 쓰기는 멱등하다. 마스터와 허가·회수는 자연키 기준 upsert로,
허가에 딸린 성분·EDI코드는 해당 허가의 기존 행을 지우고 다시 넣는 방식으로 처리한다.
같은 배치로 몇 번을 실행해도 결과 테이블이 같아야 재실행을 두려워하지 않을 수 있다.

변환 도중 발견한 문제는 validation_log에 남긴다. 교차 테이블 규칙처럼
변환이 끝나야 판단할 수 있는 검사는 validate 단계의 책임이다.
"""

import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date

import psycopg
from psycopg.types.json import Jsonb

from src.load.db import get_connection
from src.transform.normalize import (
    clean_text,
    normalize_product_key,
    parse_date,
    parse_int,
    parse_yn,
    split_ingredients,
    split_multi,
)

logger = logging.getLogger(__name__)

# 이 모듈이 기록하는 검증 규칙. 재실행 시 같은 배치의 이전 기록을 지우는 범위로 쓴다.
TRANSFORM_RULES = (
    "REQUIRED_FIELD_MISSING",
    "VALUE_FORMAT_INVALID",
    "PRODUCT_REVIEW_NEEDED",
    "RECALL_SOURCE_DUPLICATE",
)


@dataclass
class Issue:
    """변환 중 발견한 데이터 문제 한 건.

    Attributes:
        batch_id (uuid.UUID): 문제가 발견된 raw 배치.
        rule_code (str): 검증 규칙 식별자.
        severity (str): error, warning, info 중 하나.
        target_key (str): 대상 레코드 식별자.
        detail (dict): 판단 근거가 되는 원본 값.
    """

    batch_id: uuid.UUID
    rule_code: str
    severity: str
    target_key: str
    detail: dict


class IssueCollector:
    """변환 중 발생한 문제를 모으고, 값 변환 실패를 문제로 바꿔준다.

    변환 함수마다 try/except를 반복하지 않도록 한 곳에 모았다.
    값 하나가 이상하다고 배치 전체를 멈추면 나머지 4만여 건도 반영되지 않으므로,
    해당 값만 비우고 기록을 남긴 뒤 계속 진행한다.

    Attributes:
        batch_id (uuid.UUID): 현재 처리 중인 배치.
        issues (list[Issue]): 모인 문제 목록.
    """

    def __init__(self, batch_id: uuid.UUID) -> None:
        """수집기를 만든다.

        Args:
            batch_id (uuid.UUID): 현재 처리 중인 배치.
        """
        self.batch_id = batch_id
        self.issues: list[Issue] = []

    def add(self, rule_code: str, severity: str, target_key: str, detail: dict) -> None:
        """문제 한 건을 기록한다.

        Args:
            rule_code (str): 검증 규칙 식별자.
            severity (str): error, warning, info 중 하나.
            target_key (str): 대상 레코드 식별자.
            detail (dict): 판단 근거가 되는 원본 값.
        """
        self.issues.append(Issue(self.batch_id, rule_code, severity, target_key, detail))

    def convert(self, func, value: object, field: str, target_key: str):
        """값을 변환하고, 실패하면 None을 돌려주며 문제로 기록한다.

        Args:
            func (Callable[[object], object]): 변환 함수.
            value (object): 원본 값.
            field (str): 원본 필드명. 기록에 남긴다.
            target_key (str): 대상 레코드 식별자.

        Returns:
            object | None: 변환 결과. 실패하면 None.
        """
        try:
            return func(value)
        except ValueError as exc:
            self.add("VALUE_FORMAT_INVALID", "error", target_key,
                     {"field": field, "value": value, "reason": str(exc)})
            return None


def latest_batch(conn: psycopg.Connection, source: str) -> uuid.UUID | None:
    """소스의 가장 최근 성공 배치를 찾는다.

    Args:
        conn (psycopg.Connection): 열린 DB 커넥션.
        source (str): 데이터 소스 구분자.

    Returns:
        uuid.UUID | None: 배치 식별자. 성공한 배치가 없으면 None.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT batch_id FROM ingest_log
             WHERE source = %s AND status = 'success'
             ORDER BY finished_at DESC
             LIMIT 1
            """,
            (source,),
        )
        row = cur.fetchone()
    return row[0] if row else None


def read_payloads(conn: psycopg.Connection, batch_id: uuid.UUID) -> list[dict]:
    """배치의 원본 레코드를 적재 순서대로 읽는다.

    Args:
        conn (psycopg.Connection): 열린 DB 커넥션.
        batch_id (uuid.UUID): 읽을 배치.

    Returns:
        list[dict]: 원본 레코드 목록.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT payload FROM raw_record WHERE batch_id = %s ORDER BY id", (batch_id,))
        return [row[0] for row in cur.fetchall()]


def _id_map(conn: psycopg.Connection, sql: str) -> dict:
    """키와 대리키 쌍을 조회해 딕셔너리로 돌려준다.

    Args:
        conn (psycopg.Connection): 열린 DB 커넥션.
        sql (str): 키, 대리키 순서로 두 컬럼을 고르는 쿼리.

    Returns:
        dict: 키에서 대리키로의 매핑.
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        return dict(cur.fetchall())


# ================
# 허가정보
# ================

def parse_permit(payload: dict, issues: IssueCollector) -> dict | None:
    """허가정보 원본 한 건을 적재용 값으로 바꾼다.

    Args:
        payload (dict): 원본 레코드.
        issues (IssueCollector): 문제 기록기.

    Returns:
        dict | None: 적재용 값. 필수 필드가 없어 적재할 수 없으면 None.
    """
    item_seq = clean_text(payload.get("ITEM_SEQ"))
    bizrno = clean_text(payload.get("BIZRNO"))
    item_name = payload.get("ITEM_NAME")
    target = item_seq or "(ITEM_SEQ 없음)"

    missing = [f for f, v in (("ITEM_SEQ", item_seq), ("BIZRNO", bizrno),
                              ("ITEM_NAME", clean_text(item_name))) if v is None]
    if missing:
        issues.add("REQUIRED_FIELD_MISSING", "error", target, {"fields": missing})
        return None

    try:
        product_key = normalize_product_key(item_name, bizrno)
    except ValueError as exc:
        issues.add("VALUE_FORMAT_INVALID", "error", target,
                   {"field": "ITEM_NAME", "value": item_name, "reason": str(exc)})
        return None

    return {
        "item_seq": item_seq,
        "bizrno": bizrno,
        "product_key": product_key,
        "item_name_raw": item_name,
        "name_ko": clean_text(item_name),
        "name_en": clean_text(payload.get("ITEM_ENG_NAME")),
        "entp_name": clean_text(payload.get("ENTP_NAME")),
        "entp_eng_name": clean_text(payload.get("ENTP_ENG_NAME")),
        "spclty_pblc": clean_text(payload.get("SPCLTY_PBLC")),
        "permit_date": issues.convert(parse_date, payload.get("ITEM_PERMIT_DATE"),
                                      "ITEM_PERMIT_DATE", target),
        "cancel_date": issues.convert(parse_date, payload.get("CANCEL_DATE"),
                                      "CANCEL_DATE", target),
        "cancel_name": clean_text(payload.get("CANCEL_NAME")),
        "induty": clean_text(payload.get("INDUTY")),
        "prduct_type": clean_text(payload.get("PRDUCT_TYPE")),
        "permit_kind_code": clean_text(payload.get("PERMIT_KIND_CODE")),
        "prduct_prmisn_no": clean_text(payload.get("PRDUCT_PRMISN_NO")),
        "ingr_cnt_raw": issues.convert(parse_int, payload.get("ITEM_INGR_CNT"),
                                       "ITEM_INGR_CNT", target),
        "ingredients": split_ingredients(payload.get("ITEM_INGR_NAME")),
        # 같은 코드가 두 번 적혀 있으면 PK (item_seq, edi_code)에 걸리므로 순서를 지키며 중복을 없앤다.
        "edi_codes": list(dict.fromkeys(split_multi(payload.get("EDI_CODE"), ","))),
    }


def _latest_first(rows: list[dict]) -> list[dict]:
    """허가일이 늦은 순으로 정렬한다. 허가일이 같으면 ITEM_SEQ가 큰 쪽이 앞선다.

    대표값을 고를 때 쓴다. 가장 최근 허가가 현재 제품 정보에 가장 가깝다고 본다.

    Args:
        rows (list[dict]): parse_permit 결과 목록.

    Returns:
        list[dict]: 정렬된 새 목록.
    """
    return sorted(rows, key=lambda r: (r["permit_date"] or date.min, r["item_seq"]), reverse=True)


def upsert_companies(conn: psycopg.Connection, rows: list[dict]) -> dict[str, int]:
    """허가정보에 나온 업체를 사업자번호 기준으로 반영한다.

    같은 사업자번호에 업체명이 여러 개 나오는 경우 가장 최근 허가의 이름을 쓴다.

    Args:
        conn (psycopg.Connection): 열린 DB 커넥션.
        rows (list[dict]): parse_permit 결과 목록.

    Returns:
        dict[str, int]: 사업자번호에서 company_id로의 매핑.
    """
    companies: dict[str, dict] = {}
    for row in _latest_first(rows):
        companies.setdefault(row["bizrno"], row)

    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO company (bizrno, name_ko, name_en)
            VALUES (%s, %s, %s)
            ON CONFLICT (bizrno) DO UPDATE
               SET name_ko = EXCLUDED.name_ko,
                   name_en = EXCLUDED.name_en,
                   updated_at = now()
            """,
            [(b, r["entp_name"], r["entp_eng_name"]) for b, r in companies.items()],
        )
    return _id_map(conn, "SELECT bizrno, company_id FROM company")


def upsert_ingredients(conn: psycopg.Connection, rows: list[dict]) -> dict[str, int]:
    """허가정보에 나온 성분을 영문명 기준으로 반영한다.

    Args:
        conn (psycopg.Connection): 열린 DB 커넥션.
        rows (list[dict]): parse_permit 결과 목록.

    Returns:
        dict[str, int]: 영문 성분명에서 ingredient_id로의 매핑.
    """
    names = sorted({name for row in rows for name in row["ingredients"]})
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO ingredient (name_en) VALUES (%s) ON CONFLICT (name_en) DO NOTHING",
            [(n,) for n in names],
        )
    return _id_map(conn, "SELECT name_en, ingredient_id FROM ingredient")


def upsert_products(
        conn: psycopg.Connection,
        rows: list[dict],
        company_ids: dict[str, int],
        issues: IssueCollector,
) -> dict[str, int]:
    """허가를 정규화 키로 묶어 제품을 반영한다.

    같은 키로 묶인 허가끼리 다음 중 하나에 해당하면 정말 같은 제품인지
    자동으로 판단할 수 없다. 이때 억지로 확정하지 않고 review_needed로 표시해
    사람이 확인하게 남긴다.

        - 성분 구성이 다르다.
        - 전문/일반 구분이 다르다.
        - 정상 상태인 허가가 둘 이상이다. 갱신이라면 이전 허가는 만료나 취하로
          끝나 있어야 한다. 둘 다 살아 있다면 이름만 같은 별개 품목일 수 있다.

    Args:
        conn (psycopg.Connection): 열린 DB 커넥션.
        rows (list[dict]): parse_permit 결과 목록.
        company_ids (dict[str, int]): 사업자번호에서 company_id로의 매핑.
        issues (IssueCollector): 문제 기록기.

    Returns:
        dict[str, int]: 정규화 키에서 product_id로의 매핑.
    """
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        groups[row["product_key"]].append(row)

    values = []
    for key, members in groups.items():
        rep = _latest_first(members)[0]
        ingredient_sets = {tuple(sorted(set(m["ingredients"]))) for m in members}
        spclty_values = {m["spclty_pblc"] for m in members}
        active = [m["item_seq"] for m in members if m["cancel_name"] == "정상"]
        review_needed = len(ingredient_sets) > 1 or len(spclty_values) > 1 or len(active) > 1

        if review_needed:
            issues.add("PRODUCT_REVIEW_NEEDED", "warning", key, {
                "item_seqs": sorted(m["item_seq"] for m in members),
                "ingredient_variants": len(ingredient_sets),
                "spclty_variants": sorted(v or "" for v in spclty_values),
                "active_permits": sorted(active),
            })

        values.append((key, rep["name_ko"], rep["name_en"], company_ids[rep["bizrno"]],
                       rep["spclty_pblc"], review_needed))

    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO product
                   (normalized_key, name_ko, name_en, company_id, spclty_pblc, review_needed)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (normalized_key) DO UPDATE
               SET name_ko = EXCLUDED.name_ko,
                   name_en = EXCLUDED.name_en,
                   company_id = EXCLUDED.company_id,
                   spclty_pblc = EXCLUDED.spclty_pblc,
                   review_needed = EXCLUDED.review_needed,
                   updated_at = now()
            """,
            values,
        )
    return _id_map(conn, "SELECT normalized_key, product_id FROM product")


def upsert_permits(
        conn: psycopg.Connection,
        rows: list[dict],
        product_ids: dict[str, int],
        ingredient_ids: dict[str, int],
        batch_id: uuid.UUID,
) -> None:
    """허가 건과 딸린 성분·EDI코드를 반영한다.

    성분과 EDI코드는 허가 건의 속성 목록이라 행 단위로 비교해 고치기보다
    해당 허가의 기존 행을 모두 지우고 다시 넣는다. 원본에서 성분 하나가 빠졌을 때
    upsert만으로는 빠진 행이 남지만, 교체 방식이면 원본과 정확히 같아진다.

    Args:
        conn (psycopg.Connection): 열린 DB 커넥션.
        rows (list[dict]): parse_permit 결과 목록.
        product_ids (dict[str, int]): 정규화 키에서 product_id로의 매핑.
        ingredient_ids (dict[str, int]): 영문 성분명에서 ingredient_id로의 매핑.
        batch_id (uuid.UUID): 원본 배치 식별자.
    """
    item_seqs = [r["item_seq"] for r in rows]

    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO permit
                   (item_seq, product_id, item_name_raw, permit_date, cancel_date, cancel_name,
                    induty, prduct_type, permit_kind_code, prduct_prmisn_no, ingr_cnt_raw, batch_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (item_seq) DO UPDATE
               SET product_id = EXCLUDED.product_id,
                   item_name_raw = EXCLUDED.item_name_raw,
                   permit_date = EXCLUDED.permit_date,
                   cancel_date = EXCLUDED.cancel_date,
                   cancel_name = EXCLUDED.cancel_name,
                   induty = EXCLUDED.induty,
                   prduct_type = EXCLUDED.prduct_type,
                   permit_kind_code = EXCLUDED.permit_kind_code,
                   prduct_prmisn_no = EXCLUDED.prduct_prmisn_no,
                   ingr_cnt_raw = EXCLUDED.ingr_cnt_raw,
                   batch_id = EXCLUDED.batch_id,
                   updated_at = now()
            """,
            [
                (r["item_seq"], product_ids[r["product_key"]], r["item_name_raw"],
                 r["permit_date"], r["cancel_date"], r["cancel_name"], r["induty"],
                 r["prduct_type"], r["permit_kind_code"], r["prduct_prmisn_no"],
                 r["ingr_cnt_raw"], batch_id)
                for r in rows
            ],
        )

        cur.execute("DELETE FROM permit_ingredient WHERE item_seq = ANY(%s)", (item_seqs,))
        cur.executemany(
            "INSERT INTO permit_ingredient (item_seq, ingredient_id, position) VALUES (%s, %s, %s)",
            [
                (r["item_seq"], ingredient_ids[name], pos)
                for r in rows
                for pos, name in enumerate(r["ingredients"], start=1)
            ],
        )

        cur.execute("DELETE FROM permit_edi_code WHERE item_seq = ANY(%s)", (item_seqs,))
        cur.executemany(
            "INSERT INTO permit_edi_code (item_seq, edi_code) VALUES (%s, %s)",
            [(r["item_seq"], code) for r in rows for code in r["edi_codes"]],
        )


def delete_orphans(conn: psycopg.Connection) -> dict[str, int]:
    """어떤 허가에도 연결되지 않은 제품과 성분을 지운다.

    upsert는 새 키를 넣을 뿐 옛 키를 지우지 않는다. 제품 키 규칙이나 성분 분해 규칙을
    고치면 이전 규칙으로 만든 행이 아무 허가도 가리키지 않은 채 남는다.
    이런 행은 기록할 문제가 아니라 치워야 할 잔여물이므로 변환 마지막에 정리한다.

    Args:
        conn (psycopg.Connection): 열린 DB 커넥션.

    Returns:
        dict[str, int]: 테이블별 지운 행 수.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM product pr
             WHERE NOT EXISTS (SELECT 1 FROM permit p WHERE p.product_id = pr.product_id)
            """
        )
        products = cur.rowcount
        cur.execute(
            """
            DELETE FROM ingredient i
             WHERE NOT EXISTS (SELECT 1 FROM permit_ingredient pi
                                WHERE pi.ingredient_id = i.ingredient_id)
            """
        )
        return {"product": products, "ingredient": cur.rowcount}


def transform_permits(conn: psycopg.Connection, batch_id: uuid.UUID) -> tuple[int, IssueCollector]:
    """허가정보 배치 하나를 정규화 테이블에 반영한다.

    업체·성분·제품을 먼저 반영해야 허가 건이 그 대리키를 참조할 수 있다.
    허가를 모두 반영한 뒤에야 어느 제품이 비었는지 알 수 있으므로 정리는 마지막에 한다.

    Args:
        conn (psycopg.Connection): 열린 DB 커넥션.
        batch_id (uuid.UUID): 허가정보 배치.

    Returns:
        tuple[int, IssueCollector]: 반영한 허가 건수와 문제 기록기.
    """
    issues = IssueCollector(batch_id)
    rows = [r for r in (parse_permit(p, issues) for p in read_payloads(conn, batch_id)) if r]

    company_ids = upsert_companies(conn, rows)
    ingredient_ids = upsert_ingredients(conn, rows)
    product_ids = upsert_products(conn, rows, company_ids, issues)
    upsert_permits(conn, rows, product_ids, ingredient_ids, batch_id)

    orphans = delete_orphans(conn)
    if any(orphans.values()):
        logger.info("연결이 없는 행 정리 %s", orphans)

    return len(rows), issues


# ================
# 회수정보
# ================

def parse_recall(payload: dict, issues: IssueCollector) -> dict | None:
    """회수정보 원본 한 건을 적재용 값으로 바꾼다.

    Args:
        payload (dict): 원본 레코드.
        issues (IssueCollector): 문제 기록기.

    Returns:
        dict | None: 적재용 값. 자연키나 필수 필드를 만들 수 없으면 None.
    """
    item_seq = clean_text(payload.get("ITEM_SEQ"))
    target = item_seq or "(ITEM_SEQ 없음)"
    reason = clean_text(payload.get("RTRVL_RESN"))
    product = clean_text(payload.get("PRDUCT"))

    missing = [f for f, v in (("ITEM_SEQ", item_seq), ("RTRVL_RESN", reason),
                              ("PRDUCT", product)) if v is None]
    if missing:
        issues.add("REQUIRED_FIELD_MISSING", "error", target, {"fields": missing})
        return None

    recall_date = issues.convert(parse_date, payload.get("RECALL_COMMAND_DATE"),
                                 "RECALL_COMMAND_DATE", target)
    is_enforced = issues.convert(parse_yn, payload.get("ENFRC_YN"), "ENFRC_YN", target)
    if recall_date is None or is_enforced is None:
        # 변환 실패는 convert가 이미 기록했다. 날짜가 비어 온 경우만 따로 남긴다.
        if clean_text(payload.get("RECALL_COMMAND_DATE")) is None:
            issues.add("REQUIRED_FIELD_MISSING", "error", target, {"fields": ["RECALL_COMMAND_DATE"]})
        return None

    return {
        "item_seq": item_seq,
        "recall_command_date": recall_date,
        "rtrvl_cmmnd_date": issues.convert(parse_date, payload.get("RTRVL_CMMND_DT"),
                                           "RTRVL_CMMND_DT", target),
        "reason": reason,
        "is_enforced": is_enforced,
        "product_name_raw": product,
        "company_name_raw": clean_text(payload.get("ENTRPS")),
        "bizrno": clean_text(payload.get("BIZRNO")),
        "std_codes": split_multi(payload.get("STD_CD"), ",") or None,
        "payload": payload,
    }


def transform_recalls(conn: psycopg.Connection, batch_id: uuid.UUID) -> tuple[int, IssueCollector]:
    """회수정보 배치 하나를 recall 테이블에 반영한다.

    자연키가 같은 원본 행은 한 행으로 합치고 원 행 수를 source_row_count에 남긴다.
    합친 행들의 원본이 완전히 같으면 info, 조금이라도 다르면 warning으로 기록한다.
    다르다면 합치면서 버려진 값이 있다는 뜻이라 확인이 필요하다.

    Args:
        conn (psycopg.Connection): 열린 DB 커넥션.
        batch_id (uuid.UUID): 회수정보 배치.

    Returns:
        tuple[int, IssueCollector]: 반영한 회수 건수와 문제 기록기.
    """
    issues = IssueCollector(batch_id)
    groups: dict[tuple, list[dict]] = defaultdict(list)

    for payload in read_payloads(conn, batch_id):
        row = parse_recall(payload, issues)
        if row:
            groups[(row["item_seq"], row["recall_command_date"], row["reason"])].append(row)

    values = []
    for key, members in groups.items():
        rep = members[0]
        if len(members) > 1:
            identical = all(m["payload"] == rep["payload"] for m in members)
            issues.add("RECALL_SOURCE_DUPLICATE", "info" if identical else "warning",
                       f"{key[0]}|{key[1].isoformat()}",
                       {"rows": len(members), "identical": identical, "reason": key[2]})

        values.append((rep["item_seq"], rep["recall_command_date"], rep["rtrvl_cmmnd_date"],
                       rep["reason"], rep["is_enforced"], rep["product_name_raw"],
                       rep["company_name_raw"], rep["bizrno"], rep["std_codes"],
                       len(members), batch_id))

    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO recall
                   (item_seq, recall_command_date, rtrvl_cmmnd_date, reason, is_enforced,
                    product_name_raw, company_name_raw, bizrno, std_codes,
                    source_row_count, batch_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (item_seq, recall_command_date, reason) DO UPDATE
               SET rtrvl_cmmnd_date = EXCLUDED.rtrvl_cmmnd_date,
                   is_enforced = EXCLUDED.is_enforced,
                   product_name_raw = EXCLUDED.product_name_raw,
                   company_name_raw = EXCLUDED.company_name_raw,
                   bizrno = EXCLUDED.bizrno,
                   std_codes = EXCLUDED.std_codes,
                   source_row_count = EXCLUDED.source_row_count,
                   batch_id = EXCLUDED.batch_id,
                   updated_at = now()
            """,
            values,
        )

    return len(values), issues


# ================
# 검증 기록
# ================

def write_issues(conn: psycopg.Connection, collectors: list[IssueCollector]) -> int:
    """변환 중 모인 문제를 validation_log에 기록한다.

    같은 배치를 다시 변환하면 같은 문제가 또 나오므로, 이 모듈이 남긴
    해당 배치의 이전 기록을 먼저 지운다. 지우지 않으면 재실행할 때마다
    문제 건수가 불어나 품질 리포트를 믿을 수 없게 된다.

    Args:
        conn (psycopg.Connection): 열린 DB 커넥션.
        collectors (list[IssueCollector]): 배치별 문제 기록기.

    Returns:
        int: 기록한 문제 건수.
    """
    batch_ids = [c.batch_id for c in collectors]
    issues = [i for c in collectors for i in c.issues]

    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM validation_log WHERE batch_id = ANY(%s) AND rule_code = ANY(%s)",
            (batch_ids, list(TRANSFORM_RULES)),
        )
        cur.executemany(
            """
            INSERT INTO validation_log (batch_id, rule_code, severity, target_key, detail)
            VALUES (%s, %s, %s, %s, %s)
            """,
            [(i.batch_id, i.rule_code, i.severity, i.target_key, Jsonb(i.detail)) for i in issues],
        )
    return len(issues)


def run_transform(conn: psycopg.Connection) -> dict[str, int]:
    """허가정보와 회수정보의 최신 성공 배치를 변환한다.

    한 트랜잭션 안에서 수행하므로 중간에 실패하면 아무것도 반영되지 않는다.

    Args:
        conn (psycopg.Connection): 열린 DB 커넥션.

    Returns:
        dict[str, int]: 소스별 반영 건수와 기록한 문제 건수.

    Raises:
        RuntimeError: 허가정보에 성공한 배치가 없을 때. 회수는 허가 없이 의미가 없다.
    """
    permit_batch = latest_batch(conn, "permit")
    if permit_batch is None:
        raise RuntimeError("허가정보에 성공한 수집 배치가 없음")

    permit_count, permit_issues = transform_permits(conn, permit_batch)
    logger.info("허가 반영 %d건 (batch=%s)", permit_count, permit_batch)
    collectors = [permit_issues]
    summary = {"permit": permit_count}

    recall_batch = latest_batch(conn, "recall")
    if recall_batch is None:
        logger.warning("회수정보에 성공한 수집 배치가 없어 건너뜀")
    else:
        recall_count, recall_issues = transform_recalls(conn, recall_batch)
        logger.info("회수 반영 %d건 (batch=%s)", recall_count, recall_batch)
        collectors.append(recall_issues)
        summary["recall"] = recall_count

    summary["issues"] = write_issues(conn, collectors)
    logger.info("검증 기록 %d건", summary["issues"])
    return summary


# ================
# 실행부
# ================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    with get_connection() as conn:
        print(run_transform(conn))
