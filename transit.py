"""
transit.py — 지하철 노선(공식 노선색)·역 + 고속도로 → transit.js
=================================================================
OSM Overpass(무료)로 사업지 반경 지하철 노선/역과 고속도로/간선을 가져온다.
- 노선: 공식 노선색(네이버/카카오와 동일)으로 색칠
- 역: 흰 동그라미 + 노선색 테두리 + 역이름 (map_leaflet이 그림)

사용:
    python transit.py
"""
from __future__ import annotations

import json
import math
import re
import sys
from pathlib import Path

import requests

TOOL_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOL_ROOT))

from build_map import SITE_QUERY, geocode_site, stamp_html  # noqa: E402
from _config import load_config                  # noqa: E402

OVERPASS = "https://overpass-api.de/api/interpreter"
OUT = TOOL_ROOT / "transit.js"

# 한국 주요 지하철 노선 공식색 (네이버/카카오 동일)
# ⚠ 순서 중요 — 위에서부터 부분매칭하므로 지역·특수 노선을 범용 'N호선'보다 먼저 둔다
#   (안 그러면 "대전 1호선"이 수도권 "1호선" 파란색에 먼저 걸린다)
LINE_COLORS = {
    # 지역·광역·특수 노선 (우선 매칭)
    "대전": "#007448",
    "부산 1": "#F06A00", "부산 2": "#81BF48", "부산 3": "#BB8C00", "부산 4": "#217DCB",
    "대구 1": "#D93F5C", "대구 2": "#00AA80", "대구 3": "#FFB100",
    "광주": "#009088", "인천 1": "#7CA8D5", "인천 2": "#ED8B00",
    "신분당": "#D4003B", "수인분당": "#FABE00", "동해": "#0054A6",
    "경의중앙": "#77C4A3", "공항철도": "#0090D2", "경춘": "#0C8E72",
    "우이신설": "#B0CE18", "서해": "#8FC31F", "김포골드": "#A17800", "GTX-A": "#9A6292",
    "분당": "#FABE00",
    # 수도권 1~9호선 (범용 — 맨 뒤)
    "1호선": "#0052A4", "2호선": "#00A84D", "3호선": "#EF7C1C", "4호선": "#00A5DE",
    "5호선": "#996CAC", "6호선": "#CD7C2F", "7호선": "#747F00", "8호선": "#E6186C", "9호선": "#BDB092",
}

DEFAULT_LINE = "#5b6571"


def line_color(tags: dict) -> str:
    name = (tags.get("name") or "")
    ref = (tags.get("ref") or "")
    # 1) OSM colour 태그 우선 — 네이버/카카오와 동일한 공식색이 대부분 채워져 있다
    #    (지역 노선명이 "부산 도시철도 N호선"이라 아래 LINE_COLORS 부분매칭이 어긋나는 문제를
    #     원천 차단. OSM 색이 있으면 그게 정답)
    c = (tags.get("colour") or "").strip()
    if c.startswith("#") and len(c) in (4, 7):
        return c
    # 2) 공식색 fallback — 노선명(name)만으로 매칭. ref는 붙이지 않는다
    #    ("…→ 인천" + ref "1" 이 "인천 1"로 합쳐져 인천1호선색에 오매칭되는 것을 방지)
    for key, col in LINE_COLORS.items():
        if key in name:
            return col
    # 3) name에 키워드가 없을 때만 호선 번호로 ('Line 4', ref='4')
    m = re.search(r"(?:호선|[Ll]ine)\s*([1-9])", name) or re.fullmatch(r"\s*([1-9])\s*", ref)
    if m and f"{m.group(1)}호선" in LINE_COLORS:
        return LINE_COLORS[f"{m.group(1)}호선"]
    return DEFAULT_LINE


def fetch(lat: float, lng: float, radius: int) -> dict:
    hw_radius = radius + 1000  # 고속도로는 굵직한 축이라 살짝 넓게 (기존 4km/5km 비율 유지)
    q = f"""
    [out:json][timeout:90];
    (
      relation["route"="subway"](around:{radius},{lat},{lng});
      relation["route"="light_rail"](around:{radius},{lat},{lng});
      relation["route"="monorail"](around:{radius},{lat},{lng});
      relation["route"="train"]["service"~"commuter|regional"](around:{radius},{lat},{lng});
    );
    out geom;
    (
      node["railway"="station"](around:{radius},{lat},{lng});
    );
    out;
    (
      way["highway"="motorway"](around:{hw_radius},{lat},{lng});
      way["highway"="trunk"](around:{hw_radius},{lat},{lng});
    );
    out geom;
    """
    r = requests.post(
        OVERPASS, data={"data": q},
        headers={"User-Agent": "realprice-map/1.0 (real-estate research; local tool)"},
        timeout=150,
    )
    r.raise_for_status()
    try:
        return r.json()
    except ValueError:
        raise RuntimeError(f"Overpass 응답이 JSON이 아님 (서버 혼잡 가능성): {r.text[:200]}")


