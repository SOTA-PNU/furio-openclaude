# OpenCode × Furiosa NPU — 터미널 코딩 에이전트

> 이 문서의 명령은 저장소의 `server/` 폴더 안에서 실행한다고 가정한다. 라우터 기동은 `serve-router.sh`(배포본은 자기 위치 기준으로 동작한다)를 쓴다. 전체 개요는 [루트 README](../README.md).

[OpenCode](https://opencode.ai) 는 100% 오픈소스 터미널 코딩 에이전트입니다. 이 폴더는
OpenCode 가 **furiosa-llm 의 OpenAI 호환 API** 를 백엔드로 쓰도록 묶어 둔 것입니다.
즉, 코드 생성·수정·파일 편집·셸 실행 같은 에이전트 작업을 **RNGD NPU** 위에서 돌립니다.

> 출처: furiosa-ai 공식 레퍼런스 앱
> <https://github.com/furiosa-ai/furiosa-apps/tree/main/coding-agent/opencode>
> ("openclaw" 라고 부르신 그 에이전트의 정식 이름이 **OpenCode** 입니다.)
> 같은 furiosa-apps `coding-agent/` 에는 OpenCode 외에 Unit Test Generator(Streamlit) 도 있습니다.

이 머신에서 설치·연결·동작까지 모두 실측으로 확인했습니다 (2026-06-19). 자세한 결과는 맨 아래 §검증 참고.

---

## 폴더 구성

```
coding-agent/
├── README.md              ← 지금 이 파일
├── furiosa_router.py      ★ 전 모델 lazy-serving 라우터(선택→자동서빙, 카드 LRU 교체)
├── serve-router.sh        ★ 라우터 기동/종료 (+ opencode.json 자동 생성)   ← 권장 진입점
├── launch-opencode.sh     ★ 라우터 띄우고 OpenCode TUI 실행
├── serve-opencode.sh      ← (구버전) 단일 모델 1개만 직접 serve
├── furiosa_patches/       ← furiosa-llm 에 추가하는 qwen3_coder tool 파서(+install.sh)
├── sdi-code/              ★ 원격 클라이언트("sdi") — 각자 Mac/Win 에서 서버 NPU 에 붙는 CLI
└── opencode/
    ├── opencode.json      ← provider 설정(serve-router.sh 가 전 모델로 자동 생성)
    ├── furiosa-opencode.py← 벤더 원본(단일 모델용 — 라우터 구성에선 미사용)
    ├── opencode.sh        ← 벤더 원본 진입 스크립트(미사용)
    ├── README.md          ← 벤더 원본 설명
    └── opencode.png
```

---

## ★ 전 모델 자동 서빙 — 라우터 (권장, switch-model 지원)

`rngd-npu/artifacts` 의 **모든 모델을 OpenAI 호환 엔드포인트 1개(:8400)로 노출**하는 라우터입니다.
OpenCode 모델 선택창(switch model)에 전부 뜨고, **고르면 그 모델이 올바른 옵션(tool·reasoning
파서·pp·devices)으로 자동 서빙**됩니다. NPU 4장이 모자라면 가장 오래 안 쓴 백엔드를 내려서(LRU
evict) 카드를 확보합니다. furiosa-llm 은 serve 1프로세스=모델 1개라, 라우터가 여러 serve 를
띄웠다 내렸다 하며 다중 모델을 한 엔드포인트로 묶어 줍니다.

**축출 규칙(2026-08-11):** LRU 는 *한가한* 백엔드 중에서만 고릅니다.

| 규칙 | 이유 |
|---|---|
| 처리 중(inflight>0)인 백엔드는 축출 안 함 | `last_used` 는 요청 **시작** 시각이라, 90초 스트리밍 중인 백엔드가 가장 오래 논 것처럼 보여 진행 중인 턴이 끊겼다 |
| 막 ready 된 백엔드에 `ROUTER_EVICT_GRACE`(기본 20초) 상주 보장 | 7분 걸려 올린 모델이 첫 요청도 못 받고 쫓겨나 카드만 왕복했다 |
| 후보가 없으면 `ROUTER_EVICT_WAIT`(기본 300초)까지 대기 → 넘으면 강제 축출 | 대기는 턴을 지키려는 것, 강제는 교착을 막으려는 것 |
| `ready` 전에는 프록시 대상이 아님 | `_start` 는 `_wait_ready` 前에 `running` 에 넣는다. `alive()` 만 보면 아직 듣지도 않는 포트에 붙어 ConnectError(→500) |

검증: `python3 test_furiosa_router.py` (NPU 없이 도는 축출 정책 테스트 14개).

> 클라이언트도 함께 고쳐야 의미가 있습니다. openclaude 의 백그라운드 호출(세션 제목·주제
> 판정)은 실행 시점의 `OPENAI_MODEL` 을 계속 썼기 때문에, `/model` 로 tp32 모델(4장 전부)로
> 바꿔도 제목 생성 한 번에 그 모델이 통째로 내려갔다 다시 올라왔습니다(~100초). 포크의
> `getSmallFastModel()` 이 이제 `/model` 로 바꾼 세션 모델을 따라갑니다.

### 사용법 (2줄)

```bash
cd ~/RNGD-proj/Model_Benchmark/rngd-npu/coding-agent
bash serve-router.sh          # 라우터 :8400 기동 (+ opencode.json 전 모델로 자동 생성)
bash launch-opencode.sh       # OpenCode TUI (별도 터미널). switch model 로 아무 모델이나 선택
```

OpenCode 안에서 모델을 바꾸면(`/models` 또는 switch model), 첫 요청 때 라우터가 그 모델을
자동으로 띄웁니다. 큰 모델(tp32 70B 등)은 콜드스타트가 수십 초~분 걸릴 수 있습니다(그동안 첫
요청이 대기). 비대화형 한 줄 테스트:

```bash
cd opencode
PATH="$HOME/.opencode/bin:$PATH" opencode run --model furiosa/Qwen3-32B-FP8 "작업 지시"
```

상태 확인: `curl -s localhost:8400/router/status | python3 -m json.tool`  (지금 떠 있는 백엔드·여유 카드)

### 등록 모델 (rngd-npu/artifacts) 과 자동 적용 옵션

라우터가 모델별로 자동으로 붙이는 플래그. `agent` = OpenCode 도구호출(에이전트) 가능 여부.

| 모델 (picker id) | 카드 | pp | tool 파서 | reasoning | agent |
|---|---|---|---|---|---|
| **Qwen3-32B-FP8** (기본) | 1 | - | hermes | qwen3 | ✅ |
| Qwen3-32B-FP8-16k | 1 | - | hermes | qwen3 | ✅ |
| Qwen3-32B-FP8-tp32 | 4 | - | hermes | qwen3 | ✅ |
| Qwen2.5-Coder-32B-Instruct | 2 | pp2 | hermes | - | ✅ |
| Qwen2.5-Coder-14B-Instruct | 1 | - | hermes | - | ✅ |
| Qwen2.5-Coder-7B-Instruct | 1 | - | hermes | - | ⚠️ 약함(7B) |
| Qwen2.5-Coder-14B-Base | 1 | - | hermes | - | ⚠️ base |
| Llama-3.3-70B-Instruct | 4 | - | llama3_json | - | ✅ |
| Qwen3-Coder-30B-A3B-FP8 | 1 | - | qwen3_coder | - | ⚠️ weak (아래) |
| Qwen3-Coder-30B-A3B-bf16 | 2 | pp2 | qwen3_coder | - | ⚠️ weak (아래) |
| EXAONE-4.0-32B-FP8 | 4 | - | (hermes) | exaone4 | ❌ chat-only |

⚠️ **에이전트 도구호출 한계(실측 2026-06-19)**: furiosa-llm 2026.2.0 tool 파서는
`{hermes, llama3_json/llama4_json, openai}` 뿐입니다.
- **Qwen3-Coder-30B-A3B** (⚠️ weak): 자체 `qwen3_coder` 포맷을 쓰는데 기본 파서가 없었음 →
  `furiosa_patches/` 에 **`qwen3_coder` tool 파서를 추가**해 tool calling 을 부활시켰습니다(API/스트리밍
  레벨 검증 완료, router de-stream 경유). **단 a3b(3B-active MoE) 모델 자체**가 OpenCode 의 큰
  system prompt 에선 환각/불완전 출력이 잦아 실사용 에이전트로는 불안정(FP8 0/4·bf16 0/3 실측).
  자세한 측정·원인은 [`furiosa_patches/README.md`](furiosa_patches/README.md).
- **EXAONE-4** 는 tool 파서가 없어 채팅·추론만 가능(chat-only).
- 코딩 에이전트로는 도구호출이 검증된 **Qwen3-32B-FP8**(기본) 또는 **Qwen2.5-Coder-32B** 를 권장합니다.
- 제외된 아티팩트: `qwen2.5-72b`(드라이버 크래시), `qwen3-coder-next`(qwen3_next serve 미지원).

모델별 옵션을 바꾸려면 `furiosa_router.py` 의 `REGISTRY` 를 수정하세요(`bash serve-router.sh` 로 재기동).
`python3 furiosa_router.py list` 로 현재 등록·플래그를 볼 수 있습니다.

### 사용 가능한 파서 목록 + 위치 (Part 2 조사 결과)

furiosa-llm 2026.2.0 패키지(`/home/jun/furiosa/lib/python3.12/site-packages/furiosa_llm/server/`):

- **Tool 파서** — 폴더 `server/tool_parsers/`
  - 파일: `hermes_tool_parser.py`, `llama_tool_parser.py`, `openai_tool_parser.py` (+ `abstract_tool_parser.py`)
  - 등록명(= `--tool-call-parser` 값): **`hermes`, `llama3_json`, `llama4_json`, `openai`**
    (`llama3_json`·`llama4_json` 은 같은 클래스 두 이름)
- **Reasoning 파서** — 폴더 `server/reasoning_parsers/`
  - 파일: `deepseek_r1_reasoning_parser.py`, `exaone4_reasoning_parser.py`, `qwen3_reasoning_parser.py` (+ `abs_reasoning_parsers.py`)
  - 등록명(= `--reasoning-parser` 값): **`deepseek_r1`, `exaone4`, `qwen3`**

(없는 것: Qwen3-Coder 용 `qwen3_coder` tool 파서, EXAONE tool 파서 → 위 ⚠️ 한계의 원인)

### tool_parser vs reasoning_parser, 그리고 opencode.json 의 `limit`

- **tool_parser** = 모델 출력에서 **도구 호출**(함수명+인자)을 뽑아 `tool_calls` 필드로. (행동)
- **reasoning_parser** = 모델 출력에서 **추론 `<think>…</think>`** 를 떼어 `reasoning_content` 로. (생각 분리)
  - thinking 모델(토크나이저에 `<think>` 토큰 있음)만 켠다. 안 켜면 `<think>` 가 답(content)에 섞임.
    non-thinking 모델에 켜면 chat 요청이 전부 `HTTP 400 (could not locate think start/end tokens)`.
  - 둘은 독립 → 동시 사용 가능(예: Qwen3-32B = `hermes` + `qwen3`).
- **picker 표시 이름의 꼬리표**: 제가 붙인 도구호출 능력 힌트. (이름만)=에이전트 OK / `[tools~weak]`=파서는
  맞지만 모델이 작아 불안정 / `[chat-only]`=tool 포맷 파서가 없어 도구 실행 불가(대화만).
- **`limit.context` / `limit.output`** = OpenCode 클라이언트 측 예산 힌트(자동 감지 아님). `furiosa_router.py`
  의 `REGISTRY[ctx]` 로 모델별 지정. 모델 실제 max-model-len 이하여야 함(예: Qwen3-32B 는 40960 까지
  가능하나 32768 로 보수적 설정 — 늘리려면 `ctx` 수정). `output` 은 1회 생성 토큰 상한(현재 전 모델 8192).

---

## ★ 원격 클라이언트 — 각자 Mac/Windows 에서 (SSH 없이): `sdi code`

서버에 SSH 하지 않고 팀원이 **자기 PC 에 CLI(`sdi`)를 설치**해 서버 NPU 로 코딩 에이전트를 쓰는 구성.
라우터(:8400)는 이미 `0.0.0.0` 바인딩이라, **Bearer 인증을 켜고** 네트워크로 열면 됩니다.

```bash
# 서버: 인증 켜고 라우터 기동
SDI_API_KEY="<비밀키>" bash serve-router.sh start
# 유저(Mac/Linux): 설치 → sdi 명령
SDI_SERVER=http://10.125.19.138:8400 SDI_API_KEY=<받은키> bash sdi-code/install.sh
sdi            # Claude Code 같은 TUI, 추론은 서버 NPU
```
설치기·Windows(install.ps1)·네트워킹(LAN/VPN/TLS)·보안·한계는 [`sdi-code/README.md`](sdi-code/README.md).
(검증: LAN IP+Bearer 로 401/200 인증 + `sdi run` 정상 — localhost 아닌 네트워크 경로.)

## (구버전) 단일 모델만 직접 serve — 빠른 시작 (2단계)

### 1) 백엔드 serve 띄우기 — **tool calling 필수**

OpenCode 는 함수호출(tool calling) 로 파일을 읽고 쓰는 에이전트입니다. 그래서 serve 가
반드시 `--enable-auto-tool-choice --tool-call-parser` 로 떠 있어야 합니다. 이 플래그 없이
뜬 serve 는 tool 요청을 **HTTP 400 으로 거부**합니다(실측).

```bash
bash serve-opencode.sh        # Qwen3-32B-FP8 을 npu:0 1장에 tool 플래그로 :8000 기동
tail -f ~/RNGD-proj/Model_Benchmark/rngd-npu/chat/serve_logs/8000.log   # "Uvicorn running" 뜨면 준비됨
```

실제로 실행되는 명령(참고):

```bash
furiosa-llm serve <artifacts>/qwen3-32b-fp8-tp8 \
  --served-model-name furiosa-ai/Qwen3-32B-FP8 \
  --devices npu:0 --host 0.0.0.0 --port 8000 \
  --enable-prefix-caching \
  --enable-auto-tool-choice --tool-call-parser hermes --reasoning-parser qwen3
```

- 모델은 로컬 prebuilt 아티팩트 `qwen3-32b-fp8-tp8`(= 벤더 권장 Qwen3-32B-FP8, tp8 = 카드 1장)을 씁니다.
- `--served-model-name furiosa-ai/Qwen3-32B-FP8` 로 모델 id 를 벤더 기본값과 똑같이 노출합니다.
  그래서 벤더 `opencode.sh` 가 **한 줄도 안 고치고** 그대로 동작합니다.
- Qwen3 계열은 tool 파서가 `hermes`, reasoning 파서가 `qwen3` 가 정답입니다(벤더 README 와 동일).
- npu:0 1장만 쓰므로 npu:1~3 은 다른 모델(예: chat 앱)에 그대로 쓸 수 있습니다.

#### tool calling 두 플래그가 무슨 뜻인지 (furiosa-llm help + 소스 확인)

서버 측 함수호출(tool calling) 기능의 짝꿍 한 쌍입니다(vLLM 계열).

- **`--enable-auto-tool-choice`** = 기능을 켜는 **스위치**. 모델이 스스로 "도구를 쓸지/어떤 도구를
  부를지" 판단하는 `tool_choice:"auto"` 경로를 허용합니다. 안 켜면 `tools` 요청을 `HTTP 400
  ("auto" tool choice requires --enable-auto-tool-choice and --tool-call-parser to be set)` 으로 거부합니다.
- **`--tool-call-parser hermes`** = 모델 출력을 읽어내는 **디코더/통역사**. 모델은 OpenAI 식 JSON 을
  바로 뱉지 않고, 학습된 표기법대로 텍스트로 도구호출을 적습니다. 파서가 그 텍스트를 OpenAI `tool_calls`
  필드로 번역합니다. 그래서 모델 표기법에 맞는 파서를 골라야 합니다(`{hermes, llama4_json, llama3_json, openai}`).
  - `hermes` = Qwen 계열(Qwen2.5/Qwen3·Coder)·Hermes 모델 표기. 소스(`server/tool_parsers/hermes_tool_parser.py`)
    상 start=`<tool_call>`, end=`</tool_call>`, regex `<tool_call>(.*?)</tool_call>`. 모델이
    `<tool_call>{"name":"get_weather","arguments":{"city":"Seoul"}}</tool_call>` 로 생성하면 파서가 그 사이 JSON 을
    뽑아 `tool_calls` 로 변환합니다.
  - help 에 "Required for --enable-auto-tool-choice" 라고 명시 — 스위치를 켤 때 반드시 함께 지정.
- 곁다리 **`--reasoning-parser qwen3`** = 도구가 아니라 추론(`<think>...</think>`)을 `reasoning_content` 로
  분리하는 파서. Qwen3 가 thinking 모델이라 같이 붙였습니다.

##### "도구"·"번역"이 실제로 뭔지 — 한 바퀴 예시

LLM 은 글자만 출력하지, 스스로 파일을 읽거나 명령을 실행하지 못합니다. 그래서:
- **도구(tool)** = 클라이언트(OpenCode)가 "이런 함수 쓸 수 있어"라고 모델에게 건네는 함수 명세
  (이름+설명+인자 스키마). OpenCode 의 실제 도구: read/write/edit/bash/grep/list…
- 모델은 도구를 *실행*하는 게 아니라 호출을 **글로 적습니다**(아래 원문).
- **번역(파싱)** = 그 글자를 OpenAI 표준 `tool_calls` JSON 으로 옮기는 것(= `--tool-call-parser` 역할).

read_file 도구로 직접 돌린 실제 왕복(2026-06-19, :8000 Qwen3-32B-FP8):

```
모델 원문(번역 전):  <tool_call>{"name":"read_file","arguments":{"path":"/etc/hostname"}}</tool_call>
파서 번역 후      :  tool_calls=[{function:{name:"read_file", arguments:"{\"path\":\"/etc/hostname\"}"}}], finish=tool_calls
클라이언트가 실행 :  open("/etc/hostname") → "esc8000"
결과 되먹임 후 답 :  "The hostname of this machine is esc8000."
```

역할 분담: 모델=무엇을 부를지 판단(글), 파서=글→구조화 번역, 클라이언트=실제 실행+결과 되먹임.
이 왕복 반복이 곧 "에이전트"입니다.

### 2) OpenCode 실행

```bash
bash launch-opencode.sh       # 환경변수 맞춰서 opencode TUI 실행
# 또는 벤더 방식 그대로:
cd opencode && bash opencode.sh
```

런처가 하는 일: opencode 가 PATH 에 없으면 자동 설치 → 현재 폴더에 `opencode.json` 작성 →
`/v1/models` 도달 확인 → `opencode` 터미널 UI 실행.

비대화형(스크립트/CI)으로 한 번만 돌리려면:

```bash
cd opencode
PATH="$HOME/.opencode/bin:$PATH" opencode run --model furiosa/furiosa-ai/Qwen3-32B-FP8 "여기에 작업 지시"
```

---

## 환경변수로 바꾸기

| 변수 | 기본값 | 설명 |
|---|---|---|
| `FURIOSA_BASE_URL` | `http://localhost:8000/v1` | furiosa-llm API 엔드포인트 |
| `FURIOSA_MODEL` | `furiosa-ai/Qwen3-32B-FP8` | opencode.json 에 적히는 모델 id (serve 의 `--served-model-name` 과 같아야 함) |

코딩 특화 모델(Qwen3-Coder-30B-A3B-FP8)로 바꾸고 싶으면 serve 와 모델 id 를 함께 맞춥니다:

```bash
FURIOSA_ART=~/RNGD-proj/Model_Benchmark/rngd-npu/artifacts/qwen3-coder-30b-a3b-inst-fp8-tp8-65k-tc \
FURIOSA_NAME=furiosa-ai/Qwen3-Coder-30B-A3B-FP8 FURIOSA_PP=2 FURIOSA_DEVICES=npu:0,npu:1 \
bash serve-opencode.sh
# 그리고 opencode/opencode.json 의 모델 id 도 furiosa-ai/Qwen3-Coder-30B-A3B-FP8 로 맞춰서 실행
FURIOSA_MODEL=furiosa-ai/Qwen3-Coder-30B-A3B-FP8 bash launch-opencode.sh
```

(a3b 코더는 bf16 이 아닌 FP8 이라 1장에 올라가지만, 컨텍스트 65k 를 다 쓰려면 2장 pp2 가 안전합니다.)

---

## 내가 빌드한 아티팩트를 OpenCode 모델로 추가하기  ✅ 가능

OpenCode 는 OpenAI 호환 엔드포인트면 무엇이든 모델로 씁니다. furiosa-llm 은 **serve 1프로세스 = 아티팩트
1개 = 포트 1개** 라서, "아티팩트 추가" = "그 아티팩트를 serve 해서 `opencode.json` 의 `provider` 에 한 칸
더 넣기" 입니다.

### 3단계 레시피

1) 아티팩트를 tool 플래그로 serve (빈 카드 + 새 포트):
   ```bash
   cd ~/RNGD-proj/Model_Benchmark/rngd-npu/coding-agent
   FURIOSA_ART=<아티팩트경로> FURIOSA_NAME=<원하는_모델_id> \
   FURIOSA_DEVICES=npu:1 FURIOSA_PORT=8002 FURIOSA_REASONING= \
   bash serve-opencode.sh
   ```
