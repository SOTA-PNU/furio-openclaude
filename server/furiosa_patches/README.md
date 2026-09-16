# furiosa_patches — Qwen3-Coder용 `qwen3_coder` tool 파서

furiosa-llm 2026.2.0 의 tool 파서는 `{hermes, llama3_json/4, openai}` 뿐이라 **Qwen3-Coder**
계열의 tool call 형식을 못 읽습니다(채팅만 가능, 에이전트 도구호출 불가). 이 폴더는 그
`qwen3_coder` tool 파서를 furiosa-llm 에 추가합니다.

## 파일

| 파일 | 역할 |
|---|---|
| `qwen3_coder_tool_parser.py` | 파서 본체. `@ToolParserManager.register_module("qwen3_coder")` 로 등록 |
| `install.sh` | venv 의 `furiosa_llm/server/tool_parsers/` 에 복사 + `__init__.py` 에 import 추가(멱등) |

## 설치

```bash
bash furiosa_patches/install.sh
# → '등록된 tool 파서: [..., qwen3_coder]' 확인
# 이후: furiosa-llm serve <a3b> --enable-auto-tool-choice --tool-call-parser qwen3_coder
```

furiosa-llm 재설치/업그레이드 후 다시 실행하면 복구됩니다(벤더 site-packages 를 수정하므로).
라우터(`furiosa_router.py`)는 Qwen3-Coder 모델에 자동으로 `--tool-call-parser qwen3_coder` 를 붙입니다.

## 동작 원리

### 등록 (다른 파서와 동일한 메커니즘)
`@ToolParserManager.register_module("qwen3_coder")` 가 이름 `"qwen3_coder"` ↔ 클래스를 레지스트리에
등록 → `cli/serve.py` 가 `ToolParserManager.tool_parsers.keys()` 로 CLI 선택지를 만들고(하드코딩 아님,
그래서 등록만 하면 `--tool-call-parser qwen3_coder` 가 통함) → `serving_chat.py` 가
`get_tool_parser("qwen3_coder")` 로 클래스를 찾아 사용.

### 파싱 (모델 출력이 들쭉날쭉해서 매우 관대하게)
Qwen3-Coder chat template 이 모델에 지시하는 표준 형식은 XML 입니다:
```
<tool_call>
<function=NAME>
<parameter=KEY>
VALUE
</parameter>
</function>
</tool_call>
```
그러나 **실측상 a3b(특히 FP8) 모델은 이 형식을 잘 안 지킵니다.** 호출마다 다른 형식을 내서,
파서는 다음을 모두 처리합니다(`extract_tool_calls`):
- 표준 XML `<function=NAME>` + `<parameter=K>V</parameter>`
- 깨진 함수태그 `<function=NAME}` (닫는 `>` 대신 `}`) + 뒤따르는 bare JSON 인자
- `<tool_call>` 안의 JSON 배열/객체. 키도 제각각이라 모두 관대 처리:
  래퍼 `function|tool_call|tool`, 이름 `name|tool_name`, 인자 `arguments|parameters|args`
- 인자 값 타입은 요청 tool 스키마(`parameters.properties[K].type`)로 보정

### de-stream (router 쪽)
OpenCode 는 streaming(SSE)으로 요청하는데, 위 형식들의 **스트리밍** 파싱은 까다롭습니다. 그래서
`furiosa_router.py` 는 `tool == "qwen3_coder"` 모델에 한해 **백엔드를 비스트리밍(stream=false)으로
호출 → 견고한 `extract_tool_calls` 로 파싱 → 결과를 SSE 로 재구성**해 OpenCode 에 돌려줍니다.

## 검증

- 파서 단위검증: 실측 4변종(JSON배열 / 깨진태그+bare JSON / 정상 XML / 멀티콜) + 키변종
  (`tool_call`/`tool_name`) 모두 올바른 `name`+`arguments` 추출 성공.
- 비스트리밍 라우터 경유: a3b 가 완전한 호출을 낼 때 `finish_reason=tool_calls` 정상.

## ⚠️ 모델 신뢰도 한계 (객관적 실측)

파서는 **모델이 (어떤 형식으로든) 완전한 호출을 내면** 안정적으로 추출합니다. 그러나
**Qwen3-Coder-30B-A3B-FP8 모델 자체**가 OpenCode 의 무거운 컨텍스트(대형 system prompt + 다수 tool)
에서 자주 **불완전/환각 출력**을 냅니다 — 예: `<tool_call>` 만 내고 멈춤, 또는
`{"error":{"type":"llm_call_failed",...}}` 같은 환각 JSON, 또는 공백 누락·오타가 섞인 깨진 텍스트.
이건 파서/서빙이 아니라 모델 품질(FP8 양자화 저하 + 30B-A3B 의 한계) 문제입니다.

### 측정 (2026-06-20, 실측)

| 경로 | 결과 |
|---|---|
| 파서 단위검증 (4변종+키변종) | ✅ 전부 올바른 name+arguments 추출 |
| 직접 API, 단순 프롬프트(`temperature=0`, tool 1개) | ✅ `finish_reason=tool_calls`, 3/3 성공 |
| de-stream 스트리밍, 단순 프롬프트 | ✅ SSE 에 유효 `tool_calls` 델타 전달 |
| **OpenCode 실제 루프 (a3b FP8)** | ❌ **0/4** |
| **OpenCode 실제 루프 (a3b bf16)** | ❌ **0/3** |

원인 이분 탐색(객관적):
- temperature 0 으로 고정 → 여전히 실패 (temperature 아님)
- tool 1개로 축소 → 여전히 실패 (tool 개수 아님)
- **OpenCode 의 9650자 system prompt** 가 트리거 → 모델이 `<tool_call>` 뒤에
  `{"error":{"type":"llm_call_failed",...}}` 같은 **환각 JSON** 을 냄(FP8·bf16 공통)
- 짧은 system 으로 바꾸면 환각은 멈추지만 모델이 도구 사용을 거부(Python 으로 대신)

**결론**: `qwen3_coder` 파서로 **tool calling 자체는 부활**(API/스트리밍 레벨 검증 완료)했으나,
**Qwen3-Coder-30B-A3B** 모델이 OpenCode 의 무거운 에이전트 컨텍스트를 감당하지 못해
**실사용 에이전트로는 불안정**하다(파서/서빙이 아니라 모델 한계 — FP8·bf16 동일).

참고: a3b 는 `qwen3_moe`(총 expert 128개 `num_local_experts`, 토큰당 활성 8개 `num_experts_per_tok`)로
**토큰당 활성 ~3B**(총 ~30B 중) = A3B. 실효 용량이 ~3B급이라 9650자 system prompt 를 정확히 따르기엔
약한 것으로 보인다. 단 환각 에러 JSON 은 **모델이 생성한 텍스트**(HTTP 200·finish=stop·문자열 출처가
매번 다름)이지 MoE expert 라우팅 에러가 아니다 — 트리거로 분리·입증된 변수는 system prompt 크기다. 그래서 picker 에서
`[tools~weak]` 로 표시한다.

코딩 에이전트로는 도구호출이 안정적인 **Qwen3-32B-FP8** 또는 **Qwen2.5-Coder-32B** 를 권장합니다.
