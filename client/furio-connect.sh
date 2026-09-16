#!/usr/bin/env bash
# ───────────────────────────────────────────────────────────────────────────
# furio connect — 서버 NPU 라우터로 가는 보안 SSH 터널 (부산대 서버: SSH 포트 10022)
#
# 외부 입구가 SSH(10022)뿐이라 라우터(:8400)에 직접 HTTP 로 못 붙는다.
# SSH 터널로 내 PC 의 localhost:8400 을 서버 라우터에 연결한다(코딩은 내 PC 로컬, 추론만 서버).
#
# 사용:
#   SDI_SSH_USER=jun bash furio-connect.sh        # 이 창 켜 둔 채로, 다른 창에서 furio 사용
#   그 다음 다른 터미널:
#     SDI_SERVER=http://127.0.0.1:8400 bash install.sh   # (최초 1회)
#     furio                                                # 사용
# ───────────────────────────────────────────────────────────────────────────
set -euo pipefail
: "${SDI_SSH_USER:?서버 계정을 지정하세요 (예: SDI_SSH_USER=jun)}"
HOST="${SDI_SSH_HOST:-164.125.19.138}"     # 공인 IP
SSH_PORT="${SDI_SSH_PORT:-10022}"          # 서버 SSH 포트
LOCAL_PORT="${SDI_LOCAL_PORT:-8400}"       # 내 PC 포트(설치/사용 시 SDI_SERVER=http://127.0.0.1:이포트)

# 이미 이 포트로 라우터가 닿으면(터널 가동 중) 새로 안 띄움
if curl -fsS --max-time 3 "http://127.0.0.1:$LOCAL_PORT/v1/models" >/dev/null 2>&1; then
  echo "✅ 이미 localhost:$LOCAL_PORT 로 서버에 연결돼 있습니다(터널 가동 중). 바로 'furio' 쓰세요."
  echo "   (새로 열려면 다른 포트로:  SDI_LOCAL_PORT=8401 bash furio-connect.sh)"
  exit 0
fi

echo "🔌 터널: localhost:$LOCAL_PORT  →  (ssh -p $SSH_PORT $SDI_SSH_USER@$HOST)  →  서버 localhost:8400"
echo "   비밀번호를 입력하세요. 이 창은 켜 둔 채로 다른 터미널에서 furio 를 쓰세요. (종료: Ctrl-C)"
echo "   설치/사용 시 서버주소:  SDI_SERVER=http://127.0.0.1:$LOCAL_PORT"
ssh -p "$SSH_PORT" -N -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
  -L "$LOCAL_PORT:localhost:8400" "$SDI_SSH_USER@$HOST" || {
  echo ""
  echo "[!] 터널 실패. 'Address already in use' 면 맥의 localhost:$LOCAL_PORT 가 이미 사용 중:"
  echo "    ① 이미 터널? curl http://127.0.0.1:$LOCAL_PORT/v1/models (되면 그냥 furio)"
  echo "    ② 끊고 다시: lsof -nP -iTCP:$LOCAL_PORT -sTCP:LISTEN → kill, 또는 pkill -f '$LOCAL_PORT:localhost:8400'"
  echo "    ③ 다른 포트: SDI_LOCAL_PORT=8401 bash furio-connect.sh (설치/사용도 SDI_SERVER=http://127.0.0.1:8401)"
  exit 1
}
