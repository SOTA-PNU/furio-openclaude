#!/usr/bin/env python3
"""qwen3_coder 파서 테스트 — NPU 없이 돈다.

핵심은 **스트리밍이 비스트리밍과 같은 결과를 내는가** 다.
2026-08-13 실측: 같은 요청·같은 모델에서 stream=false 는 tool_calls 1건, stream=true 는 0건이었다.
스트리밍 경로가 `<function=NAME>` XML 방언만 알아보고 JSON 방언 호출을 통째로 버렸기 때문이다.
openclaude 는 항상 스트리밍이라 이게 곧 "말만 하고 파일은 안 건드림" 이었다.

실행:  /home/jun/furiosa/bin/python3 test_qwen3_coder_tool_parser.py
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qwen3_coder_tool_parser import Qwen3CoderToolParser  # noqa: E402


class FakeFn:
    def __init__(self, name, properties):
        self.name = name
        self.parameters = {"type": "object", "properties": properties}


class FakeTool:
    def __init__(self, name, properties):
        self.function = FakeFn(name, properties)


class FakeRequest:
    tools = [
        FakeTool("Read", {"file_path": {"type": "string"}}),
        FakeTool("Write", {"file_path": {"type": "string"}, "content": {"type": "string"}}),
    ]


REQ = FakeRequest()


def parser():
    return Qwen3CoderToolParser(tokenizer=None)


def run_stream(text, chunk=7):
    """텍스트를 조각내 흘려보내고 (content, tool_calls) 를 모은다."""
    p = parser()
    content, calls = [], []
    prev = ""
    for i in range(chunk, len(text) + chunk, chunk):
        cur = text[:i]
        if cur == prev:
            continue
        delta = cur[len(prev):]
        msg = p.extract_tool_calls_streaming(prev, cur, delta, [], [], [], REQ)
        if msg is not None:
            if getattr(msg, "content", None):
                content.append(msg.content)
            for tc in (getattr(msg, "tool_calls", None) or []):
                calls.append(tc)
        prev = cur
    return "".join(content), calls, p


def as_pairs(calls):
    """(name, arguments dict) 목록. name/arguments 가 나뉘어 와도 합친다."""
    merged = {}
    for tc in calls:
        idx = tc.index
        slot = merged.setdefault(idx, {"name": None, "args": ""})
        fn = tc.function
        if getattr(fn, "name", None):
            slot["name"] = fn.name
        if getattr(fn, "arguments", None):
            slot["args"] += fn.arguments
    out = []
    for idx in sorted(merged):
        raw = merged[idx]["args"] or "{}"
        try:
            args = json.loads(raw)
        except Exception:
            args = {"__unparsed__": raw}
        out.append((merged[idx]["name"], args))
    return out


# 실측된 두 방언 ---------------------------------------------------------------
XML = ('먼저 파일을 읽겠습니다.\n\n<tool_call>\n<function=Read>\n'
       '<parameter=file_path>\nbase_carter_run.py\n</parameter>\n</function>\n</tool_call>')
JSON_IN_MARKER = ('먼저 파일을 읽겠습니다.\n\n<tool_call>\n'
                  '{"name": "Read", "arguments": {"file_path": "base_carter_run.py"}}\n</tool_call>')
JSON_NO_MARKER = ('먼저 파일을 읽겠습니다.\n\n'
                  '[{"function": {"name": "Read", "arguments": "{\\"file_path\\": \\"base_carter_run.py\\"}"}}]')
BROKEN_CLOSER = ('먼저 파일을 읽겠습니다.\n\n<tool_call>\n<function=Read>\n'
                 '<parameter=file_path>\nbase_carter_run.py\n</parameter>\n</function>\n<tool_call>')
# 2026-08-13 실측 — tp32 공식 코더에 **진짜 openclaude 요청**(시스템 31.7k자, 도구 12개)을
# 재생했을 때 나온 형태. 이름 키가 "name" 이 아니라 "func" 이고 "tool" 래퍼 안에 있으며,
# 닫는 태그도 </tool_call> 이 아니라 <tool_call> 이다. 이 하나를 몰라서 호출을 통째로 버렸다.
FUNC_KEY_DIALECT = (
    "I'll help you modify base_carter_run.py。首先，我需要查看这两个文件的内容。\n\n"
    '<tool_call>\r\n'
    '[{"id":"call_8jxq","tool":{"call_id":"call_8jxq","func":"Glob",'
    '"arguments":{"pattern":"PROMPT.md"}},"output":""},'
    '{"id":"call_8jxr","tool":{"call_id":"call_8jxr","func":"Glob",'
    '"arguments":{"pattern":"base_carter_run.py"}},"output":""}]\n<tool_call>'
)


class StreamingMatchesNonStreaming(unittest.TestCase):
    """같은 텍스트라면 스트리밍과 비스트리밍이 같은 호출을 내야 한다."""

    def _both(self, text, name, args):
        info = parser().extract_tool_calls(text, REQ)
        self.assertTrue(info.tools_called, "비스트리밍이 호출을 못 찾았다")
        self.assertEqual(info.tool_calls[0].function.name, name)
        self.assertEqual(json.loads(info.tool_calls[0].function.arguments), args)

        _, calls, _ = run_stream(text)
        pairs = as_pairs(calls)
        self.assertEqual(len(pairs), 1, f"스트리밍 호출 수가 다르다: {pairs}")
        self.assertEqual(pairs[0][0], name)
        self.assertEqual(pairs[0][1], args)

    def test_xml_dialect(self):
        self._both(XML, "Read", {"file_path": "base_carter_run.py"})

    def test_json_inside_marker(self):
        # 회귀 지점: 예전 스트리밍 파서는 여기서 호출을 통째로 버렸다.
        self._both(JSON_IN_MARKER, "Read", {"file_path": "base_carter_run.py"})

    def test_json_without_marker(self):
        self._both(JSON_NO_MARKER, "Read", {"file_path": "base_carter_run.py"})

    def test_broken_closing_tag(self):
        # 모델이 </tool_call> 대신 <tool_call> 로 닫는 실측 케이스.
        self._both(BROKEN_CLOSER, "Read", {"file_path": "base_carter_run.py"})

    def test_func_key_dialect_from_the_real_prompt(self):
        # 회귀 지점: 실제 16k 프롬프트에서 모델이 쓰는 "func"/"tool" 형태.
        info = parser().extract_tool_calls(FUNC_KEY_DIALECT, REQ)
        self.assertTrue(info.tools_called, "비스트리밍이 호출을 못 찾았다")
        got = [(c.function.name, json.loads(c.function.arguments)) for c in info.tool_calls]
        self.assertEqual(got, [("Glob", {"pattern": "PROMPT.md"}),
                               ("Glob", {"pattern": "base_carter_run.py"})])

        _, calls, _ = run_stream(FUNC_KEY_DIALECT)
        self.assertEqual(as_pairs(calls), got, "스트리밍이 비스트리밍과 달라졌다")

    def test_toolname_input_dialect(self):
        # 2026-08-13 실측(같은 모델, 다른 실행) — 이번엔 키가 toolName/input 이었다.
        # 이 모델은 호출마다 키를 바꾼다. 별칭을 넓게 잡는 이유다.
        text = ('먼저 확인하겠습니다.\n\n<tool_call>\n'
                '[{"id":"c1","tool":{"input":{"file_path":"base_carter_run.py"},'
                '"toolName":"Read"}}]\n</tool_call>')
        info = parser().extract_tool_calls(text, REQ)
        self.assertTrue(info.tools_called)
        self.assertEqual(info.tool_calls[0].function.name, "Read")
        self.assertEqual(json.loads(info.tool_calls[0].function.arguments),
                         {"file_path": "base_carter_run.py"})
        _, calls, _ = run_stream(text)
        self.assertEqual(as_pairs(calls), [("Read", {"file_path": "base_carter_run.py"})])

    def test_func_key_dialect_keeps_the_prose(self):
        content, _, _ = run_stream(FUNC_KEY_DIALECT)
        self.assertIn("I'll help you modify", content)
        self.assertNotIn("<tool_call>", content, "도구 마크업이 답변으로 새면 안 된다")


class ContentIsPreserved(unittest.TestCase):
    def test_leading_prose_is_streamed(self):
        content, calls, _ = run_stream(JSON_IN_MARKER)
        self.assertIn("먼저 파일을 읽겠습니다", content)
        self.assertEqual(len(as_pairs(calls)), 1)

    def test_plain_answer_streams_completely(self):
        text = "NPU 는 4장이고 각 카드는 8 PE 입니다. 도구는 쓰지 않습니다."
        content, calls, _ = run_stream(text)
        self.assertEqual(content, text, "도구가 없는 답변은 한 글자도 잃으면 안 된다")
        self.assertEqual(calls, [])

    def test_prose_with_braces_is_not_mistaken_for_a_call(self):
        # 산문 속 중괄호를 호출로 오인하면 그 뒤 답변이 통째로 사라진다.
        text = 'JSON 예시는 {"a": 1} 이고, 그 뒤에도 설명이 이어집니다. 끝.'
        content, calls, _ = run_stream(text)
        self.assertEqual(content, text)
        self.assertEqual(calls, [])

    def test_code_block_with_name_key_but_no_call(self):
        text = '설정 파일은 {"name": "furio"} 형태입니다. 도구 호출이 아닙니다.'
        content, calls, _ = run_stream(text)
        self.assertEqual(content, text, "arguments 가 없으면 호출로 보지 않는다")
        self.assertEqual(calls, [])


class ServerStateContract(unittest.TestCase):
    """serving_chat 이 읽는 상태(prev_tool_call_arr)를 정확히 유지해야 한다."""

    def test_no_phantom_tool_turn_when_nothing_parsed(self):
        # 예전 구현은 이름 파싱 전에 빈 dict 를 넣어, 도구를 하나도 못 냈는데도 서버가
        # finish_reason='tool_calls' 를 붙였다 — 클라이언트엔 '도구 0개인 도구 턴'.
        _, calls, p = run_stream("<tool_call>\n이건 호출이 아니라 그냥 잘린 텍스트")
        self.assertEqual(calls, [])
        self.assertEqual(p.prev_tool_call_arr, [],
                         "파싱 못 한 채로 도구 턴을 선언하면 안 된다")

    def test_prev_tool_call_arr_holds_the_final_parse(self):
        _, _, p = run_stream(JSON_IN_MARKER)
        self.assertEqual(len(p.prev_tool_call_arr), 1)
        self.assertEqual(p.prev_tool_call_arr[0]["name"], "Read")
        self.assertEqual(p.prev_tool_call_arr[0]["arguments"],
                         {"file_path": "base_carter_run.py"})

    def test_streamed_args_match_what_was_emitted(self):
        _, calls, p = run_stream(XML)
        emitted = as_pairs(calls)[0][1]
        self.assertEqual(json.loads(p.streamed_args_for_tool[0]), emitted)


class Arguments(unittest.TestCase):
    def test_arguments_are_never_emitted_half_read(self):
        # 인자가 자라는 중에 절반만 나가면 클라이언트가 깨진 JSON 을 받는다.
        _, calls, _ = run_stream(JSON_IN_MARKER, chunk=3)
        for _, args in as_pairs(calls):
            self.assertNotIn("__unparsed__", args)

    def test_multiline_content_argument_survives(self):
        payload = "line1\nline2\nline3"
        text = ('<tool_call>\n<function=Write>\n'
                '<parameter=file_path>\nout.py\n</parameter>\n'
                f'<parameter=content>\n{payload}\n</parameter>\n</function>\n</tool_call>')
        _, calls, _ = run_stream(text)
        pairs = as_pairs(calls)
        self.assertEqual(pairs[0][0], "Write")
        self.assertEqual(pairs[0][1]["content"], payload)

    def test_chunking_does_not_change_the_result(self):
        want = {"file_path": "base_carter_run.py"}
        for chunk in (1, 2, 5, 13, 97):
            _, calls, _ = run_stream(JSON_IN_MARKER, chunk=chunk)
            pairs = as_pairs(calls)
            self.assertEqual(len(pairs), 1, f"chunk={chunk}")
            self.assertEqual(pairs[0][1], want, f"chunk={chunk}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
