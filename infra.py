"""
infra.py — 사업지 반경 인프라(학교/생활편의/관공서) → infra.js
================================================================
카카오 로컬 '카테고리 검색'으로 사업지 반경 시설을 3그룹으로 모아
map_leaflet.html이 아이콘으로 찍게 infra.js로 내보낸다.

그룹:
  school : 학교       (카테고리 SC4)
  conv   : 생활편의   (대형마트 MT1 + 문화시설/영화관 CT1 + '백화점' 키워드)
  gov    : 관공서     (공공기관 PO3)

사용:
    python infra.py            # 반경 = config.json radius_infra_m (기본 3km)
    python infra.py 5000       # 반경 5km로 override
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

import requests

TOOL_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOL_ROOT))

from build_map import SITE_QUERY, geocode_site, stamp_html  # noqa: E402
from _config import load_config                   # noqa: E402
from _env import load_keys                        # noqa: E402

CAT_URL = "https://dapi.kakao.com/v2/local/search/category.json"
KW_URL = "https://dapi.kakao.com/v2/local/search/keyword.json"
OUT = TOOL_ROOT / "infra.js"


def _pages(url: str, headers: dict, params: dict) -> list:
    out = []
    for page in range(1, 4):
        p = dict(params, size=15, page=page)
        try:
            r = requests.get(url, headers=headers, params=p, timeout=10)
        except Exception as e:
            print(f"   ⚠️ 카카오 호출 실패({type(e).__name__}: {e}) — 이 카테고리 일부 누락")
            break
        if r.status_code != 200:
            print(f"   ⚠️ 카카오 응답 {r.status_code} — 이 카테고리 일부 누락")
            break
        try:
            j = r.json()
        except ValueError:
            print(f"   ⚠️ 카카오 응답 JSON 파싱 실패: {r.text[:200]}")
            break
        out += j.get("documents", [])
        if j.get("meta", {}).get("is_end", True):
            break
        time.sleep(0.03)
    return out


def cat(headers, code, x, y, radius):
    return _pages(CAT_URL, headers, {"category_group_code": code, "x": x, "y": y, "radius": radius})


def kw(headers, query, x, y, radius):
    return _pages(KW_URL, headers, {"query": query, "x": x, "y": y, "radius": radius})


def to_points(docs):
    pts = []
    for d in docs:
        try:
            pts.append({"name": d["place_name"], "lat": float(d["y"]), "lng": float(d["x"])})
        except (KeyError, ValueError):
            pass
    return pts


def dedup(pts):
    seen, out = set(), []
    for p in pts:
        k = (p["name"], round(p["lat"], 5), round(p["lng"], 5))
        if k in seen:
            continue
        seen.add(k)
        out.append(p)
    return out


def main() -> int:
    radius = int(sys.argv[1]) if len(sys.argv) > 1 else int(load_config()["radius_infra_m"])

    keys = load_keys()
    rest = keys.get("KAKAO_REST_KEY")
    if not rest:
        print("카카오 REST 키가 없습니다 (.env 확인)")
        return 1
    headers = {"Authorization": f"KakaoAK {rest}"}

    site = geocode_site(SITE_QUERY)
    if not site:
        print("사업지 좌표를 못 구했습니다")
        return 1
    x, y = site["lng"], site["lat"]
    print(f"▶ {SITE_QUERY} 반경 {radius}m 인프라 수집 중...")

    big_radius = max(radius, 5000)  # 백화점·스타디움은 드물어서 약간 넓게 (서울 고밀도 대응 5km)
    school = to_points(cat(headers, "SC4", x, y, radius))
    gov = to_points(cat(headers, "PO3", x, y, radius))

    # 백화점: 본점만 — 이름을 '브랜드+지점' 두 토큰으로 정규화해 입점매장(구찌 등)·
    #          주차장·문화센터·카드센터를 본점 하나로 흡수하고, 철물·가구·아울렛 등
    #          가짜 백화점과 부속시설은 제외
    DEPT_BRANDS = ("롯데백화점", "현대백화점", "신세계백화점", "갤러리아백화점")
    DEPT_EXCLUDE = ("주차장", "문화센터", "센터", "에비뉴엘", "아울렛", "할인", "상가", "철물", "가구")
    def dept_key(nm):
        s = nm.replace(" ", "")                          # 공백 유무 차이 흡수
        for b in DEPT_BRANDS:
            i = s.find(b)
            if i >= 0:
                m = re.match(r"(.+?점)", s[i + len(b):])  # 브랜드 뒤 첫 '점'까지 = 지점명
                return f"{b} {m.group(1)}" if m else b
        return nm.strip()
    dept, seen_d = [], set()
    for p in to_points(kw(headers, "백화점", x, y, big_radius)):
        nm = p["name"]
        if any(e in nm for e in DEPT_EXCLUDE):
            continue
        if not any(b in nm for b in DEPT_BRANDS):
            continue
        k = dept_key(nm)
        if k in seen_d:
            continue
        seen_d.add(k)
        p["name"] = k
        dept.append(p)

    # 대형 체육시설 — 랜드마크급만. 핵심 시설명을 포함하되 부속(주차장·편의점·충전소·
    #   보조경기장·어린이시설 등)·동명 지하철역·대학 구내 운동장은 제외
    STADIUM_KW = ("월드컵경기장", "종합운동장", "스포츠타운")
    STADIUM_EXCLUDE = (
        "주차장", "입구", "충전", "관리사무소", "매점", "보조경기장",
        "눈썰매", "테니스", "인라인", "롤러", "어린이", "장난감", "육아",
        "이마트24", "CU ", "GS25", "세븐일레븐", "편의점", "호선", "경기장역", "대학교",
    )
    def stadium_key(nm):
        s = nm.replace(" ", "")
        for k in STADIUM_KW:
            i = s.find(k)
            if i >= 0:
                return s[: i + len(k)]   # 핵심 시설명 끝까지 (앞 지역명 포함, 부속 흡수)
        return nm
    stadium_raw = to_points(
        kw(headers, "종합운동장", x, y, big_radius)
        + kw(headers, "월드컵경기장", x, y, big_radius)
        + kw(headers, "스포츠타운", x, y, big_radius)
    )
    stadium, seen_s = [], set()
    for p in stadium_raw:
        nm = p["name"]
        if not any(k in nm for k in STADIUM_KW):
            continue
        if any(e in nm for e in STADIUM_EXCLUDE):
            continue
        k = stadium_key(nm)
        if k in seen_s:
            continue
        seen_s.add(k)
        p["name"] = k
        stadium.append(p)

    payload = {
        "school": dedup(school),
        "dept": dedup(dept),
        "gov": dedup(gov),
        "stadium": dedup(stadium),
        "site": site,
        "radius": radius,
    }
    OUT.write_text(
        "window.INFRA_DATA = " + json.dumps(payload, ensure_ascii=False) + ";\n",
        encoding="utf-8",
    )
    stamp_html()
    print(f"학교 {len(payload['school'])} · 백화점 {len(payload['dept'])} · 관공서 {len(payload['gov'])} · 스타디움 {len(payload['stadium'])} 저장 -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
