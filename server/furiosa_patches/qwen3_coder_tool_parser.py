# SPDX-License-Identifier: Apache-2.0
# Qwen3-Coder tool-call parser for furiosa-llm.
#
# furiosa-llm 2026.2.0 기본 tool 파서{hermes, llama3_json/4, openai}는 Qwen3-Coder 의
# tool call 을 못 읽는다. Qwen3-Coder 의 chat template 은 모델에게 다음 XML 형식을 지시한다:
#   <tool_call>
#   <function=FUNC>
#   <parameter=KEY>
#   VALUE
#   </parameter>
#   </function>
#   </tool_call>
# 그러나 실측상 a3b(FP8) 모델은 이 지시를 항상 따르지 않고, <tool_call> 안에 OpenAI 식
# JSON 배열([{"function":{"name":..,"arguments":".."}}])을 내거나 닫는 태그를 </tool_call>
# 대신 <tool_call> 로 잘못 내기도 한다. 그래서 이 파서는 **두 형식 모두** 관대하게 파싱한다.
#   1) XML  : <function=NAME> + <parameter=KEY>VALUE</parameter>
#   2) JSON : <tool_call> 뒤의 [ { ... } ] / { ... } (OpenAI tool_calls 형태)
# 출력은 OpenAI 표준 tool_calls(name + arguments(JSON 문자열)). 인자 타입은 tool 스키마로 보정.
#
# 등록명: "qwen3_coder"  →  furiosa-llm serve --tool-call-parser qwen3_coder
# 스트리밍도 같은 추출기를 쓴다(emit-on-complete). 예전엔 스트리밍만 XML 방언 전용이라
# JSON 방언 호출이 통째로 사라졌고, openclaude 는 항상 스트리밍이라 "말만 하고 파일은 안 건드림"
# 으로 나타났다. 자세한 내용은 extract_tool_calls_streaming 주석 참고.
import json
import logging
import re
from typing import Sequence, Union

from furiosa_llm.server.protocol import (
    ChatCompletionRequest,
    DeltaFunctionCall,
    DeltaMessage,
    DeltaToolCall,
    ExtractedToolCallInformation,
    FunctionCall,
    ToolCall,
)
from furiosa_llm.server.tool_parsers.abstract_tool_parser import ToolParser, ToolParserManager
from furiosa_llm.server.utils import random_tool_call_id
from furiosa_llm.vllm_compat import AnyTokenizer

logger = logging.getLogger(__name__)