2) `opencode/opencode.json` 의 `provider` 에 그 포트로 한 칸 추가:
   ```json
   "furiosa-coder7": {
     "npm": "@ai-sdk/openai-compatible",
     "options": { "baseURL": "http://localhost:8002/v1" },
     "models": { "<FURIOSA_NAME 과 똑같은 id>": { "limit": { "context": 32768, "output": 8192 } } }
   }
   ```
   - provider 키 이름은 자유(`furiosa-coder7` 처럼), `baseURL` 은 그 포트, **모델 key 는 `/v1/models`
     가 보여주는 id 와 정확히 일치**해야 합니다(= serve 의 `--served-model-name`).
3) 확인 후 사용:
   ```bash
   opencode models                                    # 두 모델 다 보이면 성공
   opencode run --model furiosa-coder7/<id> "..."      # 또는 TUI 모델 선택기에서 고르기
   ```

> tp8 = 카드 1장이라 4장이면 최대 ~3~4개 모델을 포트를 나눠 동시에 띄울 수 있습니다(pp>1 이면 그만큼 줄어듦).

### 모델 계열별 파서 (중요)

| 모델 | `--tool-call-parser` | `--reasoning-parser` (`FURIOSA_REASONING`) |
|---|---|---|
| Qwen2.5 / Qwen2.5-Coder | hermes | (없음) → `FURIOSA_REASONING=` |
| Qwen3 / Qwen3-32B | hermes | qwen3 |
| Qwen3-Coder-30B-A3B | hermes | (없음) |
| Llama-3.x | llama3_json | (없음) |
| EXAONE-4 | (전용 tool 파서 없음 → 에이전트 제한) | exaone4 |

