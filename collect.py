"""
collect.py — 국토부 RTMS 아파트 매매+전월세 실거래 → SQLite
=====================================================
분양성검토 엔진의 research_collector.rtms_trade_month()/rtms_rent_month()를
그대로 재사용한다. (국토부 API 호출 + XML 파싱은 이미 검증된 코드 — 새로 안 짠다.)

config.json의 지역(법정동코드)·개월 수 기준으로 매매·전월세를 받아
지역별 DB(data/realprice_{법정동코드}.db) trades·rents 테이블에 적재.
재실행하면 새 거래만 추가(중복은 UNIQUE 인덱스로 무시).
전월세 수집 실패는 경고만 — 매매(기존 지도)는 항상 보호.

사용:
    python collect.py                  # config.json의 지역·개월
    python collect.py 11590 18         # 인자로 override (동작구, 18개월)
"""
from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path

# ── 분양성검토 엔진 engine/ 을 import 경로에 추가 (_collect_comparables.py 패턴) ──
TOOL_ROOT = Path(__file__).resolve().parent
ENGINE_ENGINE = TOOL_ROOT.parent.parent / "분양_부동산" / "0.분양성검토_엔진" / "engine"
sys.path.insert(0, str(ENGINE_ENGINE))

# research_collector를 import하는 순간 Windows 콘솔이 UTF-8로 잡힌다(그쪽 모듈 상단 코드).
from research_collector import rtms_trade_month, rtms_rent_month, PYEONG  # noqa: E402

import db as dbm          # noqa: E402
from _env import load_keys  # noqa: E402
from _config import load_config  # noqa: E402

# 도로명 주소 앞에 붙일 시군구명 (지오코딩 정확도용) — 과거 작업 지역 fallback.
# 새 지역은 여기 추가할 필요 없음: config.json의 region_name을 우선 사용한다.
LAWD_REGION = {
    "30140": "대전 중구",
    "30110": "대전 동구",
    "30170": "대전 서구",
    "30200": "대전 유성구",
    "30230": "대전 대덕구",
    "11590": "서울 동작구",
    "11305": "서울 강북구",
    "41430": "경기 의왕시",
    "41461": "경기도 용인시",
    "31140": "울산 남구",
}


def recent_months(n: int) -> list[str]:
    """최근 n개월 YYYYMM 리스트 (오래된 것 → 최근 순)."""
    out: list[str] = []
    now = datetime.now()
    y, m = now.year, now.month
    for _ in range(n):
        out.append(f"{y}{m:02d}")
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return list(reversed(out))


def _to_amount(s: str | None) -> int:
    """'12,345' → 12345 (만원)."""
    return int(re.sub(r"[^\d]", "", s or "") or 0)


def _to_float(s: str | None) -> float:
    try:
        return float(s or 0)
    except ValueError:
        return 0.0


def _to_int(s: str | None) -> int:
    try:
        return int(float(s or 0))
    except ValueError:
        return 0


def _road_addr(it: dict, region: str) -> str:
    """도로명 주소 조합: '대전 중구 계룡로 123-4'. 부족하면 빈 문자열."""
    road = (it.get("roadNm") or "").strip()
    bonbun = (it.get("roadNmBonbun") or "").strip().lstrip("0")
    bubun = (it.get("roadNmBubun") or "").strip().lstrip("0")
    if not (region and road and bonbun):
        return ""
    addr = f"{region} {road} {bonbun}"
    if bubun:
        addr += f"-{bubun}"
    return addr


def normalize(it: dict, lawd: str, region: str) -> dict | None:
    """RTMS item dict → trades 행 dict. 단지명 없으면 None."""
    name = (it.get("aptNm") or "").strip()
    if not name:
        return None
    area = _to_float(it.get("excluUseAr"))
    y = (it.get("dealYear") or "").strip()
    mo = (it.get("dealMonth") or "").strip()
    d = (it.get("dealDay") or "").strip()
    deal_date = ""
    if y and mo and d:
        deal_date = f"{y}-{int(mo):02d}-{int(d):02d}"
    return {
        "lawd_cd": lawd,
        "apt_name": name,
        "legal_dong": (it.get("umdNm") or "").strip(),
        "jibun": (it.get("jibun") or "").strip(),
        "road_addr": _road_addr(it, region),
        "exclu_area": round(area, 2),
        "pyeong": round(area / PYEONG, 2) if area else 0.0,
        "deal_amount": _to_amount(it.get("dealAmount")),
        "deal_date": deal_date,
        "floor": _to_int(it.get("floor")),
        "build_year": _to_int(it.get("buildYear")),
        "deal_gbn": (it.get("dealingGbn") or "").strip(),  # 중개거래|직거래|'' — 직거래 제외 토글용
    }


