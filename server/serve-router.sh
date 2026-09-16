#!/usr/bin/env bash
# ───────────────────────────────────────────────────────────────────────────
# furio 라우터 기동/종료. 이 파일이 있는 폴더를 기준으로 동작한다.
#
#   bash serve-router.sh          # 기동 (:8400)
#   bash serve-router.sh stop     # 라우터 + 백엔드 전부 종료
#
# 환경변수(전부 선택):
#   FURIO_VENV        furiosa-llm 이 설치된 venv        (기본: ~/furiosa)
#   FURIOSA_LLM_BIN   furiosa-llm 실행 파일 직접 지정   (venv 를 안 쓸 때)
#   FURIO_ARTIFACTS   직접 빌드한 아티팩트 폴더         (기본: /mnt/nvme2n1p1/models/artifacts)
#   FURIO_CLIENT_DIST openclaude 포크 빌드 산출물 dist/ (설치기가 여기서 받아 간다)
#   FURIO_LOGDIR      로그 폴더                         (기본: 이 폴더의 logs/)
#   SDI_API_KEY       주면 라우터가 Bearer 인증을 요구한다(사내망 밖에 열 때 권장)
# ───────────────────────────────────────────────────────────────────────────
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOGDIR="${FURIO_LOGDIR:-$HERE/logs}"
LOG="$LOGDIR/router.log"
PIDFILE="$HERE/.router.pid"

# venv 가 있으면 켠다(furiosa-llm 이 그 안에 있다). 없으면 FURIOSA_LLM_BIN 으로 직접 지정.
VENV="${FURIO_VENV:-$HOME/furiosa}"
# shellcheck disable=SC1091
[ -f "$VENV/bin/activate" ] && source "$VENV/bin/activate" || true

stop_router() {
  if [ -f "$PIDFILE" ]; then
    kill "$(cat "$PIDFILE")" 2>/dev/null && echo "  라우터 종료 (pid $(cat "$PIDFILE"))" || true
    rm -f "$PIDFILE"
  fi
  # pid 파일이 없거나 다른 방식으로 뜬 라우터도 정리한다.
  # 문자클래스로 pkill 자신과의 self-match 를 피한다.
  pkill -f 'furiosa_router[.]py serve' 2>/dev/null && echo "  라우터 종료(패턴 일치)" || true
}

if [ "${1:-start}" = "stop" ]; then
  stop_router
  pkill -f 'furiosa-llm[ ]serve' 2>/dev/null && echo "  백엔드 종료" || true
  echo "종료 완료"; exit 0
fi

echo "[..] 기존 라우터·백엔드 정리 (라우터가 NPU 카드를 직접 스케줄링한다)"
stop_router
pkill -f 'furiosa-llm[ ]serve' 2>/dev/null || true
sleep 3

echo "[..] 라우터 기동 → :8400   (로그: $LOG)"
mkdir -p "$LOGDIR"
: > "$LOG"
nohup python3 "$HERE/furiosa_router.py" serve >> "$LOG" 2>&1 &
echo $! > "$PIDFILE"
sleep 2

if ! kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "[fail] 라우터가 바로 죽었습니다 — 로그를 보세요: tail -30 $LOG" >&2
  exit 1
fi
if [ -n "${SDI_API_KEY:-}" ]; then
  echo "[ok] 🔒 인증 ON — 사용자는 SDI_API_KEY 로 접속해야 합니다."
else
  echo "[ok] 🔓 인증 OFF — 키 없이 접속 가능(신뢰하는 망에서만 쓰세요)."
  echo "        키를 요구하려면:  SDI_API_KEY=<키> bash serve-router.sh"
fi
echo "[ok] 모델 목록:  curl -s localhost:8400/v1/models | python3 -m json.tool"
echo "     라우터 상태: curl -s localhost:8400/router/status | python3 -m json.tool"