def main() -> int:
    site = geocode_site(SITE_QUERY)
    if not site:
        print("사업지 좌표를 못 구했습니다")
        return 1

    radius = int(load_config()["radius_transit_m"])
    print(f"▶ {SITE_QUERY} 반경 {radius}m 철도 노선·역 + 고속도로 수집 중...")
    try:
        data = fetch(site["lat"], site["lng"], radius)
    except Exception as e:
        print(f"Overpass 호출 실패: {e}")
        return 1

    subway, highway, stations = [], [], []
    seen_st, seen_way = set(), set()
    line_stops = []   # (color, lat, lng) — 각 노선이 '실제 정차'하는 역 좌표. 통과/환승 구분 근거
    for el in data.get("elements", []):
        t = el.get("type")
        tags = el.get("tags", {})
        # 광역전철(동해선 등)은 OSM에서 route=train + service=commuter — 지하철과 같이 그린다
        if t == "relation" and tags.get("route") in ("subway", "light_rail", "monorail", "train"):
            col = line_color(tags)
            for m in el.get("members", []):
                if m.get("type") == "way" and m.get("geometry"):
                    if m.get("ref") in seen_way:   # 상행·하행 relation이 공유하는 선로 중복 제거
                        continue
                    seen_way.add(m.get("ref"))
                    subway.append({"c": col, "l": [[round(g["lat"], 5), round(g["lon"], 5)] for g in m["geometry"]]})
                elif m.get("type") == "node" and "stop" in (m.get("role") or "") and m.get("lat") is not None:
                    line_stops.append((col, m["lat"], m["lon"]))   # 이 노선이 여기 '정차'
        elif t == "node" and tags.get("railway") == "station":
            nm = tags.get("name", "")
            if nm and nm not in seen_st:   # 환승역(노선별 노드 분리)은 이름 1개로 합침
                seen_st.add(nm)
                stations.append({"name": nm, "lat": round(el["lat"], 5), "lng": round(el["lon"], 5)})
        elif t == "way" and tags.get("highway") in ("motorway", "trunk") and el.get("geometry"):
            highway.append([[round(g["lat"], 5), round(g["lon"], 5)] for g in el["geometry"]])

    # 역별 '정차 노선색' 계산 — 선로가 옆을 지나가기만 하면(거제역 옆 동해선) 제외, 실제 정차역만 환승 표시
    STOP_RADIUS_M = 200   # 노선별 stop 노드가 역 중심에서 ~200m 안이면 그 노선이 정차
                          # (환승역은 노선별 승강장이 최대 ~180m 벌어짐 — 교대 1호선 승강장 171m. 인접역은 500m+라 안전)
    for st in stations:
        cos = math.cos(st["lat"] * math.pi / 180)
        cols = []
        for col, la, ln in line_stops:
            dla = (la - st["lat"]) * 111000
            dln = (ln - st["lng"]) * 111000 * cos
            if dla * dla + dln * dln <= STOP_RADIUS_M * STOP_RADIUS_M and col not in cols:
                cols.append(col)
        if cols:
            st["lines"] = cols   # 권위 데이터. map_leaflet이 근접추정보다 우선 사용

    payload = {"subway": subway, "highway": highway, "stations": stations, "site": site}
    OUT.write_text(
        "window.TRANSIT_DATA = " + json.dumps(payload, ensure_ascii=False) + ";\n",
        encoding="utf-8",
    )
    stamp_html()
    print(f"철도 노선 {len(subway)}구간 · 역 {len(stations)}개 · 고속도로/간선 {len(highway)}구간 저장")
    if not subway:
        print("   ℹ️ 철도 노선 0구간 — 반경 내에 지하철·광역전철이 실제로 없는지 확인 필요 (비수도권은 정상일 수 있음)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
