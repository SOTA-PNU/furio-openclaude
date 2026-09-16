#!/usr/bin/env python3
"""
furio 목(mock) 라우터 — NPU 서버 없이 클라이언트 기능만 테스트한다.

서버에 접속할 수 없는 개인 PC 에서 openclaude 포크에 넣은 기능
(모델별 LED·dp/pp 위젯·모델 설명·Shift+Tab 자동모드)을 그대로 확인하려고 만들었다.
NPU 추론은 하지 않는다 — 채팅 응답은 고정 문구다.

실제 라우터(coding-agent/furiosa_router.py)와 같은 엔드포인트를 같은 모양으로 낸다:
  GET  /v1/models        모델 목록(기본 + dp/pp 변형)
  GET  /router/models    설명·tp/dp/pp·카드 수 (설치 시 desc.json 으로 저장됨)
  GET  /router/status    모델별 상태 — LED 의 데이터 원천
  POST /v1/chat/completions   고정 응답(스트리밍 지원)

상태는 시간 기반으로 진짜처럼 움직인다: 어떤 모델로 첫 요청이 오면 loading 이 되고
LOAD_SECONDS 뒤 up 이 된다. 카드가 모자라면 LRU 로 내려서(stopping) 자리를 만든다.
→ 노랑 깜빡임 → 초록 전환, 여러 모델 동시 표시가 실제와 같은 순서로 보인다.

실행:
  python3 mock-router.py                 # :8400
  python3 mock-router.py --port 8400 --load-seconds 8
  (표준 라이브러리만 사용 — 설치할 것 없음. macOS 기본 python3 로 동작.)

그 다음 다른 터미널에서:
  SDI_SERVER=http://127.0.0.1:8400 bash install.sh
  furio
"""
import argparse
import hashlib
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CARDS = 4

# 실제 REGISTRY 를 축약한 표(같은 필드 모양). tp·ctx 는 2026-07-22 서버 실측값 그대로라
# 컨텍스트 한도까지 진짜와 동일하게 테스트된다.
MODELS = [
    # (id, tp, cards, ctx, artifact, tools)
    ("gpt-oss-120b",                     32, 4, 131072, "fxb", "ok"),
    ("Solar-Open-100B-NVFP4A16",         32, 4, 131072, "fxb", "ok"),
    ("Qwen3-32B-FP8",                    32, 4,  40960, "v2",  "ok"),
    ("Llama-3.3-70B-Instruct",           32, 4, 131072, "v2",  "ok"),
    ("K-EXAONE-236B-A23B-NVFP4A16",      32, 4, 262144, "fxb", "weak"),
    ("Qwen3-Coder-30B-A3B-Instruct-FP8", 32, 4, 262144, "fxb", "no"),
    ("Qwen3-Coder-30B-A3B-Instruct",      8, 2, 262144, "v2",  "no"),   # bf16 57GB → pp2~4(1장 초과)
    ("Qwen3-30B-A3B-FP8",                32, 4,  40960, "fxb", "weak"),
    ("Llama-3.1-8B-Instruct",             8, 1, 131072, "v2",  "weak"),
    ("Qwen3-8B-FP8",                      8, 1,  40960, "fxb", "weak"),
    ("Qwen3-4B-FP8",                      8, 1,  40960, "fxb", "weak"),
    ("Qwen2.5-0.5B-Instruct",             4, 1,  32768, "v2",  "weak"),
]
REG = {m[0]: dict(tp=m[1], cards=m[2], ctx=m[3], art=m[4], tools=m[5]) for m in MODELS}
NAME_HINT = {"ok": "", "weak": "  [tools~weak]", "no": "  [chat-only]"}
# tp8 대체 빌드가 있는 모델(실제 라우터의 reg["tps"] 축약). tp8 은 로컬 v2 → pp 허용.
TP8 = {"Qwen3-32B-FP8", "Qwen3-Coder-30B-A3B-Instruct-FP8", "Qwen3-30B-A3B-FP8"}
# pp1 로는 못 뜨는(1장 초과) 모델의 pp 선택지. 첫값이 기본 pp(맨이름이 뜻하는 pp).
PP_OPTS = {"Qwen3-Coder-30B-A3B-Instruct": [2, 3, 4]}


