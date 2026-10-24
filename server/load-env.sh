#!/usr/bin/env bash
# ───────────────────────────────────────────────────────────────────────────
# serve-router.sh 가 source 하는 .env 로더.   쓰는 법:  source load-env.sh <파일>
#
# 규칙
#   · 한 줄에 KEY=VALUE 하나. 키는 [A-Za-z_][A-Za-z0-9_]* 만(공백 불가). 빈 줄·'#' 주석·CRLF 는 건너뛴다.
#   · 값은 글자 그대로다 — 따옴표도 값에 들어가고(QUOTED="x" → 값 "x" 따옴표 포함), 둘째 '=' 부터도 값이다.
#   · 셸에서 이미 준 변수는 .env 보다 우선한다. 빈 값으로 준 것도 '준 것'이다:
#       ROUTER_PIN= bash serve-router.sh     # 이번 한 번만 핀 없이
#   · 꼴이 틀린 줄은 파일명·줄 번호를 들어 실패(return 1) → set -e 인 호출자는 거기서 멈춘다(라우터를 띄우지 않는다).
#   · 파일이 없으면 아무 일도 안 한다.
#   · 결과: DOTENV_LOADED(.env 에서 가져온 키들), DOTENV_SKIPPED(셸 값이 우선해 건너뛴 키들) — 공백 구분.
# ───────────────────────────────────────────────────────────────────────────
load_dotenv() {
  local f="$1" line k v n=0
  DOTENV_LOADED=""
  DOTENV_SKIPPED=""
  [ -f "$f" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    n=$((n + 1))
    line="${line%$'\r'}"
    if [[ "$line" =~ ^[[:space:]]*$ || "$line" =~ ^[[:space:]]*# ]]; then
      continue
    fi
    k="${line%%=*}"
    v="${line#*=}"
    if [ "$k" = "$line" ] || ! [[ "$k" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
      echo "[fail] $f:$n — 'KEY=VALUE' 꼴이 아닙니다(키는 영문·숫자·_ 만, 공백 없이): $line" >&2
      return 1
    fi
    # ${!k+x}: 셸에 '설정돼 있으면'(빈 값 포함) x. ${!k:-} 로 재면 빈 값이 '안 준 것'이 돼 .env 가 덮어쓴다.
    if [ -z "${!k+x}" ]; then
      export "$k=$v"
      DOTENV_LOADED="${DOTENV_LOADED:+$DOTENV_LOADED }$k"
    else
      DOTENV_SKIPPED="${DOTENV_SKIPPED:+$DOTENV_SKIPPED }$k"
    fi
  done < "$f"
  return 0
}

if [ $# -lt 1 ]; then
  echo "[fail] load-env.sh: .env 경로를 인자로 주세요 (source load-env.sh <파일>)" >&2
  return 1 2>/dev/null || exit 1
fi
load_dotenv "$1"
