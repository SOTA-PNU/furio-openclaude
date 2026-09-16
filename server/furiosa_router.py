#!/usr/bin/env python3
"""
Furiosa NPU lazy-serving 라우터 (OpenCode 용)
=============================================
OpenAI 호환 엔드포인트 1개를 열어서, rngd-npu/artifacts 의 모든 빌드 아티팩트를
"모델"로 노출한다. OpenCode 모델 선택창(switch model)에 전부 뜬다.

어떤 모델 X 로 /v1/chat/completions 요청이 오면:
  1) X 를 서빙 중인 furiosa-llm serve 가 있으면 그쪽으로 프록시(스트리밍).
  2) 없으면 X 에 맞는 "올바른 옵션"(tool 파서·reasoning 파서·pp·devices)으로
     furiosa-llm serve 를 그 자리에서 띄운다(lazy). NPU 카드가 모자라면
     least-recently-used 백엔드를 내려서(evict) 카드를 확보한다.
  3) 준비되면 그 백엔드로 프록시.

→ OpenCode 에서 모델만 고르면, 첫 요청 때 알아서 올바르게 서빙되어 바로 쓰인다.

실행:
  python3 furiosa_router.py serve              # 라우터 :8400 기동
  python3 furiosa_router.py gen-config PATH    # opencode.json 생성(전 모델 등록)
  python3 furiosa_router.py list               # 등록 모델/플래그 출력
"""
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import atexit
import hashlib
import hmac

# 옛 로컬 아티팩트 폴더(상대경로 레지스트리 항목 호환용). 배포본에선 환경변수로 옮길 수 있다.
ART = os.environ.get("FURIO_ARTIFACTS_LEGACY", "/home/jun/RNGD-proj/Model_Benchmark/rngd-npu/artifacts")
# 새로 빌드한 tp8 v2 아티팩트 8종이 사는 곳(nvme2 공용 저장소). 레지스트리의 tps 경로 접두사.
# 옛 ART 는 이제 빈 폴더다(.gitkeep 만) — 로컬 아티팩트는 전부 여기서 온다. [[chat-service-model-catalog]]
NVME_ART = os.environ.get("FURIO_ARTIFACTS", "/mnt/nvme2n1p1/models/artifacts")
# 백엔드 로그 폴더. import 시점에 만들어지므로 남의 서버에서도 쓸 수 있는 경로여야 한다
# → 기본값은 이 파일이 있는 폴더의 logs/. 서버에서 다른 곳에 모으려면 FURIO_LOGDIR 로 지정.
LOGDIR = os.environ.get("FURIO_LOGDIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs"))
# serve 바이너리. ~/furiosa venv 가 부분 업그레이드로 깨질 수 있어(import 사망) 환경변수로 뺀다.
# 작동본: FURIOSA_LLM_BIN=/home/jun/furiosa-3.0-test/bin/furiosa-llm  [[furiosa-venv-version-skew]]
FURIOSA_LLM = os.environ.get("FURIOSA_LLM_BIN", "/home/jun/furiosa/bin/furiosa-llm")
ROUTER_PORT = 8400
BACKEND_PORT_BASE = 8410
ALL_CARDS = [0, 1, 2, 3]
# 백엔드 serve 준비 대기(초). fxb 허브 모델의 '첫' 기동은 HF 가중치 다운로드까지 포함될 수 있어
# 최대 모델 기준으로 여유 있게(K-EXAONE-236B ≈ 150GB — 실측 대역폭 ~150MiB/s 에서 ~1000s+).
# 다운로드 중 타임아웃되면 백엔드가 죽고 503 이 나며, 재시도 시 HF 가 이어받아 결국 성공한다.
# 필요하면 ROUTER_READY_TIMEOUT 환경변수로 조정.
READY_TIMEOUT = int(os.environ.get("ROUTER_READY_TIMEOUT", "2400"))
CARD_FREE_TIMEOUT = 90     # evict 후 카드 메모리 해제 대기(초)
# 축출 정책. 처리 중인 요청이 있는 백엔드는 내리지 않고 이만큼 기다린다 — 진행 중인 턴을
# 중간에 끊지 않기 위해서다. 넘기면 교착을 피하려고 마지막 수단으로 LRU 를 끊는다.
EVICT_WAIT = int(os.environ.get("ROUTER_EVICT_WAIT", "300"))
# 막 ready 된 백엔드에 주는 최소 상주 시간(초). 없으면 7분 걸려 올린 모델이 첫 요청도
# 못 받고 즉시 쫓겨나, 카드만 왕복하고 아무도 전진하지 못한다.
EVICT_GRACE = int(os.environ.get("ROUTER_EVICT_GRACE", "20"))
# openclaude 포크(NPU LED·dp/pp) 빌드 산출물 — install.sh 가 여기서 dist 를 받아 덮어쓴다.
CLIENT_DIST = os.environ.get("FURIO_CLIENT_DIST", "/home/jun/openclaude-fork/dist")
CLIENT_PKG = os.environ.get("FURIO_CLIENT_PKG", "/home/jun/openclaude-fork/package.json")

os.makedirs(LOGDIR, exist_ok=True)   # chat/ 은 git 미추적이라 디렉토리가 없을 수 있음