def tp_choices(base):
    return sorted({REG[base]["tp"]} | ({8} if base in TP8 else set()))


def pp_default(base):
    return PP_OPTS[base][0] if base in PP_OPTS else 1


def cards_base(tp):
    return 1 if tp <= 8 else (tp + 7) // 8


def variant_cards(tp, dp, pp):
    slots = dp * pp
    if tp < 8:
        per_card = 8 // tp
        return (slots + per_card - 1) // per_card
    return slots * cards_base(tp)


def art_of(base, tp):
    # tp8 대체 빌드는 로컬 v2. 기본 빌드는 표의 art 그대로.
    return "v2" if (tp != REG[base]["tp"] and tp in tp_choices(base)) else REG[base]["art"]


def par_choices(base, tp):
    """주어진 tp 에서 (dp, pp) 선택지. tp32(4장 독점)면 ([1],[1]). pp 는 v2 에서만."""
    if cards_base(tp) >= CARDS:
        return [1], [1]
    if base in PP_OPTS:                       # 1장 초과 모델 — 지정 pp + 최소 pp 기준 dp
        ppmin = PP_OPTS[base][0]
        pp = [n for n in PP_OPTS[base] if variant_cards(tp, 1, n) <= CARDS]
        dp = [n for n in (1, 2, 4) if variant_cards(tp, n, ppmin) <= CARDS]
        return dp, pp
    dp = [n for n in (1, 2, 4) if variant_cards(tp, n, 1) <= CARDS]
    pp = [1] if art_of(base, tp) == "fxb" else [n for n in (1, 2, 4) if variant_cards(tp, 1, n) <= CARDS]
    return dp, pp


def parse_variant(mid):
    base, tp, dp, pp = mid, None, 1, None
    while "@" in base:
        head, _, tag = base.rpartition("@")
        m = re.fullmatch(r"(tp|dp|pp)(\d+)", tag)
        if not m:
            return mid, None, 1, 1
        base, v = head, int(m.group(2))
        if m.group(1) == "tp":
            tp = v
        elif m.group(1) == "dp":
            dp = v
        else:
            pp = v
    if tp is None:
        tp = REG[base]["tp"] if base in REG else 8
    if pp is None:
        pp = pp_default(base) if base in REG else 1
    return base, tp, dp, pp


def variant_id(base, tp, dp, pp):
    sfx = (f"@tp{tp}" if tp != REG[base]["tp"] else "") + (f"@dp{dp}" if dp > 1 else "") + (f"@pp{pp}" if pp != pp_default(base) else "")
    return base + sfx


def all_ids():
    out = []
    for base in REG:
        out.append(base)
        default_tp = REG[base]["tp"]
        for tp in tp_choices(base):
            dps, pps = par_choices(base, tp)
            for dp in dps:
                for pp in pps:
                    if tp == default_tp and dp == 1 and pp == pp_default(base):
                        continue
                    if variant_cards(tp, dp, pp) > CARDS:
                        continue
                    out.append(variant_id(base, tp, dp, pp))
    return out


def describe(mid):
    base, tp, dp, pp = parse_variant(mid)
    ctx = REG[base]["ctx"]
    return (f"tp{tp}·dp{dp}·pp{pp} · {variant_cards(tp, dp, pp)}장 · "
            f"ctx {ctx // 1024}k · {art_of(base, tp)}")


