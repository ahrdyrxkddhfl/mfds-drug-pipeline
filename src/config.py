"""환경변수를 읽어 애플리케이션 설정을 제공한다.

.env 파일의 값을 한 곳에서 읽어 모듈 전역에서 공유한다.
설정을 클래스로 묶은 이유는 접근 경로를 하나로 고정하기 위해서다.
각 모듈이 os.getenv를 직접 호출하면 같은 변수를 다른 이름으로 참조하거나
기본값이 제각각이 되는데, 이 파일만 보면 필요한 환경변수 전체를 알 수 있다.
"""

import os

from dotenv import load_dotenv

load_dotenv()

class Config:
    """환경변수에서 읽어온 설정값 모음.

    클래스 속성은 모듈 로드 시점에 한 번만 평가된다.
    실행 도중 .env를 수정해도 반영되지 않으므로 재시작이 필요하다.

    Attributes:
        API_KEY (str | None): 공공데이터포털 인증키.
        PERMIT_API_URL (str | None): 의약품 제품 허가정보 API 요청주소.
        RECALL_API_URL (str | None): 의약품 회수, 판매중지 정보 API 요청주소.
        DB_USER (str | None): PostgreSQL 사용자명.
        DB_PASSWORD (str | None): PostgreSQL 비밀번호.
        DB_NAME (str | None): PostgreSQL 데이터베이스명.
        DB_HOST (str): PostgreSQL 호스트. 미설정 시 localhost.
        DB_PORT (str): PostgreSQL 포트. 미설정 시 5433(docker-compose 호스트 포트).
    """

    API_KEY = os.getenv("MFDS_API_KEY")
    PERMIT_API_URL = os.getenv("PERMIT_API_URL")
    RECALL_API_URL = os.getenv("RECALL_API_URL")

    DB_USER = os.getenv("POSTGRES_USER")
    DB_PASSWORD = os.getenv("POSTGRES_PASSWORD")
    DB_NAME = os.getenv("POSTGRES_DB")

    # 호스트와 포트만 기본값을 두는 이유는 로컬 개발 환경이 고정적이기 때문이다.
    # 인증키나 DB명은 기본값을 주면 잘못된 대상에 붙어도 조용히 실행되므로 두지 않는다.
    DB_HOST = os.getenv("POSTGRES_HOST", "localhost")
    DB_PORT = os.getenv("POSTGRES_PORT", "5433")

    @classmethod
    def db_dsn(cls) -> str:
        """psycopg 연결에 사용할 DSN 문자열을 만든다.

        Returns:
            str: postgresql://user:password@host:port/dbname 형식의 접속 문자열.
        """
        return (
            f"postgresql://{cls.DB_USER}:{cls.DB_PASSWORD}"
            f"@{cls.DB_HOST}:{cls.DB_PORT}/{cls.DB_NAME}"
        )

    @classmethod
    def validate(cls) -> None:
        """필수 환경변수가 모두 채워졌는지 확인한다.

        누락된 값을 실행 시작 시점에 잡아내기 위한 함수다.
        검증 없이 진행하면 API_KEY가 None인 채로 요청이 나가 인증 오류로 돌아오거나,
        DB 접속 실패가 수집을 한참 진행한 뒤에야 드러나 원인 추적이 어려워진다.

        Raises:
            ValueError: 필수 환경변수 중 비어 있는 항목이 있을 때.
        """
        required = ("API_KEY", "PERMIT_API_URL", "DB_USER", "DB_NAME")
        missing = [name for name in required if not getattr(cls, name)]

        if missing:
            raise ValueError(f"환경변수 누락: {', '.join(missing)}")

config = Config()


# ================
# 실행부
# ================

if __name__ == "__main__":
    config.validate()
    print("환경변수 검증 통과")
    print(f"DB: {config.DB_HOST}:{config.DB_PORT}/{config.DB_NAME}")