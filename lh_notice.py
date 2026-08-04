"""
lh_notice.py — LH 공급공고 → lh.js (선택 레이어)
==================================================
사업지 시도의 LH 분양·임대 공고를 받아 지도에 마커로 얹는다 = 경쟁 공공 공급 시각화.

데이터 경로: k-skill 프록시(무인증) 경유 LH 공고 API.
    GET {proxy_base}/v1/lh-notice/search
정본 API(공공데이터포털 LH) 전환 시 config.json의 proxy_base만 교체.

⚠️ 프록시 특성 (2026-06-12 실호출로 확인):
- cnpCdNm 파라미터를 서버가 무시하고 전국 공고를 반환 → 시도명 클라이언트 필터 필수
- 주소 필드가 없고 공고명(pan_nm) 텍스트뿐 → 공고명 첫 토큰으로 카카오 키워드 지오코딩,
  실패 건은 lat=null로 남겨 지도 사이드 목록에만 표시 (정상 경로)

non-fatal 골든룰: 프록시·파싱 실패 시 ⚠️ 경고 + 빈 payload 기록 후 exit 0 —
지도 본체(매매·전월세)는 절대 죽이지 않는다. 낡은 lh.js를 남기지 않고 빈 파일을
쓰는 이유: 보고용 지도에서 지난주 공고가 오늘 것처럼 보이는 사고 방지.

사용:
    python lh_notice.py
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

import requests

TOOL_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOL_ROOT))

from _config import load_config   # noqa: E402
from _env import load_keys        # noqa: E402
from geocode import _search, KAKAO_KEYWORD  # noqa: E402

OUT = TOOL_ROOT / "lh.js"
SOURCE_LABEL = "LH 공고문 (k-skill-proxy 경유)"

# region_name 첫 토큰("울산") → LH 공고의 cnp_cd_nm 표기
SIDO_MAP = {
    "서울": "서울특별시", "부산": "부산광역시", "대구": "대구광역시",
    "인천": "인천광역시", "광주": "광주광역시", "대전": "대전광역시",
    "울산": "울산광역시", "세종": "세종특별자치시", "경기": "경기도",
    "강원": "강원특별자치도", "충북": "충청북도", "충남": "충청남도",
    "전북": "전북특별자치도", "전남": "전라남도", "경북": "경상북도",
    "경남": "경상남도", "제주": "제주특별자치도",
}

UPP_NM = {"01": "토지", "05": "분양주택", "06": "임대주택", "13": "주거복지", "22": "상가"}


def derive_sido(cfg: dict) -> str:
    """config의 lh_sido 우선, 없으면 region_name 첫 토큰을 시도명으로 변환."""
    if cfg.get("lh_sido"):
        return cfg["lh_sido"]
    first = (cfg.get("region_name") or "").split()[0] if cfg.get("region_name") else ""
    return SIDO_MAP.get(first, first)


def clean_place_query(pan_nm: str) -> str:
    """공고명 → 지오코딩용 검색어. '[정정공고]' 류 접두와 괄호 내용 제거 후 첫 토큰."""
    s = re.sub(r"\[[^\]]*\]", " ", pan_nm)       # [정정공고] 등
    s = re.sub(r"\([^)]*\)", " ", s)             # (괄호 안 부연)
    s = s.strip()
    return s.split()[0] if s else ""


def write_payload(notices: list[dict], sido: str, error: str = "") -> None:
    data = {
        "notices": notices,
        "sido": sido,
        "source": SOURCE_LABEL,
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    if error:
        data["error"] = error
    OUT.write_text("window.LH_DATA = " + json.dumps(data, ensure_ascii=False) + ";\n",
                   encoding="utf-8")


def main() -> int:
    cfg = load_config()
    sido = derive_sido(cfg)
    base = (cfg.get("proxy_base") or "").rstrip("/")
    if not (sido and base):
        print("⚠️ LH 공고: 시도명 또는 proxy_base 미설정 — 이 레이어만 건너뜁니다.")
        write_payload([], sido, error="config 미설정")
        _stamp()
        return 0

    print(f"▶ LH 공급공고 수집 — {sido} · 상태 '{cfg.get('lh_pan_ss', '공고중')}'")

    # 1) 프록시 호출 (서버가 시도 필터를 무시하므로 전국분을 받아 클라이언트 필터)
    try:
        r = requests.get(
            f"{base}/v1/lh-notice/search",
            params={"panSs": cfg.get("lh_pan_ss", "공고중"), "pageSize": 500},
            timeout=15,
        )
        r.raise_for_status()
        items = r.json().get("items", [])
    except Exception as e:
        print(f"⚠️ LH 공고 수집 실패({type(e).__name__}: {e}) — 이 레이어만 건너뜁니다.")
        write_payload([], sido, error=str(e))
        _stamp()
        return 0

    mine = [it for it in items if (it.get("cnp_cd_nm") or "").strip() == sido]
    print(f"   전국 {len(items)}건 중 {sido} {len(mine)}건")

    # 2) 좌표화 — 공고명 첫 토큰 + 시도 접두 키워드 검색. 실패는 lat=null (사이드 목록행)
    keys = load_keys()
    rest = keys.get("KAKAO_REST_KEY")
    headers = {"Authorization": f"KakaoAK {rest}"} if rest else None

    notices = []
    ok = 0
    for it in mine:
        raw = it.get("raw") or {}
        place = clean_place_query(it.get("pan_nm") or "")
        lat = lng = None
        if headers and place:
            try:
                res = _search(KAKAO_KEYWORD, headers, f"{sido} {place}")
                if res:
                    lat, lng = res
                    ok += 1
            except Exception:
                pass  # 개별 지오코딩 실패는 사이드 목록으로 — 지도는 깨지지 않는다
        notices.append({
            "name": it.get("pan_nm") or "",
            "type": (raw.get("UPP_AIS_TP_NM") or UPP_NM.get(it.get("upp_ais_tp_cd") or "", "기타")),
            "sub_type": it.get("ais_tp_cd_nm") or "",
            "status": it.get("pan_ss") or "",
            "date": raw.get("PAN_NT_ST_DT") or "",
            "close": it.get("clsg_dt") or "",
            "url": it.get("detail_url") or "",
            "lat": lat,
            "lng": lng,
        })

    write_payload(notices, sido)
    _stamp()
    print(f"✅ lh.js 생성 — 공고 {len(notices)}건 (좌표 확보 {ok}건 / 미확인 {len(notices)-ok}건)")
    print(f"   → {OUT}")
    return 0


def _stamp() -> None:
    """캐시버스팅 — build_map.stamp_html 재사용 (lh.js도 패턴에 포함됨)."""
    try:
        from build_map import stamp_html
        stamp_html()
    except Exception:
        pass


if __name__ == "__main__":
    raise SystemExit(main())
