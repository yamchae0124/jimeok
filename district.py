"""
district.py — 개발지구 경계 폴리곤 → district.js
=================================================
VWorld(국토부 공간정보) 데이터 API로 사업지 주변 개발지구 경계를 폴리곤으로 가져온다.
아실(asil.kr)이 보여주는 "공공주택지구·도시개발구역·신도시" 경계선과 같은 데이터.
- lt_c_lhzone  사업지구경계도  → 청라·루원시티·공공주택지구 등 이름 있는 큰 지구 (메인)
- lt_c_ud601   주거환경개선지구도 → 재개발성 소규모 지구 (보조)
map_leaflet.html이 L.polygon으로 채워 그리고 라벨을 붙인다.

⚠️ 선택 레이어 — VWORLD_KEY 없거나 호출 실패해도 지도 본체는 빌드된다(switch_region에서 required=False).
⚠️ VWorld 인증키는 등록 도메인으로만 통과 → Referer 헤더에 VWORLD_DOMAIN(www.vworld.kr) 넣어 호출.

사용:
    python district.py
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import requests

TOOL_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOL_ROOT))

from build_map import SITE_QUERY, geocode_site, stamp_html  # noqa: E402
from _config import load_config                              # noqa: E402
from _env import load_keys                                   # noqa: E402

API = "https://api.vworld.kr/req/data"
OUT = TOOL_ROOT / "district.js"

# (레이어, 이름필드, 종류) — 종류는 map에서 색·범례 구분용
LAYERS = [
    ("lt_c_lhzone", "zonename", "zone"),     # 사업지구경계도 (메인)
    ("lt_c_ud601",  "uname",    "improve"),  # 주거환경개선지구 (보조)
]

# 사업지 중심 BOX 반경 — 인접 대형 지구(루원시티 등)까지 잡되 과밀 방지
BOX_RADIUS_M = 6000


def _box(lat: float, lng: float, r: int) -> str:
    dlat = r / 111320
    dlng = r / (111320 * math.cos(math.radians(lat)))
    return f"BOX({lng-dlng:.6f},{lat-dlat:.6f},{lng+dlng:.6f},{lat+dlat:.6f})"


def _rings(geom: dict) -> list[list[list[float]]]:
    """GeoJSON Polygon/MultiPolygon → 폴리곤별 [ [ [lat,lng],... ](링), ... ] 리스트.
    좌표는 [lng,lat] 순서로 오므로 뒤집고 5자리(≈1m) 반올림."""
    def ring(coords):
        return [[round(c[1], 5), round(c[0], 5)] for c in coords]
    t = geom.get("type")
    if t == "Polygon":
        return [[ring(r) for r in geom["coordinates"]]]
    if t == "MultiPolygon":
        return [[ring(r) for r in poly] for poly in geom["coordinates"]]
    return []


def fetch(layer: str, box: str, key: str, domain: str) -> list[dict]:
    params = {
        "service": "data", "version": "2.0", "request": "GetFeature",
        "format": "json", "size": "100", "page": "1",
        "data": layer, "geomFilter": box, "crs": "EPSG:4326",
        "domain": domain, "key": key,
    }
    r = requests.get(API, params=params, headers={"Referer": f"https://{domain}"}, timeout=40)
    r.raise_for_status()
    js = r.json()
    resp = js.get("response", {})
    if resp.get("status") != "OK":
        err = resp.get("error", {})
        # 결과 0건도 status=NOT_FOUND로 와서 정상 — 빈 리스트로 흡수
        if err.get("code") == "NOT_FOUND" or resp.get("status") == "NOT_FOUND":
            return []
        raise RuntimeError(f"{layer}: {resp.get('status')} {err}")
    return resp.get("result", {}).get("featureCollection", {}).get("features", [])


def main() -> int:
    keys = load_keys()
    key = keys.get("VWORLD_KEY")
    domain = keys.get("VWORLD_DOMAIN", "www.vworld.kr")
    if not key:
        print("⚠️ VWORLD_KEY 없음 — 개발지구 레이어 건너뜀 (.env에 VWORLD_KEY 추가하면 활성화)")
        OUT.write_text("window.DISTRICT_DATA = {zones:[], source:'VWORLD_KEY 없음'};\n", encoding="utf-8")
        stamp_html()
        return 0

    site = geocode_site(SITE_QUERY)
    if not site:
        print("사업지 좌표를 못 구했습니다 — 개발지구 BOX를 못 잡음")
        return 1

    box = _box(site["lat"], site["lng"], BOX_RADIUS_M)
    print(f"▶ {SITE_QUERY} 주변 개발지구 경계 수집 (VWorld · 반경 {BOX_RADIUS_M//1000}km)")

    zones: list[dict] = []
    seen: set[str] = set()
    for layer, namefld, kind in LAYERS:
        try:
            feats = fetch(layer, box, key, domain)
        except Exception as e:
            print(f"   ⚠️ {layer} 호출 실패: {e}")
            continue
        added = 0
        for f in feats:
            p = f.get("properties", {})
            name = (p.get(namefld) or "").strip()
            shapes = _rings(f.get("geometry", {}))
            if not shapes:
                continue
            # 이름 없는 보조지구는 시군구로 라벨 보강 (중복 이름은 합치지 않고 그대로 둔다)
            if not name:
                name = p.get("sigg_name") or p.get("uname") or "개발지구"
            dkey = f"{kind}|{name}|{round(shapes[0][0][0][0],3)}"  # 종류+이름+첫좌표로 중복키
            if dkey in seen:
                continue
            seen.add(dkey)
            zones.append({"name": name, "kind": kind, "shapes": shapes})
            added += 1
        print(f"   · {layer}: {added}건")

    payload = {"zones": zones, "source": "VWorld 사업지구경계도·주거환경개선지구"}
    OUT.write_text("window.DISTRICT_DATA = " + json.dumps(payload, ensure_ascii=False) + ";\n", encoding="utf-8")
    stamp_html()

    zone_n = sum(1 for z in zones if z["kind"] == "zone")
    imp_n = sum(1 for z in zones if z["kind"] == "improve")
    print(f"✅ district.js 생성 — 개발지구 {zone_n}건 · 주거환경개선 {imp_n}건")
    if zones:
        print("   " + " · ".join(z["name"] for z in zones if z["kind"] == "zone")[:200])
    else:
        print("   ℹ️ 반경 내 등록 개발지구 0건 — 실제로 없을 수 있음(기존 시가지)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