⚠️ **reasoning 파서는 모델과 맞아야 합니다.** thinking 모델이 아닌데 `--reasoning-parser qwen3` 를 주면
serve 는 뜨지만 모든 chat 요청이 `HTTP 400 ("Qwen3 reasoning parser could not locate think start/end
tokens")` 으로 죽습니다(실측). non-thinking 모델(예: Qwen2.5-Coder)은 반드시 `FURIOSA_REASONING=` 로 끄세요.

**thinking 모델 판별법**: furiosa-llm reasoning 파서는 토크나이저 vocab 에서 `<think>`/`</think>` 토큰을
찾고(없으면 위 400). 그래서 확정적 판별 = 토큰 존재 여부:
```bash
grep -l '"<think>"' <아티팩트>/tokenizer_config.json <아티팩트>/tokenizer.json   # 잡히면 thinking 계열
```
실측: qwen3-32b-fp8=YES(thinking) / qwen2.5-coder=no / qwen3-coder-a3b=YES이나 템플릿 비활성(켜도 무해·꺼도 됨)
/ exaone-4=YES / llama-3.3=no. 주의: "토큰 있음 ≠ 실제 thinking"(Qwen3-Coder처럼 토큰만 있고 기본 비활성도 있음).

**hermes vs `*_json` (tool 파서 포맷 차이)**: 모델이 도구호출을 적는 말투가 계열마다 달라서 파서도 다릅니다.
출력은 셋 다 OpenAI 표준 `tool_calls` 로 같게 변환 — 입력 포맷만 다름.
- `hermes` (Qwen/Hermes): `<tool_call>{"name":…,"arguments":{…}}</tool_call>` — **태그로 감싼 JSON**
- `llama3_json`/`llama4_json` (Llama 3/4): `<|python_tag|>{"name":…,"parameters":{…}}` — **특수토큰+생 JSON**, 병렬은 `; ` 구분
- `openai` (gpt-oss/harmony): 평문 아니라 **토큰ID 레벨** recipient/channel 포맷
모델↔파서 안 맞으면 도구호출이 `tool_calls` 로 안 잡히고 일반 텍스트로 새어 에이전트 실패. (규칙: Qwen→hermes, Llama→llama3_json)

