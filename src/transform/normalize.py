"""원본 문자열을 적재 가능한 값으로 바꾸는 순수 함수 모음.

DB나 API에 의존하는 코드는 두지 않는다. 정제 규칙은 데이터를 보면서 계속 바뀌는데,
입출력이 없는 함수로 분리해두면 규칙을 고칠 때마다 단위 테스트로 바로 확인할 수 있다.
"""

import re
import unicodedata
from datetime import date

_WHITESPACE = re.compile(r"\s+")
_DATE_DIGITS = re.compile(r"[0-9]{8}(?:[0-9]{6})?")

# 다른 이름을 병기한 괄호. 제품을 구분하는 정보가 아니라 별칭이므로 키에서 뺀다.
# 키에서 빼는 괄호는 이것뿐이다. 그 밖의 괄호는 모두 남긴다(_keeps_group 참고).
ALIAS_PREFIXES = ("수출명",)

# 같은 단위를 다르게 적은 표기. 왼쪽을 오른쪽으로 통일한다.
SPELLING_VARIANTS = (("밀리그람", "밀리그램"),)


def clean_text(value: object) -> str | None:
    """공백과 개행을 정리하고 빈 값을 None으로 통일한다.

    원본은 결측을 null로 줄 때와 빈 문자열로 줄 때가 필드마다 다르다.
    두 표기를 하나로 합쳐야 이후 단계에서 결측 판정이 한 가지로 끝난다.

    Args:
        value (object): 원본 값. 문자열이 아니면 문자열로 바꿔 처리한다.

    Returns:
        str | None: 연속 공백과 개행을 공백 하나로 줄이고 양끝을 자른 문자열.
            결과가 비면 None.
    """
    if value is None:
        return None
    text = _WHITESPACE.sub(" ", str(value)).strip()
    return text or None


def parse_date(value: object) -> date | None:
    """8자리 날짜 또는 14자리 일시 문자열을 날짜로 바꾼다.

    실데이터에서 확인한 형식은 YYYYMMDD와 YYYYMMDDHHMMSS 두 가지뿐이다.
    그 밖의 형식은 추측해서 해석하지 않고 예외로 알린다.
    형식이 바뀌었다는 신호를 조용히 삼키면 날짜가 틀린 채로 적재된다.

    Args:
        value (object): 원본 값.

    Returns:
        date | None: 변환한 날짜. 값이 비어 있으면 None.

    Raises:
        ValueError: 지원하지 않는 형식이거나 존재하지 않는 날짜일 때.
    """
    text = clean_text(value)
    if text is None:
        return None
    if not _DATE_DIGITS.fullmatch(text):
        raise ValueError(f"지원하지 않는 날짜 형식: {value!r}")
    return date(int(text[:4]), int(text[4:6]), int(text[6:8]))


def parse_int(value: object) -> int | None:
    """정수 문자열을 정수로 바꾼다.

    Args:
        value (object): 원본 값.

    Returns:
        int | None: 변환한 정수. 값이 비어 있으면 None.

    Raises:
        ValueError: 정수로 해석할 수 없을 때.
    """
    text = clean_text(value)
    return None if text is None else int(text)


def parse_yn(value: object) -> bool:
    """Y/N 표기를 불리언으로 바꾼다.

    Args:
        value (object): 원본 값.

    Returns:
        bool: Y이면 True, N이면 False.

    Raises:
        ValueError: Y나 N이 아닐 때. 빈 값도 여기에 포함된다.
    """
    text = clean_text(value)
    if text == "Y":
        return True
    if text == "N":
        return False
    raise ValueError(f"Y/N이 아닌 값: {value!r}")


def split_multi(value: object, sep: str) -> list[str]:
    """구분자로 이어 붙인 다중값을 목록으로 나눈다.

    원본 순서와 중복을 그대로 유지한다. 성분명은 같은 성분이 반복 나열되는
    사례가 있고(Fentanyl/Fentanyl), 그 순서 자체가 원본 정보이기 때문이다.
    중복 제거가 필요한 곳에서는 호출부가 직접 처리한다.

    Args:
        value (object): 원본 값.
        sep (str): 구분자.

    Returns:
        list[str]: 정리된 값 목록. 빈 조각은 버린다. 값이 비어 있으면 빈 리스트.
    """
    text = clean_text(value)
    if text is None:
        return []
    return [part for part in (clean_text(p) for p in text.split(sep)) if part]


_OPENERS = {"(": ")", "[": "]"}


