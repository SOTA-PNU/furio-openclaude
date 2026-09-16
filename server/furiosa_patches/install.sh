#!/usr/bin/env bash
# furiosa-llm 에 qwen3_coder tool 파서를 설치(등록)한다.
# furiosa-llm 재설치/업그레이드 후 다시 실행하면 복구된다(멱등).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
TP=$(python3 -c "import furiosa_llm.server.tool_parsers as m, os; print(os.path.dirname(m.__file__))")
echo "[..] tool_parsers 폴더: $TP"

# 되돌림 방지. 2026-08-25 사고: 파서 수정이 머지됐는데 서버 체크아웃을 pull 하지 않은 채
# 이 스크립트를 돌려, 설치돼 있던 고친 파서(314줄)가 리포의 옛 사본(213줄)으로 덮였다.
# 증상은 조용했다 — 도구 호출이 다시 사라지고 모델이 혼잣말만 했다.
# 그래서 리포 사본이 설치본보다 오래됐으면 멈춘다(FURIO_FORCE_PARSER=1 로 무시 가능).
INSTALLED="$TP/qwen3_coder_tool_parser.py"
if [ -f "$INSTALLED" ] && [ "$HERE/qwen3_coder_tool_parser.py" -ot "$INSTALLED" ] \
   && ! cmp -s "$HERE/qwen3_coder_tool_parser.py" "$INSTALLED"; then
  echo "[stop] 리포 사본이 설치본보다 오래됐습니다 — 덮어쓰면 수정이 되돌아갑니다."
  echo "       리포: $(wc -l < "$HERE/qwen3_coder_tool_parser.py")줄  설치본: $(wc -l < "$INSTALLED")줄"
  echo "       먼저 'git -C ~/RNGD-proj pull' 로 최신화하세요."
  echo "       그래도 덮어쓰려면: FURIO_FORCE_PARSER=1 bash $0"
  [ "${FURIO_FORCE_PARSER:-0}" = "1" ] || exit 1
fi

cp "$HERE/qwen3_coder_tool_parser.py" "$TP/qwen3_coder_tool_parser.py"
echo "[ok] 파서 파일 복사 ($(wc -l < "$INSTALLED")줄)"

# __init__.py 에 import + __all__ 멱등 추가
python3 - "$TP/__init__.py" <<'PY'
import sys
p = sys.argv[1]
src = open(p).read()
imp = "from .qwen3_coder_tool_parser import Qwen3CoderToolParser"
if imp not in src:
    # 마지막 from .*_tool_parser import 줄 뒤에 삽입
    lines = src.splitlines()
    last = max(i for i, l in enumerate(lines) if l.startswith("from .") and "import" in l)
    lines.insert(last + 1, imp)
    src = "\n".join(lines) + ("\n" if not src.endswith("\n") else "")
    # __all__ 에도 추가
    src = src.replace('"OpenAIToolParser",', '"OpenAIToolParser",\n    "Qwen3CoderToolParser",', 1)
    open(p, "w").write(src)
    print("[ok] __init__.py 에 등록 추가")
else:
    print("[ok] 이미 등록돼 있음")
PY

# 검증
python3 -c "
import furiosa_llm.server.tool_parsers as m
ks = list(m.ToolParserManager.tool_parsers.keys())
assert 'qwen3_coder' in ks, f'등록 실패: {ks}'
print('[ok] 등록된 tool 파서:', ks)
"