class Fleet:
    """카드 점유·상태를 시간 기반으로 흉내 낸다(실제 라우터의 lazy serving + LRU)."""

    def __init__(self, load_seconds, unload_seconds):
        self.load_s = load_seconds
        self.unload_s = unload_seconds
        self.lock = threading.Lock()
        self.run = {}   # mid -> dict(cards=[..], t0=..., state=..., stop_t0=...)

    def _now_state(self, e):
        if e["state"] == "stopping":
            return "stopping" if time.time() - e["stop_t0"] < self.unload_s else "gone"
        return "loading" if time.time() - e["t0"] < self.load_s else "up"

    def _reap(self):
        for mid in [m for m, e in self.run.items() if self._now_state(e) == "gone"]:
            del self.run[mid]

    def _held(self):
        return sum(len(e["cards"]) for e in self.run.values())

    def request(self, mid):
        """이 모델을 서빙하도록 보장(비동기 — 즉시 반환하고 상태만 바꾼다)."""
        base, tp, dp, pp = parse_variant(mid)
        if base not in REG:
            return False
        need = variant_cards(tp, dp, pp)
        with self.lock:
            self._reap()
            if mid in self.run:
                if self.run[mid]["state"] != "stopping":
                    return True
                del self.run[mid]
            # 카드가 모자라면 오래된 것부터 내린다(LRU) → LED 가 노랑으로 보인다
            while self._held() + need > CARDS:
                victim = min((m for m, e in self.run.items() if e["state"] != "stopping"),
                             key=lambda m: self.run[m]["t0"], default=None)
                if victim is None:
                    break
                self.run[victim]["state"] = "stopping"
                self.run[victim]["stop_t0"] = time.time()
                self.run[victim]["cards"] = []
            used = {c for e in self.run.values() for c in e["cards"]}
            free = [c for c in range(CARDS) if c not in used][:need]
            self.run[mid] = dict(cards=free, t0=time.time(), state="loading", stop_t0=0.0)
            return True

    def status(self):
        with self.lock:
            self._reap()
            out = {}
            for mid, e in self.run.items():
                st = self._now_state(e)
                _, tp, dp, pp = parse_variant(mid)
                out[mid] = dict(port=8410, cards=e["cards"], alive=True,
                                tp=tp, dp=dp, pp=pp, state=st,
                                idle_s=round(time.time() - e["t0"], 1))
            used = {c for e in self.run.values() for c in e["cards"]}
            return out, [c for c in range(CARDS) if c not in used]