# ── 모델 레지스트리 ────────────────────────────────────────────────────────
# 공식 모델 17종 전부 (claude-agent/available_model.md — huggingface.co/furiosa-ai, 2026.3).
# model_id = HF 저장소 basename. 두 종류의 아티팩트가 섞여 있다:
#   · prebuilt FXB 저장소  → ~/.cache/furiosa/llm/fxb 에서 해석 (fxb show 로 실측)
#   · v2 아티팩트 저장소   → 저장소에 artifact.json (serve 가 v2026.3 태그를 기본 revision 으로 받음)
# 필드:
#   path      : furiosa-ai/... HF ID (또는 ART 상대/절대 경로 — artifacts/ 시절 호환)
#   tp        : 아티팩트의 tensor_parallel_size(PE 수). cards = ceil(tp/8). tp<8 이면 카드 일부만
#               사용(예: tp4 → npu:X:0-3) — devices 는 _start 가 tp 로 계산.
#   cards     : 점유 NPU 칩 수 (스케줄링 단위 — 부분 점유도 1장으로 계산)
#   tool      : --tool-call-parser 값. None = 파서 없음(tool calling 비활성)
#   reasoning : --reasoning-parser 값 또는 None (thinking 모델만; 아니면 None — 주면 400)
#   extra     : 모델별 추가 serve 인자 (예: K-EXAONE 의 enable_thinking)
#   kind      : "chat"(기본) | "embedding" | "reranker" — chat 이 아니면 opencode/furio 목록에서 제외
#   ctx       : 클라이언트 컨텍스트 한도 힌트 = 아티팩트 max_position_embeddings (serve 엔 전달 안 됨)
# 파서 매핑 근거: available_model.md (2026.3 공식) / tp·ctx 근거: fxb show + artifact.json 실측(2026-07-16)
# ★ MoE tp8 아티팩트는 등록하지 않는다(2026-08-29 실측). artifact.json 의 model_type 을 위장해
#   게이트를 지나면 적재는 되지만 답이 틀리고 에러도 안 난다 — furiosa-llm 2026.3.0 의 v2 아티팩트
#   경로가 Qwen3Moe 를 지원하지 않는다. 근거: bench_prebuilt_vs_custom/README.md. MoE 는 FXB 로만.
REGISTRY = {
    # ── agent-ready (tool calling 검증/신뢰 순) ──
    "gpt-oss-120b":                     dict(path="furiosa-ai/gpt-oss-120b",                     tp=32, cards=4, pp=1, tool="openai",     reasoning=None,         ctx=131072),
    "Solar-Open-100B-NVFP4A16":         dict(path="furiosa-ai/Solar-Open-100B-NVFP4A16",         tp=32, cards=4, pp=1, tool="solar_open", reasoning="solar_open", ctx=131072),
    "Qwen3-32B-FP8":                    dict(path="furiosa-ai/Qwen3-32B-FP8",                    tp=32, cards=4, pp=1, tool="hermes",     reasoning="qwen3",      ctx=40960,   tps={8: f"{NVME_ART}/qwen3-32b-tp8"}),
    "Llama-3.3-70B-Instruct":           dict(path="furiosa-ai/Llama-3.3-70B-Instruct",           tp=32, cards=4, pp=1, tool="llama3_json", reasoning=None,        ctx=131072),
    # ── chat (파서는 있으나 신뢰도 낮거나 미실측) ──
    "EXAONE-4.0-32B-FP8":               dict(path="furiosa-ai/EXAONE-4.0-32B-FP8",               tp=32, cards=4, pp=1, tool="hermes",     reasoning="exaone4",    ctx=131072, tps={8: f"{NVME_ART}/exaone4-tp8"}),
    "K-EXAONE-236B-A23B-NVFP4A16":      dict(path="furiosa-ai/K-EXAONE-236B-A23B-NVFP4A16",      tp=32, cards=4, pp=1, tool="hermes",     reasoning="deepseek_v3", ctx=262144,
                                             extra=["--default-chat-template-kwargs", '{"enable_thinking": true}']),
    "Qwen3-30B-A3B-Instruct-2507-FP8":  dict(path="furiosa-ai/Qwen3-30B-A3B-Instruct-2507-FP8",  tp=32, cards=4, pp=1, tool="hermes",     reasoning=None,         ctx=262144),
    "Qwen3-30B-A3B-Thinking-2507-FP8":  dict(path="furiosa-ai/Qwen3-30B-A3B-Thinking-2507-FP8",  tp=32, cards=4, pp=1, tool="hermes",     reasoning="qwen3",      ctx=262144),
    "Qwen3-30B-A3B-FP8":                dict(path="furiosa-ai/Qwen3-30B-A3B-FP8",                tp=32, cards=4, pp=1, tool="hermes",     reasoning="qwen3",      ctx=40960),
    # 코더 계열은 전용 qwen3_coder 파서를 쓴다. 공식 카드는 hermes 를 안내하지만 모델이 실제로 내는 건
    # XML(<function=..><parameter=..>) 이라 hermes 는 파싱에 실패한다(오프라인 실측: hermes/llama3_json/
    # solar_open 전부 tool_calls 비고 content 로 누출, qwen3_coder 만 정상 추출).
    # 파서 실체는 coding-agent/furiosa_patches/qwen3_coder_tool_parser.py — venv 에 install.sh 로 등록한다
    # (furiosa-llm 재설치 시 등록이 날아가므로 재실행 필요).
    "Qwen3-Coder-30B-A3B-Instruct-FP8": dict(path="furiosa-ai/Qwen3-Coder-30B-A3B-Instruct-FP8", tp=32, cards=4, pp=1, tool="qwen3_coder", reasoning=None,        ctx=262144, greedy_default=True),
    # BF16 코더 — coder-bf16-tp8 v2. 가중치 57GB 라 1장(47.5GB) 초과 → pp1 OOM.
    # pp_opts=[2,3,4] 로 pp1 제외하고 2·3·4장 층분할만 노출(기본 pp2, 실측 OK).
    # ❌ 제거(2026-08-29): coder-bf16-tp8 은 MoE 위장 아티팩트라 답이 조용히 틀린다.
    # "Qwen3-Coder-30B-A3B-Instruct":     dict(path=f"{NVME_ART}/coder-bf16-tp8",                  tp=8,  cards=2, pp=1, tool="qwen3_coder", reasoning=None,        ctx=262144, pp_opts=[2, 3, 4], greedy_default=True),
    "Qwen3-VL-32B-Instruct":            dict(path="furiosa-ai/Qwen3-VL-32B-Instruct",            tp=32, cards=4, pp=1, tool="hermes",     reasoning=None,         ctx=262144),
    # fxb→v2 재지정: 로컬 tp8 v2 아티팩트로 서빙해 -pp 층분할 잠금해제(tp 동일=8). 되돌리려면 path 원복. [[chat-service-model-catalog]]
    "Llama-3.1-8B-Instruct":            dict(path=f"{NVME_ART}/llama31-8b-tp8",                   tp=8,  cards=1, pp=1, tool="llama3_json", reasoning=None,        ctx=131072),
    "Qwen3-8B-FP8":                     dict(path="furiosa-ai/Qwen3-8B-FP8",                     tp=8,  cards=1, pp=1, tool="hermes",     reasoning="qwen3",      ctx=40960),
    "Qwen3-4B-FP8":                     dict(path="furiosa-ai/Qwen3-4B-FP8",                     tp=8,  cards=1, pp=1, tool="hermes",     reasoning="qwen3",      ctx=40960),
    # ctx 는 32768 이 아니라 4096 이다(2026-08-06 정정 — 백엔드 /v1/models 가 max_model_len:4096 을 보고).
    # openclaude 의 첫 요청이 ~15.7k 토큰이라 이 모델은 furio 로는 못 쓴다(400) — 라우터 스모크 테스트용.
    "Qwen2.5-0.5B-Instruct":            dict(path="furiosa-ai/Qwen2.5-0.5B-Instruct",            tp=4,  cards=1, pp=1, tool="hermes",     reasoning=None,         ctx=4096),
    # ── 비-chat (furio/opencode 목록에서 제외 — /v1/embeddings·/v1/rerank 로 사용) ──
    "Qwen3-Embedding-8B":               dict(path="furiosa-ai/Qwen3-Embedding-8B",               tp=8,  cards=1, pp=1, tool=None,         reasoning=None,         ctx=40960, kind="embedding"),
    "Qwen3-Reranker-8B":                dict(path="furiosa-ai/Qwen3-Reranker-8B",                tp=8,  cards=1, pp=1, tool=None,         reasoning=None,         ctx=40960, kind="reranker"),
}
# 참고:
#   · reasoning 파서: available_model.md 매핑 그대로. gpt-oss 는 목록에 없음(harmony 가 serve 내부에서
#     reasoning 을 직접 처리 — 파서 불필요, 실측으로 reasoning 필드 출력 확인).
#   · K-EXAONE: enable_thinking 템플릿 kwargs 없이는 추론 불가(available_model.md 비고).
#   · Qwen3-Coder-30B-A3B: 파서는 available_model.md 대로 hermes 이나, 2026.3.0 에 전용 qwen3_coder
#     파서가 없어 tool calling 이 깨진다(실측: 모델의 XML tool 포맷을 hermes 가 파싱 못함 → tool_calls
#     빈 채로 원문이 content 로 누출). 에이전트 도구용 아님 — 코딩 채팅 전용. 도구호출은 gpt-oss-120b·
#     Solar-Open-100B·Qwen3-32B-FP8·Llama-3.3-70B 를 쓸 것.
#   · tp32 모델은 4장 전체 점유 — 한 번에 한 개만 서빙되고 요청 시 LRU 교체.
#   · v2 아티팩트 모델의 첫 사용은 HF 다운로드(수십~백GB) 포함 — READY_TIMEOUT 참고.

