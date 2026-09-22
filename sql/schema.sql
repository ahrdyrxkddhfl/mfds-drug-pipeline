-- ============================================================
-- mfds-drug-pipeline 스키마
-- 데이터 출처: 식품의약품안전처 의약품 제품 허가정보, 의약품 회수·판매중지 정보
--            (공공데이터포털)
-- ============================================================


-- ============================================================
-- raw 레이어
-- API 응답을 가공 없이 보관. 정제 로직 수정 시 재처리용 안전망.
-- ============================================================

-- 원본 레코드
-- 소스별로 테이블을 나누지 않고 source 컬럼으로 구분한다.
--   - 소스마다 필드 구성이 달라도 JSONB가 흡수하므로 DDL이 바뀌지 않음
--   - 소스가 늘어도 테이블 추가 없이 source 값만 늘어남
--   - 적재 코드가 테이블명을 문자열로 조립할 필요가 없음
-- 배치마다 새 행으로 쌓이며, transform은 소스별 최신 성공 배치만 읽는다.
CREATE TABLE raw_record (
    id           BIGSERIAL PRIMARY KEY,
    source       VARCHAR(30) NOT NULL,                -- permit / recall
    payload      JSONB NOT NULL,                      -- API 응답 원본
    fetched_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    batch_id     UUID NOT NULL,                       -- 수집 회차 식별자
    page_no      INT
);


-- ============================================================
-- 참조 테이블
-- ============================================================

-- 업체 마스터
-- 동일 회사가 여러 ENTP_SEQ로 분산되는 사례 존재 (예: 삼양홀딩스, 대한약품공업)
-- 상대적으로 안정적인 사업자번호를 통합 기준으로 채택
CREATE TABLE company (
    company_id   BIGSERIAL PRIMARY KEY,
    bizrno       VARCHAR(20) UNIQUE NOT NULL,         -- 사업자등록번호
    name_ko      TEXT,
    name_en      TEXT,
    created_at   TIMESTAMPTZ DEFAULT now(),
    updated_at   TIMESTAMPTZ DEFAULT now()
);

-- 성분 마스터
-- 한글 성분명은 표기 체계가 혼재 (예: 염산케타민 / 케타민염산염)
-- 영문명을 기준 키로 채택
CREATE TABLE ingredient (
    ingredient_id BIGSERIAL PRIMARY KEY,
    name_en       TEXT UNIQUE NOT NULL,
    name_ko       TEXT,
    created_at    TIMESTAMPTZ DEFAULT now()
);


-- ============================================================
-- 제품 / 허가
-- ============================================================

-- 제품 마스터
-- ITEM_SEQ는 허가 건 단위 식별자로, 허가 갱신 시 신규 발급됨 (예: 퀸셀)
-- 따라서 제품 식별자로 사용 불가 → 별도 정규화 키로 동일 제품 판정
--
-- 정규화 키 구성: 정규화된 제품명 + 사업자번호
--   - 공백/개행 제거, 유니코드 정규화(μ/㎍), 표기 통일(밀리그람/밀리그램)
--   - 괄호 내용은 분리하되 용도 구분자(수출용 등)는 키에 포함
--   - 함량이 다르면 별개 제품으로 취급
CREATE TABLE product (
    product_id      BIGSERIAL PRIMARY KEY,
    normalized_key  TEXT UNIQUE NOT NULL,             -- 동일 제품 판정 키
    name_ko         TEXT NOT NULL,                    -- 대표 제품명 (정규화)
    name_en         TEXT,
    company_id      BIGINT REFERENCES company(company_id),
    spclty_pblc     VARCHAR(20),                      -- 전문/일반의약품
    review_needed   BOOLEAN DEFAULT FALSE,            -- 자동 판정 애매 → 수동 검토
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);

-- 허가 건
-- 원본 API 1행 = 이 테이블 1행
-- PRDLST_STDR_CODE는 전 건이 ITEM_SEQ와 같은 값이라 저장하지 않는다.
-- 앞으로도 같다는 보장은 없으므로 동일성은 검증 규칙으로 매 배치 확인한다.
CREATE TABLE permit (
    item_seq         VARCHAR(20) PRIMARY KEY,
    product_id       BIGINT REFERENCES product(product_id),
    item_name_raw    TEXT NOT NULL,                   -- 원본 품목명 (대조용)
    permit_date      DATE,
    cancel_date      DATE,                            -- 취하/만료일
    cancel_name      VARCHAR(50),                     -- 정상/유효기간만료/취하/폐업/행정(취소)/취소
                                                      -- 행정(취소)와 취소는 같은 뜻인지 근거가 없어 원본 값 유지
    induty           TEXT,                            -- 업종 (분류 체계 혼재)
    prduct_type      TEXT,                            -- 약효 분류
    permit_kind_code VARCHAR(20),                     -- 허가/신고
    prduct_prmisn_no VARCHAR(20),
    ingr_cnt_raw     INT,                             -- 원본 성분 수. 분해 결과와 대조하는 검증용
    batch_id         UUID,
    created_at       TIMESTAMPTZ DEFAULT now(),
    updated_at       TIMESTAMPTZ DEFAULT now()
);