### 내 빌드 아티팩트 → 추천 설정 (serve_models.sh 카탈로그 기준)

| 아티팩트 | id 예시 | tool / reasoning | 카드 |
|---|---|---|---|
| qwen3-coder-30b-a3b-inst-fp8-tp8-65k-tc | Qwen3-Coder-30B-A3B-FP8 | hermes / 없음 | 1 (FP8) — **에이전트 추천** |
| qwen3-32b-fp8-tp8 | Qwen3-32B-FP8 | hermes / qwen3 | 1 (현재 :8000 가동) |
| qwen2.5-coder-32b-inst-tp8 | Qwen2.5-Coder-32B | hermes / 없음 | 2 (pp2) |
| qwen2.5-coder-14b-inst-tp8 | Qwen2.5-Coder-14B | hermes / 없음 | 1 |
| qwen2.5-coder-7b-inst-tp8 | Qwen2.5-Coder-7B | hermes / 없음 | 1 (가볍지만 함수호출 신뢰도 낮음) |
| llama-3.3-70b-inst-tp32 | Llama-3.3-70B | llama3_json / 없음 | 4 (tp32) |

작은 7B 는 함수호출 신뢰도가 낮아 에이전트로는 한계가 있습니다. 코딩 에이전트로는
**Qwen3-Coder-30B-A3B-FP8** 또는 **Qwen3-32B-FP8** 을 권합니다.

