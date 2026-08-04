"""
_env.py — API 키 로더
=====================
국토부 키는 분양성검토 엔진 .env에서 자동으로 가져오고(이미 발급됨),
카카오 키는 이 도구 폴더의 .env에서 읽는다. 도구 .env가 우선.

필요 키:
- DATA_GO_KR_KEY  : 국토부 실거래가 (엔진 .env에 이미 있음)
- KAKAO_REST_KEY  : 카카오 로컬 API 지오코딩 (도구 .env, 부장님이 발급)
- KAKAO_JS_KEY    : 카카오맵 표시 (도구 .env, map.html이 사용)
"""
from __future__ import annotations

from pathlib import Path

TOOL_ROOT = Path(__file__).resolve().parent
TOOL_ENV = TOOL_ROOT / ".env"
ENGINE_ENV = TOOL_ROOT.parent.parent / "분양_부동산" / "0.분양성검토_엔진" / ".env"


def _parse(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def load_keys() -> dict[str, str]:
    """엔진 .env(국토부) + 도구 .env(카카오) 병합. 도구 .env가 우선."""
    env: dict[str, str] = {}
    env.update(_parse(ENGINE_ENV))   # 국토부 키
    env.update(_parse(TOOL_ENV))     # 카카오 키 (있으면 국토부 키도 덮어씀)
    return env