-- ============================================================
-- 관계 테이블
-- ============================================================

-- 허가-성분
-- 동일 성분이 반복 나열되는 케이스 존재 (예: Fentanyl/Fentanyl, CNT 2)
-- position으로 원본 순서를 보존하고, 고유 성분 수는 별도 집계
CREATE TABLE permit_ingredient (
    item_seq       VARCHAR(20) REFERENCES permit(item_seq),
    ingredient_id  BIGINT REFERENCES ingredient(ingredient_id),
    position       INT,                               -- 원본 나열 순서
    PRIMARY KEY (item_seq, ingredient_id, position)
);

-- 허가-EDI코드
-- 원본은 콤마 구분 다중값 (예: "644902212,644902220") → 행 단위로 분해
CREATE TABLE permit_edi_code (
    item_seq   VARCHAR(20) REFERENCES permit(item_seq),
    edi_code   VARCHAR(30),
    PRIMARY KEY (item_seq, edi_code)
);


-- 회수·판매중지 건
-- 원천에 회수 건 식별자가 없어 (품목, 회수일, 사유)를 자연키로 사용한다.
-- 같은 자연키로 동일 페이지에 중복 행이 오는 사례가 있다(958건 중 4건).
-- 목록 API에 없는 제조번호로 구분되는 별개 행일 수 있어, 1행으로 합치되 원 행 수를 보존한다.
-- item_seq는 permit에 FK를 걸지 않는다. 회수 API는 허가정보 API보다 품목 범위가 넓어
-- 약 44%가 허가정보에 없다. FK를 걸면 이 행들이 적재되지 않으므로 연결 여부는 조회 시 판단한다.
CREATE TABLE recall (
    recall_id            BIGSERIAL PRIMARY KEY,
    item_seq             VARCHAR(20) NOT NULL,
    recall_command_date  DATE NOT NULL,               -- RECALL_COMMAND_DATE. 항상 존재
    rtrvl_cmmnd_date     DATE,                        -- RTRVL_CMMND_DT. 14자리 일시 중 날짜만 사용
    -- 포털 설명상 두 날짜는 승인일자·회수명령일자에 해당하나, 명세에 필드별 설명이 없어
    -- 의미를 단정하지 않고 원본 필드명을 유지한다.
    reason               TEXT NOT NULL,               -- RTRVL_RESN
    is_enforced          BOOLEAN NOT NULL,            -- ENFRC_YN
    product_name_raw     TEXT NOT NULL,
    company_name_raw     TEXT,
    bizrno               VARCHAR(20),
    std_codes            TEXT[],                      -- STD_CD 콤마 분해. 빈 값은 NULL
    source_row_count     INT NOT NULL DEFAULT 1,      -- 합쳐진 원본 행 수
    batch_id             UUID,
    created_at           TIMESTAMPTZ DEFAULT now(),
    updated_at           TIMESTAMPTZ DEFAULT now(),
    UNIQUE (item_seq, recall_command_date, reason)
);


-- ============================================================
-- 운영 로그
-- ============================================================

-- 수집 이력
-- 배치 단위 성공/실패와 건수를 기록. 수집 누락 탐지의 근거.
CREATE TABLE ingest_log (
    batch_id      UUID PRIMARY KEY,
    source        VARCHAR(30) NOT NULL,               -- permit / recall
    started_at    TIMESTAMPTZ NOT NULL,
    finished_at   TIMESTAMPTZ,
    status        VARCHAR(20),                        -- running/success/failed
    total_count   INT,                                -- API가 보고한 전체 건수
    fetched_count INT,                                -- 실제 수집 건수
    error_message TEXT
);

-- 검증 결과
-- 품질 리포트의 원천 데이터. 규칙별 탐지 건수를 배치마다 누적.
CREATE TABLE validation_log (
    id           BIGSERIAL PRIMARY KEY,
    batch_id     UUID,
    rule_code    VARCHAR(50) NOT NULL,                -- 검증 규칙 식별자
    severity     VARCHAR(10) NOT NULL,                -- error / warning / info
    target_key   TEXT,                                -- 대상 레코드 식별자
    detail       JSONB,                               -- 탐지 상세
    detected_at  TIMESTAMPTZ DEFAULT now()
);


-- ============================================================
-- 인덱스
-- ============================================================

CREATE INDEX idx_permit_product      ON permit(product_id);
CREATE INDEX idx_permit_permit_date  ON permit(permit_date);
CREATE INDEX idx_permit_cancel_name  ON permit(cancel_name);
CREATE INDEX idx_product_company     ON product(company_id);
CREATE INDEX idx_recall_item_seq     ON recall(item_seq);
CREATE INDEX idx_raw_record_batch    ON raw_record(source, batch_id);
CREATE INDEX idx_validation_batch    ON validation_log(batch_id, rule_code);