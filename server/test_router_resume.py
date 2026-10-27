"""콘솔의 라우터 다시 시작은 누르기 직전 상태로 돌아온다. NPU 없이 돈다.

POST /router/restart 는 재시작 스크립트를 띄우기 전에 그 순간의 핀(실행 중에 /router/pin 으로 바꾼 것)과 올라와 있거나
올리는 중인 모델(올린 순서)을 다시 시작 기록(LOGDIR/router-resume.json)에 적는다. 새 라우터는 시작할 때 한 번 읽고 지운다:
10분 안이면 ROUTER_PIN 대신 그 핀, 시작 핀 올리기는 ROUTER_PIN 대신 그 모델들(비었으면 아무것도 올리지 않는다).
오래됐거나 읽지 못하면 로그만 남기고 지운 뒤 예전처럼. 기록이 없으면(손으로 serve-router.sh) 예전처럼 ROUTER_PIN.

furiosa-llm 실행·furiosa-smi 호출은 막고, 재시작 spawn·uvicorn 은 가짜로 바꾼다. 기록 파일은 시험마다 임시 폴더에.

실행:
  taskset -c 64-127 <venv>/bin/python -m pytest server/test_router_resume.py -q
"""
import json
import os
import sys
import tempfile
import threading
import time
import types

os.environ.setdefault("FURIO_LOGDIR", tempfile.mkdtemp(prefix="router-resume-"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest

import furiosa_router as fr

PIN = "gpt-oss-20b@dp4"       # .env 의 ROUTER_PIN(카드 4장)
FIRST = "Qwen3-8B-FP8"        # 먼저 올린 모델(카드 1장)
SECOND = "Qwen3-4B-FP8"       # 나중에 올린 모델(카드 1장)
QUEUED = "Llama-3.1-8B-Instruct"   # 줄 서 있던 모델(카드 1장)
EMB = "Qwen3-Embedding-8B"    # 채팅 모델 아님(카드 1장)


class FakeProc:
    def __init__(self, dead=False):
        self.dead = dead
        self.pid = 4242

    def poll(self):
        return 0 if self.dead else None

    def terminate(self):
        pass

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


def _no_sleep(seconds):
    raise AssertionError(f"실제로 {seconds}초 기다리면 안 된다. 시험은 가짜 시계로")


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    """NPU·진짜 재시작 길을 막고, 로그 폴더·다시 시작 기록을 임시 폴더로. 라우터는 ROUTER_PIN=PIN 으로 막 시작한 모습."""

    def _forbidden(*a, **k):
        raise AssertionError("NPU 경로·실제 프로세스 실행 금지")

    def _no_spawn(*a, **k):
        raise AssertionError("재시작 spawn 은 시험마다 가짜로 바꿔야 한다")

    monkeypatch.setattr(fr.subprocess, "Popen", _forbidden)
    monkeypatch.setattr(fr.subprocess, "run", _forbidden)
    monkeypatch.setattr(fr, "_smi_read", lambda: {})
    monkeypatch.setattr(fr, "free_port", _forbidden)
    monkeypatch.setattr(fr, "wait_cards_free", lambda cards, timeout=0: True)
    monkeypatch.setattr(fr, "model_availability", lambda m: (True, None))
    monkeypatch.setattr(fr, "PIN_START_CARD_WAIT", 0)
    monkeypatch.setattr(fr, "_sleep", _no_sleep)
    monkeypatch.setattr(fr, "spawn_restart", _no_spawn)
    logdir = tmp_path / "logs"
    logdir.mkdir()
    monkeypatch.setattr(fr, "LOGDIR", str(logdir))
    monkeypatch.setattr(fr, "ROUTER_LOG", str(logdir / "router.log"))
    monkeypatch.setattr(fr, "RESTART_LOG", str(logdir / "restart.log"))
    monkeypatch.setattr(fr, "RESUME_FILE", str(logdir / "router-resume.json"))
    monkeypatch.setattr(fr, "_RESTART", {"at": None})
    monkeypatch.setattr(fr, "ROUTER", fr.Router())
    monkeypatch.setenv("ROUTER_PIN", PIN)
    monkeypatch.setattr(fr, "PINNED", {PIN})
    monkeypatch.delenv("SDI_API_KEY", raising=False)
    monkeypatch.delenv("FURIOSA_API_KEY", raising=False)
    with fr._SMI_LOCK:
        fr._SMI_CACHE["v"] = {}
    yield logdir
    with fr._SMI_LOCK:
        fr._SMI_CACHE["v"] = {}


def client():
    from fastapi.testclient import TestClient
    return TestClient(fr.build_app())


def backend(model_id, cards, *, ready=True, dead=False):
    b = fr.Backend(model_id, 8410 + cards[0], FakeProc(dead=dead), list(cards))
    b.ready = ready
    b.ready_at = time.time() - 60 if ready else None
    return b


def cards_of(model_id):
    _base, tp, dp, pp = fr.parse_variant(model_id)
    return fr.variant_cards(tp, dp, pp)


def fake_router(monkeypatch):
    """ensure 를 기록용으로: 부른 순서를 적고, 빈 카드 앞에서부터 차지한 '올라온' 백엔드를 running 에 넣는다."""
    r = fr.Router()
    calls = []

    def ensure(model_id):
        calls.append(model_id)
        free = r._free_cards()
        b = backend(model_id, free[:cards_of(model_id)])
        r.running[model_id] = b
        r.state[model_id] = "up"
        return b.port

    monkeypatch.setattr(r, "ensure", ensure)
    logs = []
    monkeypatch.setattr(r, "_log", logs.append)
    return r, calls, logs


def write_record(logdir, *, pinned, up, age=0.0):
    rec = {"at": time.time() - age, "pinned": pinned, "up": up, "reason": "FURIO console: admin@example.com"}
    (logdir / "router-resume.json").write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")


def run_serve(monkeypatch, tmp_path, start_pin_preload):
    """`serve` 를 uvicorn·smi 없이 한 번 돈다(라우터가 막 시작한 순간). start_pin_preload 는 주어진 것으로."""
    monkeypatch.setattr(fr, "start_smi_refresher", lambda: None)
    monkeypatch.setattr(fr, "start_pin_preload", start_pin_preload)
    monkeypatch.setattr(fr, "build_app", lambda: object())
    monkeypatch.setitem(sys.modules, "uvicorn", types.SimpleNamespace(run=lambda *a, **k: None))
    monkeypatch.setattr(fr, "__file__", str(tmp_path / "furiosa_router.py"))   # .router.pid 를 임시 폴더에
    monkeypatch.setattr(sys, "argv", ["furiosa_router.py", "serve"])
    fr.main()


# ── 다시 시작 기록 쓰기(옛 라우터) ─────────────────────────────────────────────
def test_restart_records_runtime_pins_and_up_models_before_spawning(monkeypatch, isolate):
    """누르는 순간의 핀(ROUTER_PIN 이 아니라 /router/pin 으로 바꾼 것)과 올라왔거나 올리는 중인 모델을 올린 순서대로 적는다.
    내리는 중·실패·죽은 백엔드는 적지 않는다. 재시작 스크립트를 띄울 때 기록은 이미 다 쓰여 있다."""
    r = fr.ROUTER
    r.running[FIRST] = backend(FIRST, [0])
    r.state[FIRST] = "up"
    r.running[SECOND] = backend(SECOND, [1], ready=False)     # serve 는 떴고 준비를 기다리는 중
    r.state[SECOND] = "loading"
    r.running["Qwen3-32B-FP8@tp8"] = backend("Qwen3-32B-FP8@tp8", [2], dead=True)   # 라우터 몰래 죽은 백엔드
    r.state["Qwen3-32B-FP8@tp8"] = "up"
    r.running[EMB] = backend(EMB, [3])
    r.state[EMB] = "stopping"                                  # 내리는 중
    r.state[QUEUED] = "loading"                                # 다른 모델 적재 뒤에 줄 서 있음
    r.state["Qwen3-30B-A3B-FP8"] = "error"
    seen = []

    def spawn(**kw):
        seen.append(json.loads((isolate / "router-resume.json").read_text(encoding="utf-8")))
        return FakeProc()

    monkeypatch.setattr(fr, "spawn_restart", spawn)
    c = client()
    assert c.post("/router/pin", json={"models": [FIRST, SECOND]}).json() == \
        {"ok": True, "pinned": ["Qwen3-4B-FP8", "Qwen3-8B-FP8"]}
    t0 = time.time()

    res = c.post("/router/restart", json={"reason": "콘솔\n다시 시작"})

    assert res.status_code == 202
    assert len(seen) == 1
    rec = json.loads((isolate / "router-resume.json").read_text(encoding="utf-8"))
    assert rec == seen[0]
    assert rec["pinned"] == ["Qwen3-4B-FP8", "Qwen3-8B-FP8"]
    assert rec["up"] == ["Qwen3-8B-FP8", "Qwen3-4B-FP8", "Llama-3.1-8B-Instruct"]
    assert rec["reason"] == "콘솔 다시 시작"
    assert t0 - 1 <= rec["at"] <= time.time() + 1
    assert sorted(os.listdir(isolate)) == ["restart.log", "router-resume.json"]   # 임시 파일이 남지 않는다


def test_restart_records_nothing_up_when_the_admin_took_the_pin_down(monkeypatch, isolate):
    """관리자가 핀 모델을 내려 둔 채(핀은 그대로) 누르면 up 은 빈 목록이다."""
    monkeypatch.setattr(fr, "spawn_restart", lambda **kw: FakeProc())
    assert client().post("/router/restart", json={}).status_code == 202
    rec = json.loads((isolate / "router-resume.json").read_text(encoding="utf-8"))
    assert (rec["pinned"], rec["up"]) == (["gpt-oss-20b@dp4"], [])


def test_restart_during_the_start_delay_keeps_the_models_about_to_come_up(monkeypatch, isolate):
    """새 라우터가 시작 핀 올리기를 기다리는 사이(콘솔에는 아직 내려 있다) 또 다시 시작해도 올리려던 모델을 잃지 않는다.
    그사이 콘솔이 핀에서 뺀 모델은 시작 핀 올리기도 건너뛸 것이라 적지 않는다. 다 올린 뒤에는 떠 있는 것만 적는다."""
    r, calls, logs = fake_router(monkeypatch)
    monkeypatch.setattr(fr, "ROUTER", r)
    fr.set_pins([FIRST, SECOND])
    gate = threading.Event()
    monkeypatch.setattr(fr, "_sleep", lambda seconds: gate.wait(5))
    t = fr.start_pin_preload(models=[FIRST, SECOND, QUEUED], restore_pins=[FIRST, SECOND])   # QUEUED 는 핀이 아니던 모델
    fr.set_pins([FIRST])                                       # 기다리는 사이 콘솔이 SECOND 를 핀에서 뺐다
    monkeypatch.setattr(fr, "spawn_restart", lambda **kw: FakeProc())
    assert client().post("/router/restart", json={}).status_code == 202
    rec = json.loads((isolate / "router-resume.json").read_text(encoding="utf-8"))
    assert (rec["pinned"], rec["up"]) == (["Qwen3-8B-FP8"], ["Qwen3-8B-FP8", "Llama-3.1-8B-Instruct"])
    gate.set()
    t.join(timeout=5)
    assert calls == ["Qwen3-8B-FP8", "Llama-3.1-8B-Instruct"]
    r.running.pop(QUEUED)                                      # 다 올린 뒤 관리자가 내렸다
    r.state.pop(QUEUED)
    assert fr.save_resume("again")["up"] == ["Qwen3-8B-FP8"]


def test_spawn_failure_drops_the_record(monkeypatch, isolate):
    """재시작 스크립트를 띄우지 못했으면 다시 시작은 일어나지 않았다. 나중에 손으로 띄운 라우터가 이 기록을 읽지 않게 지운다."""
    existed = []

    def boom(**kw):
        existed.append((isolate / "router-resume.json").exists())
        raise OSError("no bash")

    monkeypatch.setattr(fr, "spawn_restart", boom)
    assert client().post("/router/restart", json={}).status_code == 202
    assert existed == [True]                                   # 띄우려 할 때는 있었다
    assert not (isolate / "router-resume.json").exists()
    assert fr._RESTART["at"] is None


def test_restart_goes_on_when_the_record_cannot_be_written(monkeypatch, isolate, capsys):
    """기록을 못 써도 재시작은 한다(새 라우터는 예전처럼 ROUTER_PIN). 로그 한 줄."""
    (isolate / "not-a-dir").write_text("x")
    monkeypatch.setattr(fr, "RESUME_FILE", str(isolate / "not-a-dir" / "router-resume.json"))
    spawned = []
    monkeypatch.setattr(fr, "spawn_restart", lambda **kw: spawned.append(kw) or FakeProc())
    assert client().post("/router/restart", json={}).status_code == 202
    assert len(spawned) == 1
    assert "다시 시작 기록을 쓰지 못했습니다" in capsys.readouterr().out


# ── 시작(새 라우터) ────────────────────────────────────────────────────────────
def test_start_with_a_fresh_record_restores_pins_and_loads_the_recorded_models(monkeypatch, isolate, tmp_path, capsys):
    write_record(isolate, pinned=[SECOND], up=[FIRST, SECOND], age=590)
    calls = []
    run_serve(monkeypatch, tmp_path, lambda router=None, models=None, delay=None, restore_pins=None:
              calls.append((models, restore_pins)))
    assert fr.PINNED == {"Qwen3-4B-FP8"}                       # ROUTER_PIN(gpt-oss-20b@dp4) 이 아니라 기록의 핀
    assert calls == [(["Qwen3-8B-FP8", "Qwen3-4B-FP8"], ["Qwen3-4B-FP8"])]
    assert not (isolate / "router-resume.json").exists()      # 한 번 읽고 지운다
    out = capsys.readouterr().out
    assert "다시 시작 전 상태로: 핀 ['Qwen3-4B-FP8'], 올림 ['Qwen3-8B-FP8', 'Qwen3-4B-FP8']" in out
    assert "수업 모드: pin=['Qwen3-4B-FP8']" in out


def test_start_with_a_record_of_nothing_up_loads_nothing(monkeypatch, isolate, tmp_path, capsys):
    """관리자가 내려 둔 모델이 다시 시작 뒤 ROUTER_PIN 으로 다시 올라오던 문제: 기록의 up 이 비었으면 아무것도 올리지 않는다."""
    write_record(isolate, pinned=[PIN], up=[])
    monkeypatch.setattr(fr, "preload_pins_at_start", lambda *a, **k: pytest.fail("올릴 모델이 없으면 부르지 않는다"))
    real = fr.start_pin_preload
    threads = []
    run_serve(monkeypatch, tmp_path, lambda *a, **k: threads.append(real(*a, **k)))
    assert threads == [None]                                   # 시작 핀 올리기 스레드가 없다
    assert fr.PINNED == {"gpt-oss-20b@dp4"}                    # 핀은 그대로(내려 둔 채)
    assert "다시 시작 전 상태로: 핀 ['gpt-oss-20b@dp4'], 올림 []" in capsys.readouterr().out


def test_start_without_a_record_uses_ROUTER_PIN_as_before(monkeypatch, isolate, tmp_path, capsys):
    """손으로 serve-router.sh 를 띄우면 기록이 없다. 예전 그대로 start_pin_preload() 가 ROUTER_PIN 을 올린다."""
    calls = []
    run_serve(monkeypatch, tmp_path, lambda *a, **k: calls.append((a, k)))
    assert calls == [((), {})]
    assert fr.PINNED == {"gpt-oss-20b@dp4"}
    assert "다시 시작" not in capsys.readouterr().out


def test_stale_record_is_ignored_deleted_and_logged(monkeypatch, isolate, tmp_path, capsys):
    write_record(isolate, pinned=[SECOND], up=[SECOND], age=601)
    calls = []
    run_serve(monkeypatch, tmp_path, lambda *a, **k: calls.append((a, k)))
    assert calls == [((), {})]                                 # 예전처럼 ROUTER_PIN
    assert fr.PINNED == {"gpt-oss-20b@dp4"}
    assert not (isolate / "router-resume.json").exists()
    out = capsys.readouterr().out
    assert "다시 시작 기록이 오래돼" in out and "다시 시작 전 상태로" not in out


@pytest.mark.parametrize("make", [
    lambda at: b"not json",
    lambda at: b"\xff\xfe\x00broken",
    lambda at: b"[]",
    lambda at: json.dumps({"pinned": [], "up": []}).encode(),
    lambda at: json.dumps({"at": "now", "pinned": [], "up": []}).encode(),
    lambda at: json.dumps({"at": True, "pinned": [], "up": []}).encode(),
    lambda at: json.dumps({"at": at, "up": []}).encode(),
    lambda at: json.dumps({"at": at, "pinned": "Qwen3-4B-FP8", "up": []}).encode(),
    lambda at: json.dumps({"at": at, "pinned": [], "up": ["Qwen3-4B-FP8", 1]}).encode(),
    lambda at: json.dumps({"at": at, "pinned": [""], "up": []}).encode(),
    lambda at: b'{"at": NaN, "pinned": [], "up": []}',
], ids=["text", "bytes", "list", "no-at", "at-str", "at-bool", "no-pinned", "pinned-str", "up-int",
        "pinned-empty-name", "at-nan"])
def test_unreadable_record_is_ignored_deleted_and_logged(isolate, capsys, make):
    p = isolate / "router-resume.json"
    p.write_bytes(make(time.time()))
    assert fr.take_resume() is None
    assert not p.exists()
    assert "다시 시작 기록을 읽지 못해" in capsys.readouterr().out


def test_record_round_trip_from_restart_to_the_next_start(monkeypatch, isolate, tmp_path, capsys):
    """옛 라우터가 누를 때 적은 것을 새 라우터가 그대로 쓴다(콘솔이 바꾼 핀, 그때 떠 있던 모델)."""
    r = fr.ROUTER
    r.running[SECOND] = backend(SECOND, [0])
    r.state[SECOND] = "up"
    monkeypatch.setattr(fr, "spawn_restart", lambda **kw: FakeProc())
    c = client()
    c.post("/router/pin", json={"models": [SECOND]})
    assert c.post("/router/restart", json={}).status_code == 202
    # 새 프로세스: ROUTER_PIN 으로 import 된 모습
    monkeypatch.setattr(fr, "PINNED", {PIN})
    monkeypatch.setattr(fr, "ROUTER", fr.Router())
    calls = []
    run_serve(monkeypatch, tmp_path, lambda router=None, models=None, delay=None, restore_pins=None:
              calls.append((models, restore_pins)))
    assert fr.PINNED == {"Qwen3-4B-FP8"}
    assert calls == [(["Qwen3-4B-FP8"], ["Qwen3-4B-FP8"])]


# ── 시작 때 올리기(같은 길, 같은 지연) ───────────────────────────────────────────
def test_restore_load_brings_up_the_recorded_models_in_order_after_the_delay(monkeypatch):
    """기록의 모델을 그 순서대로, PIN_START_DELAY 뒤에 올린다. 핀이 아니던 모델(학생이 고른 것)도, 채팅 모델이 아닌 것도 떠 있던 그대로.
    ROUTER_PIN(gpt-oss-20b@dp4) 은 올리지 않는다."""
    fr.set_pins([FIRST])
    r, calls, logs = fake_router(monkeypatch)
    slept = []
    monkeypatch.setattr(fr, "_sleep", slept.append)
    t = fr.start_pin_preload(r, models=[FIRST, SECOND, EMB], restore_pins=[FIRST])
    assert t is not None and t.daemon
    t.join(timeout=5)
    assert calls == ["Qwen3-8B-FP8", "Qwen3-4B-FP8", "Qwen3-Embedding-8B"]
    assert slept == [fr.PIN_START_DELAY]


def test_restore_load_results(monkeypatch):
    fr.set_pins([FIRST])
    r, calls, logs = fake_router(monkeypatch)
    out = fr.preload_pins_at_start(r, [FIRST, "no-such-model", SECOND, EMB], restore_pins=[FIRST])
    assert out == {"Qwen3-8B-FP8": "up", "no-such-model": "unknown", "Qwen3-4B-FP8": "up", "Qwen3-Embedding-8B": "up"}
    assert calls == ["Qwen3-8B-FP8", "Qwen3-4B-FP8", "Qwen3-Embedding-8B"]


def test_ROUTER_PIN_load_still_takes_pinned_chat_models_only(monkeypatch):
    """기록 없는 시작 핀 올리기는 예전 그대로: 핀이 아니면 건너뛰고, 채팅 모델이 아니면 올리지 않는다."""
    fr.set_pins([FIRST, EMB])
    r, calls, logs = fake_router(monkeypatch)
    out = fr.preload_pins_at_start(r, [FIRST, SECOND, EMB])
    assert out == {"Qwen3-8B-FP8": "up", "Qwen3-4B-FP8": "unpinned", "Qwen3-Embedding-8B": "unknown"}
    assert calls == ["Qwen3-8B-FP8"]


def test_restore_load_skips_a_recorded_pin_the_console_dropped_during_the_delay(monkeypatch):
    """기다리는 사이 콘솔(게이트웨이)이 핀을 다른 모델로 바꿨으면 기록에서 핀이던 모델은 건너뛴다. 핀이 아니던 모델은 그대로 올린다."""
    fr.set_pins([FIRST])
    r, calls, logs = fake_router(monkeypatch)
    monkeypatch.setattr(fr, "_sleep", lambda seconds: fr.set_pins([QUEUED]))
    t = fr.start_pin_preload(r, models=[FIRST, SECOND], restore_pins=[FIRST], delay=10)
    t.join(timeout=5)
    assert calls == ["Qwen3-4B-FP8"]
    assert any("Qwen3-8B-FP8" in line and "핀이 아니라" in line for line in logs)


class FakeClock:
    def __init__(self):
        self.t = 1000.0
        self.slept = []

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.t += seconds


def test_restore_load_waits_for_cards_without_dropping_a_model_that_was_not_pinned(monkeypatch):
    """옛 serve 가 카드를 늦게 놓아 기다리는 동안에도, 핀이 아니던 모델을 '핀이 아니라' 로 건너뛰지 않는다."""
    fr.set_pins([FIRST])
    clock = FakeClock()
    monkeypatch.setattr(fr, "_monotonic", clock.monotonic)
    monkeypatch.setattr(fr, "_sleep", clock.sleep)
    reads = []

    def smi():
        reads.append(1)
        busy = len(reads) <= 3
        mem = {c: (30.0 if busy else 0.0) for c in range(4)}
        with fr._SMI_LOCK:
            fr._SMI_CACHE["v"] = dict(mem)
        return mem

    monkeypatch.setattr(fr, "_smi_read", smi)
    r, calls, logs = fake_router(monkeypatch)
    assert fr.preload_pins_at_start(r, [SECOND], card_wait=90, restore_pins=[]) == {"Qwen3-4B-FP8": "up"}
    assert calls == ["Qwen3-4B-FP8"] and 0 < sum(clock.slept) < 90
