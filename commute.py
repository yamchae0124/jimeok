"""
commute.py — 사업지 → 주요 거점 자동차 통근시간 → commute.js (선택 레이어)
==========================================================================
"강남 35분" 같은 마케팅 카피 소재. 지도 좌하단 배지 패널 + 보고서 모드 합류.

데이터 경로: k-skill 프록시(무인증) 경유 카카오모빌리티 길찾기.
    GET {proxy_base}/v1/kakao-mobility/directions?origin={lng},{lat}&destination={lng},{lat}
(우리 카카오 키엔 모빌리티 권한이 없어 프록시 경유 — 정본 전환 시 proxy_base만 교체)

거점: config.json의 commute_targets [{"name","query"}] 우선.
비우면 자동 — 수도권(서울/경기/인천)은 강남역·서울시청·여의도역(좌표 하드코딩,
변하지 않는 랜드마크라 지오코딩 호출 절약), 비수도권은 시청 + KTX역(카카오 검색).

non-fatal: 거점 단위 try/except — 한 거점 실패해도 나머지 진행, 전체 실패도 exit 0.

사용:
    python commute.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import requests

TOOL_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOL_ROOT))

from _config import load_config   # noqa: E402
from _env import load_keys        # noqa: E402
from geocode import _search, KAKAO_KEYWORD  # noqa: E402

OUT = TOOL_ROOT / "commute.js"
SOURCE_LABEL = "카카오모빌리티 자동차 기준 (k-skill-proxy 경유)"

METRO = {"서울", "경기", "인천"}
# 수도권 디폴트 거점 — 변하지 않는 랜드마크라 좌표 하드코딩 (지오코딩 호출 절약)
METRO_TARGETS = [
    {"name": "강남역",   "lat": 37.4979, "lng": 127.0276},
    {"name": "서울시청", "lat": 37.5665, "lng": 126.9780},
    {"name": "여의도역", "lat": 37.5215, "lng": 126.9244},
]


def resolve_targets(cfg: dict, headers: dict | None) -> list[dict]:
    """거점 목록 → [{name, lat, lng}]. config 우선, 비면 지역별 자동."""
    custom = cfg.get("commute_targets") or []
    if custom:
        out = []
        for t in custom:
            q = t.get("query") or t.get("name") or ""
            if t.get("lat") and t.get("lng"):
                out.append({"name": t.get("name") or q, "lat": t["lat"], "lng": t["lng"]})
                continue
            if not (headers and q):
                continue
            try:
                res = _search(KAKAO_KEYWORD, headers, q)
                if res:
                    out.append({"name": t.get("name") or q, "lat": res[0], "lng": res[1]})
            except Exception:
                pass
        return out

    first = (cfg.get("region_name") or "").split()[0] if cfg.get("region_name") else ""
    if first in METRO:
        return list(METRO_TARGETS)

    # 비수도권 — 시청 + KTX역을 카카오 검색으로
    out = []
    if headers and first:
        for name, q in [(f"{first}시청", f"{first}시청"), ("KTX역", f"{first} KTX 역")]:
            try:
                res = _search(KAKAO_KEYWORD, headers, q)
                if res:
                    out.append({"name": name, "lat": res[0], "lng": res[1]})
            except Exception:
                pass
    return out


def write_payload(site: dict | None, targets: list[dict], error: str = "") -> None:
    data = {
        "site": site,
        "targets": targets,
        "source": SOURCE_LABEL,
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    if error:
        data["error"] = error
    OUT.write_text("window.COMMUTE_DATA = " + json.dumps(data, ensure_ascii=False) + ";\n",
                   encoding="utf-8")


def main() -> int:
    cfg = load_config()
    base = (cfg.get("proxy_base") or "").rstrip("/")

    keys = load_keys()
    rest = keys.get("KAKAO_REST_KEY")
    headers = {"Authorization": f"KakaoAK {rest}"} if rest else None

    # 사업지 좌표 — build_map과 같은 함수 재사용
    try:
        from build_map import geocode_site
        site = geocode_site(cfg["site_query"])
    except Exception as e:
        site = None
        print(f"⚠️ 사업지 지오코딩 실패({e})")
    if not (site and base):
        print("⚠️ 통근시간: 사업지 좌표 또는 proxy_base 없음 — 이 레이어만 건너뜁니다.")
        write_payload(site, [], error="사업지 좌표/proxy_base 없음")
        _stamp()
        return 0

    targets = resolve_targets(cfg, headers)
    if not targets:
        print("⚠️ 통근 거점을 정하지 못했습니다 — config.json commute_targets를 설정하세요.")
        write_payload(site, [], error="거점 없음")
        _stamp()
        return 0

    print(f"▶ 통근시간 — {cfg['site_query']} → {', '.join(t['name'] for t in targets)}")

    results = []
    for t in targets:
        try:
            r = requests.get(
                f"{base}/v1/kakao-mobility/directions",
                params={
                    "origin": f"{site['lng']},{site['lat']}",
                    "destination": f"{t['lng']},{t['lat']}",
                },
                timeout=15,
            )
            r.raise_for_status()
            routes = r.json().get("routes") or []
            summary = (routes[0] or {}).get("summary") or {}
            dur = summary.get("duration")
            dist = summary.get("distance")
            if dur is None:
                raise ValueError(f"duration 없음 (result: {routes[0].get('result_msg') if routes else '빈 응답'})")
            results.append({
                "name": t["name"],
                "lat": t["lat"], "lng": t["lng"],
                "minutes": round(dur / 60),
                "km": round(dist / 1000, 1) if dist else None,
            })
            print(f"   {t['name']:8s} {round(dur/60)}분 · {round(dist/1000,1)}km")
        except Exception as e:
            print(f"   ⚠️ {t['name']} 실패({type(e).__name__}: {e}) — 이 거점만 생략")

    write_payload(site, results)
    _stamp()
    print(f"✅ commute.js 생성 — 거점 {len(results)}/{len(targets)}개")
    print(f"   → {OUT}")
    return 0


def _stamp() -> None:
    try:
        from build_map import stamp_html
        stamp_html()
    except Exception:
        pass


if __name__ == "__main__":
    raise SystemExit(main())