> 실증(2026-06-19): 위 절차로 coder7(qwen2.5-coder-7b)을 :8002 에 추가 → `opencode models` 에 두 모델 모두
> 표시되고 `opencode run --model furiosa-coder7/...` 로 그 모델 생성 확인. 즉 빌드해둔 아티팩트는 OpenCode 에
> 얼마든지 추가해서 쓸 수 있습니다.

## 모델 선택창에 모르는 모델들이 보이는 이유 (OpenCode Zen)

OpenCode 모델 picker 에는 두 종류가 섞여 보입니다:
- **`furiosa/*`** — 내가 `opencode.json` 에 추가한 **로컬** 모델. 고르면 내 NPU(:8000 등)에서 실행. 무료·오프라인.
- **`opencode/*` (= "OpenCode Zen")** — opencode 가 **기본 내장**한 **원격** 모델 카탈로그. OpenCode 회사의
  클라우드 게이트웨이(미국 서버)에서 실행됩니다. 내 서버에 없어도 picker 엔 뜹니다(설정 가능한 카탈로그 항목일 뿐).

OpenCode Zen 주의점:
- 내 NPU 와 **무관** — 고르면 프롬프트·코드가 인터넷으로 OpenCode 서버에 전송됩니다(무료티어는 데이터 보관 가능).
- 사용하려면 **OpenCode 계정/키 필요**(`opencode auth login`). 기본은 credential 0 이라 그냥 고르면 인증 요구/실패.
- `big-pickle`(유료) + `*-free` 4개(무료, 한시적). 유료는 토큰당 과금.