def normalize_rent(it: dict, lawd: str) -> dict | None:
    """RTMS 전월세 item dict → rents 행 dict. 단지명 없으면 None.

    매매와 다른 점: dealAmount 대신 deposit(보증금)·monthlyRent(월세),
    contractType(신규/갱신, 2021-06 이후만)·contractTerm(계약기간).
    도로명 필드가 없어 road_addr는 만들지 않는다.
    """
    name = (it.get("aptNm") or "").strip()
    if not name:
        return None
    area = _to_float(it.get("excluUseAr"))
    y = (it.get("dealYear") or "").strip()
    mo = (it.get("dealMonth") or "").strip()
    d = (it.get("dealDay") or "").strip()
    deal_date = ""
    if y and mo and d:
        deal_date = f"{y}-{int(mo):02d}-{int(d):02d}"
    return {
        "lawd_cd": lawd,
        "apt_name": name,
        "legal_dong": (it.get("umdNm") or "").strip(),
        "jibun": (it.get("jibun") or "").strip(),
        "exclu_area": round(area, 2),
        "pyeong": round(area / PYEONG, 2) if area else 0.0,
        "deposit": _to_amount(it.get("deposit")),
        "monthly_rent": _to_amount(it.get("monthlyRent")),
        "deal_date": deal_date,
        "floor": _to_int(it.get("floor")),
        "build_year": _to_int(it.get("buildYear")),
        "contract_type": (it.get("contractType") or "").strip(),
        "contract_term": (it.get("contractTerm") or "").strip(),
    }


def collect_rents(conn, service_key: str, lawd: str, months: list[str]) -> bool:
    """전월세 수집 루프. 성공 여부만 반환 — 실패해도 매매(기존 지도)는 보호한다."""
    print(f"\n▶ 전월세 수집 ({months[0]} ~ {months[-1]})")
    total_new = 0
    total_resp = 0
    consec_fail = 0
    for ym in months:
        try:
            items = rtms_rent_month(service_key, lawd, ym)
            consec_fail = 0
        except Exception as e:
            consec_fail += 1
            print(f"  {ym}  ❌ 호출 실패: {e}")
            if consec_fail >= 3:
                print("⚠️ 전월세 3개월 연속 실패 — 전월세만 건너뜁니다 (매매 데이터는 정상).")
                return False
            continue
        rows = []
        for it in items:
            r = normalize_rent(it, lawd)
            if r:
                rows.append(r)
        new = dbm.insert_rents(conn, rows)
        total_new += new
        total_resp += len(items)
        print(f"  {ym}  응답 {len(items):>4}건 · 신규 {new:>4}건")
    print(f"  전월세 누적 {dbm.rent_count(conn):,}건 (이번 응답 {total_resp:,} / 신규 {total_new:,})")
    return True


def region_name(lawd: str, cfg: dict | None = None) -> str:
    """시군구명 — config.json 우선, 과거 지역은 LAWD_REGION fallback."""
    cfg = cfg or load_config()
    if lawd == cfg.get("lawd_cd") and cfg.get("region_name"):
        return cfg["region_name"]
    return LAWD_REGION.get(lawd, "")


def main() -> int:
    cfg = load_config()
    lawd = sys.argv[1] if len(sys.argv) > 1 else cfg["lawd_cd"]
    months_back = int(sys.argv[2]) if len(sys.argv) > 2 else int(cfg["months"])

    keys = load_keys()
    service_key = keys.get("DATA_GO_KR_KEY")
    if not service_key:
        print("❌ DATA_GO_KR_KEY를 찾을 수 없습니다. 엔진 .env 또는 도구 .env를 확인하세요.")
        return 1

    region = region_name(lawd, cfg)
    db_file = dbm.db_path(lawd)
    conn = dbm.connect(db_file)
    dbm.init_db(conn)

    months = recent_months(months_back)
    print(f"▶ {region or 'LAWD ' + lawd} · 최근 {months_back}개월 매매 수집 ({months[0]} ~ {months[-1]})")

    total_new = 0
    total_resp = 0
    total_cancel = 0
    consec_fail = 0
    for ym in months:
        try:
            items = rtms_trade_month(service_key, lawd, ym)
            consec_fail = 0
        except Exception as e:
            consec_fail += 1
            print(f"  {ym}  ❌ 호출 실패: {e}")
            if consec_fail >= 3:
                print("\n❌ 3개월 연속 호출 실패 — 키 만료·IP 차단·API 점검 가능성. 수집 중단합니다.")
                print("   공공데이터포털(data.go.kr)에서 DATA_GO_KR_KEY 상태를 확인하세요.")
                return 1
            continue
        rows = []
        for it in items:
            # 해제(취소) 거래는 시세 노이즈라 제외
            if (it.get("cdealType") or "").strip():
                total_cancel += 1
                continue
            r = normalize(it, lawd, region)
            if r:
                rows.append(r)
        new = dbm.insert_trades(conn, rows)
        total_new += new
        total_resp += len(items)
        print(f"  {ym}  응답 {len(items):>4}건 · 신규 {new:>4}건")

    print(
        f"\n✅ 매매 누적 {dbm.trade_count(conn):,}건 "
        f"(이번 응답 {total_resp:,} / 신규 {total_new:,} / 해제거래 제외 {total_cancel:,})"
    )

    # 전월세는 부가 레이어 — 실패해도 매매 적재분을 살려 return 0
    try:
        collect_rents(conn, service_key, lawd, months)
    except Exception as e:
        print(f"⚠️ 전월세 수집 중 예기치 않은 오류({e}) — 전월세만 건너뜁니다.")

    print(f"   DB: {db_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