@ToolParserManager.register_module("qwen3_coder")
class Qwen3CoderToolParser(ToolParser):
    def __init__(self, tokenizer: AnyTokenizer):
        super().__init__(tokenizer)
        self.tool_call_start = "<tool_call>"
        self.tool_call_end = "</tool_call>"
        # 함수명: 닫는 기호를 관대하게(>, }, 공백, 줄바꿈 어느 것이든 종료). 모델이
        # <function=write_file> / <function=write_file} 등 깨진 형태로도 내기 때문.
        self._func_re = re.compile(r"<function=([^>}\n\s]+)")
        self._param_re = re.compile(r"<parameter=([^>\n]+)>\n?(.*?)\n?</parameter>", re.DOTALL)
        # streaming state
        self._names_sent: set = set()
        self._args_sent: set = set()
        self._content_done = False

    # ── 스키마 기반 값 타입 보정 ──────────────────────────────────────────
    def _properties(self, request, name):
        for t in (getattr(request, "tools", None) or []):
            fn = getattr(t, "function", None)
            if fn is not None and getattr(fn, "name", None) == name:
                params = getattr(fn, "parameters", None) or {}
                if isinstance(params, dict):
                    return params.get("properties") or {}
        return {}

    def _coerce(self, raw, schema):
        t = (schema or {}).get("type")
        try:
            if t == "integer":
                return int(str(raw).strip())
            if t == "number":
                return float(str(raw).strip())
            if t == "boolean":
                return str(raw).strip().lower() in ("true", "1", "yes")
            if t in ("object", "array"):
                return json.loads(raw)
        except Exception:
            return raw
        return raw

    def _mk(self, name, args):
        arguments = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
        return ToolCall(type="function", function=FunctionCall(name=name, arguments=arguments))

    # ── 첫 JSON 값(배열/객체) raw_decode ─────────────────────────────────
    def _first_json(self, s):
        for i, ch in enumerate(s):
            if ch in "[{":
                try:
                    obj, _ = json.JSONDecoder().raw_decode(s[i:])
                    return obj
                except Exception:
                    continue
        return None

    # ── name/function 키를 가진 JSON → ToolCall 들 ────────────────────────
    # 모델이 함수/이름/인자 키를 제멋대로 쓴다(실측): function|tool_call 래퍼,
    # name|tool_name, arguments|parameters|args. 모두 관대하게 처리.
    # 이름 키는 모델이 매번 다르게 쓴다. 2026-08-13 실측(tp32 공식 코더, 16k 실제 프롬프트):
    #   [{"id":"call_..","tool":{"call_id":"..","func":"Glob","arguments":{..}},"output":""}]
    # 여기선 이름이 "name" 이 아니라 "func" 이고 "tool" 래퍼 안에 있다. 이 하나를 몰라서
    # 멀쩡한 호출을 통째로 버렸다(→ finish_reason=stop, 도구 0개, 사용자에겐 '말만 함').
    # 이 모델은 호출마다 키를 바꾼다. 지금까지 실측된 것: name / func / toolName.
    _NAME_KEYS = ("name", "tool_name", "toolName", "func", "function_name", "function")
    _ARG_KEYS = ("arguments", "parameters", "args", "params", "input")

    def _obj_to_calls(self, obj):
        out = []
        for it in (obj if isinstance(obj, list) else [obj]):
            if not isinstance(it, dict):
                continue
            node = it
            for wrap in ("function", "tool_call", "tool"):
                if isinstance(it.get(wrap), dict):
                    node = it[wrap]
                    break
            name = None
            for src in (node, it):
                for k in self._NAME_KEYS:
                    v = src.get(k)
                    if isinstance(v, str) and v.strip():
                        name = v.strip()
                        break
                if name:
                    break
            if not name:
                continue
            args = {}
            for src in (node, it):
                for k in self._ARG_KEYS:
                    if k in src and isinstance(src[k], (dict, str)):
                        args = src[k]
                        break
                if args:
                    break
            out.append(self._mk(name, args))
        return out

    # ── 견고 추출: 모델이 호출마다 형식이 달라도(XML / 깨진 태그+JSON / JSON배열) 모두 처리 ──
    def _extract_calls(self, text, request):
        # 보통은 <tool_call> 뒤에 호출이 오지만, 긴 컨텍스트에서는 **JSON 을 먼저 내고 <tool_call>
        # 을 꼬리로 붙이는** 출력이 나온다(2026-08-07 실측, tp32 공식 코더 17.8k 프롬프트):
        #   I'll read that for you.\n\n[{"function":{"name":"read_file",...}}]\n<tool_call>
        # 이때 마커 뒤만 보면 빈 문자열이라 멀쩡한 호출을 통째로 버리게 된다.
        # 그래서 마커 뒤를 먼저 보고, 못 찾으면 전체 텍스트로 한 번 더 시도한다.
        if self.tool_call_start in text:
            calls = self._extract_from(text[text.find(self.tool_call_start):], request)
            if calls:
                return calls
        return self._extract_from(text, request)

    def _extract_from(self, region, request):
        fmarks = list(self._func_re.finditer(region))
        if fmarks:
            # 형식 A: <function=NAME> 마커 (뒤에 <parameter> 또는 bare JSON 인자)
            calls = []
            for j, fm in enumerate(fmarks):
                name = fm.group(1).strip()
                end = fmarks[j + 1].start() if j + 1 < len(fmarks) else len(region)
                seg = region[fm.end():end]
                props = self._properties(request, name)
                args = {}
                for pm in self._param_re.finditer(seg):
                    k = pm.group(1).strip()
                    args[k] = self._coerce(pm.group(2), props.get(k))
                if not args:
                    jd = self._first_json(seg)
                    if isinstance(jd, dict):
                        args = jd["arguments"] if (
                            "arguments" in jd and isinstance(jd["arguments"], (dict, str))
                        ) else jd
                calls.append(self._mk(name, args))
            return calls
        # 형식 B: 함수태그 없이 JSON (배열/객체에 name 포함)
        jd = self._first_json(region)
        return self._obj_to_calls(jd) if jd is not None else []

    def _call_start(self, text):
        """도구 호출이 시작되는 위치. 사람용 content 를 여기서 잘라 낸다."""
        idxs = [i for i in (text.find(self.tool_call_start),
                            text.find("<function="),
                            text.find("[{"),
                            text.find('{"')) if i >= 0]
        return min(idxs) if idxs else len(text)

    # ── 비스트리밍 ────────────────────────────────────────────────────────
    def extract_tool_calls(self, model_output, request):
        # <tool_call> 마커가 아예 없이 OpenAI 식 JSON 만 내는 출력도 있으므로, 호출처럼 보이면
        # 마커가 없어도 시도한다. 평범한 산문을 오탐하지 않도록 name+arguments 형태일 때만.
        looks_like_json_call = (
            any(f'"{k}"' in model_output for k in self._NAME_KEYS)
            and any(f'"{k}"' in model_output for k in self._ARG_KEYS)
        )
        if self.tool_call_start not in model_output and not looks_like_json_call:
            return ExtractedToolCallInformation(tools_called=False, tool_calls=[], content=model_output)
        try:
            calls = self._extract_calls(model_output, request)
            if not calls:
                return ExtractedToolCallInformation(tools_called=False, tool_calls=[], content=model_output)
            content = model_output[:self._call_start(model_output)]
            return ExtractedToolCallInformation(
                tools_called=True, tool_calls=calls, content=content if content else None
            )
        except Exception:
            logger.exception("qwen3_coder extract_tool_calls failed")
            return ExtractedToolCallInformation(tools_called=False, tool_calls=[], content=model_output)

    # ── 스트리밍 ──────────────────────────────────────────────────────────
    # 예전 구현은 `<function=NAME>` XML 방언만 알아봤고, 못 맞추면 매 델타 None 을 돌려줬다.
    # 그런데 이 모델은 <tool_call> 안에 JSON 을 내는 쪽이 더 흔하다(헤더 13-17행). 그래서
    # 스트리밍에서는 호출이 통째로 사라지고(도구 델타 0개) 이후 content 도 같이 버려졌다 —
    # 비스트리밍은 관대하게 파싱하므로 같은 응답이 stream=false 면 정상, stream=true 면 실패하는
    # 비대칭이 됐다(2026-08-13 실측: 같은 요청/모델에서 tool_calls 1건 vs 0건).
    # openclaude 는 항상 스트리밍이라, 사용자에겐 "안내문만 말하고 파일은 안 건드림"으로 보였다.
    #
    # 그래서 방언 판별을 그만두고 **비스트리밍과 똑같은 추출기(_extract_calls)** 를 쓴다.
    #   · 호출이 시작되기 전까지는 평범하게 content 를 흘린다.
    #   · 호출처럼 보이기 시작하면 그 앞까지만 content 로 내보내고 그 뒤는 버퍼링한다.
    #   · 매 델타 전체 텍스트를 파싱해 보고, 완성된 호출부터 name+arguments 를 한 번에 낸다.
    #     (JSON 방언은 raw_decode 가 완결된 값에만 성공하므로 '완성' 판정이 공짜로 따라온다.)
    def extract_tool_calls_streaming(
        self, previous_text, current_text, delta_text,
        previous_token_ids, current_token_ids, delta_token_ids, request,
    ):
        start = self._call_started(current_text)
        # 1) 아직 호출이 아니다 → 평범한 content 스트리밍.
        if start >= len(current_text):
            return DeltaMessage(content=delta_text) if delta_text else None

        # 2) 호출 앞의 사람용 안내문은 호출 시작 지점까지만, 한 번에 흘린다.
        if not self._content_done:
            self._content_done = True
            lead = current_text[:start]
            if len(lead) > len(previous_text):
                out = lead[len(previous_text):]
                if out:
                    return DeltaMessage(content=out)

        # 3) 호출 영역 — 비스트리밍과 같은 추출기로 매번 다시 읽는다.
        try:
            calls = self._extract_calls(current_text, request)
        except Exception:
            logger.exception("qwen3_coder streaming parse failed")
            return None
        if not calls:
            # 아직 호출이 완성되지 않았다. 여기서 prev_tool_call_arr 를 건드리면 안 된다 —
            # 예전 구현은 이름 파싱 전에 빈 dict 를 넣어서, 도구를 하나도 못 냈는데도 서버가
            # finish_reason='tool_calls' 를 붙였다(클라이언트엔 '도구 0개인 도구 턴').
            return None

        self._sync_prev(calls)
        # 마지막 호출만 아직 자라는 중일 수 있다(그 앞의 호출들은 다음 호출이 시작된 시점에
        # 이미 끝난 것이다). 절반만 읽은 인자를 내보내면 클라이언트가 깨진 JSON 을 받으므로
        # '완결' 신호를 기다린다:
        #   · 닫는 태그를 봤거나(</tool_call>, </function>, 잘못 낸 두 번째 <tool_call>), 또는
        #   · 마커도 <parameter> 도 없는 순수 JSON 방언이라 raw_decode 성공 자체가 완결 증거일 때.
        last_ready = self._call_closed(current_text) or (
            self.tool_call_start not in current_text and "<parameter=" not in current_text
        )
        for idx, call in enumerate(calls):
            if idx in self._args_sent:
                continue
            args = call.function.arguments or "{}"
            if idx == len(calls) - 1 and not last_ready:
                return None
            self._names_sent.add(idx)
            self._args_sent.add(idx)
            self.current_tool_id = idx
            self.streamed_args_for_tool[idx] = args
            # name 과 arguments 를 한 델타에 함께 낸다 — 클라이언트가 아직 등록하지 않은
            # index 의 arguments 델타를 조용히 버리는 경로를 아예 만들지 않기 위해서다.
            return DeltaMessage(tool_calls=[DeltaToolCall(
                index=idx, type="function", id=random_tool_call_id(),
                function=DeltaFunctionCall(name=call.function.name, arguments=args))])
        return None

    def _call_started(self, text):
        """도구 호출이 시작된 위치. 호출이 아니면 len(text).

        평범한 산문에 중괄호가 섞였다고 호출로 오인하면 그 뒤 답변을 통째로 삼키게 된다.
        그래서 비스트리밍(extract_tool_calls)과 같은 기준으로 '호출처럼 보일 때'만 진입한다.
        """
        if self.tool_call_start in text or "<function=" in text:
            return self._call_start(text)
        if (any(f'"{k}"' in text for k in self._NAME_KEYS)
                and any(f'"{k}"' in text for k in self._ARG_KEYS)):
            return self._call_start(text)
        return len(text)

    def _call_closed(self, text):
        """호출 영역이 닫혔는가. 모델이 닫는 태그를 </tool_call> 대신 <tool_call> 로
        잘못 내는 경우가 있어(헤더 13-15행) 두 번째 여는 마커도 닫힘으로 본다."""
        region = text[self._call_start(text):]
        if self.tool_call_end in region or "</function>" in region:
            return True
        return region.count(self.tool_call_start) >= 2

    def _sync_prev(self, calls):
        """서버의 '잔여 인자 복구'(serving_chat)가 읽는 상태를 최신 파스로 맞춰 둔다.
        마지막 델타 직후 EOS 가 와서 우리가 못 내보낸 호출이 있어도 여기서 복구된다."""
        while len(self.prev_tool_call_arr) < len(calls):
            self.prev_tool_call_arr.append({})
        while len(self.streamed_args_for_tool) < len(calls):
            self.streamed_args_for_tool.append("")
        for i, c in enumerate(calls):
            try:
                parsed = json.loads(c.function.arguments) if c.function.arguments else {}
            except Exception:
                parsed = {}
            self.prev_tool_call_arr[i] = {"name": c.function.name, "arguments": parsed}
