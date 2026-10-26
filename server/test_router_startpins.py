"""라우터 시작 때 핀 모델 올리기 — NPU 없이 돈다.

2026-10-26 20:08 사고: 라우터를 재시작하자 핀(gpt-oss-20b@dp4) 이 저절로 올라오지 않아 학생 서비스가 4분 끊겼다
(라우터는 시작 때 핀을 올리지 않았다 — 전에는 바깥 preload 가 바로 와서 가려졌다). 시작하면 ROUTER_PIN 순서대로
올린다(데몬 스레드, HTTP 는 바로 뜬다). 카드가 모자라면 로그만 — 아무것도 내리지 않는다.

furiosa-llm 실행(subprocess.Popen)·furiosa-smi 호출(subprocess.run) 경로는 전부 막는다.

실행:
  taskset -c 64-127 <venv>/bin/python -m pytest server/test_router_startpins.py -q
"""
import os
import sys
import tempfile
import time
import types

os.environ.setdefault("FURIO_LOGDIR", tempfile.mkdtemp(prefix="router-startpins-"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest

import furiosa_router as fr

PIN = "gpt-oss-20b@dp4"   # 수업 핀(카드 4장)
SMALL = "Qwen3-4B-FP8"    # 카드 1장짜리


class FakeProc:
    def poll(self):
        return None

    def terminate(self):
        pass

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


@pytest.fixture(autouse=True)
def no_npu(monkeypatch):
    def _forbidden(*a, **k):
        raise AssertionError("NPU 경로 호출 금지 — furiosa-llm/furiosa-smi 가 호출됐다")

    monkeypatch.setattr(fr.subprocess, "Popen", _forbidden)
    monkeypatch.setattr(fr.subprocess, "run", _forbidden)
    monkeypatch.setattr(fr, "_smi_read", lambda: {})
    monkeypatch.setattr(fr, "free_port", _forbidden)
    monkeypatch.setattr(fr, "wait_cards_free", lambda cards, timeout=0: True)
    monkeypatch.setattr(fr, "model_availability", lambda m: (True, None))
    with fr._SMI_LOCK:
        fr._SMI_CACHE["v"] = {}
    yield
    with fr._SMI_LOCK:
        fr._SMI_CACHE["v"] = {}


def cards_of(model_id):
    _base, tp, dp, pp = fr.parse_variant(model_id)
    return fr.variant_cards(tp, dp, pp)


def fake_router(monkeypatch, *, fail=()):
    """ensure 를 기록용으로 — 부른 순서를 적고, 비어 있는 카드 앞에서부터 차지한 '올라온' 백엔드를 running 에 넣는다."""
    r = fr.Router()
    calls = []

    def ensure(model_id):
        calls.append(model_id)
        if model_id in fail:
            r._fail(model_id, "모델 서버가 시작 중에 끝났습니다")
            raise RuntimeError("모델 서버가 시작 중에 끝났습니다")
        need = cards_of(model_id)
        free = r._free_cards()
        b = fr.Backend(model_id, 8410 + len(calls), FakeProc(), free[:need])
        b.ready = True
        b.ready_at = time.time()
        r.running[model_id] = b
        r.state[model_id] = "up"
        return b.port

    monkeypatch.setattr(r, "ensure", ensure)
    logs = []
    monkeypatch.setattr(r, "_log", logs.append)
    return r, calls, logs


# ── 순서 ──────────────────────────────────────────────────────────────────
def test_pin_order_keeps_env_order_without_duplicates():
    assert fr.pin_order("b,a,,b,c") == ["b", "a", "c"]
    assert fr.pin_order("") == []
    assert fr.pin_order(None) == []


def test_preloads_pins_in_order(monkeypatch):
    monkeypatch.setattr(fr, "PINNED", {SMALL, PIN})
    # SMALL(1장) 을 먼저 올리면 PIN(4장) 은 카드가 모자란다 — 순서대로 하고 모자란 것은 로그만
    r, calls, logs = fake_router(monkeypatch)
    out = fr.preload_pins_at_start(r, [SMALL, PIN])
    assert calls == [SMALL]
    assert out == {SMALL: "up", PIN: "no_cards"}
    assert any("카드" in line and PIN in line for line in logs)
    assert PIN not in r.state  # 'loading'·'error' 로 남기지 않는다(로그만)


def test_preloads_single_pin(monkeypatch):
    monkeypatch.setattr(fr, "PINNED", {PIN})
    r, calls, logs = fake_router(monkeypatch)
    assert fr.preload_pins_at_start(r, [PIN]) == {PIN: "up"}
    assert calls == [PIN]
    assert r.state[PIN] == "up"


def test_no_cards_logs_only_and_evicts_nothing(monkeypatch):
    """남(옛 라우터가 남긴 serve 등) 이 카드를 들고 있으면 올리지 않는다 — 떠 있는 비핀도 내리지 않는다."""
    monkeypatch.setattr(fr, "PINNED", {PIN})
    r, calls, logs = fake_router(monkeypatch)
    stopped = []
    monkeypatch.setattr(r, "_stop", lambda b: stopped.append(b.model_id))
    other = fr.Backend(SMALL, 8411, FakeProc(), [0])
    other.ready, other.ready_at = True, time.time() - 600
    r.running[SMALL] = other
    out = fr.preload_pins_at_start(r, [PIN])
    assert out == {PIN: "no_cards"} and calls == [] and stopped == []
    assert any("카드" in line for line in logs)


def test_reads_card_memory_fresh_before_checking(monkeypatch):
    """시작 직후 furiosa-smi 캐시는 비어 있어 남이 든 카드도 빈 것으로 보인다 — 먼저 새로 읽는다."""
    monkeypatch.setattr(fr, "PINNED", {PIN})
    reads = []

    def smi():
        reads.append(1)
        mem = {0: 30.0, 1: 0.0, 2: 0.0, 3: 0.0}  # 카드 0 을 남이 쓴다
        with fr._SMI_LOCK:
            fr._SMI_CACHE["v"] = dict(mem)
        return mem

    monkeypatch.setattr(fr, "_smi_read", smi)
    r, calls, logs = fake_router(monkeypatch)
    assert fr.preload_pins_at_start(r, [PIN]) == {PIN: "no_cards"}
    assert reads and calls == []


def test_skips_up_unknown_unpinned_and_unavailable(monkeypatch):
    monkeypatch.setattr(fr, "PINNED", {PIN, SMALL, "no-such-model", "Qwen3-8B-FP8"})
    r, calls, logs = fake_router(monkeypatch)
    up = fr.Backend(SMALL, 8411, FakeProc(), [0])
    up.ready, up.ready_at = True, time.time()
    r.running[SMALL] = up
    r.state[SMALL] = "up"
    monkeypatch.setattr(fr, "model_availability",
                        lambda m: (False, "파일 없음") if m == "Qwen3-8B-FP8" else (True, None))
    out = fr.preload_pins_at_start(r, [SMALL, "no-such-model", "Qwen3-8B-FP8", "Qwen3-32B-FP8", PIN])
    assert out == {SMALL: "up", "no-such-model": "unknown", "Qwen3-8B-FP8": "unavailable",
                   "Qwen3-32B-FP8": "unpinned", PIN: "no_cards"}
    assert calls == []


def test_failure_logged_and_next_continues(monkeypatch):
    monkeypatch.setattr(fr, "PINNED", {SMALL, "Qwen3-8B-FP8"})
    r, calls, logs = fake_router(monkeypatch, fail={SMALL})
    out = fr.preload_pins_at_start(r, [SMALL, "Qwen3-8B-FP8"])
    assert out == {SMALL: "failed", "Qwen3-8B-FP8": "up"}
    assert calls == [SMALL, "Qwen3-8B-FP8"]
    assert r.state[SMALL] == "error"
    assert any(SMALL in line and "실패" in line for line in logs)


def test_marks_loading_before_ensure(monkeypatch):
    """올리기 시작하면 /router/status 에 바로 'loading' — 게이트웨이·콘솔이 '올리는 중' 을 본다."""
    monkeypatch.setattr(fr, "PINNED", {PIN})
    r, calls, logs = fake_router(monkeypatch)
    seen = []
    inner = r.ensure

    def ensure(model_id):
        seen.append(r.status().get(model_id, {}).get("state"))
        return inner(model_id)

    monkeypatch.setattr(r, "ensure", ensure)
    fr.preload_pins_at_start(r, [PIN])
    assert seen == ["loading"]


# ── 시작 ──────────────────────────────────────────────────────────────────
def test_start_pin_preload_runs_in_daemon_thread(monkeypatch):
    monkeypatch.setenv("ROUTER_PIN", f"{PIN}")
    monkeypatch.setattr(fr, "PINNED", {PIN})
    got = []
    monkeypatch.setattr(fr, "preload_pins_at_start", lambda router=None, models=None: got.append(models))
    t = fr.start_pin_preload()
    assert t is not None and t.daemon
    t.join(timeout=5)
    assert got == [[PIN]]


def test_start_pin_preload_without_pins_starts_nothing(monkeypatch):
    monkeypatch.setenv("ROUTER_PIN", "")
    monkeypatch.setattr(fr, "PINNED", set())
    monkeypatch.setattr(fr, "preload_pins_at_start", lambda *a, **k: pytest.fail("핀이 없으면 부르지 않는다"))
    assert fr.start_pin_preload() is None


def test_serve_starts_pin_preload(monkeypatch, tmp_path):
    """`serve` 는 uvicorn 을 띄우기 전에 시작 핀 올리기를 건다(스레드 — HTTP 는 바로 뜬다)."""
    order = []
    monkeypatch.setattr(fr, "start_smi_refresher", lambda: order.append("smi"))
    monkeypatch.setattr(fr, "start_pin_preload", lambda router=None: order.append("pins"))
    monkeypatch.setattr(fr, "build_app", lambda: object())
    monkeypatch.setitem(sys.modules, "uvicorn", types.SimpleNamespace(run=lambda *a, **k: order.append("uvicorn")))
    monkeypatch.setattr(fr, "__file__", str(tmp_path / "furiosa_router.py"))  # .router.pid 를 임시 폴더에
    monkeypatch.setattr(sys, "argv", ["furiosa_router.py", "serve"])
    fr.main()
    assert order == ["smi", "pins", "uvicorn"]
