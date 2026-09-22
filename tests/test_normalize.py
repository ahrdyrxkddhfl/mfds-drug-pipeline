"""normalize 모듈 단위 테스트.

케이스는 대부분 실데이터에서 발견한 사례를 그대로 옮겼다.
각 테스트가 규칙 하나의 근거를 고정하므로, 규칙을 바꾸면 어떤 판단이 뒤집히는지 여기서 드러난다.
알려진 한계도 현재 동작 그대로 테스트로 남겨, 한계가 바뀌면 의도한 변경인지 확인하게 한다.
"""

from datetime import date

import pytest

from src.transform.normalize import (
    clean_text,
    normalize_product_key,
    parse_date,
    parse_int,
    parse_yn,
    split_ingredients,
    split_multi,
    split_parentheses,
)

BIZ = "1111111111"


# ================
# clean_text
# ================

@pytest.mark.parametrize("value", [None, "", "   ", "\r\n"])
def test_clean_text_treats_null_and_blank_as_none(value):
    """null과 빈 문자열이 필드마다 섞여 오므로 둘 다 None으로 통일한다."""
    assert clean_text(value) is None


def test_clean_text_collapses_whitespace_and_newlines():
    assert clean_text("  비타민10㎍정\r\n  ") == "비타민10㎍정"
    assert clean_text("A  B\nC") == "A B C"


# ================
# parse_date / parse_int / parse_yn
# ================

@pytest.mark.parametrize("value, expected", [
    ("20260916", date(2026, 9, 16)),
    ("20260916000000", date(2026, 9, 16)),  # RTRVL_CMMND_DT의 14자리 일시
    (None, None),
    ("", None),
])
def test_parse_date_accepts_observed_formats(value, expected):
    assert parse_date(value) == expected


@pytest.mark.parametrize("value", ["2026-09-16", "202609", "2026091612", "20261332"])
def test_parse_date_rejects_other_formats(value):
    """확인하지 않은 형식은 추측하지 않고 예외로 알린다."""
    with pytest.raises(ValueError):
        parse_date(value)


def test_parse_int():
    assert parse_int("12") == 12
    assert parse_int(None) is None
    with pytest.raises(ValueError):
        parse_int("12개")


def test_parse_yn():
    assert parse_yn("Y") is True
    assert parse_yn("N") is False
    for value in (None, "", "?", "y"):
        with pytest.raises(ValueError):
            parse_yn(value)


# ================
# split_multi (EDI 코드, 표준코드)
# ================

def test_split_multi_trims_and_drops_empty_parts():
    assert split_multi("111, 222,,333 ", ",") == ["111", "222", "333"]
    assert split_multi("", ",") == []
    assert split_multi(None, ",") == []


def test_split_multi_keeps_order_and_duplicates():
    """중복 제거는 호출부 책임이다. 순서 자체가 원본 정보일 수 있다."""
    assert split_multi("052402330,052402330", ",") == ["052402330", "052402330"]


# ================
# split_ingredients
# ================

@pytest.mark.parametrize("value, expected_count", [
    ("Amlodipine Besylate/Rosuvastatin Calcium/Telmisartan", 3),
    # 같은 성분 반복 나열. 원본 성분 수도 2
    ("Fentanyl/Fentanyl", 2),
    # 괄호 안의 /는 구분자가 아니다
    ("Teriparatide (rDNA/ Host:E.coli K12RQ228/ Vector:pHMM193)", 1),
    ("SARS-CoV-2 Spike Protein encoded messenger Ribonucleic Acid(tozinameran)"
     "(Host: DH10B/ Vector: pST4-1525)", 1),
    # 대괄호 안의 백신 균주명
    ("Purified Inactivated Influenza Virus Antigen A[A/Darwin/1415/2025(CVR-400)(H3N2)]"
     "/Purified Inactivated Influenza Virus Antigen B[B/Tokyo/EIS13-175/2025]", 2),
    # 뒤에 공백이 붙은 /는 이름의 일부. 이 문자열의 원본 성분 수는 2
    ("Blood Coagulation Factor VIII/ Rurioctocog Alpha Pegol (rDNA)"
     "/Blood Coagulation Factor VIII/ Rurioctocog Alpha Pegol (rDNA)", 2),
    ("Follitropin-Alpha (Recombinant)/Lutropin Alpha (Human Luteinizing Hormone/ rDNA)", 2),
    # 괄호 안의 비율 표기
    ("Cinnamomi Ramulus Soft Extract (6.2~7.6→1)/Glycyrrhiza Soft Extract (2.4~2.9→1)", 2),
    (None, 0),
    ("", 0),
])
def test_split_ingredients(value, expected_count):
    assert len(split_ingredients(value)) == expected_count