def split_ingredients(value: object) -> list[str]:
    """성분명 문자열을 성분 목록으로 나눈다.

    구분자는 /이지만 성분명 안에도 /가 들어 있어 단순 분할하면 한 성분이 여러 조각이 난다.
    실데이터에서 확인한 두 가지 경우는 구분자로 보지 않는다.

        - 괄호 안의 /: Teriparatide (rDNA/ Host:E.coli ...), 백신 균주명 [A/Darwin/1415/2025]
        - 뒤에 공백이 붙은 /: Blood Coagulation Factor VIII/ Rurioctocog Alpha Pegol (rDNA)
          이 이름이 두 번 나열된 레코드의 원본 성분 수가 2여서, /+공백은 이름의 일부로 판단했다.

    괄호 밖에서 공백 없이 쓰인 /는 여전히 구분자로 본다. 그래서 N/S-Diacetylcysteine처럼
    괄호 없이 이름 안에 /가 있는 성분은 잘못 나뉜다. 이를 가려낼 기준이 없어 규칙을 더 넓히지 않고,
    원본 성분 수와 대조하는 검증 규칙이 잡아내도록 둔다.

    순서와 중복은 원본 그대로 유지한다(split_multi 참고).

    Args:
        value (object): 원본 성분명 문자열.

    Returns:
        list[str]: 성분명 목록. 빈 조각은 버린다. 값이 비어 있으면 빈 리스트.
    """
    text = clean_text(value)
    if text is None:
        return []

    parts: list[str] = []
    buf: list[str] = []
    closers: list[str] = []

    for i, ch in enumerate(text):
        if ch in _OPENERS:
            closers.append(_OPENERS[ch])
        elif closers and ch == closers[-1]:
            closers.pop()

        next_is_space = i + 1 < len(text) and text[i + 1].isspace()
        if ch == "/" and not closers and not next_is_space:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))

    return [part for part in (clean_text(p) for p in parts) if part]


def split_parentheses(text: str) -> tuple[str, list[str]]:
    """문자열을 괄호 밖 부분과 최상위 괄호 안 내용들로 나눈다.

    중첩 괄호는 바깥 괄호 하나로 묶어 내용을 통째로 돌려준다.
    괄호 짝이 맞지 않으면 어디까지가 괄호인지 판단할 수 없으므로
    나누지 않고 원문을 그대로 돌려준다.

    Args:
        text (str): 나눌 문자열.

    Returns:
        tuple[str, list[str]]: 괄호 밖 문자열과, 최상위 괄호 안 내용의 목록.
    """
    base: list[str] = []
    groups: list[str] = []
    buf: list[str] = []
    depth = 0

    for ch in text:
        if ch == "(":
            if depth > 0:
                buf.append(ch)
            else:
                buf = []
            depth += 1
        elif ch == ")" and depth > 0:
            depth -= 1
            if depth == 0:
                groups.append("".join(buf))
            else:
                buf.append(ch)
        elif depth > 0:
            buf.append(ch)
        else:
            base.append(ch)

    if depth != 0:
        return text, []
    return "".join(base), groups


def _keeps_group(group: str) -> bool:
    """괄호 내용을 제품 키에 남길지 판단한다.

    별칭(수출명)만 버리고 나머지는 모두 남긴다.
    기준은 합쳐지지 말아야 할 제품이 합쳐지는 일을 막는 것이다.
    잘못 쪼개진 제품은 나중에 합칠 수 있지만, 잘못 합쳐진 제품은
    어떤 허가가 어느 제품이었는지 되돌릴 근거가 남지 않는다.

    처음에는 용도 구분자(수출용)와 숫자가 든 괄호만 남기고 성분 나열 괄호는 버렸다.
    그런데 실데이터에서 (사향대체물질함유), (바이알)처럼 숫자도 용도 구분자도 없지만
    처방이나 용기가 다른 제품을 가르는 괄호가 확인됐다. 버려도 되는 괄호를
    목록으로 관리하는 쪽이 남겨야 하는 괄호를 목록으로 관리하는 쪽보다 안전하다.

    Args:
        group (str): 괄호 안 내용.

    Returns:
        bool: 키에 남겨야 하면 True.
    """
    stripped = group.strip()
    return not any(stripped.startswith(prefix) for prefix in ALIAS_PREFIXES)


def normalize_product_key(item_name: str, bizrno: str) -> str:
    """동일 제품 판정에 쓰는 정규화 키를 만든다.

    허가 갱신 시 ITEM_SEQ가 새로 발급되므로 ITEM_SEQ로는 같은 제품을 묶을 수 없다.
    대신 제품명을 정규화하고 사업자번호를 붙여 키로 쓴다.

    처리 순서는 다음과 같다.
        1. NFKC 정규화로 호환 문자를 통일한다(㎍ → μg, 전각 괄호 → 반각 괄호).
        2. 괄호를 분리해 별칭(수출명)은 버리고 나머지는 남긴다.
        3. 표기가 갈리는 단위 이름을 통일한다.
        4. 공백을 모두 지우고 영문을 소문자로 바꾼다.

    Args:
        item_name (str): 원본 품목명.
        bizrno (str): 사업자등록번호.

    Returns:
        str: "사업자번호|정규화된 제품명" 형식의 키.

    Raises:
        ValueError: 정규화 결과 제품명이 비었을 때.
    """
    name = unicodedata.normalize("NFKC", item_name)
    base, groups = split_parentheses(name)
    key_name = base + "".join(g for g in groups if _keeps_group(g))

    for old, new in SPELLING_VARIANTS:
        key_name = key_name.replace(old, new)

    key_name = _WHITESPACE.sub("", key_name).lower()
    if not key_name:
        raise ValueError(f"정규화 후 제품명이 비었음: {item_name!r}")
    return f"{bizrno}|{key_name}"
