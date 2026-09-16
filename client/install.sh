#!/usr/bin/env bash
# ───────────────────────────────────────────────────────────────────────────
# furio — Claude Code 같은 코딩 에이전트(openclaude)를 서버 NPU 에 붙여 쓰는 CLI 설치기 (macOS / Linux)
#
# openclaude(github.com/Gitlawb/openclaude)는 Claude Code 에서 파생된 오픈소스 CLI 라
# "정말 Claude 같은" 경험(권한 프롬프트·CLAUDE.md·서브에이전트·슬래시 등)을 그대로 주되,
# 추론은 서버 NPU(OpenAI 호환 라우터 :8400)에서 돈다. 도구 실행·파일 수정은 내 PC 로컬.
#
# 사용 (SDI_SERVER = 라우터 주소):
#   원격(집/외부): 먼저 SSH 터널 → SDI_SERVER=http://127.0.0.1:8400  (furio-connect.sh / README 2-A)
#   사내 LAN:     SDI_SERVER=http://10.125.19.138:8400
#   예) SDI_SERVER=http://127.0.0.1:8400 bash install.sh
#       (서버 인증 ON 이면 SDI_API_KEY=<키> 추가)
#
# 설치 후:  furio            # Claude 같은 코딩 에이전트 TUI (추론=서버 NPU)
#           furio -p "..."   # 비대화형 한 줄(print 모드)
# ───────────────────────────────────────────────────────────────────────────
set -euo pipefail
# 오프라인 설치(서버에 접속이 안 되는 PC): FURIO_OFFLINE=1
#   · SDI_SERVER 를 안 줘도 되고(기본 127.0.0.1:8400 — 나중에 터널/목 라우터를 붙일 주소),
#   · 서버 도달 실패가 설치를 죽이지 않는다(경고만).
#   · NPU 기능이 든 dist 는 서버에서 못 받으므로 FURIO_CLIENT_DIST=<cli.mjs 있는 폴더> 로 준다.
#     (미리 복사해 둔 dist. 없으면 업스트림으로 설치되어 NPU LED/위젯이 빠진다.)
if [ "${FURIO_OFFLINE:-0}" = "1" ]; then
  SDI_SERVER="${SDI_SERVER:-http://127.0.0.1:8400}"
else
  : "${SDI_SERVER:?SDI_SERVER 를 지정하세요 (예: http://127.0.0.1:8400  ← SSH 터널 권장)  |  서버가 없으면 FURIO_OFFLINE=1}"
fi
SDI_SERVER="${SDI_SERVER%/}"
SDI_API_KEY="${SDI_API_KEY:-}"                       # 키는 선택 — 서버 인증 OFF 면 비워도 됨
if [ -n "$SDI_API_KEY" ]; then
  case "$SDI_API_KEY" in *[!A-Za-z0-9._-]*) echo "[fail] SDI_API_KEY 에 허용 안 되는 문자(영숫자와 . _ - 만)"; exit 1 ;; esac
fi
CMD="${FURIO_CMD:-furio}"                               # 명령 이름(리브랜딩: FURIO_CMD)
BRAND="${FURIO_BRAND:-Furio (Furiosa NPU)}"
MODEL="${FURIO_MODEL:-gpt-oss-120b}"                   # 기본 모델(agent-ready). /v1/models 의 id
MAXOUT="${FURIO_MAX_OUTPUT:-8192}"                     # ⚠️ openclaude 큰 시스템프롬프트(~17.6k)+출력이 ctx 초과 방지
AUTODEF="${FURIO_AUTO:-}"                              # 자동모드 기본값. 빈값=매번확인(가장 안전) / safe=규칙기반 / edits=편집만 / 1=완전자동
ORIG_PATH="$PATH"
HOME_DIR="${FURIO_HOME:-$HOME/.$CMD}"                  # openclaude 격리 설치/설정 위치
BIN_DIR="${FURIO_BIN_DIR:-$HOME/.local/bin}"

echo "[1/4] 실행 런타임 확인 (openclaude 는 Node ≥22 요구)"
# bun 도 받아들인다. RNGD 서버처럼 배포판 node 가 18 에 묶여 있고 nvm 을 깔 수 없는(또는
# npm prefix 가 이미 잡혀 있어 nvm 이 거부하는) 환경이 있어서다. bun 은 Node 호환 런타임이라
# openclaude 가 그대로 돈다 — 아래 헬퍼와 furio 래퍼가 같은 것을 쓴다.
#
# 판정은 '그 런타임이 흉내 내는 Node 주 버전이 22 이상인가' 하나로 한다. bun 이 있기만 해서는
# 부족하다 — 옛 bun 은 아래 격리 플래그를 몰라 헬퍼가 전부 조용히 실패한다.
#   --no-env-file      : 작업 폴더의 .env·.env.local 을 자동으로 읽지 않는다
#   --config=/dev/null : 작업 폴더의 bunfig.toml(preload 스크립트)을 읽지 않는다
#                        ('-c 경로' 처럼 = 없이 쓰면 preload 가 그대로 돈다)
# node 로 돌 때는 둘 다 없는 동작이다. bun 으로 돌 때도 없어야 같은 프로그램이다.
node_major() {  # $@ = 런타임 명령. 흉내 내는 Node 주 버전, 못 읽으면 0.
  _v=$("$@" -e 'process.stdout.write(String(process.versions.node).split(".")[0])' 2>/dev/null | tail -n1 || true)
  case "$_v" in ''|*[!0-9]*) echo 0 ;; *) echo "$_v" ;; esac
}
BUN_BIN="$(command -v bun 2>/dev/null || true)"
NODEMAJ=$(node_major node)
BUNMAJ=0; [ -n "$BUN_BIN" ] && BUNMAJ=$(node_major "$BUN_BIN" --no-env-file --config=/dev/null)
if [ "$NODEMAJ" -ge 22 ]; then
  JS=node
elif [ "$BUNMAJ" -ge 22 ]; then
  JS=bun
  echo "      [ok] node $( [ "$NODEMAJ" = 0 ] && echo 없음 || node -v ) — bun $("$BUN_BIN" --version) 으로 실행합니다"
