"""
build_hub.py — per-region map_data bundles → regions/index.json
===============================================================
data/realprice_*.db 를 글로빙해 각 5자리 lawd DB마다 build_map.main()을 호출,
regions/<lawd>/map_data.js 번들을 생성하고 regions/index.json(요약 인덱스)을 쓴다.

100% 로컬 — 외부 API 호출 없음 (build_map에 --lawd를 넘겨 --no-site-geocode 강제).
백업 DB(realprice_*_backup.db)는 건너뛴다.
"""
from __future__ import annotations

import json
import re
import sys
import traceback
from pathlib import Path

TOOL_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOL_ROOT))

import build_map  # noqa: E402


def load_js_data(path: Path) -> dict | None:
    """window.MAP_DATA = {...}; 파일을 dict로 읽는다."""
    if not path.exists():
        return None
    try:
        txt = path.read_text(encoding="utf-8")
        prefix = "window.MAP_DATA = "
        if not txt.startswith(prefix):
            return None
        return json.loads(txt[len(prefix):].rstrip(";\n"))
    except Exception:
        return None


def patch_region(path: Path, lawd: str) -> None:
    """meta.region이 비어 있으면 lawd 코드로 채워 다시 쓴다."""
    data = load_js_data(path)
    if data is None:
        return
    meta = data.get("meta", {})
    if meta.get("region"):
        return
    meta["region"] = lawd
    path.write_text(
        "window.MAP_DATA = " + json.dumps(data, ensure_ascii=False) + ";\n",
        encoding="utf-8",
    )


def main() -> int:
    data_dir = TOOL_ROOT / "data"
    regions_dir = TOOL_ROOT / "regions"

    db_files = sorted(data_dir.glob("realprice_*.db"))
    print(f"발견: {len(db_files)}개 DB 파일\n")

    records: list[dict] = []
    ok = skip = fail = 0
    stem_re = re.compile(r"^realprice_(\d{5})\.db$")

    for db_path in db_files:
        m = stem_re.match(db_path.name)
        if not m:
            print(f"⏭️  SKIP: {db_path.name} (5자리 lawd 아님 또는 backup)")
            skip += 1
            continue

        lawd = m.group(1)
        out_path = regions_dir / lawd / "map_data.js"

        try:
            ret = build_map.main(["--lawd", lawd, "--out", str(out_path)])
        except Exception:
            print(f"❌ FAIL: {lawd} — 예외 발생")
            traceback.print_exc()
            fail += 1
            continue

        if ret != 0:
            print(f"❌ FAIL: {lawd} — build_map.main() returned {ret}")
            fail += 1
            continue

        patch_region(out_path, lawd)

        data = load_js_data(out_path)
        if data is None:
            print(f"❌ FAIL: {lawd} — map_data.js 읽기/파싱 실패")
            fail += 1
            continue

        meta = data.get("meta", {})
        record = {
            "lawd": lawd,
            "region": meta.get("region", lawd),
            "generated_at": meta.get("generated_at", ""),
            "trade_count": meta.get("trade_count", 0),
            "rent_count": meta.get("rent_count", 0),
            "apt_count": meta.get("apt_count", 0),
            "geocoded": meta.get("geocoded", 0),
            "center": meta.get("center"),
            "has_infra": (regions_dir / lawd / "infra.js").exists(),
            "has_transit": (regions_dir / lawd / "transit.js").exists(),
            "has_admin": (regions_dir / lawd / "admin.js").exists(),
            "has_district": (regions_dir / lawd / "district.js").exists(),
        }
        records.append(record)
        ok += 1

    records.sort(key=lambda r: (r["region"], r["lawd"]))

    index_path = regions_dir / "index.json"
    index_path.write_text(
        json.dumps(records, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"\n{'='*70}")
    print(f"{'lawd':<8} {'region':<20} {'apts':>5} {'trades':>7} {'center?':<8} {'status':>6}")
    print(f"{'-'*70}")
    for r in records:
        c = r["center"]
        has_c = "✓" if c else "✗"
        print(f"{r['lawd']:<8} {r['region']:<20} {r['apt_count']:>5} {r['trade_count']:>7} "
              f"{has_c:<8} {'ok':>6}")
    print(f"{'-'*70}")
    print(f"OK: {ok}  SKIP: {skip}  FAIL: {fail}  TOTAL: {len(records)}")
    print(f"인덱스 → {index_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())