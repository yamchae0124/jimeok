"""
admin_boundary.py — 사업지 행정 위계 3단(시도·시군구·읍면동) 경계 → admin.js
=============================================================================
SGIS 행정경계(지도추출기 미러)로 사업지가 속한 시도/시군구/읍면동 경계를 가져온다.
VWorld 개발지구(district.py)와 별개 레이어 — 행정구역 맥락용. API키 0(미러 데이터).

흐름:  사업지 지오코딩(lat/lng) → node admin_geo.cjs → window.ADMIN_DATA → stamp_html
map_leaflet.html이 drawAdmin()으로 3단 토글 폴리곤을 그린다.

⚠️ 확장자가 `.cjs`인 이유: 루트 package.json의 "type":"module" 때문에 `.js`면 node가 ESM으로
   읽어 require()가 죽는다. vendor/topojson-*.js도 같은 이유로 vendor/package.json에
   {"type":"commonjs"}를 박아뒀다. `.js`로 되돌리지 말 것.

⚠️ 선택 레이어 — 미러 없거나 node 실패해도 지도 본체는 빌드된다(switch_region required=False).
⚠️ SGIS 읍면동 = 행정동(상도1동 등). 법정동(상도동)과 라벨이 다를 수 있음 — 지도엔 행정동 표기.

사용:  python admin_boundary.py
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

TOOL_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOL_ROOT))

from build_map import SITE_QUERY, geocode_site, stamp_html  # noqa: E402

GEO = TOOL_ROOT / "admin_geo.cjs"
MIRROR = (TOOL_ROOT / ".." / "지도추출기_노가다헌터").resolve()
OUT = TOOL_ROOT / "admin.js"


def _write(payload: dict) -> None:
    OUT.write_text("window.ADMIN_DATA = " + json.dumps(payload, ensure_ascii=False) + ";\n", encoding="utf-8")
    stamp_html()


def main() -> int:
    if not (MIRROR / "data" / "mapdata.js").exists():
        print(f"⚠️ 지도추출기 미러 없음 ({MIRROR}) — 행정경계 레이어 건너뜀")
        _write({"levels": [], "source": "미러 없음"})
        return 0

    site = geocode_site(SITE_QUERY)
    if not site:
        print("⚠️ 사업지 좌표 못 구함(KAKAO_REST_KEY?) — 행정경계 레이어 건너뜀")
        _write({"levels": [], "source": "좌표 없음"})
        return 0

    print(f"▶ {SITE_QUERY} 행정경계 3단 수집 (SGIS 미러 · 좌표 {site['lat']:.4f},{site['lng']:.4f})")
    try:
        proc = subprocess.run(
            ["node", str(GEO), "--lat", str(site["lat"]), "--lng", str(site["lng"])],
            cwd=TOOL_ROOT, capture_output=True, text=True, encoding="utf-8", timeout=60,
        )
    except FileNotFoundError:
        print("⚠️ node 미설치 — 행정경계 레이어 건너뜀")
        _write({"levels": [], "source": "node 없음"})
        return 0

    if proc.returncode != 0 or not proc.stdout.strip():
        print(f"   ⚠️ admin_geo 실패: {proc.stderr.strip()[:200]}")
        _write({"levels": [], "source": "조회 실패"})
        return 0

    data = json.loads(proc.stdout)
    lv = data.get("levels", [])
    print("   · " + " › ".join(f"{x['level']}={x['name']}" for x in lv) if lv else "   · 경계 0건")
    _write(data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