→ 오프라인·프라이버시·무료가 중요하면 **`furiosa/*` 로컬 모델만** 쓰면 됩니다. 6개 중 내 하드웨어에서 실제로
도는 건 `furiosa/*` 뿐입니다.

## 설치 메모

- `opencode` CLI 는 `~/.opencode/bin/opencode` 에 설치돼 있습니다(버전 1.17.8). standalone 바이너리라
  node 런타임이 꼭 필요하진 않습니다(이 머신 node v18.19.1).
- 설치 시 `--no-modify-path` 로 깔아서 `~/.bashrc` 등 셸 설정은 건드리지 않았습니다.
  벤더 런처가 실행할 때 `~/.opencode/bin` 을 PATH 앞에 자동으로 붙입니다.
- 재설치/업그레이드: `curl -fsSL https://opencode.ai/install | bash -s -- --no-modify-path`
  또는 `~/.opencode/bin/opencode upgrade`.

---

## 검증 (2026-06-19, 이 머신 실측)

| 항목 | 결과 |
|---|---|
| `GET /v1/models` | `furiosa-ai/Qwen3-32B-FP8` |
| 일반 chat completion | 200 OK (Qwen3 thinking 모델이라 `<think>` 출력) |
| **tool calling** | 200 OK · `tool_calls=[get_weather{"city":"Seoul"}]` (플래그 없을 땐 400 이었음) |
| `opencode models` | `furiosa/furiosa-ai/Qwen3-32B-FP8` 인식 |
| `opencode run` (비대화형) | `HELLO_FROM_NPU` 정확 반환, serve 로그에 `POST /v1/chat/completions 200 OK` |
| **에이전트 툴 루프** | `Write` 툴 호출 → `result.txt`(내용 `BANANA`) 실제 생성 → "Done." |

