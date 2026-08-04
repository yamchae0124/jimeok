"""
db.py — 실거래가 지도 SQLite 저장소
=====================================
거래(trades) + 단지 좌표 캐시(apt_coords) 두 테이블만 쓴다.

- trades:     국토부 RTMS 매매 실거래 1건 = 1행. 중복 거래는 UNIQUE 인덱스로 자동 무시.
- apt_coords: 단지 1개 = 1행. 카카오 지오코딩 결과를 캐싱(단지당 1회만 좌표 변환).

collect.py(수집) → geocode.py(좌표) → build_map.py(export) 가 이 모듈을 공유한다.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"

PYEONG = 3.3058  # 1평 = 3.3058㎡ (research_collector와 동일 상수)


def db_path(lawd_cd: str) -> Path:
    """지역(법정동코드)별 DB 파일 경로 — 지역 혼입 방지의 1차 방어선."""
    return DATA_DIR / f"realprice_{lawd_cd}.db"


def apt_key(apt_name: str, legal_dong: str) -> str:
    """단지 식별 키 = 단지명|법정동. 동명이단지는 법정동으로 구분."""
    return f"{apt_name.strip()}|{legal_dong.strip()}"


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    """DB 연결. 경로 생략 시 config.json의 지역 DB. data/ 폴더가 없으면 만든다."""
    if path is None:
        from _config import load_config
        path = db_path(load_config()["lawd_cd"])
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """테이블 + 인덱스 생성 (이미 있으면 그대로)."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS trades (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            lawd_cd     TEXT NOT NULL,
            apt_name    TEXT NOT NULL,
            legal_dong  TEXT,
            jibun       TEXT,
            road_addr   TEXT,       -- 도로명 주소 (지오코딩용, 지번보다 정확)
            exclu_area  REAL,       -- 전용면적 ㎡
            pyeong      REAL,       -- 전용평수
            deal_amount INTEGER,    -- 거래금액 (만원)
            deal_date   TEXT,       -- YYYY-MM-DD
            floor       INTEGER,
            build_year  INTEGER,
            deal_gbn    TEXT        -- 중개거래|직거래|'' (RTMS dealingGbn, 2021-06 이전 빈값)
        );

        -- 같은 거래(지역·단지·전용·금액·날짜·층)는 한 번만 저장.
        -- v2: lawd_cd 포함 — 타지역 동명이단지의 동일 조건 거래가 중복으로 오판되지 않게
        DROP INDEX IF EXISTS idx_trade_unique;
        CREATE UNIQUE INDEX IF NOT EXISTS idx_trade_unique_v2
            ON trades (lawd_cd, apt_name, legal_dong, jibun, exclu_area, deal_amount, deal_date, floor);

        CREATE INDEX IF NOT EXISTS idx_trade_apt
            ON trades (apt_name, legal_dong);

        CREATE INDEX IF NOT EXISTS idx_trade_lawd
            ON trades (lawd_cd);

        CREATE TABLE IF NOT EXISTS rents (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            lawd_cd       TEXT NOT NULL,
            apt_name      TEXT NOT NULL,
            legal_dong    TEXT,
            jibun         TEXT,
            exclu_area    REAL,       -- 전용면적 ㎡
            pyeong        REAL,       -- 전용평수
            deposit       INTEGER,    -- 보증금 (만원)
            monthly_rent  INTEGER,    -- 월세 (만원, 0이면 전세)
            deal_date     TEXT,       -- YYYY-MM-DD (계약일)
            floor         INTEGER,
            build_year    INTEGER,
            contract_type TEXT,       -- 신규 | 갱신 | '' (2021-06 이전 데이터는 빈값)
            contract_term TEXT        -- 계약기간 (예: 24.06~26.06)
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_rent_unique
            ON rents (lawd_cd, apt_name, legal_dong, jibun, exclu_area,
                      deposit, monthly_rent, deal_date, floor);

        CREATE INDEX IF NOT EXISTS idx_rent_apt
            ON rents (apt_name, legal_dong);

        CREATE TABLE IF NOT EXISTS apt_coords (
            apt_key     TEXT PRIMARY KEY,   -- 단지명|법정동
            apt_name    TEXT,
            legal_dong  TEXT,
            jibun       TEXT,               -- 지오코딩에 쓴 대표 지번
            lat         REAL,
            lng         REAL,
            source      TEXT,               -- address | keyword | fail
            geocoded_at TEXT
        );
        """
    )
    # 마이그레이션: 기존 DB(구버전)에 deal_gbn 컬럼이 없으면 추가 — 기존 행은 NULL 유지
    cols = [r[1] for r in conn.execute("PRAGMA table_info(trades)")]
    if "deal_gbn" not in cols:
        conn.execute("ALTER TABLE trades ADD COLUMN deal_gbn TEXT")
    conn.commit()


def insert_trades(conn: sqlite3.Connection, rows: list[dict]) -> int:
    """거래 여러 건 적재. 중복은 INSERT OR IGNORE로 건너뜀. 새로 들어간 행 수 반환."""
    before = conn.total_changes
    conn.executemany(
        """
        INSERT OR IGNORE INTO trades
            (lawd_cd, apt_name, legal_dong, jibun, road_addr, exclu_area, pyeong,
             deal_amount, deal_date, floor, build_year, deal_gbn)
        VALUES
            (:lawd_cd, :apt_name, :legal_dong, :jibun, :road_addr, :exclu_area, :pyeong,
             :deal_amount, :deal_date, :floor, :build_year, :deal_gbn)
        """,
        rows,
    )
    conn.commit()
    return conn.total_changes - before


def trade_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]


def insert_rents(conn: sqlite3.Connection, rows: list[dict]) -> int:
    """전월세 여러 건 적재. 중복은 INSERT OR IGNORE로 건너뜀. 새로 들어간 행 수 반환."""
    before = conn.total_changes
    conn.executemany(
        """
        INSERT OR IGNORE INTO rents
            (lawd_cd, apt_name, legal_dong, jibun, exclu_area, pyeong,
             deposit, monthly_rent, deal_date, floor, build_year,
             contract_type, contract_term)
        VALUES
            (:lawd_cd, :apt_name, :legal_dong, :jibun, :exclu_area, :pyeong,
             :deposit, :monthly_rent, :deal_date, :floor, :build_year,
             :contract_type, :contract_term)
        """,
        rows,
    )
    conn.commit()
    return conn.total_changes - before


def rent_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM rents").fetchone()[0]


def distinct_apts(conn: sqlite3.Connection, only_missing: bool = True) -> list[dict]:
    """
    좌표가 필요한 단지 목록.
    only_missing=True 면 apt_coords에 아직 없는(또는 실패한) 단지만 반환.
    각 단지의 대표 지번 = 거래 건수가 가장 많은 지번.
    매매(trades) + 전월세(rents) 양쪽 단지를 모두 포함 — 전월세에만 등장하는
    단지도 지오코딩 대상이 된다 (rents엔 도로명이 없어 '' 처리).
    """
    rows = conn.execute(
        """
        WITH all_deals AS (
            SELECT apt_name, legal_dong, jibun, road_addr, lawd_cd FROM trades
            UNION ALL
            SELECT apt_name, legal_dong, jibun, '' AS road_addr, lawd_cd FROM rents
        ),
        apt_jibun AS (
            SELECT apt_name, legal_dong, jibun,
                   MAX(road_addr) AS road_addr, MAX(lawd_cd) AS lawd_cd,
                   COUNT(*) AS n
            FROM all_deals
            GROUP BY apt_name, legal_dong, jibun
        ),
        ranked AS (
            SELECT apt_name, legal_dong, jibun, road_addr, lawd_cd,
                   ROW_NUMBER() OVER (
                       PARTITION BY apt_name, legal_dong ORDER BY n DESC
                   ) AS rk
            FROM apt_jibun
        )
        SELECT apt_name, legal_dong, jibun, road_addr, lawd_cd
        FROM ranked
        WHERE rk = 1
        ORDER BY apt_name
        """
    ).fetchall()
    out = []
    for r in rows:
        key = apt_key(r["apt_name"], r["legal_dong"])
        if only_missing:
            existing = conn.execute(
                "SELECT source FROM apt_coords WHERE apt_key = ?", (key,)
            ).fetchone()
            # 이미 좌표 확보(address/keyword)면 건너뜀. 'fail'이거나 없으면 재시도.
            if existing and existing["source"] in ("address", "keyword"):
                continue
        out.append(
            {
                "apt_key": key,
                "apt_name": r["apt_name"],
                "legal_dong": r["legal_dong"],
                "jibun": r["jibun"],
                "road_addr": r["road_addr"],
                "lawd_cd": r["lawd_cd"],
            }
        )
    return out


def upsert_coord(
    conn: sqlite3.Connection,
    apt_key_: str,
    apt_name: str,
    legal_dong: str,
    jibun: str,
    lat: float | None,
    lng: float | None,
    source: str,
    geocoded_at: str,
) -> None:
    """지오코딩 결과 저장(있으면 갱신)."""
    conn.execute(
        """
        INSERT INTO apt_coords
            (apt_key, apt_name, legal_dong, jibun, lat, lng, source, geocoded_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(apt_key) DO UPDATE SET
            lat = excluded.lat,
            lng = excluded.lng,
            source = excluded.source,
            geocoded_at = excluded.geocoded_at,
            jibun = excluded.jibun
        """,
        (apt_key_, apt_name, legal_dong, jibun, lat, lng, source, geocoded_at),
    )
    conn.commit()


def coord_stats(conn: sqlite3.Connection) -> dict:
    """지오코딩 현황 집계 (성공/실패 건수)."""
    rows = conn.execute(
        "SELECT source, COUNT(*) AS n FROM apt_coords GROUP BY source"
    ).fetchall()
    return {r["source"]: r["n"] for r in rows}