elif [ -n "$BUN_BIN" ]; then
  echo "[fail] node $( [ "$NODEMAJ" = 0 ] && echo 없음 || node -v ), bun $("$BUN_BIN" --version 2>/dev/null || echo '?') — 이 bun 은 너무 옛 버전입니다."
  echo "       'bun upgrade' 후 다시 실행하세요 (bun 1.3.14 에서 확인)."
  exit 1
else
  echo "[fail] Node ≥22 도 bun 도 없습니다."
  echo "       가장 간단한 길: 'nvm install 22' (또는 brew install node)."
  echo "       node 를 못 올리면 지금 node·npm 은 그대로 두고 bun 을 더합니다:"
  echo "       'curl -fsSL https://bun.sh/install | bash' 후 새 터미널에서 다시 실행."
  exit 1
fi
command -v npm >/dev/null 2>&1 || { echo "[fail] npm 없음 — 패키지를 받는 데 필요합니다(node 버전은 상관없음). 예: apt install nodejs npm / brew install node"; exit 1; }
# 설치 중 JSON 헬퍼는 모두 js 로 부른다.
js() { if [ "$JS" = bun ]; then "$BUN_BIN" --no-env-file --config=/dev/null "$@"; else node "$@"; fi; }

echo "[2/4] openclaude 설치 (격리 prefix: $HOME_DIR — 전역 npm 안 건드림)"
mkdir -p "$HOME_DIR" "$BIN_DIR"
AUTH=(); [ -n "$SDI_API_KEY" ] && AUTH=(-H "Authorization: Bearer $SDI_API_KEY")

# 서버가 NPU 기능(모델별 LED·dp/pp)이 들어간 openclaude 포크를 빌드해 두었는지 확인.
# 있으면 npm 으로 '포크와 같은 버전'을 고정 설치한 뒤 dist 만 덮어쓴다 —
# 개인 PC 에 bun/빌드 툴체인을 깔 필요가 없고, install.sh 한 줄이 그대로 유지된다.
# 로컬 dist 우선: FURIO_CLIENT_DIST 로 미리 복사해 둔 포크 dist 를 주면 서버 없이도 NPU 기능이 적용된다.
LOCAL_DIST=""
if [ -n "${FURIO_CLIENT_DIST:-}" ]; then
  _LD="${FURIO_CLIENT_DIST%/}"
  if [ -f "$_LD/cli.mjs" ]; then
    LOCAL_DIST="$_LD"
  else
    echo "      [warn] FURIO_CLIENT_DIST 에 cli.mjs 가 없습니다: $_LD — 무시하고 진행"
  fi
fi