즉 모델 추론 + 함수호출(hermes 파싱) + 실제 툴 실행까지 **NPU 위에서 전체 에이전트 루프가 동작**합니다.

## 로봇이 막혔을 때 sdi-code 가 로봇 파일을 직접 고치게 하기 (설계 메모, 2026-06-25)

질문: "로봇이 NPU 서버에 도움을 요청하면, (맥에서 Claude/Cursor 로 디렉터리 파일을 고치듯) 서버가
로봇의 파일을 직접 수정·추가하게 할 수 있나?" → **가능하고 이 스택이 바로 그 구조입니다.**

- **뉘앙스(정확히)**: LLM 추론은 NPU 서버에서 돌지만, **파일 read/write/edit/bash 툴은 로봇(파일이 있는
  곳)에서 도는 OpenCode/sdi 에이전트가 실행**합니다(Cursor·Claude Code 와 동일). "서버가 판단, 로봇 로컬이
  실제 편집". 위 검증표의 `Write`→`result.txt` 생성이 바로 그 증거.
- **지금 robot-sim 과의 차이**: 현재는 LLM 이 코드 *문자열*을 주면 로봇이 `exec()`로 메모리 실행
  (`robot-sim/core/executor.py`, 파일·툴 없음). 제안은 에이전트가 로봇의 **진짜 컨트롤러 파일**을 읽고 고치고,
  로봇이 reload — 막혔을 때 어디를 고칠지 진단·테스트까지 에이전트가 자율로.
- **구현 단계**: ① 컨트롤러를 실제 파일(`plan_controller.py`)로 ② 막힘 감지 시
  `sdi run --agent coder "plan_controller.py 의 plan() 고쳐줘 — …"`(비대화형, 위 `opencode run` 패턴)
  ③ 종료 후 `importlib.reload` + 기존 AST 게이트 재검증 후 재개 ④ **안전**: 에이전트를 컨테이너/seccomp 로
  가두고, 프리셋 권한 제한(편집은 컨트롤러 폴더만·bash deny), 편집마다 git 커밋(되돌리기), 시뮬 스모크테스트 후
  하드웨어 적용 ⑤ 모델은 tool-calling `ok` 인 **Qwen3-32B-FP8 / Qwen2.5-Coder-32B**(a3b·7B 는 tool-call 약해 비권장).
- **주의**: 콜드스타트 지연(모델 미리 띄움), 4장 공유 LRU 축출(모델 핀 고정), 라우터 TLS 없음(SSH 터널+Bearer 키).