FLEET = None
CLIENT_DIST = None   # --client-dist: 빌드한 포크 dist 경로(있으면 install.sh 로 배포)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass   # 조용히 — 터미널을 어지럽히지 않는다

    def _send(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/v1/models":
            return self._send({"object": "list",
                               "data": [{"id": m, "object": "model", "owned_by": "furiosa-npu-mock"}
                                        for m in all_ids()]})
        if self.path == "/router/models":
            data = []
            for mid in all_ids():
                base, tp, dp, pp = parse_variant(mid)
                r = REG[base]
                dps, pps = par_choices(base, tp)
                data.append({"id": mid, "base": base,
                             "name": mid + NAME_HINT.get(r["tools"], ""),
                             "description": describe(mid), "context": r["ctx"],
                             "kind": "chat", "tp": tp, "tp_default": r["tp"],
                             "tp_choices": tp_choices(base), "dp": dp, "pp": pp,
                             "pp_default": pp_default(base),
                             "cards": variant_cards(tp, dp, pp),
                             "dp_choices": dps, "pp_choices": pps,
                             "artifact": art_of(base, tp), "tools": r["tools"]})
            return self._send({"data": data})
        if self.path == "/router/status":
            running, free = FLEET.status()
            return self._send({"running": running, "free_cards": free})
        if self.path == "/router/client/manifest.json":
            # --client-dist 로 빌드한 포크 dist 를 주면 install.sh 가 그걸 받아 덮어쓴다.
            # 안 주면 ok:false → install.sh 가 업스트림 npm 으로 안전하게 폴백한다
            # (그 경우 NPU LED·dp/pp 위젯은 안 보인다).
            if not CLIENT_DIST:
                return self._send({"ok": False, "version": "", "files": {},
                                   "note": "--client-dist 로 포크 빌드 경로를 주면 배포합니다"})
            files, ver = {}, ""
            try:
                with open(os.path.join(os.path.dirname(CLIENT_DIST.rstrip("/")), "package.json")) as f:
                    ver = json.load(f).get("version", "")
            except Exception:
                pass
            for name in ("cli.mjs", "sdk.mjs"):
                p = os.path.join(CLIENT_DIST, name)
                if os.path.isfile(p):
                    h = hashlib.sha256()
                    with open(p, "rb") as f:
                        for c in iter(lambda: f.read(1 << 20), b""):
                            h.update(c)
                    files[name] = {"sha256": h.hexdigest(), "bytes": os.path.getsize(p)}
            return self._send({"ok": bool(ver and files), "version": ver, "files": files})
        if self.path.startswith("/router/client/"):
            name = self.path.rsplit("/", 1)[-1]
            p = os.path.join(CLIENT_DIST or "", name)
            if name not in ("cli.mjs", "sdk.mjs") or not CLIENT_DIST or not os.path.isfile(p):
                return self._send({"error": {"message": f"not found: {name}"}}, 404)
            body = open(p, "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        return self._send({"error": {"message": f"not found: {self.path}"}}, 404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._send({"error": {"message": "invalid JSON"}}, 400)
        if self.path == "/router/preload":
            mid = payload.get("model") or ""
            if parse_variant(mid)[0] not in REG:
                return self._send({"error": {"message": f"unknown model '{mid}'"}}, 404)
            FLEET.request(mid)      # 실제 라우터처럼 즉시 로딩 시작
            return self._send({"ok": True, "model": mid})
        if not self.path.startswith("/v1/chat/completions"):
            return self._send({"error": {"message": f"not found: {self.path}"}}, 404)
        mid = payload.get("model") or ""
        if parse_variant(mid)[0] not in REG:
            return self._send({"error": {"message": f"unknown model '{mid}'"}}, 404)
        FLEET.request(mid)
        text = ("[mock] NPU 없이 도는 목 라우터입니다. LED·dp/pp·모델 설명 확인용이라 "
                "추론은 하지 않고 이 문구만 돌려줍니다.")
        if payload.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            base = {"id": "chatcmpl-mock", "object": "chat.completion.chunk",
                    "created": int(time.time()), "model": mid}
            def chunk(delta, finish=None):
                o = dict(base, choices=[{"index": 0, "delta": delta, "finish_reason": finish}])
                self.wfile.write(b"data: " + json.dumps(o, ensure_ascii=False).encode() + b"\n\n")
                self.wfile.flush()
            chunk({"role": "assistant"})
            chunk({"content": text})
            chunk({}, finish="stop")
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return
        return self._send({
            "id": "chatcmpl-mock", "object": "chat.completion",
            "created": int(time.time()), "model": mid,
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": text, "tool_calls": []}}],
            "usage": {"prompt_tokens": 8, "completion_tokens": 24, "total_tokens": 32},
        })


def main():
    global FLEET
    ap = argparse.ArgumentParser(description="furio 목 라우터 (NPU 없이 클라이언트 기능 테스트)")
    ap.add_argument("--port", type=int, default=8400)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--load-seconds", type=float, default=8.0,
                    help="모델이 '올라가는' 데 걸리는 시간(노랑 LED 지속) — 기본 8초")
    ap.add_argument("--unload-seconds", type=float, default=3.0,
                    help="LRU 로 내려가는 데 걸리는 시간(노랑 LED) — 기본 3초")
    ap.add_argument("--client-dist", default=None,
                    help="빌드한 openclaude 포크의 dist/ 경로. 주면 install.sh 가 이걸 받아 "
                         "NPU 기능이 들어간 클라이언트를 설치한다.")
    a = ap.parse_args()
    global CLIENT_DIST
    CLIENT_DIST = a.client_dist
    FLEET = Fleet(a.load_seconds, a.unload_seconds)
    ids = all_ids()
    print(f"[mock] http://{a.host}:{a.port}  모델 {len(ids)}개"
          f"(기본 {len(REG)} + dp/pp 변형 {len(ids) - len(REG)})  카드 {CARDS}장")
    print(f"[mock] 로딩 {a.load_seconds}s / 언로딩 {a.unload_seconds}s 로 상태를 흉내 냅니다.")
    print(f"[mock] 설치:  SDI_SERVER=http://{a.host}:{a.port} bash install.sh")
    print("[mock] 종료: Ctrl-C")
    try:
        ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\n[mock] 종료")


if __name__ == "__main__":
    main()