def test_split_ingredients_preserves_names():
    assert split_ingredients("Teriparatide (rDNA/ Host:E.coli)/Fentanyl") == [
        "Teriparatide (rDNA/ Host:E.coli)",
        "Fentanyl",
    ]


def test_split_ingredients_known_limitation_unbracketed_slash():
    """알려진 한계: 괄호 없이 이름 안에 쓰인 /는 구분자와 구별할 수 없어 잘못 나뉜다.

    성분 수 검증 규칙(INGR_CNT_MISMATCH)이 이 경우를 잡는다.
    이 테스트가 실패하면 한계가 해소된 것이므로 의도한 변경인지 확인하고 기대값을 고친다.
    """
    assert split_ingredients("Methyl-N/S-Diacetylcysteine") == ["Methyl-N", "S-Diacetylcysteine"]


# ================
# split_parentheses
# ================

def test_split_parentheses_top_level_groups():
    assert split_parentheses("아이델라주(티옥트산)(바이알)") == ("아이델라주", ["티옥트산", "바이알"])


def test_split_parentheses_nested_group_is_kept_whole():
    assert split_parentheses("브롱코박솜캡슐(동결건조균체용해물(표준화))") == (
        "브롱코박솜캡슐", ["동결건조균체용해물(표준화)"]
    )


def test_split_parentheses_unbalanced_returns_original():
    """짝이 맞지 않으면 어디까지가 괄호인지 모르므로 나누지 않는다."""
    assert split_parentheses("제품(미완성") == ("제품(미완성", [])


# ================
# normalize_product_key: 같은 제품으로 묶여야 하는 경우
# ================

@pytest.mark.parametrize("a, b", [
    # 허가 갱신. 괄호 안 띄어쓰기만 다름
    ("퀸셀(자가지방조직유래 최소조작 지방세포)", "퀸셀(자가지방조직유래최소조작지방세포)"),
    # 끝 공백과 개행
    ("퀸셀정", "퀸셀정 \r\n"),
    # 단위 표기 통일
    ("아이스타플러스정80/5/5밀리그람", "아이스타플러스정80/5/5밀리그램"),
    # NFKC: ㎍(U+338D)과 μg
    ("비타민10㎍정", "비타민10μg정"),
    # 수출명은 별칭이라 키에서 뺀다
    ("중외5%포도당생리식염액(수출명:5%DextroseInj.)", "중외5%포도당생리식염액"),
    ("디에타민정(수출명:DIETOP)", "디에타민정"),
])
def test_product_key_merges(a, b):
    assert normalize_product_key(a, BIZ) == normalize_product_key(b, BIZ)


# ================
# normalize_product_key: 별개 제품으로 남아야 하는 경우
# ================

@pytest.mark.parametrize("a, b", [
    # 용기
    ("아이델라주(티옥트산)", "아이델라주(티옥트산)(바이알)"),
    ("포폴주사(프로포폴)(바이알)", "포폴주사(프로포폴)(앰플)"),
    # 처방
    ("용표원방우황청심원", "용표원방우황청심원(사향대체물질함유)"),
    # 용법
    ("티로신에스주사(트리트먼트코오스A)", "티로신에스주사(트리트먼트코오스B)"),
    ("라이스정(집먼지진드기정제추출물)(초기요법)", "라이스정(집먼지진드기정제추출물)(유지요법)"),
    # 용도
    ("펜타닐패취", "펜타닐패취(수출용)"),
    # 괄호 안 함량
    ("타이레놀정(아세트아미노펜500mg)", "타이레놀정(아세트아미노펜650mg)"),
])
def test_product_key_keeps_apart(a, b):
    assert normalize_product_key(a, BIZ) != normalize_product_key(b, BIZ)


def test_product_key_separates_companies():
    """같은 이름이라도 사업자번호가 다르면 다른 제품이다."""
    assert normalize_product_key("퀸셀정", "1111111111") != normalize_product_key("퀸셀정", "2222222222")


def test_product_key_known_tradeoff_ingredient_paren():
    """알려진 트레이드오프: 성분명 괄호만 다른 갱신은 별개 제품으로 남는다.

    괄호를 버리는 규칙은 바이알·사향대체물질 같은 제품 구분 괄호까지 버려 과병합을 만들었다.
    과병합을 막기 위해 괄호를 남기는 쪽을 택했고, 그 대가로 이 경우는 합쳐지지 않는다.
    """
    renewed = "이달비클로정40/25밀리그램(아질사르탄메독소밀칼륨,클로르탈리돈)"
    previous = "이달비클로정40/25밀리그램"
    assert normalize_product_key(renewed, BIZ) != normalize_product_key(previous, BIZ)


def test_product_key_rejects_empty_name():
    with pytest.raises(ValueError):
        normalize_product_key("(수출명:X)", BIZ)