# 모델별 tool calling(에이전트) 지원 —
#   ok   : tool calling 실측 OK (gpt-oss·Solar 2026-07 실측, Qwen3-32B·Llama-70B 2026-06 실측)
#   weak : 파서 지정은 있으나 미실측이거나 신뢰도 낮음(a3b MoE 3B-active·소형 모델)
#   no   : 파서 없음 → 채팅 전용
TOOL_SUPPORT = {
    "gpt-oss-120b": "ok",
    "Solar-Open-100B-NVFP4A16": "ok",
    "Qwen3-32B-FP8": "ok",
    "Llama-3.3-70B-Instruct": "ok",
    "EXAONE-4.0-32B-FP8": "weak",
    "K-EXAONE-236B-A23B-NVFP4A16": "weak",
    "Qwen3-30B-A3B-Instruct-2507-FP8": "weak",
    "Qwen3-30B-A3B-Thinking-2507-FP8": "weak",
    "Qwen3-30B-A3B-FP8": "weak",
    # 2026-08-06: 전용 qwen3_coder 파서를 등록해 tool calling 을 되살렸다. hermes 로는 XML 포맷을 못 읽어
    # "no"(채팅전용)였으나, 오프라인 파서 실측에서 qwen3_coder 가 name/arguments 를 정상 추출.
    # 실기 tool calling 은 아직 미검증이라 "weak" 로 둔다.
    "Qwen3-Coder-30B-A3B-Instruct-FP8": "weak",
    "Qwen3-Coder-30B-A3B-Instruct": "weak",      # BF16 코더 — 동일 파서 사용
    "Qwen3-VL-32B-Instruct": "weak",
    "Llama-3.1-8B-Instruct": "weak",
    "Qwen3-8B-FP8": "weak",
    "Qwen3-4B-FP8": "weak",
    "Qwen2.5-0.5B-Instruct": "weak",
}

DEFAULT_MODEL = "gpt-oss-120b"   # 기본 — tool calling OK + 가중치가 이미 서버에 캐시됨

# 모델 표시명(picker)·컨텍스트 단일 출처 — 서버 opencode.json(gen_config)·/router/models·맥 install.sh 가 공유
NAME_HINT = {"ok": "", "weak": "  [tools~weak]", "no": "  [chat-only]"}
def model_display_name(m):
    base = m.split("@", 1)[0]
    kind = REGISTRY.get(base, {}).get("kind", "chat")
    if kind != "chat":
        return f"{m}  [{kind}]"
    return m + NAME_HINT.get(TOOL_SUPPORT.get(base, "ok"), "")


def artifact_path(reg):
    p = reg["path"]
    if p.startswith("/"):
        return p
    local = os.path.join(ART, p)
    # artifacts/ 에 실재하면 그 경로, 아니면 fxb 허브 ID 로 간주(furiosa-llm 이 캐시에서 해석)
    return local if os.path.isdir(local) else p


# ── 병렬화(tp/dp/pp) 변형 ───────────────────────────────────────────────────
# 2026-08-04 확정 규칙 — 추측 금지, 근거대로만 노출:
#   · tp 는 아티팩트 빌드타임 고정(-tp 는 로드 시 무시). "tp 제어" = 서로 다른 빌드 선택:
#     기본 tp(레지스트리 tp) ↔ reg["tps"][N] 의 대체 빌드(로컬 v2 tp8). [[chat-service-model-catalog]]
#   · pp 는 FXB 아티팩트에서 PanicException → v2 에서만 pp 변형을 만든다. tp8 대체빌드는 전부 v2 → pp 허용.
#   · dp 는 --devices 카드 수로 자동 추론(-dp 는 pp>1 일 때만 명시). pp>1 이면 -pp 명시.
#   · 카드 4장 예산: 인스턴스 하나 = tp·pp PE, dp 개. tp<8 은 카드당 8//tp 개 패킹, tp>8 은 ceil(tp/8)장 점유.
FXB_CACHE = os.path.expanduser("~/.cache/furiosa/llm/fxb")


def is_fxb(reg):
    """이 모델의 기본 빌드가 FXB 번들인지. fxb 캐시에 저장소 디렉토리가 있으면 FXB.
    (수동 표기 대신 실제 캐시를 보므로 아티팩트가 바뀌어도 자동으로 맞다.)"""
    repo = reg["path"]
    if repo.startswith("/"):
        return False
    return os.path.isdir(os.path.join(FXB_CACHE, "models--" + repo.replace("/", "--")))


def tp_choices(base):
    """이 모델이 고를 수 있는 tp 값들(오름차순). 기본 tp + reg['tps'] 대체 빌드."""
    reg = REGISTRY[base]
    return sorted({reg["tp"], *reg.get("tps", {}).keys()})


def pp_default(base):
    """이 모델의 기본 pp(맨이름이 뜻하는 pp). pp_opts 가 있으면 그 첫값(예: bf16 코더 2),
    없으면 1. tp_default 와 대칭 — 맨이름(@pp 없음)이 pp1 이 아닐 수 있게 한다."""
    opts = REGISTRY.get(base, {}).get("pp_opts")
    return opts[0] if opts else 1


def resolve_art(reg, tp):
    """(reg, tp) → serve 할 아티팩트 경로. tp 가 대체 빌드면 그 경로, 아니면 기본 경로."""
    if tp != reg["tp"] and tp in reg.get("tps", {}):
        return reg["tps"][tp]
    return artifact_path(reg)


def is_fxb_variant(reg, tp):
    """(reg, tp) 구성이 FXB 로 서빙되는지 = pp 불가 여부. 로컬 v2 경로면 항상 아님."""
    p = resolve_art(reg, tp)
    return False if p.startswith("/") else is_fxb(reg)


def cards_base(tp):
    """dp1·pp1 인스턴스 하나가 점유하는 카드 수. tp<=8 → 1, tp>8 → ceil(tp/8)."""
    return 1 if tp <= 8 else (tp + 7) // 8


def variant_cards(tp, dp, pp):
    """(tp,dp,pp) 구성이 실제로 점유하는 카드 수(PE 패킹 반영)."""
    slots = dp * pp
    if tp < 8:
        per_card = 8 // tp                     # 카드당 인스턴스 수
        return (slots + per_card - 1) // per_card
    return slots * cards_base(tp)              # tp>=8: 인스턴스마다 cards_base 장


def par_choices(base, tp):
    """주어진 tp 에서 (dp 선택지, pp 선택지). 인스턴스가 4장을 독점하면(tp32) ([1],[1])."""
    reg = REGISTRY[base]
    if cards_base(tp) >= len(ALL_CARDS):       # tp32 = 4장 독점
        return [1], [1]
    # pp_opts 지정 모델(예: bf16 코더 — 57GB 라 pp1 OOM)은 그 pp 만 노출. dp 는 최소 pp 기준으로
    # 4장 안에 들어가는 만큼(예: tp8·pp2 → dp2 도 2복제×2장=4장 가능).
    if reg.get("pp_opts"):
        ppmin = reg["pp_opts"][0]
        pp = [n for n in reg["pp_opts"] if variant_cards(tp, 1, n) <= len(ALL_CARDS)]
        dp = [n for n in (1, 2, 4) if variant_cards(tp, n, ppmin) <= len(ALL_CARDS)]
        return dp, pp
    dp = [n for n in (1, 2, 4) if variant_cards(tp, n, 1) <= len(ALL_CARDS)]
    if is_fxb_variant(reg, tp):
        pp = [1]
    else:
        # pp 는 3 도 유효하다(카드 3장 층분할) — coder-bf16@pp3 실측 OK. dp 는 카드 수로 추론되는
        # 복제라 2의 거듭제곱만 노출한다(3복제는 쓸 일이 없고 변형만 늘어남).
        pp = [n for n in (1, 2, 3, 4) if variant_cards(tp, 1, n) <= len(ALL_CARDS)]
    return dp, pp