FORK_VER=""
if [ -z "$LOCAL_DIST" ]; then
  FORK_VER=$(curl -fsS --max-time 10 ${AUTH[@]+"${AUTH[@]}"} "$SDI_SERVER/router/client/manifest.json" 2>/dev/null | js -e '
let s="";process.stdin.on("data",d=>s+=d).on("end",()=>{
  try{const j=JSON.parse(s);process.stdout.write(j.ok&&j.version?String(j.version):"")}catch(e){process.stdout.write("")}});' 2>/dev/null || echo "")
fi

if [ -n "$LOCAL_DIST" ]; then
  echo "      로컬 dist 사용: $LOCAL_DIST (서버 없이 NPU 기능 적용)"
  NPM_SPEC="@gitlawb/openclaude@${FURIO_CLIENT_VER:-latest}"
elif [ -n "$FORK_VER" ]; then
  echo "      서버 포크 감지 — @gitlawb/openclaude@$FORK_VER 고정 설치 후 NPU 기능 dist 적용"
  NPM_SPEC="@gitlawb/openclaude@$FORK_VER"
else
  echo "      서버 포크 없음 — 업스트림 최신으로 설치(NPU LED/위젯 없이 동작)"
  NPM_SPEC="@gitlawb/openclaude@latest"
fi
npm install -g "$NPM_SPEC" --prefix "$HOME_DIR" >/dev/null 2>&1 || { echo "[fail] openclaude 설치 실패 ($NPM_SPEC — npm 로그 확인)"; exit 1; }
OC_BIN="$HOME_DIR/bin/openclaude"
[ -x "$OC_BIN" ] || { echo "[fail] openclaude 바이너리 없음: $OC_BIN"; exit 1; }

# 포크 dist 덮어쓰기. 실패하면 업스트림 dist 가 그대로 남아 furio 는 계속 동작한다
# (NPU LED·dp/pp 위젯만 빠짐) — 설치를 통째로 실패시키지 않는다.
if [ -n "$LOCAL_DIST" ] || [ -n "$FORK_VER" ]; then
  DIST_DIR="$HOME_DIR/lib/node_modules/@gitlawb/openclaude/dist"
  [ -d "$DIST_DIR" ] || DIST_DIR=$(dirname "$(readlink -f "$OC_BIN" 2>/dev/null || echo "$OC_BIN")")/../dist
  OK=1
  for f in cli.mjs sdk.mjs; do
    if [ -n "$LOCAL_DIST" ]; then
      # sdk.mjs 는 없을 수도 있다(cli.mjs 만 복사한 경우) — cli.mjs 만 필수.
      if [ ! -f "$LOCAL_DIST/$f" ]; then
        [ "$f" = "cli.mjs" ] && { OK=0; break; }
        echo "      [warn] $f 없음 — 건너뜀(cli.mjs 만 적용)"
        continue
      fi
      cp -f "$LOCAL_DIST/$f" "$DIST_DIR/$f.new" || { OK=0; break; }
    else
      if ! curl -fsS --max-time 300 ${AUTH[@]+"${AUTH[@]}"} -o "$DIST_DIR/$f.new" "$SDI_SERVER/router/client/$f" 2>/dev/null; then
        OK=0; break
      fi
    fi
    mv -f "$DIST_DIR/$f.new" "$DIST_DIR/$f" || { OK=0; break; }
  done
  rm -f "$DIST_DIR"/*.new 2>/dev/null
  if [ "$OK" = 1 ]; then
    echo "      [ok] NPU 기능 적용 (모델별 LED·dp/pp 표시)"
  else
    echo "      [warn] 포크 dist 적용 실패 — 업스트림 그대로 사용(NPU LED 없음)"
  fi
fi

echo "[3/4] 서버 도달 확인: $SDI_SERVER"
if command -v curl >/dev/null 2>&1; then
  AUTH=(); [ -n "$SDI_API_KEY" ] && AUTH=(-H "Authorization: Bearer $SDI_API_KEY")
  # macOS 기본 bash 3.2 + set -u 에선 빈 배열 "${AUTH[@]}" 가 unbound 오류 → ${arr[@]+...} 가드로 안전 확장
  if ! curl -fsS --max-time 10 ${AUTH[@]+"${AUTH[@]}"} "$SDI_SERVER/v1/models" >/dev/null 2>&1; then
    if [ "${FURIO_OFFLINE:-0}" = "1" ]; then
      echo "      [offline] 서버 미도달 — 설치는 계속합니다($SDI_SERVER)."
      echo "                나중에 터널을 띄우거나 mock-router.py 를 이 주소로 실행하면 그대로 동작합니다."
    else
      echo "[fail] 서버 도달 실패 $SDI_SERVER/v1/models"
      echo "       └ 원격이면 SSH 터널이 떠 있나요?  bash furio-connect.sh  (그 후 SDI_SERVER=http://127.0.0.1:8400)"
      echo "       └ 서버 인증 ON 이면 SDI_API_KEY=<키> 를 같이 주세요."
      echo "       └ 서버 없이 설치하려면:  FURIO_OFFLINE=1 FURIO_CLIENT_DIST=<dist경로> bash install.sh"
      exit 1
    fi
  else
    echo "      [ok] /v1/models 응답"
  fi
fi

# 키는 0600 파일에만(있을 때). 래퍼는 런타임에 읽어 env 로 — 래퍼 텍스트엔 비밀 없음.
if [ -n "$SDI_API_KEY" ]; then (umask 177; printf '%s' "$SDI_API_KEY" > "$HOME_DIR/key"); else rm -f "$HOME_DIR/key"; fi

# 모델별 '진짜' 컨텍스트 창을 라우터에서 받아 ctx.json 에 저장(단일 출처 = 서버 REGISTRY).
# ⚠️ 이게 없으면 openclaude 는 처음 보는 우리 모델 id 를 전부 128000 토큰으로 가정한다.
#    실제론 40960~262144 로 제각각이라, 작은 모델에선 컨텍스트 초과(400)가 나고 큰 모델은 손해다.
N=$(curl -fsS --max-time 10 ${AUTH[@]+"${AUTH[@]}"} "$SDI_SERVER/router/models" 2>/dev/null | js -e '
const fs=require("fs"); let s=""; process.stdin.on("data",d=>s+=d).on("end",()=>{
  try{ const j=JSON.parse(s), m={};
    for (const x of (j.data||[])) if (x && x.id && x.context) m[x.id]=x.context;
    const n=Object.keys(m).length;
    if (n) fs.writeFileSync(process.argv[1], JSON.stringify(m));
    process.stdout.write(String(n));
  }catch(e){ process.stdout.write("0") }});' "$HOME_DIR/ctx.json" 2>/dev/null || echo 0)
case "${N:-0}" in ''|*[!0-9]*) N=0 ;; esac
if [ "$N" -gt 0 ]; then
  echo "      [ok] 모델별 컨텍스트 ${N}개 기록 (ctx.json — openclaude 가 우리 ctx 를 정확히 알게 됨)"
else
  rm -f "$HOME_DIR/ctx.json"
  echo "      [warn] /router/models 응답 없음 — 모델별 ctx 미설정(openclaude 가 128000 으로 가정할 수 있음)"
fi

# 모델 선택 목록(/model)에 뜨는 설명을 라우터에서 받아 desc.json 에 저장.
# openclaude 는 원래 "Detected from Local OpenAI-compatible" 을 하드코딩하는데,
# 그건 이 모델이 NPU 몇 장을 어떤 병렬 구성으로 쓰는지 전혀 알려주지 않는다.
# 라우터가 주는 "tp8·dp2·pp1 · 2장 · ctx 40k · fxb" 로 바꿔 고를 때 판단이 되게 한다.
# (OpenAI /v1/models 규격엔 description 필드가 없어서 설치 때 받아 두는 것이 확실하다.)
D=$(curl -fsS --max-time 10 ${AUTH[@]+"${AUTH[@]}"} "$SDI_SERVER/router/models" 2>/dev/null | js -e '
const fs=require("fs"); let s=""; process.stdin.on("data",d=>s+=d).on("end",()=>{
  try{ const j=JSON.parse(s), m={};
    for (const x of (j.data||[])) if (x && x.id && x.description) m[x.id]=x.description;
    const n=Object.keys(m).length;
    if (n) fs.writeFileSync(process.argv[1], JSON.stringify(m));
    process.stdout.write(String(n));
  }catch(e){ process.stdout.write("0") }});' "$HOME_DIR/desc.json" 2>/dev/null || echo 0)
case "${D:-0}" in ''|*[!0-9]*) D=0 ;; esac
if [ "$D" -gt 0 ]; then
  echo "      [ok] 모델 설명 ${D}개 기록 (desc.json — /model 목록에 tp·dp·pp·카드 수 표시)"
else
  rm -f "$HOME_DIR/desc.json"
fi

# 모델별 tp 선택지를 라우터에서 받아 meta.json 에 저장 → /model 의 tp 화살표 축.
# 맨이름(접미사 없는 id)의 tp 는 id 에 안 들어 있어(=기본 빌드), 클라이언트가 이 맵으로 기본 tp 와
# 고를 수 있는 tp 목록을 안다. base 별 {tp_default, tps} 한 벌.
M=$(curl -fsS --max-time 10 ${AUTH[@]+"${AUTH[@]}"} "$SDI_SERVER/router/models" 2>/dev/null | js -e '
const fs=require("fs"); let s=""; process.stdin.on("data",d=>s+=d).on("end",()=>{
  try{ const j=JSON.parse(s), m={};
    for (const x of (j.data||[])) if (x && x.base && typeof x.tp_default==="number" && Array.isArray(x.tp_choices))
      m[x.base]={tp_default:x.tp_default, tps:x.tp_choices, ...(typeof x.pp_default==="number"?{pp_default:x.pp_default}:{})};
    const n=Object.keys(m).length;
    if (n) fs.writeFileSync(process.argv[1], JSON.stringify(m));
    process.stdout.write(String(n));
  }catch(e){ process.stdout.write("0") }});' "$HOME_DIR/meta.json" 2>/dev/null || echo 0)
case "${M:-0}" in ''|*[!0-9]*) M=0 ;; esac
if [ "$M" -gt 0 ]; then
  echo "      [ok] 모델 tp 메타 ${M}개 기록 (meta.json — /model 에서 tp 화살표 선택)"
else
  rm -f "$HOME_DIR/meta.json"
fi

# 안전 자동모드(FURIO_AUTO=safe)용 규칙 파일. 사용자가 편집할 수 있게 파일로 두고, 이미 있으면 건드리지 않는다.
#   auto-allow.txt : 물어보지 않고 바로 실행할 것(읽기·조회·테스트 등)
#   auto-deny.txt  : 아예 막을 것(파괴적·되돌릴 수 없는·외부로 나가는 명령)
#   둘 다 아닌 명령은 그대로 사람에게 물어본다 ← 이게 'safe' 의 핵심
if [ ! -f "$HOME_DIR/auto-allow.txt" ]; then
  cat > "$HOME_DIR/auto-allow.txt" <<'ALLOWEOF'
# 자동 승인(묻지 않음) — 읽기/조회/테스트처럼 되돌릴 수 있는 것만.
# 문법: 도구이름 또는 도구이름(명령 접두사:*)   예) Bash(git status:*)
# 한 줄에 하나. '#' 로 시작하면 주석. 편집 후 바로 반영됩니다(재설치 불필요).
Read
Glob
Grep
TodoWrite
Bash(ls:*)
Bash(pwd:*)
Bash(cat:*)
Bash(head:*)
Bash(tail:*)
Bash(wc:*)
Bash(file:*)
Bash(stat:*)
Bash(du:*)
Bash(df:*)
Bash(tree:*)
Bash(date:*)
Bash(echo:*)
Bash(which:*)
Bash(find:*)
Bash(grep:*)
Bash(rg:*)
Bash(diff:*)
Bash(git status:*)
Bash(git diff:*)
Bash(git log:*)
Bash(git show:*)
Bash(git branch:*)
Bash(git remote:*)
Bash(npm test:*)
Bash(npm run:*)
Bash(pytest:*)
Bash(make:*)
Bash(cargo test:*)
Bash(cargo build:*)
Bash(go test:*)
Bash(go build:*)
ALLOWEOF
fi
if [ ! -f "$HOME_DIR/auto-deny.txt" ]; then
  cat > "$HOME_DIR/auto-deny.txt" <<'DENYEOF'
# 차단(묻지도 않고 거부) — 파괴적이거나 되돌릴 수 없거나 밖으로 나가는 것.
# kill/pkill 은 이 서버의 라우터까지 죽일 수 있어 막아 둡니다.
Bash(rm -rf:*)
Bash(rm -fr:*)
Bash(sudo:*)
Bash(su:*)
Bash(dd:*)
Bash(mkfs:*)
Bash(fdisk:*)
Bash(parted:*)
Bash(shutdown:*)
Bash(reboot:*)
Bash(halt:*)
Bash(poweroff:*)
Bash(chown:*)
Bash(chmod 777:*)
Bash(kill:*)
Bash(pkill:*)
Bash(killall:*)
Bash(curl:*)
Bash(wget:*)
Bash(git push:*)
Bash(git reset --hard:*)
Bash(git clean:*)
Bash(crontab:*)
Bash(npm publish:*)
Bash(ssh:*)
Bash(scp:*)
DENYEOF
fi

# Shift+Tab 으로 자동모드(bypassPermissions)에 들어갈 수 있게 한다.
#
# ⚠️ 이 설정은 자동모드를 **켜지 않는다**. 모드 순환 목록에 '나타나게만' 한다.
#    openclaude 는 기본적으로 bypassPermissions 를 목록에서 숨기고, 켜려면 실행할 때마다
#    --dangerously-skip-permissions 를 붙이게 한다. 그러면 '세션 도중 잠깐만 자동으로'가
#    불가능해서, 결국 늘 위험한 플래그로 켜 두는 쪽으로 사람을 밀어붙인다.
#    이 설정을 주면 평소엔 안전한 default 로 쓰다가 필요할 때만 Shift+Tab 으로 올라갔다
#    내려올 수 있다. 스키마 설명도 정확히 그 용도다:
#      "Allow bypass permissions mode to appear in the mode list without requiring the CLI flag"
#
#    Shift+Tab 순환:  default → acceptEdits → plan → bypassPermissions → fullAccess → default
#
#    잠그고 싶으면 FURIO_ALLOW_BYPASS=0 으로 설치하면 된다(그러면 CLI 플래그로만 가능).
CFG_DIR="$HOME_DIR/config"
if [ "${FURIO_ALLOW_BYPASS:-1}" = "0" ]; then
  echo "      [skip] Shift+Tab 자동모드 비활성(FURIO_ALLOW_BYPASS=0)"
else
  mkdir -p "$CFG_DIR"
  # 기존 settings.json 이 있으면 다른 키는 보존하고 이 항목만 병합한다.
  js -e '
const fs=require("fs"), p=process.argv[1];
let j={};
try{ j=JSON.parse(fs.readFileSync(p,"utf8"))||{} }catch(e){ j={} }
if (typeof j!=="object"||Array.isArray(j)) j={};
j.permissions = (j.permissions && typeof j.permissions==="object" && !Array.isArray(j.permissions)) ? j.permissions : {};
j.permissions.allowBypassPermissionsMode = true;
// bun -e 는 최상위에서 난 동기 fs 오류를 삼키고 0 으로 끝난다(node 는 1). 실패를 종료 코드로 알린다.
try{ fs.writeFileSync(p, JSON.stringify(j,null,2)+"\n") }catch(e){ process.exit(1) }
' "$CFG_DIR/settings.json" 2>/dev/null \
    && { echo "      [ok] 자동모드 사용 가능 — 실행 후 Shift+Tab 을 눌러 모드를 바꾼다:"
         echo "           default(매번 확인) → Accept edits → Plan → Bypass Permissions → Full Access"
         echo "           처음 Bypass 로 올라갈 때만 확인창이 한 번 뜨고, 이후엔 저장돼 안 뜬다."; } \
    || echo "      [warn] settings.json 기록 실패 — 자동모드는 FURIO_AUTO=1 로만 가능"
fi

# ── NPU 에이전트 라우팅 ──────────────────────────────────────────────────
# openclaude 의 서브에이전트(/agents, Task 도구)는 기본적으로 부모 모델을 상속하지만,
# frontmatter model 이 'opus' 로 지정된 에이전트는 OPENAI_MODEL 로 매핑돼 우리 라우터에
# 없는 모델을 부를 수 있다. 그래서 서브에이전트가 항상 우리 NPU 모델을 쓰도록 라우트를
# settings.json 에 심는다. agentModels 는 '메뉴', agentRouting 은 '에이전트→모델' 배정이다.
#   · 이미 agentModels/agentRouting 이 있으면 건드리지 않는다(사용자 배정 보존).
#   · 역할 베이스: orchestrator/code-writer/git-ops 에이전트 템플릿(agents/*.md)과
#     그 이름→모델 라우팅을 깔아 둔다. 전부 1장 모델이라 동시 상주 안전.
#   · 나중에 역할·모델을 바꾸려면 agents/*.md 와 settings.json 의 agentModels/agentRouting
#     을 편집한다(이름이 default 보다 우선). 안내: agents/README.md.
# 끄려면 FURIO_AGENT_ROUTING=0 (라우팅만), 서브에이전트 기능 자체를 끄려면 FURIO_AGENTS=0.
#
# ⚠️ 위임은 NPU 에서 비싸다. 메인 모델이 tp32(카드 4장 전부)면 서브에이전트 호출 한 번에
# 메인 모델이 통째로 내려갔다 다시 올라온다. 게다가 코드 작성이 8B 로 넘어가면 결과 품질이
# 메인 모델보다 낮다. 그래서 '끄기'를 1급 스위치로 둔다.
# 실측(2026-08-13): agents/*.md 를 지워도 Agent 도구는 그대로 광고된다(도구 12개 중 하나).
# 도구 목록에서 빼려면 permissions.deny 에 Agent/Task 를 넣어야 한다(→ 11개).
AGENT_KEY="${SDI_API_KEY:-dummy}"
if [ "${FURIO_AGENTS:-1}" = "0" ]; then
  mkdir -p "$CFG_DIR"
  js -e '
const fs=require("fs"), p=process.argv[1];
let j={}; try{ j=JSON.parse(fs.readFileSync(p,"utf8"))||{} }catch(e){ j={} }
if (typeof j!=="object"||Array.isArray(j)) j={};
j.permissions = (j.permissions && typeof j.permissions==="object" && !Array.isArray(j.permissions)) ? j.permissions : {};
const deny = Array.isArray(j.permissions.deny) ? j.permissions.deny : [];
for (const t of ["Agent","Task"]) if (!deny.includes(t)) deny.push(t);
j.permissions.deny = deny;
// 라우팅은 남겨두면 혼란만 준다 — 위임 자체를 막았으므로 함께 지운다.
delete j.agentRouting; delete j.agentModels;
// bun -e 는 최상위에서 난 동기 fs 오류를 삼키고 0 으로 끝난다(node 는 1). 실패를 종료 코드로 알린다.
try{ fs.writeFileSync(p, JSON.stringify(j,null,2)+"\n") }catch(e){ process.exit(1) }
' "$CFG_DIR/settings.json" 2>/dev/null \
    && echo "      [ok] 서브에이전트 비활성(FURIO_AGENTS=0) — Agent/Task 도구를 모델에게 안 보여준다." \
    || echo "      [warn] settings.json 기록 실패 — 수동으로 permissions.deny 에 Agent/Task 추가"
elif [ "${FURIO_AGENT_ROUTING:-1}" = "0" ]; then
  echo "      [skip] NPU 에이전트 라우팅 비활성(FURIO_AGENT_ROUTING=0)"
else
  mkdir -p "$CFG_DIR"
  _R=$(js -e '
const fs=require("fs"), p=process.argv[1], base=process.argv[2], key=process.argv[3];
let j={};
try{ j=JSON.parse(fs.readFileSync(p,"utf8"))||{} }catch(e){ j={} }
if (typeof j!=="object"||Array.isArray(j)) j={};
const m=(model)=>({base_url:base, api_key:key, model});
let added=false;
if (!j.agentModels || typeof j.agentModels!=="object" || Array.isArray(j.agentModels)) {
  j.agentModels = {
    "npu-small": m("Qwen3-4B-FP8"),                          // 1장 · 빠름 · 도구/추론 O
    "npu-8b":    m("Qwen3-8B-FP8"),                          // 1장
    "npu-coder": m("Qwen3-Coder-30B-A3B-Instruct-FP8"),     // 4장 독점 · 코드 특화
    "npu-70b":   m("Llama-3.3-70B-Instruct")                // 4장 독점 · 범용 고품질
  };
  added=true;
}
if (!j.agentRouting || typeof j.agentRouting!=="object" || Array.isArray(j.agentRouting)) {
  // 역할 골격(베이스). 전부 1장 모델이라 동시 상주 안전. 나중에 원하는 모델로 교체.
  //   code-writer 를 진짜 코더(Qwen3-Coder-30B, tp8+pp2 재빌드=2장)로 올리려면
  //   npu-coder 의 model 을 그 변형 id 로 바꾸고 code-writer 를 "npu-coder" 로 라우팅.
  j.agentRouting = {
    "orchestrator": "npu-8b",     // 총괄(위임) — 서브에이전트로 쓸 때. 메인 세션 모델은 /model
    "code-writer":  "npu-8b",     // 코드 생성 — 재빌드 후 "npu-coder" 로 교체 권장
    "git-ops":      "npu-small",  // git/GitHub
    "default":      "npu-small"
  };
  added=true;
}
// bun -e 는 최상위에서 난 동기 fs 오류를 삼키고 0 으로 끝난다(node 는 1). 실패를 종료 코드로 알린다.
try{ fs.writeFileSync(p, JSON.stringify(j,null,2)+"\n") }catch(e){ process.exit(1) }
process.stdout.write(added?"added":"kept");
' "$CFG_DIR/settings.json" "$SDI_SERVER/v1" "$AGENT_KEY" 2>/dev/null || echo fail)
  case "$_R" in
    added) echo "      [ok] NPU 에이전트 라우팅 기록 — 역할별 서브에이전트가 NPU 모델을 쓴다."
           echo "           역할↔모델 변경: $CFG_DIR/settings.json 의 agentRouting/agentModels" ;;
    kept)  echo "      [ok] NPU 에이전트 라우팅 — 기존 설정 보존(agentModels/agentRouting 유지)" ;;
    *)     echo "      [warn] settings.json 기록 실패 — NPU 에이전트 라우팅을 못 넣었습니다" ;;
  esac

  # ── 역할 에이전트 템플릿 scaffold (있으면 보존) ──────────────────────
  # 사용자가 나중에 채우도록 baseline .md 를 깐다. name=agentRouting 키와 일치.
  ADIR="$CFG_DIR/agents"; mkdir -p "$ADIR"
  scaffold_agent() {  # $1=파일 $2=heredoc(stdin)
    if [ -f "$ADIR/$1" ]; then echo "      [keep] agents/$1 (기존 유지)"; return; fi
    cat > "$ADIR/$1"; echo "      [new]  agents/$1";
  }
  scaffold_agent orchestrator.md <<'AGENTEOF'
---
name: orchestrator
description: 사용자 요청을 받아 전체 작업을 총괄하고, 코드 생성은 code-writer, git/GitHub 작업은 git-ops 서브에이전트에게 위임한다. 여러 단계·역할 분담이 필요한 작업 전반에 사용.
---
당신은 총괄(orchestrator)입니다. 사용자 요청을 분석해 계획을 세우고 적절한 역할에게 위임하세요.
- 코드 작성/수정 → Agent 도구로 subagent_type "code-writer" 호출
- git add/commit/push/pull·PR → Agent 도구로 subagent_type "git-ops" 호출
- 간단한 조회·정리는 직접 처리
각 역할 결과를 종합해 다음 단계를 결정하고, 완료되면 사용자에게 한국어로 요약 보고하세요.
(이 프롬프트·역할은 자유롭게 편집하세요.)
AGENTEOF
  scaffold_agent code-writer.md <<'AGENTEOF'
---
name: code-writer
description: 요청받은 사양대로 코드를 생성·수정한다. 코드 작성이 필요할 때 사용.
tools: Read, Write, Edit, Grep, Glob, Bash
---
당신은 코드 생성 전담입니다. 요청 사양을 받아 코드를 작성/수정하고,
변경한 파일 경로와 핵심 변경 요약만 반환하세요. git 커밋·푸시는 하지 마세요(git-ops 역할).
(이 프롬프트·도구 목록은 자유롭게 편집하세요.)
AGENTEOF
  scaffold_agent git-ops.md <<'AGENTEOF'
---
name: git-ops
description: git add/commit/push/pull 과 GitHub 작업(PR 등)을 수행한다. 변경사항을 원격에 반영하거나 원격 변화를 가져올 때 사용.
---
당신은 git/GitHub 담당입니다. 요청에 따라 git add/commit/push/pull 을 수행하세요.
GitHub API 작업(PR 생성·조회 등)은 등록된 MCP 도구(mcp__github__*)를 사용하세요(/mcp 로 등록).
커밋 메시지는 변경 요약을 한국어로 간결히. 코드 내용 자체는 수정하지 마세요(그건 code-writer).
(도구를 좁히려면 frontmatter 에 tools: 를 추가하세요 — 단 MCP 를 쓰려면 mcp 툴명도 포함.)
AGENTEOF
  scaffold_agent README.md <<AGENTEOF
# furio 역할 에이전트 베이스

이 폴더(\`$ADIR\`)의 \`*.md\` 하나가 역할 에이전트 하나입니다.
frontmatter \`name\` 이 \`settings.json\` 의 \`agentRouting\` 키와 연결됩니다.

## 채우는 곳 3군데
1. **역할 정의**: 이 폴더의 \`*.md\` — frontmatter(name·description·tools) + 본문(시스템 프롬프트) 편집.
   역할 추가는 새 \`.md\` 를 만들면 됩니다(name 을 agentRouting 에도 추가).
2. **역할↔모델**: \`../settings.json\`
   - \`agentModels\`: 모델 메뉴(라우트키 → base_url/api_key/model).
   - \`agentRouting\`: 에이전트 name → 라우트키. 이름이 default 보다 우선.
3. **모델 자체(새로 빌드/등록)**: 서버의 \`furiosa_router.py\` REGISTRY.
   - 코더를 2장으로: Qwen3-Coder-30B 를 **v2 아티팩트**로 tp8 재빌드 → REGISTRY 에
     \`tp=8, cards=1\` 로 등록 → \`agentModels.npu-coder.model\` 을 \`...@pp2\`(2장) 로 → serve-router.sh 재시작.
   - ⚠️ pp 는 v2 아티팩트에서만(fxb 면 패닉).

## 현재 베이스 배정 (전부 1장, 동시 안전)
- orchestrator → npu-8b   (총괄; 서브에이전트로 쓸 때. 메인 세션 모델은 /model 로 별도 지정)
- code-writer  → npu-8b   (재빌드 후 npu-coder 로 교체 권장)
- git-ops      → npu-small
- default      → npu-small

## 자동화 켜기(선택)
- 팀 협업:  \`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1 furio\`
- 코디네이터: \`CLAUDE_CODE_COORDINATOR_MODE=1 furio\`
- 예약/폴링:  furio 안에서 CronCreate 도구
- 조건 반복:  \`/goal <조건>\`
- 외부 동작:  \`/mcp\` 로 github 등 MCP 서버 등록 → git-ops 가 mcp__github__* 사용
AGENTEOF
  echo "      [ok] 역할 에이전트 베이스: $ADIR/*.md (편집해서 사용)"
fi

echo "[4/4] '$CMD' 명령 설치: $BIN_DIR/$CMD"
cat > "$BIN_DIR/$CMD" <<EOF
#!/usr/bin/env bash
# $BRAND — openclaude(Claude Code 계열) + 서버 NPU(OpenAI 호환 라우터). 키는 래퍼에 없음(0600 파일).
set -euo pipefail
export CLAUDE_CODE_USE_OPENAI=1
export OPENAI_BASE_URL="$SDI_SERVER/v1"
export OPENCLAUDE_CONFIG_DIR="\${OPENCLAUDE_CONFIG_DIR:-$HOME_DIR/config}"   # 유저 기존 openclaude 와 격리
# 기본 모델 = 직전에 쓰던 모델. openclaude 는 /model 로 바꿀 때마다 그 값을 config 의
# settings.json 에 저장한다(동기). 그걸 읽어 OPENAI_MODEL 기본값으로 쓰면, furio 를 다시
# 켜도 마지막 모델이 그대로 뜬다. 우선순위: 명시적 OPENAI_MODEL > 직전 모델 > 설치 기본값($MODEL).
# (furio --model 은 이와 무관하게 최우선으로 그 실행만 덮어쓴다.)
# bun 은 PATH 에서 먼저 찾고, 없으면 설치 때 찾은 절대경로를 쓴다. ssh 원격 명령·cron 처럼 비대화형으로
# 불리면 ~/.bun/bin 이 PATH 에 없어서다(RNGD 서버의 .bashrc 는 비대화형이면 그 설정 전에 끝난다).
_BUN="\$(command -v bun 2>/dev/null || true)"; [ -n "\$_BUN" ] || _BUN="$BUN_BIN"
# 짧은 JS 실행기(설정 읽기용). 이 정도는 node 18 로도 충분하고, node 가 아예 없으면 bun 으로 읽는다.
# bun 이면 작업 폴더의 .env·bunfig.toml 을 읽지 않게 막는다(설치기 [1/4] 주석 참고).
_js() {
  if command -v node >/dev/null 2>&1; then node "\$@"
  elif [ -n "\$_BUN" ]; then "\$_BUN" --no-env-file --config=/dev/null "\$@"
  else return 1; fi
}
_LAST_MODEL="\$(_js -e 'try{var fs=require("fs");var s=JSON.parse(fs.readFileSync(process.env.OPENCLAUDE_CONFIG_DIR+"/settings.json","utf8"));if(s&&typeof s.model==="string"&&s.model)process.stdout.write(s.model)}catch(e){}' 2>/dev/null || true)"
export OPENAI_MODEL="\${OPENAI_MODEL:-\${_LAST_MODEL:-$MODEL}}"
export CLAUDE_CODE_MAX_OUTPUT_TOKENS="\${CLAUDE_CODE_MAX_OUTPUT_TOKENS:-$MAXOUT}"
export OPENAI_API_KEY="\$( [ -f "$HOME_DIR/key" ] && cat "$HOME_DIR/key" || echo dummy )"
# 모델별 실제 컨텍스트 창(설치 때 라우터에서 받아둔 값). 없으면 openclaude 는 128000 으로 잘못 가정한다.
if [ -z "\${CLAUDE_CODE_OPENAI_CONTEXT_WINDOWS:-}" ] && [ -s "$HOME_DIR/ctx.json" ]; then
  export CLAUDE_CODE_OPENAI_CONTEXT_WINDOWS="\$(cat "$HOME_DIR/ctx.json")"
fi
# 모델 선택 목록의 설명(설치 때 라우터에서 받아둔 값). 없으면 openclaude 기본 문구로 표시된다.
if [ -z "\${FURIO_MODEL_DESCRIPTIONS:-}" ] && [ -s "$HOME_DIR/desc.json" ]; then
  export FURIO_MODEL_DESCRIPTIONS="\$(cat "$HOME_DIR/desc.json")"
fi
# 모델별 tp 선택지(base→{tp_default,tps}). 없으면 /model 에 tp 화살표 축이 안 뜬다(dp/pp 만).
if [ -z "\${FURIO_NPU_META:-}" ] && [ -s "$HOME_DIR/meta.json" ]; then
  export FURIO_NPU_META="\$(cat "$HOME_DIR/meta.json")"
fi
# 타임아웃: NPU 는 모델을 늦게 올려서(라우터가 최대 480초 대기) openclaude 0.25.0 기본값이 빠듯하다.
export API_TIMEOUT_MS="\${API_TIMEOUT_MS:-900000}"                                # 응답헤더 마감(0.25.0 기본 600000)
export CLAUDE_STREAM_IDLE_TIMEOUT_MS="\${CLAUDE_STREAM_IDLE_TIMEOUT_MS:-600000}"  # SSE 유휴(0.25.0서 120s→90s 축소됨)
# 완전 자동 실행 모드 토글(FURIO_AUTO, 설치시 기본 '$AUTODEF'). 런타임 override 가능. ⚠️신뢰 폴더에서만.
FURIO_AUTO="\${FURIO_AUTO:-$AUTODEF}"
AUTO_ARGS=()
case "\$FURIO_AUTO" in
  1|yes|on|bypass|full) AUTO_ARGS=(--dangerously-skip-permissions) ;;  # 모든 권한 프롬프트 생략(완전자동)
  edits|accept)         AUTO_ARGS=(--permission-mode acceptEdits) ;;   # 파일편집만 자동(Bash 등은 확인)
  safe|rules)
    # 규칙 기반 '안전 자동모드': 안전한 건 자동 승인, 위험한 건 차단, 나머지는 사람에게 질문.
    # openclaude 네이티브 'auto' 모드는 쓰지 않는다 — 그건 Anthropic 서버측 분류기
    # (feature('TRANSCRIPT_CLASSIFIER'))로 매 행동의 안전성을 판정하는 방식이라, NPU 라우터를
    # 백엔드로 쓰는 우리 환경에선 분류기가 아예 없다. 게이트만 풀면 있지도 않은 안전판정을
    # 있다고 속이는 셈이 된다. 그래서 정식 기능인 --allowed-tools/--disallowed-tools 로
    # 동등한 동작을 만든다(규칙은 auto-allow.txt/auto-deny.txt — 사용자가 직접 편집 가능).
    # 완전 자동이 필요하면 실행 중 Shift+Tab 으로 bypassPermissions 에 올라가면 된다.
    AUTO_ARGS=(--permission-mode acceptEdits)
    _ALLOW=(); _DENY=()
    if [ -f "$HOME_DIR/auto-allow.txt" ]; then
      while IFS= read -r _r || [ -n "\$_r" ]; do
        case "\$_r" in ''|'#'*) ;; *) _ALLOW+=("\$_r") ;; esac
      done < "$HOME_DIR/auto-allow.txt"
    fi
    if [ -f "$HOME_DIR/auto-deny.txt" ]; then
      while IFS= read -r _r || [ -n "\$_r" ]; do
        case "\$_r" in ''|'#'*) ;; *) _DENY+=("\$_r") ;; esac
      done < "$HOME_DIR/auto-deny.txt"
    fi
    [ \${#_ALLOW[@]} -gt 0 ] && AUTO_ARGS+=(--allowed-tools "\${_ALLOW[@]}")
    [ \${#_DENY[@]}  -gt 0 ] && AUTO_ARGS+=(--disallowed-tools "\${_DENY[@]}")
    ;;
esac
# 도구 축소(선택): FURIO_TOOLS="Bash,Edit,Read,Write,Glob,Grep" 처럼 주면 도구정의 토큰(~8.8k)이 줄어
# 작은 ctx 모델에서 여유가 생긴다. 미지정이면 전체 도구(기본).
TOOL_ARGS=()
[ -n "\${FURIO_TOOLS:-}" ] && TOOL_ARGS=(--tools "\$FURIO_TOOLS")
# 사용자 메시지 1개가 쓸 수 있는 도구 왕복 수(기본 50). 파일을 못 찾아 검색이 길어지는 작업에서
# 50 에 걸리면 '최대 턴 도달' 경고와 함께 그 자리에 멈춘다 — 세션은 살아 있어서 '계속' 을 치면
# 이어서 50턴을 더 쓴다. 올리는 건 능력이 아니라 '무인 진행 시간'을 늘리는 것이므로, 사람이
# 지켜보지 않는 실행에서는 낮게 두는 편이 안전하다(NPU 카드를 그만큼 오래 점유한다).
TURN_ARGS=()
[ -n "\${FURIO_MAX_TURNS:-}" ] && TURN_ARGS=(--max-turns "\$FURIO_MAX_TURNS")
# 실행 런타임은 여기서 고른다(설치 때가 아니라). node 를 나중에 올리거나 내려도 그대로 동작한다.
# openclaude 는 Node ≥22 를 요구하는데 배포판 node 가 18 인 서버가 있어, 그럴 때 bun 으로 넘어간다.
_NODEMAJ="\$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null | tail -n1 || true)"
case "\$_NODEMAJ" in ''|*[!0-9]*) _NODEMAJ=0 ;; esac
_USE_BUN=0
if [ "\$_NODEMAJ" -ge 22 ]; then
  :                               # node 가 충분하다 — 셔뱅대로 실행
elif [ -n "\$_BUN" ] && [ -x "\$_BUN" ]; then
  _USE_BUN=1
else
  echo "furio: Node ≥22 도 bun 도 없습니다 (현재 node: \$(node -v 2>/dev/null || echo 없음))" >&2
  echo "       'nvm install 22' 또는 'curl -fsSL https://bun.sh/install | bash'" >&2
  exit 1
fi
# 실행할 명령을 배열에 담지 않고 플래그로 가른다. macOS 기본 bash 3.2 는 set -u 에서 빈 배열을
# "\${arr[@]}" 로 펼치면 unbound 오류다(\${#arr[@]} 는 괜찮다 — bash 3.2.0 으로 확인). 인자 배열은
# 기존과 같은 \${arr[@]+...} 가드로 펼친다.
if [ "\$_USE_BUN" = 1 ]; then
  # bun 경로는 bin/openclaude 런처를 건너뛰고 dist/cli.mjs 를 바로 돌린다. 런처가 하던 일 중
  # 여기서도 필요한 것만 옮긴다(힙 크기·--expose-gc 는 node 전용 플래그라 bun 과 무관).
  #  · --max-memory 는 런처 전용 인자다. CLI 로 넘기면 모르는 옵션으로 거부되므로 걸러 내고 값만 넘긴다.
  #  · 팀원 창(tmux/iTerm2)은 argv[1] 로 자신을 다시 띄우는데 여기선 그게 실행 권한 없는 cli.mjs 다.
  #    이 래퍼를 가리키게 해 창 안에서도 런타임을 다시 고르게 한다.
  #  · 작업 폴더의 .env·bunfig.toml(preload) 자동 읽기를 끈다 — node 로 돌 때와 같게.
  export CLAUDE_CODE_TEAMMATE_COMMAND="\${CLAUDE_CODE_TEAMMATE_COMMAND:-$BIN_DIR/$CMD}"
  _ARGS=()
  for _a in "\$@"; do
    case "\$_a" in
      --max-memory=*) export OPENCLAUDE_MAX_MEMORY_MB="\${_a#--max-memory=}" ;;
      --max-memory) ;;
      *) _ARGS+=("\$_a") ;;
    esac
  done
  exec "\$_BUN" --no-env-file --config=/dev/null "$HOME_DIR/lib/node_modules/@gitlawb/openclaude/dist/cli.mjs" \${AUTO_ARGS[@]+"\${AUTO_ARGS[@]}"} \${TOOL_ARGS[@]+"\${TOOL_ARGS[@]}"} \${TURN_ARGS[@]+"\${TURN_ARGS[@]}"} \${_ARGS[@]+"\${_ARGS[@]}"}
fi
exec "$OC_BIN" \${AUTO_ARGS[@]+"\${AUTO_ARGS[@]}"} \${TOOL_ARGS[@]+"\${TOOL_ARGS[@]}"} \${TURN_ARGS[@]+"\${TURN_ARGS[@]}"} "\$@"
EOF
chmod 755 "$BIN_DIR/$CMD"

echo
echo "✅ 설치 완료. (openclaude: $HOME_DIR, 명령: $BIN_DIR/$CMD)"
case ":$ORIG_PATH:" in
  *":$BIN_DIR:"*) : ;;
  *) echo "   ⚠️  로그인 셸 PATH 에 $BIN_DIR 가 없습니다. 추가하세요:"
     echo "       echo 'export PATH=\"$BIN_DIR:\$PATH\"' >> ~/.zshrc   # (또는 ~/.bashrc) 후 새 터미널" ;;
esac
echo "   실행:  $CMD              # Claude 같은 코딩 에이전트 TUI"
echo "          $CMD -p \"...\"     # 비대화형 한 줄(print)"
echo "          $CMD --model gpt-oss-120b   # 또는 OPENAI_MODEL 로 모델 변경"
echo "   ⚠️ 작업은 일반 프로젝트 폴더에서(.claude 등 민감 경로엔 쓰기 차단). 터널 방식이면 furio 쓰는 동안 터널 유지."
echo "   제거:  rm -rf \"$HOME_DIR\" \"$BIN_DIR/$CMD\""
