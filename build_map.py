"""
build_map.py — SQLite → map_data.js
=====================================
지도(map_leaflet.html)가 읽을 데이터를 만든다. 브라우저는 SQLite를 직접 못 읽으니 export한다.

좌표가 있는 단지의 거래만 내보낸다. 좌표를 단지별로 한 번만 저장하고
거래는 단지 인덱스(a)로 참조해 용량을 줄인다(좌표 중복 제거).
필터(가격/평형/기간)는 map_leaflet.html이 이 거래 배열로 클라이언트에서 동적 집계한다.
export 후 map_leaflet.html의 데이터 스크립트 src에 ?v=타임스탬프를 박아 브라우저 캐시를 무효화한다.

사용:
    python build_map.py
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

TOOL_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOL_ROOT))

import db as dbm                  # noqa: E402
from collect import region_name   # noqa: E402
from _config import load_config   # noqa: E402
from _env import load_keys        # noqa: E402
from geocode import _search, KAKAO_ADDR, KAKAO_KEYWORD  # noqa: E402

# map_leaflet.html이 <script>로 읽는다. JSON 대신 .js로 내보내야 file:// 더블클릭으로 열린다
# (fetch는 file://에서 브라우저가 차단함).
OUT = TOOL_ROOT / "map_data.js"

_CFG = load_config()

# 사업지(중심 핀) 검색어 — config.json에서 온다. 바꾸려면 switch_region.py 또는 config.json.
SITE_QUERY = _CFG["site_query"]


def geocode_site(query: str) -> dict | None:
    """사업지 좌표를 카카오 REST로 1회 지오코딩(infra·transit과 공유). 키 없으면 None."""
    keys = load_keys()
    rest = keys.get("KAKAO_REST_KEY")
    if not (rest and query):
        return None
    headers = {"Authorization": f"KakaoAK {rest}"}
    try:
        res = _search(KAKAO_ADDR, headers, query) or _search(KAKAO_KEYWORD, headers, query)
    except Exception as e:
        print(f"⚠️ 사업지 지오코딩 실패({type(e).__name__}: {e}) — 중심 핀 없이 진행")
        return None
    if res:
        return {"name": query, "lat": res[0], "lng": res[1]}
    return None


def stamp_html(version: str | None = None) -> None:
    """map_leaflet.html의 데이터 js src에 ?v=타임스탬프 — file://에서도 캐시 무효화.
    (이게 없으면 데이터 갱신 후 강력 새로고침(Ctrl+Shift+R)을 해야 했음)"""
    version = version or datetime.now().strftime("%Y%m%d%H%M%S")
    html_path = TOOL_ROOT / "map_leaflet.html"
    if not html_path.exists():
        return
    html = html_path.read_text(encoding="utf-8")
    new = re.sub(
        r'src="(map_data|transit|infra|lh|commute|district|admin|gdc_data)\.js(\?v=\d+)?"',
        rf'src="\1.js?v={version}"',
        html,
    )
    if new != html:
        html_path.write_text(new, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    # ── 인자 파싱 ──
    # --lawd가 주어지면 --no-site-geocode 강제 (타지역 주소로 카카오 API 호출하지 않음).
    # --out이 주어지면 --no-stamp 강제 (라이브 html을 사이드 번들 경로로 덮어쓰지 않음).
    ap = argparse.ArgumentParser(
        description="SQLite → map_data.js export (with optional hub bundling).",
        epilog="--lawd → --no-site-geocode 강제 | --out → --no-stamp 강제 | --nosite → 사업지 핀 완전 제거",
    )
    ap.add_argument("--lawd", help="법정동 시군구 코드 5자리 (생략 시 config.json)")
    ap.add_argument("--out", help="출력 경로 (생략 시 map_data.js)")
    ap.add_argument("--no-stamp", action="store_true", help="map_leaflet.html 캐시 버전 갱신 안 함")
    ap.add_argument(
        "--no-site-geocode", action="store_true",
        help="사업지 지오코딩 건너뜀 (--lawd 있으면 자동 적용)",
    )
    ap.add_argument(
        "--nosite", action="store_true",
        help="사업지 핀 완전 제거: 지오코딩 skip + site_query 빈 문자열 export (배포·데모용)",
    )
    args = ap.parse_args(argv)

    lawd = args.lawd or _CFG["lawd_cd"]
    out = Path(args.out) if args.out else OUT
    no_stamp = args.no_stamp or bool(args.out)        # --out → stamp 강제 OFF
    no_site = args.no_site_geocode or args.nosite or bool(args.lawd)  # --lawd·--nosite → site geocode 강제 OFF

    out.parent.mkdir(parents=True, exist_ok=True)

    conn = dbm.connect(dbm.db_path(lawd))
    dbm.init_db(conn)

    # 지역별 DB 분리에 더해 쿼리에도 lawd 필터(이중 방어) — 타지역 혼입 원천 차단
    total_trades = conn.execute(
        "SELECT COUNT(*) FROM trades WHERE lawd_cd = ?", (lawd,)
    ).fetchone()[0]
    total_apts = conn.execute(
        "SELECT COUNT(*) FROM (SELECT 1 FROM trades WHERE lawd_cd = ? GROUP BY apt_name, legal_dong)",
        (lawd,),
    ).fetchone()[0]

    # 좌표 확보된 단지
    coord_rows = conn.execute(
        """
        SELECT apt_key, apt_name, legal_dong, lat, lng
        FROM apt_coords
        WHERE source IN ('address', 'keyword') AND lat IS NOT NULL
        """
    ).fetchall()

    apts: list[dict] = []
    key_to_idx: dict[str, int] = {}
    for r in coord_rows:
        key_to_idx[r["apt_key"]] = len(apts)
        apts.append({
            "i": len(apts),
            "name": r["apt_name"],
            "dong": r["legal_dong"],
            "lat": r["lat"],
            "lng": r["lng"],
        })

    # 거래 (좌표 있는 단지만, 날짜 있는 것만)
    trade_rows = conn.execute(
        """
        SELECT apt_name, legal_dong, exclu_area, pyeong, deal_amount, deal_date, floor, build_year, deal_gbn
        FROM trades
        WHERE deal_date <> '' AND lawd_cd = ?
        ORDER BY deal_date
        """,
        (lawd,),
    ).fetchall()

    trades: list[dict] = []
    for t in trade_rows:
        idx = key_to_idx.get(dbm.apt_key(t["apt_name"], t["legal_dong"]))
        if idx is None:
            continue
        row = {
            "a": idx,                      # 단지 인덱스
            "ar": t["exclu_area"],         # 전용 ㎡
            "py": t["pyeong"],             # 전용 평
            "amt": t["deal_amount"],       # 만원
            "d": t["deal_date"],           # YYYY-MM-DD
            "fl": t["floor"],
            "by": t["build_year"],
        }
        if (t["deal_gbn"] or "") == "직거래":
            row["dr"] = 1                  # 직거래 플래그 (중개거래·미상은 미표기 — 페이로드 절약)
        trades.append(row)

    # 전월세 (좌표 있는 단지만, 날짜 있는 것만) — 매매와 같은 단지 인덱스 공유
    rent_rows = conn.execute(
        """
        SELECT apt_name, legal_dong, exclu_area, pyeong, deposit, monthly_rent,
               deal_date, floor, build_year, contract_type
        FROM rents
        WHERE deal_date <> '' AND lawd_cd = ?
        ORDER BY deal_date
        """,
        (lawd,),
    ).fetchall()

    rents: list[dict] = []
    for t in rent_rows:
        idx = key_to_idx.get(dbm.apt_key(t["apt_name"], t["legal_dong"]))
        if idx is None:
            continue
        rents.append({
            "a": idx,                      # 단지 인덱스 (apts 공유)
            "ar": t["exclu_area"],         # 전용 ㎡
            "py": t["pyeong"],             # 전용 평
            "dep": t["deposit"],           # 보증금 만원
            "mr": t["monthly_rent"],       # 월세 만원 (0이면 전세)
            "d": t["deal_date"],           # YYYY-MM-DD
            "fl": t["floor"],
            "by": t["build_year"],
            "ct": t["contract_type"],      # 신규 | 갱신 | ''
        })

    total_rents = conn.execute(
        "SELECT COUNT(*) FROM rents WHERE lawd_cd = ?", (lawd,)
    ).fetchone()[0]

    region = region_name(lawd, _CFG)

    site = None
    if not no_site:
        site = geocode_site(SITE_QUERY)
        if site:
            print(f"   사업지 핀: {site['name']} ({site['lat']:.5f}, {site['lng']:.5f})")

    center = None
    if apts:
        center = {
            "lat": round(sum(a["lat"] for a in apts) / len(apts), 6),
            "lng": round(sum(a["lng"] for a in apts) / len(apts), 6),
        }
    elif site:
        # 실거래 단지가 0개(API 공백 등)여도 사업지 핀 좌표로 지도 중심을 잡는다 —
        # 인프라·행정경계·개발지구 레이어는 실거래와 무관하게 항상 보여야 한다.
        center = {"lat": site["lat"], "lng": site["lng"]}

    data = {
        "meta": {
            "region": region,
            "lawd": lawd,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "trade_count": total_trades,
            "rent_count": total_rents,
            "apt_count": total_apts,
            "geocoded": len(apts),
            "trades_on_map": len(trades),
            "rents_on_map": len(rents),
            "center": center,
            # --nosite: 사업지 주소 자체를 번들에서 제거 (배포판 공개 노출 차단 — site_query도 좌표 추적 단서)
            "site_query": "" if args.nosite else SITE_QUERY,
            "site": site,
            # kakao_js_key·google_maps_key export 제거 (2026-07-21):
            # 정본 map_leaflet.html은 안 읽고 _archive 구판만 쓰던 값 —
            # 배포 시 API 키 평문 노출 사고 방지.
        },
        "apts": apts,
        "trades": trades,
        "rents": rents,
    }
    out.write_text(
        "window.MAP_DATA = " + json.dumps(data, ensure_ascii=False) + ";\n",
        encoding="utf-8",
    )

    if not no_stamp:
        stamp_html()

    print("✅ map_data.js 생성")
    print(f"   전체 단지 {total_apts}개 · 매매 {total_trades:,}건 · 전월세 {total_rents:,}건")
    print(f"   좌표 확보 단지 {len(apts)}개 · 지도 표시 매매 {len(trades):,}건 · 전월세 {len(rents):,}건")
    if not apts:
        print("   ⚠️ 좌표가 0개입니다. 카카오 키 넣고 'python geocode.py'를 먼저 실행하세요.")
    print(f"   → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
