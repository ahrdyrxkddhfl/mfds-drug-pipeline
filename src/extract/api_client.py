"""공공데이터포털 API를 호출해 응답을 가져온다.

전량 수집을 전제로 페이지 단위 조회와 재시도를 담당한다.
증분 수집을 쓰지 않는 이유는 API에 날짜 기준 조회나 정렬 옵션이 없고,
응답이 업종(INDUTY) 단위로 묶여 있어 시간순으로 나열되지 않기 때문이다.
매 배치마다 전량을 받아 적재 단계에서 변경분을 판별한다.

정제나 적재는 이 모듈의 책임이 아니다.
API 응답을 그대로 돌려주고, 가공은 transform 계층에서 수행한다.
"""

import logging

import requests
from tenacity import(
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential
)

from src.config import config

logger = logging.getLogger(__name__)

# 한 번에 받아올 레코드 수.
# 상한은 10000이지만 응답이 커질수록 타임아웃과 재시도 비용이 함께 늘어난다.
# 100건은 전체 약 430회 요청으로, 실패 시 손실 범위가 작다.
PAGE_SIZE = 100

# 공공 API는 응답이 느린 편이라 넉넉하게 잡는다.
TIMEOUT_SECONDS = 30

class ApiError(Exception):
    """API가 정상 응답이 아닌 결과를 반환했을 때 발생한다."""

@retry(
    retry=retry_if_exception_type((requests.RequestException, ApiError)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
def fetch_page(base_url: str, page_no: int, page_size: int = PAGE_SIZE) -> dict:
    """지정한 페이지의 응답을 가져온다.

    네트워크 오류와 API 오류 응답에 대해 최대 3회까지 지수 백오프로 재시도한다.
    공공 API는 일시적으로 응답하지 않는 경우가 있어,
    한 번의 실패로 전체 배치를 중단시키지 않기 위한 장치다.

    Args:
        base_url (str): API 요청주소. 쿼리스트링을 제외한 엔드포인트.
        page_no (int): 조회할 페이지 번호. 1부터 시작한다.
        page_size (int): 페이지당 레코드 수.

    Returns:
        dict: 파싱된 JSON 응답 전체. header와 body를 모두 포함한다.

    Raises:
        ApiError: 응답의 resultCode가 정상(00)이 아닐 때.
        requests.RequestException: 3회 재시도 후에도 요청이 실패했을 때.
    """
    params = {
        "serviceKey": config.API_KEY,
        "pageNo": page_no,
        "numOfRows": page_size,
        "type": "json",
    }

    response = requests.get(base_url, params=params, timeout=TIMEOUT_SECONDS)
    response.raise_for_status()

    payload = response.json()

    # HTTP 200이어도 본문에 오류 코드가 담겨 오는 경우가 있어 별도로 확인한다.
    # 인증키 오류나 일일 호출 한도 초과가 이 형태로 전달된다.
    result_code = payload.get("header", {}).get("resultCode")
    if result_code != "00":
        result_msg = payload.get("header", {}).get("resultMsg", "")
        raise ApiError(f"API 오류(page={page_no}: {result_code} {result_msg}")

    return payload

def fetch_total_count(base_url: str) -> int:
    """전체 레코드 수를 조회한다.

    수집을 시작하기 전 총 페이지 수를 계산하고,
    완료 후 누락 여부를 대조하기 위해 사용한다.

    Args:
        base_url (str): API 요청주소.

    Returns:
        int: API가 보고한 전체 레코드 수.
    """
    payload = fetch_page(base_url, page_no=1, page_size=1)
    return payload["body"]["totalCount"]

def extract_items(payload: dict) -> list[dict]:
    """응답에서 레코드 목록만 꺼낸다.

    Args:
        payload (dict): fetch_page가 반환한 응답 전체.
    
    Returns:
        list[dict]: 레코드 목록. 해당 페이지에 데이터가 없으면 빈 리스트.
    """
    return payload.get("body", {}).get("items", [])

# ================
# 실행부
# ================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    total = fetch_total_count(config.PERMIT_API_URL)
    print(f"전체 건수: {total:,}")
    print(f"페이지 수: {-(-total // PAGE_SIZE):,} (page_size={PAGE_SIZE})")

    payload = fetch_page(config.PERMIT_API_URL, page_no=1)
    items = extract_items(payload)
    print(f"첫 페이지 수집: {len(items)}건")
    print(f"첫 레코드: {items[0]['ITEM_NAME']}")

    print()
    recall_total = fetch_total_count(config.RECALL_API_URL)
    print(f"회수 전체 건수: {recall_total:,}")

    recall_payload = fetch_page(config.RECALL_API_URL, page_no=1, page_size=3)
    for item in extract_items(recall_payload):
        print(item)