def variant_id(base, tp=None, dp=1, pp=1):
    """표시·요청용 모델 ID. 기본 구성(기본 tp·dp1·기본 pp)은 접미사 없이 원래 이름.
    접미사 순서는 @tp → @dp → @pp 로 고정(parse_variant 와 일치)."""
    default_tp = REGISTRY[base]["tp"] if base in REGISTRY else None
    sfx = ""
    if tp is not None and tp != default_tp:
        sfx += f"@tp{tp}"
    if dp > 1:
        sfx += f"@dp{dp}"
    if pp != pp_default(base):
        sfx += f"@pp{pp}"
    return base + sfx


def parse_variant(mid):
    """'Qwen3-32B-FP8@tp8@dp2@pp2' → ('Qwen3-32B-FP8', 8, 2, 2). tp 접미사가 없으면
    기본 tp(레지스트리)로 채운다. 모르는 접미사는 base 를 미등록으로 남겨 404 를 유도한다."""
    base, tp, dp, pp = mid, None, 1, None
    while "@" in base:
        head, _, tag = base.rpartition("@")
        m = re.fullmatch(r"(tp|dp|pp)(\d+)", tag)
        if not m:
            return mid, None, 1, 1     # 접미사 아님 → 원본 그대로(=미등록으로 404)
        base = head
        v = int(m.group(2))
        if m.group(1) == "tp":
            tp = v
        elif m.group(1) == "dp":
            dp = v
        else:
            pp = v
    if tp is None:
        tp = REGISTRY[base]["tp"] if base in REGISTRY else 8
    if pp is None:
        pp = pp_default(base) if base in REGISTRY else 1
    return base, tp, dp, pp


def all_model_ids():
    """/v1/models 에 노출할 전체 ID(기본 + 유효한 tp/dp/pp 변형)."""
    out = []
    for m, reg in REGISTRY.items():
        out.append(m)
        if reg.get("kind", "chat") != "chat":
            continue                  # embedding/reranker 는 변형 없음
        default_tp = reg["tp"]
        for tp in tp_choices(m):
            dps, pps = par_choices(m, tp)
            for dp in dps:
                for pp in pps:
                    if tp == default_tp and dp == 1 and pp == pp_default(m):
                        continue      # 기본 = 맨이름(위에서 추가)
                    if variant_cards(tp, dp, pp) > len(ALL_CARDS):
                        continue
                    out.append(variant_id(m, tp, dp, pp))
    return out


def par_flags(dp, pp):
    """serve 에 넣을 병렬화 플래그. pp=1 이면 없음(dp 는 --devices 카드 수로 자동 추론),
    pp>1 이면 -pp 명시(+ dp>1 이면 -dp 도)."""
    if pp <= 1:
        return []
    flags = ["-pp", str(pp)]
    if dp > 1:
        flags += ["-dp", str(dp)]
    return flags


def model_desc(model_id):
    """모델 선택 목록에 띄울 한 줄 설명 — 어떤 병렬 구성으로 서빙되는지."""
    base, tp, dp, pp = parse_variant(model_id)
    reg = REGISTRY[base]
    cards = variant_cards(tp, dp, pp)
    art = "fxb" if is_fxb_variant(reg, tp) else "v2"
    ctx = reg["ctx"]
    ctxs = f"{ctx // 1024}k" if ctx >= 1024 else str(ctx)
    return f"tp{tp}·dp{dp}·pp{pp} · {cards}장 · ctx {ctxs} · {art}"


# ── NPU 카드 상태 ──────────────────────────────────────────────────────────
_SMI_CACHE = {"t": 0.0, "v": {}}
_SMI_LOCK = threading.Lock()
SMI_REFRESH = 5.0   # 백그라운드 갱신 주기(초)


def npu_used_mem(fresh=False):
    """카드별 사용 메모리 {npu_id: used_GiB}.

    기본은 **캐시 즉시 반환(블로킹 없음)**. furiosa-smi 는 모델 로딩 중 매우 느려져서
    (2026-07-22 실측: /router/status 가 17.5초 → 클라이언트 LED 폴링 전멸) 요청 경로에서
    직접 호출하면 안 된다. 갱신은 _smi_refresher 데몬이 백그라운드로 돌린다.
    fresh=True 는 축출 직후처럼 최신값이 꼭 필요한 경로(요청 경로 아님)에서만."""
    if not fresh:
        with _SMI_LOCK:
            return dict(_SMI_CACHE["v"])
    return _smi_read()


def _smi_read():
    try:
        out = subprocess.run(["furiosa-smi", "status"], capture_output=True, text=True, timeout=15).stdout
    except Exception:
        return {}
    mem = {}
    for line in out.splitlines():
        m = re.search(r"npu(\d+)\b.*?(\d+\.\d+)\s*/\s*\d+\.\d+\s*GiB", line)
        if m:
            mem[int(m.group(1))] = float(m.group(2))
    with _SMI_LOCK:
        _SMI_CACHE["t"], _SMI_CACHE["v"] = time.time(), dict(mem)
    return mem


def _smi_refresher():
    """furiosa-smi 를 백그라운드에서만 호출해 캐시를 채운다 — 요청 경로는 절대 블로킹되지 않는다.
    같은 주기로 죽은 백엔드도 회수한다(자식 serve 가 라우터 몰래 죽는 경우 대비)."""
    while True:
        try:
            _smi_read()
        except Exception:
            pass
        try:
            r = globals().get("ROUTER")
            if r is not None:
                with r.lock:
                    r._reap_dead()
        except Exception:
            pass
        time.sleep(SMI_REFRESH)


def start_smi_refresher():
    t = threading.Thread(target=_smi_refresher, daemon=True)
    t.start()
    return t


def wait_cards_free(cards, timeout=CARD_FREE_TIMEOUT):
    deadline = time.time() + timeout
    while time.time() < deadline:
        mem = npu_used_mem(fresh=True)   # 축출 직후 — 캐시된 옛값을 믿으면 안 됨
        if all(mem.get(c, 0.0) < 2.0 for c in cards):
            return True
        time.sleep(2)
    return False


