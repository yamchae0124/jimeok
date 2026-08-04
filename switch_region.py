"""
switch_region.py — 지역 전환 원커맨드
======================================
사업지 주소 + 법정동코드 한 줄이면 config 갱신 → 7단계(수집(매매+전월세)→지오코딩→지도→
인프라→교통→LH공고→통근시간)를 순서대로 끝까지 돌린다. LH·통근은 선택 레이어(k-skill 프록시)
— 실패해도 지도 본체는 빌드된다. 예전처럼 코드 3곳을 손으로 고치고 따로 실행할 필요 없음.
DB는 지역별 파일(data/realprice_{코드}.db)로 자동 분리 — 백업·삭제 절차도 없어졌다.

사용:
    python switch_region.py "경기 의왕시 포일동 000-0" 41430
    python switch_region.py "경기 의왕시 포일동 000-0" 41430 36
    python switch_region.py "..." 41430 24 --region "경기 의왕시"

인자:
    1) 사업지 주소 (중심 핀, 따옴표로 감싸기)
    2) 법정동 시군구 코드 5자리 (행안부 법정동코드 앞 5자리)
    3) 수집 개월 수 (생략 시 24)
    --region "시군구명"  주소 앞 두 토큰과 다를 때만 지정 (지오코딩 접두용)
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

TOOL_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOL_ROOT))

from _config import load_config, save_config  # noqa: E402

# (label, script, required) — required=False 단계는 실패해도 지도 본체는 계속 빌드된다
STEPS = [
    ("실거래 수집(매매+전월세)", "collect.py",   True),
    ("좌표 지오코딩",            "geocode.py",   True),
    ("지도 데이터",              "build_map.py", True),
    ("인프라",                   "infra.py",     True),
    ("철도·도로",                "transit.py",   True),
    ("개발지구 경계",            "district.py",  False),   # VWorld — 키/호출 실패해도 지도는 산다
    ("행정구역 경계(시도·시군구·동)", "admin_boundary.py", False),   # SGIS 미러 — 키0, 미러/node 없어도 지도는 산다
    ("LH 공급공고",              "lh_notice.py", False),   # k-skill 프록시 — 죽어도 지도는 산다
    ("통근시간",                 "commute.py",   False),   # k-skill 프록시 — 죽어도 지도는 산다
]


def _read_data_js(name: str) -> dict | None:
    """window.X = {...}; 형식의 데이터 js를 dict로 읽는다 (요약 출력용)."""
    p = TOOL_ROOT / name
    if not p.exists():
        return None
    try:
        txt = p.read_text(encoding="utf-8")
        return json.loads(txt[txt.index("{"): txt.rindex("}") + 1])
    except Exception:
        return None


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    region_override = None
    if "--region" in sys.argv:
        i = sys.argv.index("--region")
        if i + 1 < len(sys.argv):
            region_override = sys.argv[i + 1]
            args = [a for a in args if a != region_override]

    if len(args) < 2:
        print(__doc__)
        return 1

    site_query = args[0]
    lawd = args[1]
    months = int(args[2]) if len(args) > 2 else 24

    if not re.fullmatch(r"\d{5}", lawd):
        print(f"❌ 법정동코드는 5자리 숫자여야 합니다: {lawd!r}")
        return 1

    # 시군구명: 명시 없으면 주소 앞 두 토큰 ("경기 의왕시 포일동 000-0" → "경기 의왕시")
    region = region_override or " ".join(site_query.split()[:2])

    cfg = load_config()
    cfg.update({
        "site_query": site_query,
        "lawd_cd": lawd,
        "region_name": region,
        "months": months,
    })
    save_config(cfg)
    print(f"▶ 지역 전환: {region} · {site_query} · LAWD {lawd} · {months}개월")
    print(f"   config.json 갱신 완료 · DB = data/realprice_{lawd}.db\n")

    for label, script, required in STEPS:
        print(f"━━ [{label}] python {script} " + "━" * 30)
        proc = subprocess.run([sys.executable, str(TOOL_ROOT / script)], cwd=TOOL_ROOT)
        if proc.returncode != 0:
            if required:
                print(f"\n❌ [{label}] 단계 실패 (exit {proc.returncode}) — 여기서 중단합니다.")
                print(f"   원인 해결 후 'python {script}'부터 다시 실행하면 이어서 갑니다.")
                return proc.returncode
            print(f"\n⚠️ [{label}] 실패 (exit {proc.returncode}) — 선택 레이어라 이 레이어 없이 계속 진행합니다.")
        print()

    # ── 결과 요약 (✅얻은것 / ❌못얻은것) ──
    md = _read_data_js("map_data.js") or {}
    inf = _read_data_js("infra.js") or {}
    tr = _read_data_js("transit.js") or {}
    meta = md.get("meta", {})
    site = meta.get("site")

    print("━━ 전환 완료 요약 " + "━" * 40)
    if site:
        print(f"✅ 사업지 핀: {site['name']} ({site['lat']:.5f}, {site['lng']:.5f})")
    else:
        print("❌ 사업지 핀 좌표 없음 — 주소를 카카오에서 못 찾음. 주소 표기 확인 필요")
    print(f"✅ 단지 {meta.get('geocoded', 0)}개 · 매매 {meta.get('trades_on_map', 0):,}건 "
          f"· 전월세 {meta.get('rents_on_map', 0):,}건")
    print(f"✅ 학교 {len(inf.get('school', []))} · 백화점 {len(inf.get('dept', []))} "
          f"· 관공서 {len(inf.get('gov', []))} · 운동장 {len(inf.get('stadium', []))}")
    rail = len(tr.get("subway", []))
    print(f"{'✅' if rail else '⚠️'} 철도 {rail}구간 · 역 {len(tr.get('stations', []))}개 "
          f"· 고속도로 {len(tr.get('highway', []))}구간")

    ds = _read_data_js("district.js") or {}
    zones = ds.get("zones", [])
    zn = [z for z in zones if z.get("kind") == "zone"]
    if zn:
        print(f"✅ 개발지구 {len(zn)}건: " + " · ".join(z["name"] for z in zn[:6])
              + (" 외" if len(zn) > 6 else ""))
    else:
        print(f"⚠️ 개발지구 0건{'(' + ds['source'] + ')' if ds.get('source') and 'KEY' in ds.get('source','') else ''}")

    ad = _read_data_js("admin.js") or {}
    alv = ad.get("levels", [])
    if alv:
        print("✅ 행정경계: " + " › ".join(f"{x['level']} {x['name']}" for x in alv))
    else:
        print(f"⚠️ 행정경계 0건{'(' + ad['source'] + ')' if ad.get('source') else ''}")

    lh = _read_data_js("lh.js") or {}
    lh_n = lh.get("notices", [])
    if lh.get("error"):
        print(f"⚠️ LH 공고: 수집 실패({lh['error']}) — 레이어 없이 표시")
    else:
        located = sum(1 for n in lh_n if n.get("lat"))
        print(f"{'✅' if lh_n else '⚠️'} LH 공고 {len(lh_n)}건 (좌표 {located} · 목록만 {len(lh_n) - located})")
    cm = _read_data_js("commute.js") or {}
    cm_t = cm.get("targets", [])
    if cm_t:
        print("✅ 통근: " + " · ".join(f"{t['name']} {t['minutes']}분" for t in cm_t))
    else:
        print(f"⚠️ 통근시간 없음{'(' + cm['error'] + ')' if cm.get('error') else ''}")
    print(f"\n→ 도구_자동화/지도_열기.bat 더블클릭 (로컬서버 → 실거래지도+행정구역SVG 두 탭)")
    print(f"   (실거래지도만 보면 map_leaflet.html 더블클릭도 OK · SVG탭은 서버 필요)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
