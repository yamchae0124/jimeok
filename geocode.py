"""
geocode.py — 단지 좌표 지오코딩 (카카오 로컬 API)
==================================================
trades에서 distinct 단지를 뽑아 카카오 로컬 API로 위·경도를 구해 apt_coords에 캐싱한다.
단지당 1회만 변환(이미 좌표 있으면 건너뜀) → 카카오 일 한도 절약.

정확도 순서로 3단 시도:
    1) 도로명 주소  (가장 정확)
    2) 지번 주소     (도로명 없을 때)
    3) 단지명 키워드 (둘 다 실패 시 fallback)

매칭 실패율을 콘솔에 그대로 노출한다(숨기지 않음 = 한계의 정량 확인).

사용:
    python geocode.py            # 좌표 없는 단지만
    python geocode.py --all      # 실패분까지 전부 재시도
"""
from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path

import requests

TOOL_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOL_ROOT))

import db as dbm              # noqa: E402
from _env import load_keys   # noqa: E402
from collect import region_name  # noqa: E402

KAKAO_ADDR = "https://dapi.kakao.com/v2/local/search/address.json"
KAKAO_KEYWORD = "https://dapi.kakao.com/v2/local/search/keyword.json"


class KakaoQuotaError(RuntimeError):
    """카카오 일 할당량 초과(429) — 재시도 무의미, 즉시 중단해야 하는 에러."""


def _search(url: str, headers: dict, query: str) -> tuple[float, float] | None:
    """카카오 검색 → (lat, lng) 또는 None. 카카오 x=경도, y=위도."""
    if not query.strip():
        return None
    r = requests.get(url, headers=headers, params={"query": query, "size": 1}, timeout=10)
    if r.status_code == 429:
        raise KakaoQuotaError("카카오 API 429 — 일 할당량 초과")
    r.raise_for_status()
    docs = r.json().get("documents", [])
    if not docs:
        return None
    d = docs[0]
    return float(d["y"]), float(d["x"])


def geocode_one(headers: dict, apt: dict) -> tuple[float | None, float | None, str]:
    """단지 1개 → (lat, lng, source). source = address | keyword | fail.
    개별 검색 실패는 로그 남기고 다음 단계로, 429(할당량)는 즉시 위로 던진다."""
    region = region_name(apt.get("lawd_cd", ""))

    def _log(stage: str, e: Exception) -> None:
        print(f"   ⚠️ {apt['apt_name']} {stage} 검색 실패: {type(e).__name__}: {e}")

    # 1) 도로명 주소
    if apt.get("road_addr"):
        try:
            res = _search(KAKAO_ADDR, headers, apt["road_addr"])
            if res:
                return res[0], res[1], "address"
        except KakaoQuotaError:
            raise
        except Exception as e:
            _log("도로명", e)

    # 2) 지번 주소
    if region and apt.get("jibun"):
        try:
            res = _search(KAKAO_ADDR, headers, f"{region} {apt['legal_dong']} {apt['jibun']}")
            if res:
                return res[0], res[1], "address"
        except KakaoQuotaError:
            raise
        except Exception as e:
            _log("지번", e)

    # 3) 단지명 키워드
    try:
        res = _search(KAKAO_KEYWORD, headers, f"{region} {apt['apt_name']}".strip())
        if res:
            return res[0], res[1], "keyword"
    except KakaoQuotaError:
        raise
    except Exception as e:
        _log("키워드", e)

    return None, None, "fail"


def main() -> int:
    only_missing = "--all" not in sys.argv

    keys = load_keys()
    rest_key = keys.get("KAKAO_REST_KEY")
    if not rest_key:
        print("❌ KAKAO_REST_KEY가 없습니다. 도구 폴더의 .env에 카카오 REST 키를 넣어주세요.")
        print("   발급: https://developers.kakao.com → 내 애플리케이션 → 앱 키 → REST API 키")
        print("   .env 예:  KAKAO_REST_KEY=발급받은키")
        return 1
    headers = {"Authorization": f"KakaoAK {rest_key}"}

    conn = dbm.connect()
    dbm.init_db(conn)
    apts = dbm.distinct_apts(conn, only_missing=only_missing)
    print(f"▶ 좌표 필요한 단지 {len(apts)}개 지오코딩 시작")

    ok_addr = ok_kw = fail = 0
    fails: list[str] = []
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    for a in apts:
        try:
            lat, lng, source = geocode_one(headers, a)
        except KakaoQuotaError:
            print(f"\n❌ 카카오 일 할당량 초과 — {ok_addr + ok_kw}/{len(apts)}개 처리 후 중단.")
            print("   내일 다시 'python geocode.py'를 실행하면 남은 단지만 이어서 처리합니다.")
            return 1
        dbm.upsert_coord(
            conn, a["apt_key"], a["apt_name"], a["legal_dong"],
            a.get("jibun", ""), lat, lng, source, now_str,
        )
        if source == "address":
            ok_addr += 1
        elif source == "keyword":
            ok_kw += 1
        else:
            fail += 1
            fails.append(f"{a['apt_name']} ({a['legal_dong']} {a.get('jibun', '')})")
        time.sleep(0.04)  # 카카오 호출 간격

    total = len(apts)
    matched = ok_addr + ok_kw
    print(f"\n✅ 지오코딩 완료: {matched}/{total} 성공 "
          f"(도로명·지번 {ok_addr} · 키워드 {ok_kw} · 실패 {fail})")
    if total:
        print(f"   매칭률 {matched / total * 100:.1f}%")
    if fails:
        print(f"\n⚠️ 좌표 못 찾은 단지 {len(fails)}개 (지도에서 빠짐):")
        for f in fails[:30]:
            print(f"   · {f}")
        if len(fails) > 30:
            print(f"   …외 {len(fails) - 30}개")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