def free_port(start=BACKEND_PORT_BASE):
    for p in range(start, start + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
    raise RuntimeError("no free backend port")


# ── 백엔드(furiosa-llm serve 1개) ─────────────────────────────────────────
class Backend:
    def __init__(self, model_id, port, proc, cards):
        self.model_id = model_id
        self.port = port
        self.proc = proc
        self.cards = cards           # 점유 중인 npu id 리스트
        self.tp = 8
        self.dp = 1
        self.pp = 1
        self.last_used = time.time()
        # serve 프로세스가 살아 있는 것과 HTTP 서버가 듣는 것은 다르다. _wait_ready 통과
        # 전까지는 False — ensure() 의 락프리 fast-path 가 로딩 중 백엔드로 프록시해서
        # ConnectError(→500) 를 내던 문제를 막는다.
        self.ready = False
        self.ready_at = None
        # 처리 중인 요청 수. 0 이 아닌 백엔드는 축출 후보에서 제외한다 — 스트리밍 중
        # last_used 는 요청 '시작' 시각에 멈춰 있어서, 긴 턴을 도는 백엔드가 오히려
        # 가장 오래 논 것처럼 보여 LRU 희생양이 됐다(턴이 중간에 끊김).
        self.inflight = 0
        self.evicting = False

    def alive(self):
        return self.proc.poll() is None

    def usable(self):
        return self.ready and not self.evicting and self.alive()


class Router:
    def __init__(self):
        self.running = {}            # model_id -> Backend
        # model_id -> "loading" | "up" | "stopping" | "error".  없으면 "down".
        # 클라이언트 LED 의 단일 출처 — "지금 올라가는 중"을 표현하려고 도입했다
        # (기존엔 running 에 들어간 뒤에야 보여서 콜드스타트 2분이 침묵이었다).
        self.state = {}
        self.lock = threading.RLock()
        # inflight/evicting 전용 짧은 락. self.lock 은 콜드스타트 동안 수 분간 잡혀 있어
        # 여기에 쓸 수 없다(warm 요청이 다 막힌다). 절대 오래 잡지 않는다.
        self.ilock = threading.Lock()
        atexit.register(self.shutdown_all)

    # 현재 비어 있는 카드: 내 백엔드가 점유 중이지도 않고, furiosa-smi 상 실제로도 비어
    # 있어야 free. (외부 serve 가 든 카드와 충돌 방지)
    def _free_cards(self):
        owned = set()
        for b in self.running.values():
            # 죽은 백엔드가 든 카드는 곧 회수 대상이므로 점유로 치지 않는다.
            # 이게 없으면 자식 serve 가 죽어도(외부 pkill·OOM) 그 카드가 계속
            # owned 로 잡혀 free_cards 가 비고 새 로딩이 막힌다.
            if b.alive():
                owned.update(b.cards)
        mem = npu_used_mem()
        return [c for c in ALL_CARDS if c not in owned and mem.get(c, 0.0) < 2.0]

    def _reap_dead(self):
        """serve 프로세스가 죽은 백엔드를 running/state 에서 제거한다.

        자식 serve 는 라우터와 별개로 죽을 수 있다(OOM, 외부 pkill, serve-router
        재시작이 furiosa-llm 만 종료). 그때 라우터가 이 백엔드를 계속 들고 있으면
        /router/status 가 죽은 모델을 'up' 으로 보고하고 카드도 놓지 않는다.
        smi 갱신 데몬이 5초마다 호출한다. 반드시 락 하에서 부른다."""
        for mid, b in list(self.running.items()):
            if not b.alive():
                self._log(f"reap dead '{mid}' (port {b.port}, cards {b.cards}) — serve 프로세스 종료됨")
                self.running.pop(mid, None)
                # up/down 은 running 으로 표현되므로 state 에서도 지운다(→ 'down').
                # loading/error 전이 중이면 그 표시는 건드리지 않는다.
                if self.state.get(mid) in ("up", "stopping"):
                    self.state.pop(mid, None)

    def _log(self, msg):
        print(f"[router {time.strftime('%H:%M:%S')}] {msg}", flush=True)

    def _stop(self, b):
        self._log(f"evict '{b.model_id}' (port {b.port}, cards {b.cards})")
        # 축출 표시를 먼저 박는다 — acquire() 의 락프리 fast-path 가 방금 내리기로 정한
        # 백엔드를 붙잡아 inflight 를 올리는 경쟁(그리고 그 요청이 끊기는 사고)을 막는다.
        with self.ilock:
            b.evicting = True
            b.ready = False
        # 내려가는 동안에도 LED 가 '전환중'(노랑)으로 보이도록 먼저 표시한다.
        if self.state.get(b.model_id) != "error":
            self.state[b.model_id] = "stopping"
        try:
            b.proc.terminate()
            try:
                b.proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                b.proc.kill()
                b.proc.wait(timeout=15)
        except Exception as e:
            self._log(f"  terminate error: {e}")
        self.running.pop(b.model_id, None)
        wait_cards_free(b.cards)
        if self.state.get(b.model_id) == "stopping":
            self.state.pop(b.model_id, None)   # → "down"

    def _evict_until(self, need):
        """need 장이 빌 때까지 LRU 를 내린다. 단, 일하는 백엔드는 건드리지 않는다.

        예전엔 무조건 LRU 를 죽였다. 그런데 last_used 는 요청 '시작' 시각이라 90초짜리
        턴을 스트리밍 중인 백엔드가 가장 오래 논 것처럼 보였고, 그 사이 다른 모델 요청이
        하나만 와도 진행 중인 턴이 통째로 끊겼다(클라이언트엔 ConnectError → 500).
        또 막 올라온 백엔드가 첫 요청을 받기도 전에 쫓겨나(K-EXAONE 7분 로딩 → 0초 사용)
        아무도 전진하지 못하는 구간이 있었다.

        그래서 후보는 '처리 중 요청 0개 + ready 후 GRACE 초 지난' 백엔드뿐이다. 후보가
        없으면 기다린다(self.lock 은 쥔 채로 — warm 요청은 락프리 경로라 안 막히고,
        그 요청들이 끝나야 후보가 생긴다). EVICT_WAIT 을 넘기면 교착보다는 낫다고 보고
        마지막 수단으로 LRU 를 끊는다."""
        deadline = time.time() + EVICT_WAIT
        while len(self._free_cards()) < need and self.running:
            now = time.time()
            with self.ilock:
                idle = [b for b in self.running.values()
                        if b.inflight == 0 and (b.ready_at is None or now - b.ready_at >= EVICT_GRACE)]
            if idle:
                self._stop(min(idle, key=lambda b: b.last_used))
                continue
            if now >= deadline:
                victim = min(self.running.values(), key=lambda b: b.last_used)
                self._log(f"evict-wait {EVICT_WAIT}s 초과 — 사용 중인 '{victim.model_id}' 을(를) 강제로 내린다")
                self._stop(victim)
                continue
            time.sleep(1)

    def _start(self, model_id):
        base, tp, dp, pp = parse_variant(model_id)
        reg = REGISTRY[base]
        # PE 기반 배치. 한 카드 = 8 PE. 인스턴스 하나 = tp*pp PE, dp 개.
        #   · tp<8  → 한 카드에 8//tp 개 패킹(예: tp4 → npu:0:0-3, npu:0:4-7).
        #   · tp==8 → 인스턴스당 카드 1장.
        #   · tp>8  → 인스턴스당 ceil(tp/8) 장(예: tp32 → 4장 npu:0,1,2,3).
        slots = dp * pp
        need = variant_cards(tp, dp, pp)
        if need > len(ALL_CARDS):
            raise RuntimeError(f"{need}장 필요 — 카드는 {len(ALL_CARDS)}장뿐")
        self.state[model_id] = "loading"
        self._evict_until(need)
        free = self._free_cards()
        if len(free) < need:
            self.state[model_id] = "error"
            raise RuntimeError(f"need {need} cards, only {len(free)} free")
        cards = free[:need]
        if tp < 8:
            # 각 그룹을 카드 안의 tp-크기 PE 구간에 순서대로 채운다.
            # slot i → 카드 cards[i//per_card], 그 카드 안 (i%per_card) 번째 tp 구간.
            per_card = 8 // tp
            devs = []
            for i in range(slots):
                c = cards[i // per_card]
                s = (i % per_card) * tp
                devs.append(f"npu:{c}:{s}-{s + tp - 1}")
            devices = ",".join(devs)
        else:
            # 인스턴스 i 는 cards_base(tp) 장을 통째로 차지(tp8→1장, tp32→4장).
            inst_cards = cards_base(tp)
            devs = []
            for i in range(slots):
                for c in cards[i * inst_cards:(i + 1) * inst_cards]:
                    devs.append(f"npu:{c}")
            devices = ",".join(devs)
        port = free_port()
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", model_id)
        logpath = os.path.join(LOGDIR, f"router-{safe}.log")
        cmd = [
            FURIOSA_LLM, "serve", resolve_art(reg, tp),
            "--served-model-name", model_id,
            "--devices", devices, "--host", "127.0.0.1", "--port", str(port),
            "--enable-prefix-caching",
        ]
        if reg["tool"]:
            cmd += ["--enable-auto-tool-choice", "--tool-call-parser", reg["tool"]]
        cmd += par_flags(dp, pp)
        if reg["reasoning"]:
            cmd += ["--reasoning-parser", reg["reasoning"]]
        if reg.get("extra"):
            cmd += reg["extra"]
        self._log(f"start '{model_id}' art={resolve_art(reg, tp)} devices={devices} tp={tp} dp={dp} pp={pp} tool={reg['tool']} reasoning={reg['reasoning']} → :{port}")
        logf = open(logpath, "w")
        # start_new_session=True — 라우터를 띄운 셸/프로세스그룹이 정리돼도 백엔드가
        # 딸려 죽지 않게 세션을 분리한다(chat/chat_app.py 와 동일).
        proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT,
                                start_new_session=True)
        b = Backend(model_id, port, proc, cards)
        b.tp, b.dp, b.pp = tp, dp, pp
        self.running[model_id] = b
        try:
            self._wait_ready(b, logpath)
        except Exception:
            self.state[model_id] = "error"
            self._stop(b)
            raise
        # HTTP 서버가 실제로 뜬 뒤에야 usable — 이 전에는 프록시가 붙으면 ConnectError 다.
        with self.ilock:
            b.ready = True
            b.ready_at = time.time()
        self.state[model_id] = "up"
        self._log(f"ready '{model_id}' on :{port}")
        return b

    def _wait_ready(self, b, logpath):
        import httpx
        deadline = time.time() + READY_TIMEOUT
        url = f"http://127.0.0.1:{b.port}/v1/models"
        while time.time() < deadline:
            if not b.alive():
                tail = ""
                try:
                    with open(logpath) as f:
                        tail = "".join(f.readlines()[-15:])
                except Exception:
                    pass
                raise RuntimeError(f"serve process exited early:\n{tail}")
            try:
                if httpx.get(url, timeout=3).status_code == 200:
                    return
            except Exception:
                pass
            time.sleep(2)
        raise RuntimeError(
            f"serve not ready within {READY_TIMEOUT}s — 첫 사용 모델이면 가중치 다운로드가 진행 중이었을 수 "
            f"있습니다. 재시도하면 다운로드를 이어받습니다(또는 ROUTER_READY_TIMEOUT 을 늘려 재기동).")

    def ensure(self, model_id):
        """model_id('Qwen3-4B-FP8@dp2' 같은 변형 포함)가 서빙되도록 보장하고 포트 반환(블로킹).

        요청을 실제로 프록시할 거면 ensure() 가 아니라 acquire()/release() 를 써야 한다 —
        ensure() 는 반환 직후 그 백엔드가 축출될 수 있다. (preload 처럼 '올려만 두는'
        용도가 ensure() 의 자리다.)"""
        base = parse_variant(model_id)[0]
        if base not in REGISTRY:
            raise KeyError(model_id)
        # fast-path: 이미 '쓸 수 있으면' 락 없이 즉시 반환. 다른 모델의 콜드스타트가
        # self.lock 을 (최대 READY_TIMEOUT) 잡고 있어도 warm 모델 요청은 막히지 않는다.
        # usable() 은 alive() 보다 엄격하다 — _start 는 _wait_ready 前에 running 에
        # 넣으므로, alive() 만 보면 아직 듣지도 않는 포트로 프록시해 500 이 났다.
        b = self.running.get(model_id)
        if b and b.usable():
            b.last_used = time.time()
            return b.port
        with self.lock:
            b = self.running.get(model_id)
            if b and b.usable():
                b.last_used = time.time()
                return b.port
            if b:
                # _start·_stop 은 이 락 안에서만 도니, 락을 쥔 지금 남아 있는데 usable 이
                # 아니면 잔재다(죽었거나 중간에 실패). 카드를 이중 점유하지 않도록 치운다.
                if b.alive():
                    self._stop(b)
                else:
                    self.running.pop(model_id, None)
            return self._start(model_id).port

    def acquire(self, model_id):
        """모델을 보장하고, 그 백엔드를 '사용 중'으로 표시한 뒤 포트를 돌려준다.

        표시된 동안에는 _evict_until 이 이 백엔드를 축출 후보에서 뺀다. 반드시 짝으로
        release() 를 불러야 한다(스트리밍이면 스트림이 끝난 뒤)."""
        for _ in range(20):
            port = self.ensure(model_id)
            with self.ilock:
                b = self.running.get(model_id)
                if b and b.port == port and b.usable():
                    b.inflight += 1
                    b.last_used = time.time()
                    return port
            # ensure() 와 이 사이에 축출됐다(다른 모델 요청). 다시 올린다.
            time.sleep(0.2)
        raise RuntimeError(f"'{model_id}' 를 잡지 못했다 — 축출 경쟁이 계속됨")

    def release(self, model_id):
        """acquire() 로 잡은 백엔드를 놓는다. last_used 를 '끝난 시각'으로 갱신해
        긴 스트리밍이 LRU 상 가장 오래 논 것처럼 보이던 문제도 함께 없앤다."""
        with self.ilock:
            b = self.running.get(model_id)
            if b:
                b.inflight = max(0, b.inflight - 1)
                b.last_used = time.time()

    def status(self):
        # 락 없이 스냅샷(list 복사 후 순회) — ensure() 가 콜드스타트 동안 self.lock 을 잡고 있어도
        # 상태 조회는 블로킹되지 않는다. dict 읽기는 GIL 하에서 안전.
        # 죽은 백엔드는 보고하지 않는다 — _reap_dead 데몬이 곧 치우지만, 그 사이에도
        # 클라이언트 LED 가 '초록(up)' 으로 보이면 안 되므로 여기서 즉시 걸러낸다.
        out = {mid: dict(port=b.port, cards=b.cards, alive=True, tp=b.tp, dp=b.dp, pp=b.pp,
                         state=self.state.get(mid, "up"),
                         idle_s=round(time.time() - b.last_used, 1))
               for mid, b in list(self.running.items()) if b.alive()}
        # running 에 아직 없는 전환중 모델(콜드스타트 진행 중이 대표적)도 함께 노출 —
        # 클라이언트 LED 가 '올라가는 중'을 볼 수 있어야 하므로 이쪽이 본질이다.
        # 단 'up' 은 제외한다 — up 은 살아있는 백엔드가 뒷받침해야만 보여야 하고,
        # 백엔드 없이 남은 up 은 죽은 것이므로 여기서 되살리면 안 된다.
        for mid, st in list(self.state.items()):
            if mid not in out and st in ("loading", "stopping", "error"):
                base, tp, dp, pp = parse_variant(mid)
                out[mid] = dict(port=None, cards=[], alive=False, tp=tp, dp=dp, pp=pp,
                                state=st, idle_s=None)
        return out

    def shutdown_all(self):
        for b in list(self.running.values()):
            try:
                b.proc.terminate()
            except Exception:
                pass


ROUTER = Router()


# ── FastAPI 앱 ─────────────────────────────────────────────────────────────
def build_app():
    import httpx
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse, StreamingResponse
    from starlette.background import BackgroundTask
    from starlette.concurrency import run_in_threadpool

    # docs/openapi 자동 엔드포인트는 비활성(인증 미적용 + 0.0.0.0 노출 시 정보유출 방지)
    app = FastAPI(title="furiosa-router", docs_url=None, redoc_url=None, openapi_url=None)
    aclient = httpx.AsyncClient(timeout=httpx.Timeout(None))

    # 선택적 Bearer 인증: SDI_API_KEY(또는 FURIOSA_API_KEY) 가 설정돼 있으면 /v1·/router
    # 요청에 'Authorization: Bearer <key>' 를 요구. (원격 Mac/Win 클라이언트 노출 시 필수)
    API_KEY = os.environ.get("SDI_API_KEY") or os.environ.get("FURIOSA_API_KEY")

    @app.middleware("http")
    async def _auth(request, call_next):
        if API_KEY and request.url.path.startswith(("/v1", "/router")):
            # 상수시간 비교(타이밍 사이드채널로 키 유출 방지)
            if not hmac.compare_digest(request.headers.get("authorization", ""), f"Bearer {API_KEY}"):
                return JSONResponse({"error": {"message": "missing or invalid API key"}}, status_code=401)
        return await call_next(request)

    @app.get("/v1/models")
    async def list_models():
        # 기본 모델 + dp/pp 변형을 함께 노출한다. 변형을 별도 필드가 아니라 모델 ID 로 싣는 이유는
        # OpenAI 호환 API 를 벗어나지 않기 위해서다(어떤 클라이언트든 그냥 고르면 동작).
        return {"object": "list",
                "data": [{"id": m, "object": "model", "owned_by": "furiosa-npu"}
                         for m in all_model_ids()]}

    # sync 핸들러(async 아님) — FastAPI 가 threadpool 에서 돌리므로 furiosa-smi 호출·락 대기가
    # 이벤트 루프를 막지 않는다. (async 로 두면 콜드스타트 900s 동안 라우터 전체가 얼어붙음 — 실측)
    # ── 포크 클라이언트 배포 ────────────────────────────────────────────────
    # furio 는 openclaude 포크(NPU LED·dp/pp 위젯)를 쓴다. 개인 PC 에 bun/빌드 툴체인을
    # 깔게 하지 않으려고, 서버가 빌드한 dist 만 내려보내고 나머지(bin·node_modules)는
    # npm 에서 '포크와 같은 버전'을 고정 설치한다 — install.sh 한 줄이 유지된다.
    def _client_files():
        out = {}
        for name in ("cli.mjs", "sdk.mjs"):
            p = os.path.join(CLIENT_DIST, name)
            if os.path.isfile(p):
                h = hashlib.sha256()
                with open(p, "rb") as f:
                    for chunk in iter(lambda: f.read(1 << 20), b""):
                        h.update(chunk)
                out[name] = {"sha256": h.hexdigest(), "bytes": os.path.getsize(p)}
        return out

    @app.get("/router/client/manifest.json")
    def client_manifest():
        ver = ""
        try:
            with open(CLIENT_PKG) as f:
                ver = json.load(f).get("version", "")
        except Exception:
            pass
        files = _client_files()
        return {"version": ver, "files": files,
                "ok": bool(ver and files),
                "note": "npm 으로 같은 version 을 설치한 뒤 이 파일들로 dist 를 덮어쓸 것"}

    @app.get("/router/client/{name}")
    def client_file(name: str):
        from fastapi.responses import FileResponse
        if name not in ("cli.mjs", "sdk.mjs"):
            return JSONResponse({"error": {"message": "not found"}}, status_code=404)
        p = os.path.join(CLIENT_DIST, name)
        if not os.path.isfile(p):
            return JSONResponse({"error": {"message": f"{name} 없음 — 서버에서 포크를 빌드하세요"}},
                                status_code=503)
        return FileResponse(p, media_type="application/javascript")

    @app.post("/router/preload")
    async def preload(request: Request):
        """모델을 미리 올려 둔다(즉시 반환). 클라이언트가 /model 에서 고르는 순간 부르므로,
        첫 메시지를 보낼 때까지 기다리지 않고 바로 로딩이 시작된다. ensure() 는 준비될
        때까지 블로킹이라 백그라운드 스레드로 던지고, 진행 상황은 /router/status 로 본다."""
        try:
            payload = json.loads(await request.body() or b"{}")
        except Exception:
            payload = {}
        model = payload.get("model") or ""
        base = parse_variant(model)[0]
        if base not in REGISTRY:
            return JSONResponse({"error": {"message": f"unknown model '{model}'"}}, status_code=404)
        if REGISTRY[base].get("kind", "chat") != "chat":
            return JSONResponse({"ok": False, "reason": "not a chat model"}, status_code=400)

        def _go():
            try:
                ROUTER.ensure(model)
            except Exception as e:
                ROUTER._log(f"preload '{model}' failed: {e}")

        threading.Thread(target=_go, daemon=True).start()
        return {"ok": True, "model": model}

    @app.get("/router/status")
    def router_status():
        return {"running": ROUTER.status(), "free_cards": ROUTER._free_cards()}

    @app.get("/router/models")
    async def router_models():
        # 표시명·컨텍스트·병렬구성 단일 출처 → 클라이언트(install.sh·모델 선택 UI)가 그대로 쓴다.
        # description 은 openclaude 가 "Detected from ..." 로 하드코딩하므로 클라이언트가
        # 이 값으로 덮어쓴다.
        out = []
        for mid in all_model_ids():
            base, tp, dp, pp = parse_variant(mid)
            reg = REGISTRY[base]
            dps, pps = par_choices(base, tp)
            out.append({
                "id": mid, "base": base, "name": model_display_name(mid),
                "description": model_desc(mid),
                "context": reg["ctx"], "kind": reg.get("kind", "chat"),
                "tp": tp, "tp_default": reg["tp"], "tp_choices": tp_choices(base),
                "dp": dp, "pp": pp, "pp_default": pp_default(base),
                "cards": variant_cards(tp, dp, pp),
                "dp_choices": dps, "pp_choices": pps,
                "artifact": "fxb" if is_fxb_variant(reg, tp) else "v2",
                "tools": TOOL_SUPPORT.get(base, "ok"),
            })
        return {"data": out}

    def _sse_from_completion(data):
        # 비스트리밍 chat.completion → OpenAI 스트리밍(SSE) 청크로 변환
        cid = data.get("id", "chatcmpl-router")
        created = data.get("created", 0)
        model = data.get("model", "")
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message", {}) or {}
        fr = choice.get("finish_reason")

        def chunk(delta, finish=None):
            o = {"id": cid, "object": "chat.completion.chunk", "created": created,
                 "model": model, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            return "data: " + json.dumps(o, ensure_ascii=False) + "\n\n"

        async def gen():
            yield chunk({"role": "assistant"})
            if msg.get("reasoning"):
                yield chunk({"reasoning": msg["reasoning"]})
            if msg.get("content"):
                yield chunk({"content": msg["content"]})
            for i, tc in enumerate(msg.get("tool_calls") or []):
                fn = tc.get("function", {})
                yield chunk({"tool_calls": [{"index": i, "id": tc.get("id") or f"call-router-{i}",
                                             "type": "function",
                                             "function": {"name": fn.get("name"),
                                                          "arguments": fn.get("arguments", "")}}]})
            yield chunk({}, finish=fr or "stop")
            yield "data: [DONE]\n\n"
        return gen()

    async def _proxy(request: Request, subpath: str):
        raw = await request.body()
        try:
            payload = json.loads(raw)
            model = payload.get("model")
        except Exception:
            return JSONResponse({"error": {"message": "invalid JSON body"}}, status_code=400)
        if parse_variant(model or "")[0] not in REGISTRY:
            return JSONResponse({"error": {"message": f"unknown model '{model}'. /v1/models 참고."}}, status_code=404)
        # 확률 샘플러 패닉 방지: 아티팩트의 generation_config 가 do_sample=true 인 모델은 클라이언트가
        # temperature 를 안 보내면 그 기본값(예: temp 0.7·top_k 20)으로 랜덤 샘플러를 타는데,
        # furiosa-generator 의 sampling/random.rs 가 가중치가 전부 0/NaN 이면 InvalidWeight 로 패닉한다.
        # 패닉하면 그 serve 는 이후 200 을 주면서 completion_tokens=0 만 내놓아(엔진 스레드 사망)
        # 클라이언트에는 "아무 반응 없음"으로 보인다 — 재시작 말고는 복구가 없다. [[furiosa-llm-invalidweight-panic]]
        # 그리디(temperature=0)는 이 경로를 아예 타지 않으므로, 취약 모델은 그리디로 **강제**한다.
        # 클라이언트 값을 존중하지 않는 이유: openclaude 는 항상 temperature=1 을 보내고(claude.ts:1933)
        # 그 값이 곧 패닉 경로다. 존중하면 코더 모델이 매번 죽어 사용 자체가 불가능하다.
        # top_k/top_p 도 함께 걷어낸다 — 남아 있으면 필터 후 가중치가 다시 0/NaN 이 될 수 있다.
        if REGISTRY[parse_variant(model)[0]].get("greedy_default"):
            payload["temperature"] = 0
            payload.pop("top_p", None)
            payload.pop("top_k", None)
            raw = json.dumps(payload).encode()
        # acquire = ensure + '사용 중' 표시. 표시된 동안에는 다른 모델 요청이 이 백엔드를
        # 축출하지 못한다(진행 중인 턴이 중간에 끊기던 원인). 어느 경로로 나가든 release 를
        # 정확히 한 번 부른다 — 스트리밍이면 스트림이 다 끝난 뒤.
        try:
            port = await run_in_threadpool(ROUTER.acquire, model)
        except Exception as e:
            return JSONResponse({"error": {"message": f"failed to serve '{model}': {e}"}}, status_code=503)
        released = False

        def _release_once():
            nonlocal released
            if not released:
                released = True
                ROUTER.release(model)

        url = f"http://127.0.0.1:{port}/v1/{subpath}"

        # de-stream(옵션): 특정 모델이 스트리밍 tool 파싱이 취약하면 REGISTRY 에 destream=True 를 주어,
        # 백엔드를 비스트리밍으로 호출→견고한 extract_tool_calls→결과를 SSE 로 재구성해 보낸다.
        # 2026.3.0 기준 현재 쓰는 파서(hermes·llama3_json·openai·solar_open)는 모두 스트리밍 tool 파싱을
        # 내장(extract_tool_calls_streaming)하므로 기본은 어떤 모델도 destream 을 켜지 않는다(전량 raw 패스스루).
        needs_destream = (
            subpath == "chat/completions"
            and bool(payload.get("stream"))
            and bool(REGISTRY.get(parse_variant(model)[0], {}).get("destream"))
        )
        if needs_destream:
            body2 = dict(payload)
            body2["stream"] = False
            try:
                r = await aclient.post(url, json=body2, timeout=httpx.Timeout(None))
                data = r.json()
            except Exception as e:
                return JSONResponse({"error": {"message": f"backend error: {e}"}}, status_code=502)
            finally:
                _release_once()
            if r.status_code != 200:
                return JSONResponse(data, status_code=r.status_code)
            return StreamingResponse(_sse_from_completion(data), media_type="text/event-stream")

        try:
            req = aclient.build_request("POST", url, content=raw,
                                        headers={"Content-Type": "application/json"})
            resp = await aclient.send(req, stream=True)
        except Exception as e:
            _release_once()
            return JSONResponse({"error": {"message": f"backend error: {e}"}}, status_code=502)

        async def _body():
            # release 를 제너레이터의 finally 에 둔다 — 정상 종료는 물론 클라이언트가
            # 도중에 끊어(GeneratorExit) 백그라운드 태스크가 안 돌 때도 확실히 풀린다.
            # 안 풀리면 그 백엔드는 영영 '사용 중'으로 남아 축출 불가가 된다.
            try:
                async for chunk in resp.aiter_raw():
                    yield chunk
            finally:
                _release_once()

        async def _done():
            try:
                await resp.aclose()
            finally:
                _release_once()   # 멱등 — 위 finally 가 이미 풀었으면 아무 일도 안 한다

        return StreamingResponse(
            _body(),
            status_code=resp.status_code,
            headers={"Content-Type": resp.headers.get("content-type", "application/json")},
            background=BackgroundTask(_done),
        )

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        return await _proxy(request, "chat/completions")

    @app.post("/v1/completions")
    async def completions(request: Request):
        return await _proxy(request, "completions")

    @app.post("/v1/embeddings")
    async def embeddings(request: Request):
        return await _proxy(request, "embeddings")

    @app.post("/v1/rerank")
    async def rerank(request: Request):
        return await _proxy(request, "rerank")

    return app


# ── opencode.json 생성 ─────────────────────────────────────────────────────
def gen_opencode_json(path):
    models = {}
    for m in all_model_ids():
        reg = REGISTRY[parse_variant(m)[0]]
        if reg.get("kind", "chat") != "chat":
            continue
        models[m] = {"name": model_display_name(m),
                     "description": model_desc(m),
                     "limit": {"context": reg["ctx"], "output": 8192}}
    cfg = {
        "$schema": "https://opencode.ai/config.json",
        "provider": {
            "furiosa": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "FuriosaNPU (router)",
                "options": {"baseURL": f"http://localhost:{ROUTER_PORT}/v1"},
                "models": models,
            }
        },
        "model": f"furiosa/{DEFAULT_MODEL}",
        "small_model": f"furiosa/{DEFAULT_MODEL}",
    }
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"wrote {path} with {len(models)} models (provider 'furiosa' → :{ROUTER_PORT})")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "list":
        for m, reg in REGISTRY.items():
            kind = reg.get("kind", "chat")
            agent = TOOL_SUPPORT.get(m, "ok") if kind == "chat" else f"-({kind})"
            print(f"  {m:32s} cards={reg['cards']} pp={reg['pp']} tool={str(reg['tool']):11s} "
                  f"reasoning={str(reg['reasoning']):11s} agent={agent}")
    elif cmd == "gen-config":
        gen_opencode_json(sys.argv[2])
    elif cmd == "serve":
        import uvicorn
        start_smi_refresher()   # furiosa-smi 를 백그라운드로만 호출 → /router/status 가 항상 즉답
        api_key = os.environ.get("SDI_API_KEY") or os.environ.get("FURIOSA_API_KEY")
        # 인증 on/off 는 SDI_API_KEY 설정 여부로 결정:
        #   키 있음 → 네트워크 개방 + Bearer 인증(사용자도 같은 키 필요)
        #   키 없음 → 네트워크 개방 + 무인증(승인된 사내망 사용자는 키 없이 접속)
        # 사내망 공유 서버라 기본은 0.0.0.0. 로컬 전용으로 닫으려면 SDI_BIND=127.0.0.1.
        host = os.environ.get("SDI_BIND") or "0.0.0.0"
        if host != "127.0.0.1" and not api_key:
            ROUTER._log(f"ℹ️  인증 OFF — :{ROUTER_PORT} 가 네트워크에 개방됩니다(키 불필요 모드, 승인된 사내망 전용). "
                        f"키를 요구하려면 SDI_API_KEY 를 설정하세요.")
        pidfile = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".router.pid")
        with open(pidfile, "w") as f:
            f.write(str(os.getpid()))
        atexit.register(lambda: os.path.exists(pidfile) and os.remove(pidfile))
        authmode = "on" if api_key else ("off(loopback)" if host == "127.0.0.1" else "OFF(open)")
        nvar = len(all_model_ids()) - len(REGISTRY)
        ROUTER._log(f"furiosa-router up on {host}:{ROUTER_PORT}  ({len(REGISTRY)} models + {nvar} dp/pp variants, "
                    f"auth={authmode})  pid={os.getpid()}")
        uvicorn.run(build_app(), host=host, port=ROUTER_PORT, log_level="warning")
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
