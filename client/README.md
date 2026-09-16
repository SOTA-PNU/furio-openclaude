# furio — Claude Code 같은 코딩 에이전트(openclaude) + 서버 NPU

> 이 문서의 명령은 저장소의 `client/` 폴더 안에서 실행한다고 가정한다. 저장소 루트에서 실행할 때는 `client/install.sh` 처럼 앞에 폴더 이름을 붙인다. 전체 개요는 [루트 README](../README.md).

[openclaude](https://github.com/Gitlawb/openclaude)

```
[내 Mac/Win] furio (openclaude, Node≥22) ──► localhost:8400 ──[SSH 터널 :10022]──► [서버] furiosa_router → furiosa-llm serve(NPU)
```

---

## 1. NPU 서버 — 라우터 기동

```bash
cd ~/RNGD-proj/Model_Benchmark/rngd-npu/coding-agent
bash serve-router.sh start                               # 이미 sdi 용으로 떠 있으면 그대로 사용
#curl -s localhost:8400/v1/models | python3 -m json.tool  # 확인
```

## 2. 사용자 — 각자 PC

> **사전 요구: Node ≥ 22 또는 [bun](https://bun.sh)** (openclaude 요구). 없으면 `nvm install 22` 또는 `brew install node`(Mac) / nodejs.org(Win).
> node 를 못 올리는 곳(예: node 18 이 묶인 서버)은 `curl -fsSL https://bun.sh/install | bash` 로 bun 을 깔면 설치기와 `furio` 가 bun 으로 돕니다. 패키지를 받는 데 npm 은 필요합니다.

### 2-A. 원격(집/외부) — SSH 터널 (권장)
**터미널 ①** — 터널(유지):
```bash
SDI_SSH_USER=jun bash furio-connect.sh
#   (= ssh -p 10022 -N -L 8400:localhost:8400 jun@164.125.19.138)  ← 비밀번호
```
>  다른 서버 계정이면 `jun`(또는 `SDI_SSH_USER`)만 바꾸면 됩니다(install/furio/설정 그대로).


**터미널 ②** — 설치(최초 1회) + 사용:
```bash
SDI_SERVER=http://127.0.0.1:8400 bash install.sh   # 인증 ON 이면 SDI_API_KEY=<키> 추가
furio                                               # Claude 같은 TUI
```

### 2-B. 사내 같은 LAN — 직접 (터널 불필요)
```bash
SDI_SERVER=http://10.125.19.138:8400 bash install.sh
furio
```

**Windows (PowerShell)** — 터미널① 터널 후:
```powershell
$env:SDI_SERVER="http://127.0.0.1:8400"
powershell -ExecutionPolicy Bypass -File install.ps1
furio
```

### 2-C. 서버에 못 붙는 PC — 설치하는 두 가지 방법

NPU 기능이 든 dist 는 우리 포크라 npm 에 없다. 그래서 **dist 를 어디서 가져오느냐**가 갈린다.
어느 쪽이든 미리 `cli.mjs`(+`sdk.mjs`)를 그 PC 로 옮겨 둬야 한다
(서버 접속 되는 다른 PC 에서 `curl -O $SDI_SERVER/router/client/cli.mjs`, 또는 USB/scp, 또는 아래 2-D 처럼 직접 빌드).

**방법 ①(가장 간단) — 라우터 없이 그냥 설치**

```bash
FURIO_OFFLINE=1 \
FURIO_CLIENT_DIST=~/furio-dist \        # cli.mjs 가 있는 폴더
SDI_SERVER=http://127.0.0.1:8400 \      # 나중에 터널/목을 붙일 주소(생략하면 이 값이 기본)
bash install.sh
```
- 서버 도달 실패해도 설치가 **안 죽는다**(경고만).
- dist 는 로컬 폴더에서 가져오므로 **NPU 기능이 그대로 들어간다**.
- 모델별 ctx/설명(`ctx.json`·`desc.json`)만 못 받아온다 → 나중에 서버·목에 붙은 뒤 재설치하면 채워진다.
- 이후 그 주소에 SSH 터널을 띄우거나(2-A) 목 라우터를 띄우면 그대로 동작한다.

**방법 ② — 목 라우터를 먼저 띄우고 설치**(ctx/설명까지 완전)

```bash
python3 mock-router.py --client-dist ~/furio-dist    # :8400 (dist 도 같이 배포)
SDI_SERVER=http://127.0.0.1:8400 bash install.sh     # FURIO_OFFLINE 불필요
```
목이 실제 라우터와 같은 엔드포인트를 내주므로 dist·ctx·설명·LED 까지 전부 채워진다.

> 실측: 죽은 주소(`:19999`)로 방법 ① 설치 → dist sha 가 서버 빌드본과 일치, 이후 그 주소에 목을
> 띄우니 `▶ ● Qwen3-4B-FP8 npu0 · ● Qwen3-8B-FP8 npu1` 정상 표시.

### 2-D. 목(mock) 라우터로 기능만 테스트

NPU 추론 없이 **클라이언트에 넣은 기능만** 확인할 때 쓴다(모델별 LED·dp/pp 위젯·모델
설명·Shift+Tab 자동모드). `mock-router.py` 가 실제 라우터와 같은 엔드포인트를 같은 모양으로
내주고, 모델 상태도 시간 기반으로 진짜처럼 움직인다(loading → up, 카드 모자라면 LRU 로 stopping).

**터미널 ①** — 목 라우터(파이썬 표준 라이브러리만, 설치할 것 없음):
```bash
python3 mock-router.py                       # :8400, 로딩 8초로 흉내
python3 mock-router.py --load-seconds 15     # 노랑 LED 를 더 오래 보고 싶으면
```

**터미널 ②** — 설치 후 실행:
```bash
SDI_SERVER=http://127.0.0.1:8400 bash install.sh
furio
```

이대로면 업스트림 openclaude 가 깔려 NPU 기능이 **안 보인다**. 기능을 보려면 포크를 직접
빌드해서 목에 물려야 한다(Node ≥22 + [bun](https://bun.sh) 필요, 인터넷은 npm 만):

```bash
git clone https://github.com/Gitlawb/openclaude.git && cd openclaude
git am /경로/claude-agent/openclaude-npu.patch     # 이 저장소의 패치
bun install && bun run build                      # → dist/cli.mjs

# 목을 이 dist 로 다시 띄우면 install.sh 가 알아서 받아 간다
python3 mock-router.py --client-dist /경로/openclaude/dist
```

확인 포인트:
- 화면 맨 아래 `? for shortcuts` 위에 `● 모델명 npu0,1` — 초록(올라감)/노랑 깜빡임(전환중)/빨강(미로드)
- `/model` 에서 모델마다 `tp8·dp2·pp1 · 2장 · ctx 40k · fxb` 설명
- `/model` 에서 소형 모델을 고르면 `NPU  dp [1] 2 4   pp [1] 2 4` 행 + ←/→ 로 변경, **tab** 으로 축(tp/dp/pp) 전환
- tp8 빌드가 따로 있는 30B/32B 모델(Qwen3-32B-FP8·Qwen3-Coder-30B·EXAONE-4.0-32B 등)은 `tp [8·32]` 행이 뜬다 —
  기본 tp32(4장 독점) ↔ **tp8(1장, pp 로 층분할)** 을 tab→tp→←/→ 로 고른다(`@tp8@pp2` 등으로 조립·서빙)
- `/model` 에서 **고르는 즉시** 로딩 시작 — 메시지를 보내지 않아도 LED 가 노랑으로 바뀐다
- `/model` 목록에서 카드에 올라간 모델은 **전부** 초록(전환중이면 노랑), 아직 안 올린 모델만 기본색
- 여러 모델을 번갈아 고르면 4장에 나눠 올라가는 모습이 상태줄에 함께 표시
- 빈 입력창에서 **↓** → 상태줄로 포커스(`[▶ ● 모델명]`), **←/→** 로 올라간 모델 사이 이동,
  **↵** 로 전환, **esc** 로 취소 — `/model` 을 열지 않고 바꿀 수 있다

채팅 응답은 고정 문구다(추론 안 함).


## 3. 사용

```bash
furio                       # Claude 같은 코딩 에이전트 TUI (추론=서버 NPU, 코딩=로컬)
furio -p "버그 고쳐줘"        # 비대화형 한 줄(print 모드)
furio --model gpt-oss-120b  # 모델 변경(또는 OPENAI_MODEL 환경변수) — 목록: curl -s localhost:8400/v1/models
furio --continue            # 직전 대화 이어가기
```
TUI 안: `/` 슬래시 명령, 권한 프롬프트(Claude 처럼 도구 실행 전 확인), `@파일` 등. ⚠️ **작업은 일반 프로젝트 폴더에서** — `.claude` 등 민감 경로엔 쓰기가 차단됩니다. 터널 방식이면 furio 쓰는 동안 터널 유지.

### 완전 자동 실행 모드 (권한 확인 없이 끝까지)
기본은 Claude 처럼 도구 실행 전 **확인**합니다. 확인 없이 알아서 진행시키려면 두 가지 방법:

**① 한 번만(그 실행에만)** — 플래그로:
```bash
furio --dangerously-skip-permissions          # TUI, 모든 권한 프롬프트 생략(완전자동)
furio -p "리팩터링하고 테스트 돌려줘" --dangerously-skip-permissions   # 비대화형 1회 완전자동
furio --permission-mode acceptEdits           # 파일 편집만 자동(Bash 등 위험작업은 확인)
```

**② 늘 자동으로(영구)** — `FURIO_AUTO` 환경변수(설치기가 래퍼에 심어둠):
```bash
FURIO_AUTO=1 furio                # 이번 셸에서만 완전자동
echo 'export FURIO_AUTO=1' >> ~/.zshrc   # 항상 완전자동(새 터미널부터)
# 또는 설치 때 기본값으로 굽기:  FURIO_AUTO=1 SDI_SERVER=... bash install.sh
```
**`FURIO_AUTO` 4단계**

| 값 | 동작 | 실제로 넘기는 것 |
|---|---|---|
| (빈값) | 모든 도구 실행 전 확인 — **기본** | 없음 |
| `edits` / `accept` | 파일 편집만 자동, 셸 명령은 확인 | `--permission-mode acceptEdits` |
| **`safe` / `rules`** | **안전한 건 자동, 위험한 건 차단, 나머지는 질문** ⭐ | `--permission-mode acceptEdits` + `--allowed-tools`/`--disallowed-tools` |
| `1`/`yes`/`on`/`full`/`bypass` | 전부 자동(아무것도 안 물음) | `--dangerously-skip-permissions` |

`safe` 는 "전부 묻기"와 "전부 허용" 사이의 실용적인 중간입니다. 규칙에 없는 명령은 **여전히 사람에게 물어보므로**, 완전자동보다 안전하면서도 읽기·검색·테스트 같은 반복 작업은 막히지 않습니다.
```bash
FURIO_AUTO=safe furio
```

**규칙은 파일로 열려 있고 직접 고칠 수 있습니다** (`~/.furio/` 안):

| 파일 | 역할 |
|---|---|
| `~/.furio/auto-allow.txt` | 묻지 않고 바로 실행 (기본 36개: `Read`·`Grep`·`Bash(ls:*)`·`Bash(git status:*)`·`Bash(npm test:*)` …) |
| `~/.furio/auto-deny.txt` | 아예 차단 (기본 26개: `Bash(rm -rf:*)`·`Bash(sudo:*)`·`Bash(git push:*)`·`Bash(kill:*)`·`Bash(curl:*)` …) |

한 줄에 규칙 하나, `#` 은 주석입니다. 문법은 `도구이름` 또는 `도구이름(명령접두사:*)` 이고, 예를 들어 `Bash(git:*)` 는 `git status`·`git commit` 을 모두 덮습니다. **macOS/Linux 는 편집 즉시 반영**되고, Windows 는 규칙이 `.cmd` 에 구워지므로 `install.ps1` 을 다시 실행해야 반영됩니다.

> 왜 이렇게 만들었나 — openclaude 에 **네이티브 `auto` 모드가 있긴 하지만 우리는 쓸 수 없습니다.** `--permission-mode auto` 를 주면 받아들이는 척하고 런타임에 조용히 `default` 로 되돌립니다. 자세한 이유는 §6.

> 🔒 완전자동은 파일 삭제·셸 명령까지 사람이 안 막습니다. **신뢰하는 프로젝트 폴더에서만** 쓰세요.
> 무한 진행은 **똑같은 도구 호출이 3번 반복되면 자동 차단**되는 가드가 기본으로 막아줍니다(설정 불필요). 추가 한도인 `--max-turns N`·`--max-budget-usd` 는 **비대화형(`-p`) 전용**이라 TUI 에선 효과가 없습니다.

> **껐다 켜기/정리**: `furio`·openclaude 는 맥에 설치된 로컬 명령이라 서버 stop 해도 안 지워집니다(정리 불필요). 다음엔 서버 `serve-router.sh start` + 맥 터널만. 완전 삭제는 `rm -rf ~/.furio ~/.local/bin/furio`.

---

## 4. 문제 해결

### ❗ `400 ... exceeds model maximum context length`
openclaude 의 첫 요청은 큽니다(~17.8k 토큰 = 시스템프롬프트 8.0k + 도구정의 8.8k). 여기에 출력토큰까지 더해지면 모델 ctx 를 넘습니다.
설치기가 **`CLAUDE_CODE_MAX_OUTPUT_TOKENS=8192`** 를 자동 적용하지만, 직접 키웠다면 줄이세요:
```bash
CLAUDE_CODE_MAX_OUTPUT_TOKENS=8192 furio
```
**근본 원인 하나 더 — 설치기가 자동으로 해결합니다.** openclaude 는 우리 라우터의 모델 id 를 모르기 때문에, 알려주지 않으면 **모든 모델을 128000 토큰이라고 가정**합니다. 실제 우리 모델은 40960~262144 로 제각각이라, 작은 모델에선 초과가 나고 큰 모델은 손해입니다.
설치기가 `/router/models` 에서 모델별 실제 ctx 를 받아 `~/.furio/ctx.json` 에 저장하고, 래퍼가 `CLAUDE_CODE_OPENAI_CONTEXT_WINDOWS` 로 넘깁니다. **서버에서 모델을 추가/변경했다면 install.sh 를 다시 돌려야 이 값이 갱신됩니다.**
```bash
cat ~/.furio/ctx.json                       # 지금 알고 있는 모델별 ctx
/set-context-window 40960                   # (TUI 안) 이번 세션만 임시로 바꿔 원인 확인 — /clear-context-window 로 복구
```

> ⚠️ ctx ≥ ~26k 모델만 openclaude 에 쓰세요: gpt-oss-120b(131072)·Qwen3-30B-A3B 계열(40960~262144)·Solar-Open-100B(131072) 등. `curl -s localhost:8400/router/models` 로 모델별 ctx 확인.

**그래도 빠듯하면 도구를 줄이세요**(도구정의가 ~8.8k 를 차지합니다):
```bash
FURIO_TOOLS="Bash,Edit,Read,Write,Glob,Grep" furio     # 필요한 도구만 → 요청 토큰 대폭 절감
```

### 첫 실행에서 처음 보는 확인 화면이 뜸
openclaude 0.25.0 은 서드파티 프로바이더에 대해 **최초 1회 온보딩·신뢰(trust) 화면**을 보여줍니다. 한 번 넘기면 `~/.furio/config` 에 기록되어 다시 안 뜹니다. 비대화형(`-p`)에서 걸리면 대화형으로 한 번 실행해 통과시켜 두세요.

### 모델 응답이 오다 말고 끊김 / 첫 요청이 타임아웃
NPU 는 모델을 **늦게** 올립니다(라우터가 최대 480초 대기). openclaude 0.25.0 은 응답헤더 마감(`API_TIMEOUT_MS`, 기본 600초)과 SSE 유휴 한도(`CLAUDE_STREAM_IDLE_TIMEOUT_MS`, 0.25.0 에서 120초→**90초**로 축소)를 두는데 둘 다 우리 콜드스타트엔 빠듯합니다.
설치기가 각각 **900000ms / 600000ms** 로 넉넉히 잡아 둡니다. 더 늘리려면:
```bash
API_TIMEOUT_MS=1200000 CLAUDE_STREAM_IDLE_TIMEOUT_MS=900000 furio
```

### `node: ... required` / Node 버전 오류
openclaude 는 Node ≥22 가 필요합니다. 둘 중 하나를 깔고 설치기를 다시 돌리세요.
```bash
nvm install 22 && nvm use 22                 # 또는 brew install node (Mac)
curl -fsSL https://bun.sh/install | bash     # node 를 못 올릴 때 (예: 서버의 node 18)
```
다시 설치한 `furio` 는 실행할 때마다 node 버전을 봅니다. 22 이상이면 node 로, 아니면 bun 으로 돌고,
둘 다 없으면 무엇을 깔아야 하는지 알려주고 멈춥니다.

### 모델이 도구 대신 권한/위치 얘기만 하고 파일을 안 만듦
`.claude` 같은 **민감 경로**에서 실행했을 때 납니다 — **일반 프로젝트 폴더**에서 실행하세요.

### `furio: command not found`
```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc   # 후 새 터미널
```

### `서버 도달 실패` (설치/사용)
SSH 터널이 떠 있는지: `curl http://127.0.0.1:8400/v1/models` (모델 나오면 OK). 안 되면 `bash furio-connect.sh`. (자세히는 sdi-code/README.md 의 터널/`No route to host` 항목과 동일.)

---

## 5. 검증 기록 — openclaude v0.25.0 (2026-07-21)

install.sh 는 `@latest` 로 설치하므로 **자동으로 최신(v0.25.0)** 을 받습니다. 0.20.1 → 0.25.0 업그레이드를 소스로 감사하고 실측했습니다.

**그대로인 것(우리 통합 계약 무사)** — 패키지 `@gitlawb/openclaude`·바이너리 `openclaude`·Node ≥22, env 6종(`CLAUDE_CODE_USE_OPENAI`/`OPENAI_BASE_URL`/`OPENAI_MODEL`/`OPENAI_API_KEY`/`CLAUDE_CODE_MAX_OUTPUT_TOKENS`/`OPENCLAUDE_CONFIG_DIR`), 플래그 6종(`--dangerously-skip-permissions`·`--permission-mode acceptEdits`·`-p`·`--model`·`--continue`). 커스텀 모델 id 는 화이트리스트 없이 그대로 전달되고, `dummy` 키도 로컬 주소(127.0.0.1)라 그대로 통과합니다. 새로 생긴 `safetyLevel` 은 `OPENCLAUDE_SAFETY_LEVEL` 을 **명시적으로 설정할 때만** 동작하므로 우리에겐 무영향입니다.

**바뀌어서 대응한 것**
| 변화 | 대응 |
|---|---|
| 모르는 모델 ctx 를 128000 으로 가정 | 설치기가 `/router/models` 로 실제 ctx 를 받아 `~/.furio/ctx.json` → `CLAUDE_CODE_OPENAI_CONTEXT_WINDOWS` |
| `API_TIMEOUT_MS` 응답헤더 마감 신설(기본 600s) | 래퍼에서 **900000ms** 로 상향(라우터 콜드스타트 480s 대비 여유) |
| SSE 유휴 한도 120s → **90s** 축소 | 래퍼에서 `CLAUDE_STREAM_IDLE_TIMEOUT_MS=600000` |
| 서드파티 최초 1회 온보딩·신뢰 화면 | 문서화(§4) |

**바뀌지 않은 것(오해 주의)** — 0.25.0 의 "토큰 최적화"는 **첫 요청 크기를 줄이지 않습니다**(17.8k vs 0.20.1 의 17.6k, 측정오차 수준). 따라서 ctx ≥ ~26k 요구와 `CLAUDE_CODE_MAX_OUTPUT_TOKENS=8192` 는 계속 필요합니다. 더 줄이려면 `FURIO_TOOLS` 로 도구를 추리세요.

---

## 6. 자동모드는 왜 직접 만들었나 (openclaude 네이티브 `auto` 조사)

**openclaude 에도 `auto` 모드가 있습니다. 그런데 우리 환경에서는 작동하지 않습니다.**

`--permission-mode auto` 는 인자 검증은 통과하지만(`choices` 에 `auto` 가 들어 있음), 시작할 때 게이트에 걸려 **말없이 `default` 로 강등**됩니다. 디버그 로그로 확인한 실제 동작:
```
[auto-mode] verifyAutoModeGateAccess: enabledState=disabled modelSupported=false canEnterAuto=false
[auto-mode] kickOutOfAutoIfNeeded applying: ctx.mode=auto reason=circuit-breaker
[DEBUG] Applying permission update: Setting mode to 'default'
```
막는 것은 두 겹인데, 두 번째가 **우회 불가**입니다.

| 관문 | 위치 | 우리에게 |
|---|---|---|
| A. `tengu_auto_mode_config.enabled` 기본 `disabled` | `src/utils/permissions/permissionSetup.ts:1466` | 로컬 플래그 파일로 해제 가능(원격 서비스 아님) |
| B. `modelSupportsAutoMode()` | `src/utils/betas.ts:169-207` | **불가** — `getAPIProvider() !== 'firstParty' \|\| !isFirstPartyAnthropicBaseUrl()` 이면 무조건 거부 |

우리는 `CLAUDE_CODE_USE_OPENAI=1` 이라 provider 가 `openai` 이고, 대안인 `ANTHROPIC_BASE_URL` 은 `https://api.anthropic.com`(443) 만 통과시키므로 **NPU 라우터를 가리키면서 이 검사를 통과할 방법이 없습니다.** 소스 주석도 "플래그 override 로 못 켜게 일부러 앞단에 뒀다"고 명시합니다.

> 아이러니: 분류기(`src/utils/permissions/yoloClassifier.ts`)는 **provider 중립**이라 실제로는 우리 NPU 엔드포인트를 쓰도록 짜여 있습니다. 기능이 아니라 **게이트**가 막는 것입니다.

**그래서 두 가지 선택지 중 (b) 를 골랐습니다.**
- (a) `betas.ts` 를 패치해 리빌드 → `install.sh` 가 `@latest` 로 재설치할 때마다 날아가고 Bun 빌드 체인이 필요. **채택 안 함.**
- (b) 정식 기능(`--allowed-tools`/`--disallowed-tools`)으로 동등한 동작을 우리 층에 구현 → **업그레이드에도 살아남고 openclaude 를 건드리지 않음. 채택.**

### 파일 구조 — 무엇이 어디에 있나

```
claude-agent/                       ← 저장소(서버). 여기를 고치고 맥에 복사해 설치
├── install.sh                      ← ⭐ 자동모드가 구현된 곳 (macOS/Linux)
│     ├─ 규칙 파일 생성부           …  auto-allow.txt / auto-deny.txt 기본값 작성(이미 있으면 보존)
│     └─ 래퍼 생성부 case "$FURIO_AUTO"  …  safe|rules 분기에서 규칙을 읽어 플래그로 변환
├── install.ps1                     ← 같은 것의 Windows 판(규칙을 .cmd 에 구움)
├── furio-connect.sh                ← SSH 터널
└── README.md

~/.furio/                           ← 맥에 설치된 결과물
├── auto-allow.txt                  ← ✏️ 자동 승인 규칙(직접 편집 가능, 즉시 반영)
├── auto-deny.txt                   ← ✏️ 차단 규칙(직접 편집 가능, 즉시 반영)
├── ctx.json                        ← 모델별 컨텍스트(설치 때 라우터에서 받음)
├── key                             ← API 키(0600, 있을 때만)
├── config/                         ← openclaude 설정·세션(유저 기존 openclaude 와 격리)
└── bin/openclaude                  ← openclaude 본체(격리 설치, 손대지 않음)

~/.local/bin/furio                  ← 실행 명령(래퍼). 위 규칙을 읽어 openclaude 에 넘김
```

**동작 순서**: `furio` 실행 → 래퍼가 `FURIO_AUTO` 확인 → `safe` 면 `auto-allow.txt`/`auto-deny.txt` 를 읽어 `--allowed-tools`/`--disallowed-tools` 인자로 조립 → `openclaude` 실행. openclaude 의 규칙 파서는 괄호를 인식하므로 `Bash(git status:*)` 처럼 **공백이 든 규칙도 안전**합니다(실측 확인).
