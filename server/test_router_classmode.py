"""라우터 수업 모드 테스트 — NPU 없이 돈다.

Router 를 ALL_CARDS 4장·가짜 Backend 객체로 조립하고 `_stop` 을 기록용으로 바꿔 검사한다.
furiosa-llm 실행(subprocess.Popen)·furiosa-smi 호출(subprocess.run) 경로는 전부 막는다 —
이 테스트가 NPU 에 모델을 올리는 일은 없어야 한다.

실행:
  taskset -c 64-127 <venv>/bin/python -m pytest server/test_router_classmode.py -q
"""
import os
import sys
import tempfile
import time

# import 부작용(LOGDIR 생성)을 임시 폴더로 보낸다 — 레포 안에 logs/ 를 만들지 않게.
os.environ.setdefault("FURIO_LOGDIR", tempfile.mkdtemp(prefix="router-classmode-"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest

import furiosa_router as fr

PIN = "gpt-oss-20b@dp4"       # 수업 핀 모델(계획 초기값)
BUSY = "Qwen3-8B-FP8"         # 처리 중(inflight>0)
IDLE = "Qwen3-4B-FP8"         # 한가함


class FakeProc:
    """subprocess.Popen 흉내 — 살아 있고, 종료 요청만 기록한다."""

    def __init__(self):
        self.terminated = False

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.terminated = True


@pytest.fixture(autouse=True)
def no_npu(monkeypatch):
    """NPU 를 건드리는 모든 길을 막는다. 어느 테스트든 여기 걸리면 곧바로 실패."""

    def _forbidden(*a, **k):
        raise AssertionError("NPU 경로 호출 금지 — furiosa-llm/furiosa-smi 가 호출됐다")

    monkeypatch.setattr(fr.subprocess, "Popen", _forbidden)
    monkeypatch.setattr(fr.subprocess, "run", _forbidden)
    monkeypatch.setattr(fr, "_smi_read", lambda: {})
    monkeypatch.setattr(fr, "free_port", _forbidden)
    monkeypatch.setattr(fr, "wait_cards_free", lambda cards, timeout=0: True)
    monkeypatch.setattr(fr, "EVICT_GRACE", 0)
    monkeypatch.delenv("SDI_API_KEY", raising=False)
    monkeypatch.delenv("FURIOSA_API_KEY", raising=False)
    yield


def make_backend(model_id, cards, inflight=0, last_used=None):
    b = fr.Backend(model_id, 8410 + min(cards), FakeProc(), list(cards))
    b.ready = True
    b.ready_at = time.time() - 60
    b.inflight = inflight
    b.last_used = time.time() if last_used is None else last_used
    return b


def make_router(monkeypatch, *backends):
    """Router + 기록용 _stop. _stop 은 실제 종료 대신 이름만 적고 running 에서 뺀다
    (빼지 않으면 _evict_until 이 같은 백엔드를 영원히 고른다)."""
    r = fr.Router()
    stopped = []

    def _stop(b):
        stopped.append(b.model_id)
        r.running.pop(b.model_id, None)
        r.state.pop(b.model_id, None)

    monkeypatch.setattr(r, "_stop", _stop)
    for b in backends:
        r.running[b.model_id] = b
        r.state[b.model_id] = "up"
    return r, stopped


# ── _evict_until ──────────────────────────────────────────────────────────
def test_evict_until_skips_pinned(monkeypatch):
    """핀 1개(LRU 상 가장 오래 놀았음)·비핀 1개 유휴, need 1 → 비핀만 내린다."""
    monkeypatch.setattr(fr, "PINNED", {PIN})
    pinned = make_backend(PIN, [0, 1, 2], last_used=time.time() - 1000)   # 핀 없이는 1순위 희생양
    other = make_backend(IDLE, [3], last_used=time.time() - 10)
    r, stopped = make_router(monkeypatch, pinned, other)
    assert r._free_cards() == []

    r._evict_until(1)

    assert stopped == [IDLE]
    assert PIN in r.running
    assert r._free_cards() == [3]


def test_no_force_evict_raises_after_deadline(monkeypatch):
    """FORCE_EVICT 꺼짐: 후보(비핀·유휴)가 없으면 EVICT_WAIT 뒤 강제 종료 대신 RuntimeError.
    _start 가 그 예외를 받으면 state 는 'error', 아무도 내려가지 않는다."""
    monkeypatch.setattr(fr, "EVICT_WAIT", 0)
    monkeypatch.setattr(fr, "FORCE_EVICT", False)
    monkeypatch.setattr(fr, "PINNED", {PIN})
    pinned = make_backend(PIN, [0, 1, 2])
    busy = make_backend(BUSY, [3], inflight=1)
    r, stopped = make_router(monkeypatch, pinned, busy)

    with pytest.raises(RuntimeError, match="카드 부족"):
        r._evict_until(1)
    assert stopped == []

    with pytest.raises(RuntimeError, match="카드 부족"):
        r._start(IDLE)
    assert r.state[IDLE] == "error"
    assert stopped == []
    assert set(r.running) == {PIN, BUSY}


def test_force_evict_on_still_skips_pinned(monkeypatch):
    """FORCE_EVICT 켜짐(기본): 기한 뒤 강제 축출은 비핀 중에서만 고른다 — 핀은 끝까지 남는다."""
    monkeypatch.setattr(fr, "EVICT_WAIT", 0)
    monkeypatch.setattr(fr, "FORCE_EVICT", True)
    monkeypatch.setattr(fr, "PINNED", {PIN})
    pinned = make_backend(PIN, [0, 1, 2], last_used=time.time() - 1000)
    busy = make_backend(BUSY, [3], inflight=1)
    r, stopped = make_router(monkeypatch, pinned, busy)

    r._evict_until(1)

    assert stopped == [BUSY]
    assert PIN in r.running


def test_all_pinned_raises_without_waiting(monkeypatch):
    """내릴 수 있는 백엔드가 아예 없으면(전부 핀) EVICT_WAIT 만큼 락을 쥐고 기다리지 않고 즉시 실패."""
    monkeypatch.setattr(fr, "EVICT_WAIT", 300)
    monkeypatch.setattr(fr, "PINNED", {PIN})
    pinned = make_backend(PIN, [0, 1, 2, 3])
    r, stopped = make_router(monkeypatch, pinned)

    t0 = time.time()
    with pytest.raises(RuntimeError, match="카드 부족"):
        r._evict_until(1)
    assert time.time() - t0 < 2
    assert stopped == []


# ── 못 채우는 need 는 아무것도 내리지 않고 즉시 실패 ─────────────────────────
def test_unattainable_need_evicts_nothing(monkeypatch):
    """핀 [0,1,2]·비핀 유휴 [3], need 2 → 비핀을 내려도 1장뿐이니 내리지 않고 즉시 RuntimeError.
    (검토 지적: 예전엔 멀쩡한 비핀을 먼저 내린 뒤에야 실패했다.) need 1 은 그대로 된다."""
    monkeypatch.setattr(fr, "PINNED", {PIN})
    pinned = make_backend(PIN, [0, 1, 2])
    other = make_backend(IDLE, [3])
    r, stopped = make_router(monkeypatch, pinned, other)

    with pytest.raises(RuntimeError, match="카드 부족"):
        r._evict_until(2)
    assert stopped == []
    assert set(r.running) == {PIN, IDLE}
    assert other.evicting is False and other.usable()

    r._evict_until(1)
    assert stopped == [IDLE]


def test_unattainable_need_does_not_force_kill(monkeypatch):
    """FORCE_EVICT 켜짐·EVICT_WAIT 0: 비핀 처리 중 [3] 을 끊어도 need 2 는 못 채운다 → 끊지 않는다."""
    monkeypatch.setattr(fr, "EVICT_WAIT", 0)
    monkeypatch.setattr(fr, "FORCE_EVICT", True)
    monkeypatch.setattr(fr, "PINNED", {PIN})
    pinned = make_backend(PIN, [0, 1, 2])
    busy = make_backend(BUSY, [3], inflight=1)
    r, stopped = make_router(monkeypatch, pinned, busy)

    with pytest.raises(RuntimeError, match="카드 부족"):
        r._evict_until(2)
    assert stopped == []
    assert busy.evicting is False


def test_unattainable_need_does_not_wait(monkeypatch):
    """FORCE_EVICT 꺼짐·EVICT_WAIT 3: 못 채울 need 는 기한까지 self.lock 을 쥐고 기다리지 않는다."""
    monkeypatch.setattr(fr, "EVICT_WAIT", 3)
    monkeypatch.setattr(fr, "FORCE_EVICT", False)
    monkeypatch.setattr(fr, "PINNED", {PIN})
    pinned = make_backend(PIN, [0, 1, 2])
    busy = make_backend(BUSY, [3], inflight=1)
    r, stopped = make_router(monkeypatch, pinned, busy)

    t0 = time.time()
    with pytest.raises(RuntimeError, match="카드 부족"):
        r._evict_until(2)
    assert time.time() - t0 < 1
    assert stopped == []


def test_unattainable_need_external_cards_no_pin(monkeypatch):
    """핀 없이도 같다: 외부 serve 가 [0,1,2] 를 쥐고(smi 사용량) 내 비핀 유휴가 [3] 뿐이면
    need 2 는 못 채운다 → 유휴 백엔드를 헛되이 내리지 않고 즉시 실패."""
    monkeypatch.setattr(fr, "PINNED", set())
    monkeypatch.setattr(fr, "npu_used_mem", lambda fresh=False: {0: 40.0, 1: 40.0, 2: 40.0})
    other = make_backend(IDLE, [3])
    r, stopped = make_router(monkeypatch, other)
    assert r._free_cards() == []

    with pytest.raises(RuntimeError, match="카드 부족"):
        r._evict_until(2)
    assert stopped == []
    assert IDLE in r.running


def test_evict_until_marks_victim_before_stop(monkeypatch):
    """희생양은 고른 그 ilock 안에서 evicting 표시 — _stop 에 이르기 전 acquire() 의 락프리 경로가
    그 백엔드를 새로 잡지 못하게(기록용 _stop 은 표시를 안 하므로 _evict_until 이 박은 것이다)."""
    monkeypatch.setattr(fr, "PINNED", {PIN})
    pinned = make_backend(PIN, [0, 1, 2])
    other = make_backend(IDLE, [3])
    r, stopped = make_router(monkeypatch, pinned, other)

    r._evict_until(1)
    assert stopped == [IDLE]
    assert other.evicting is True


# ── Router.evict ──────────────────────────────────────────────────────────
def test_evict_marks_evicting_in_same_lock_as_busy_check(monkeypatch):
    """evict(): busy 판정과 evicting 표시를 같은 ilock 안에서. 거절(busy)된 백엔드엔 표시가 남지 않는다."""
    monkeypatch.setattr(fr, "PINNED", set())
    busy = make_backend(BUSY, [2], inflight=1)
    idle = make_backend(IDLE, [3])
    r, stopped = make_router(monkeypatch, busy, idle)

    assert r.evict(BUSY) == "busy"
    assert busy.evicting is False and busy.usable()
    assert stopped == []

    assert r.evict(IDLE) == "ok"
    assert idle.evicting is True
    assert stopped == [IDLE]

    assert r.evict(BUSY, force=True) == "ok"
    assert busy.evicting is True
    assert stopped == [IDLE, BUSY]


# ── HTTP ──────────────────────────────────────────────────────────────────
def test_evict_endpoint_pinned_409_busy_409_ok(monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(fr, "PINNED", {PIN})
    pinned = make_backend(PIN, [0, 1])
    busy = make_backend(BUSY, [2], inflight=1)
    idle = make_backend(IDLE, [3])
    r, stopped = make_router(monkeypatch, pinned, busy, idle)
    monkeypatch.setattr(fr, "ROUTER", r)
    c = TestClient(fr.build_app())

    res = c.post("/router/evict", json={"model": PIN})
    assert res.status_code == 409
    assert res.json()["reason"] == "pinned"

    res = c.post("/router/evict", json={"model": BUSY})
    assert res.status_code == 409
    assert res.json()["reason"] == "busy"
    assert stopped == []

    res = c.post("/router/evict", json={"model": IDLE})
    assert res.status_code == 200
    assert res.json() == {"ok": True}
    assert stopped == [IDLE]

    # force 는 핀·처리 중 검사를 건너뛴다(수업 끝에 핀 모델을 내리는 길)
    res = c.post("/router/evict", json={"model": BUSY, "force": True})
    assert res.status_code == 200
    res = c.post("/router/evict", json={"model": PIN, "force": True})
    assert res.status_code == 200
    assert stopped == [IDLE, BUSY, PIN]

    # 안 떠 있는 모델 → 404, 모르는 모델 → 404
    res = c.post("/router/evict", json={"model": IDLE})
    assert res.status_code == 404
    res = c.post("/router/evict", json={"model": "no-such-model"})
    assert res.status_code == 404


def test_evict_endpoint_force_must_be_json_true(monkeypatch):
    """force 는 JSON true 만 — "true"·"false"·1 같은 값은 force 가 아니다(409 그대로)."""
    from fastapi.testclient import TestClient

    monkeypatch.setattr(fr, "PINNED", {PIN})
    pinned = make_backend(PIN, [0, 1])
    busy = make_backend(BUSY, [2], inflight=1)
    r, stopped = make_router(monkeypatch, pinned, busy)
    monkeypatch.setattr(fr, "ROUTER", r)
    c = TestClient(fr.build_app())

    for bad in ("true", "false", "yes", 1):
        res = c.post("/router/evict", json={"model": BUSY, "force": bad})
        assert (res.status_code, res.json()["reason"]) == (409, "busy"), bad
        res = c.post("/router/evict", json={"model": PIN, "force": bad})
        assert (res.status_code, res.json()["reason"]) == (409, "pinned"), bad
    assert stopped == []


def test_status_lists_pinned(monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(fr, "PINNED", {PIN, IDLE})
    monkeypatch.setattr(fr, "FORCE_EVICT", False)
    r, _ = make_router(monkeypatch, make_backend(PIN, [0, 1, 2, 3]))
    monkeypatch.setattr(fr, "ROUTER", r)
    c = TestClient(fr.build_app())

    st = c.get("/router/status").json()
    assert st["pinned"] == sorted([PIN, IDLE])
    assert st["force_evict"] is False
    assert st["running"][PIN]["state"] == "up"


# ── REGISTRY ──────────────────────────────────────────────────────────────
def test_registry_20b_ctx_and_flag():
    """20b: ctx 65536 (클라이언트 ctx 힌트) + serve 에 --max-model-len 65536, round-robin 은 그대로."""
    reg = fr.REGISTRY["gpt-oss-20b"]
    assert reg["ctx"] == 65536
    extra = reg["extra"]
    assert extra[extra.index("--max-model-len") + 1] == "65536"
    assert extra[extra.index("--data-parallel-routing-policy") + 1] == "round-robin"
    # /router/models 의 context 도 같은 값(단일 출처)
    from fastapi.testclient import TestClient
    rows = {m["id"]: m for m in TestClient(fr.build_app()).get("/router/models").json()["data"]}
    assert rows[PIN]["context"] == 65536
    assert rows["gpt-oss-20b"]["context"] == 65536


# ── 실행 중 핀 바꾸기(POST /router/pin)·모델 상태(/router/models 의 state·error) ──────────
def _client(monkeypatch, r):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(fr, "ROUTER", r)
    return TestClient(fr.build_app())


def _rows(c):
    return {m["id"]: m for m in c.get("/router/models").json()["data"]}


def test_pin_endpoint_replaces_pins(monkeypatch):
    """POST /router/pin 은 실행 중 핀 목록을 통째로 바꾼다. status·evict·_evict_until 이 새 목록을 따른다."""
    monkeypatch.setattr(fr, "PINNED", {PIN})
    pinned = make_backend(PIN, [0, 1, 2])
    idle = make_backend(IDLE, [3])
    r, stopped = make_router(monkeypatch, pinned, idle)
    c = _client(monkeypatch, r)

    res = c.post("/router/pin", json={"models": [IDLE, BUSY, IDLE]})
    assert res.status_code == 200
    assert res.json() == {"ok": True, "pinned": sorted([IDLE, BUSY])}
    assert c.get("/router/status").json()["pinned"] == sorted([IDLE, BUSY])

    # 새 핀은 내릴 수 없고, 옛 핀은 이제 보통 모델처럼 내려간다
    res = c.post("/router/evict", json={"model": IDLE})
    assert (res.status_code, res.json()["reason"]) == (409, "pinned")
    res = c.post("/router/evict", json={"model": PIN})
    assert res.status_code == 200
    assert stopped == [PIN]

    # 빈 목록 = 핀 없음
    res = c.post("/router/pin", json={"models": []})
    assert res.json() == {"ok": True, "pinned": []}
    assert c.get("/router/status").json()["pinned"] == []


def test_pin_change_applies_to_lru(monkeypatch):
    """핀을 바꾸면 _evict_until 도 새 핀을 건드리지 않는다(옛 핀은 희생양이 될 수 있다)."""
    monkeypatch.setattr(fr, "PINNED", {PIN})
    old = make_backend(PIN, [0, 1, 2], last_used=time.time() - 1000)
    new = make_backend(IDLE, [3], last_used=time.time() - 2000)
    r, stopped = make_router(monkeypatch, old, new)
    c = _client(monkeypatch, r)

    assert c.post("/router/pin", json={"models": [IDLE]}).status_code == 200
    r._evict_until(1)
    assert stopped == [PIN]
    assert IDLE in r.running


def test_pin_endpoint_rejects_bad_body(monkeypatch):
    """모르는 이름·없는 변형(카드 4장을 넘는 dp8)은 404, 목록이 아니면 400. 어느 경우도 핀은 그대로."""
    monkeypatch.setattr(fr, "PINNED", {PIN})
    r, _ = make_router(monkeypatch)
    c = _client(monkeypatch, r)

    for bad in (["no-such-model"], [PIN, "gpt-oss-20b@dp8"]):
        res = c.post("/router/pin", json={"models": bad})
        assert res.status_code == 404, bad
        assert res.json()["ok"] is False
    for bad in ({"models": PIN}, {"models": [1]}, {"models": [""]}, {}, [PIN]):
        res = c.post("/router/pin", json=bad)
        assert res.status_code == 400, bad
    res = c.post("/router/pin", content=b"not json")
    assert res.status_code == 400
    assert c.get("/router/status").json()["pinned"] == [PIN]


def test_pin_endpoint_uses_router_key(monkeypatch):
    """라우터 키가 있으면 preload·evict 처럼 /router/pin 도 Bearer 가 있어야 한다."""
    from fastapi.testclient import TestClient

    monkeypatch.setattr(fr, "PINNED", set())
    monkeypatch.setenv("SDI_API_KEY", "test-router-key")
    r, _ = make_router(monkeypatch)
    monkeypatch.setattr(fr, "ROUTER", r)
    c = TestClient(fr.build_app())

    assert c.post("/router/pin", json={"models": [PIN]}).status_code == 401
    res = c.post("/router/pin", json={"models": [PIN]},
                 headers={"authorization": "Bearer test-router-key"})
    assert res.status_code == 200
    assert fr.PINNED == {PIN}


def test_router_models_state_and_error(monkeypatch):
    """/router/models 각 행: cards(필요 장수, 정수)·state(up·loading·stopping·down·error)·error(있으면 짧은 문구)."""
    monkeypatch.setattr(fr, "PINNED", {PIN})
    up = make_backend(PIN, [0, 1, 2, 3])
    r, _ = make_router(monkeypatch, up)
    r.state[IDLE] = "loading"
    r.state["Qwen3-32B-FP8"] = "stopping"
    r.state[BUSY] = "error"
    r.errors[BUSY] = "모델 서버가 시작 중에 끝났습니다\nINFO loading\n\nRuntimeError: out of memory\n"
    c = _client(monkeypatch, r)

    rows = _rows(c)
    assert rows[PIN]["state"] == "up" and rows[PIN]["error"] is None
    assert rows[PIN]["cards"] == 4
    assert rows[IDLE]["state"] == "loading"
    assert rows["Qwen3-32B-FP8"]["state"] == "stopping"
    assert rows["gpt-oss-120b"]["state"] == "down" and rows["gpt-oss-120b"]["error"] is None
    assert rows["gpt-oss-120b"]["cards"] == 4
    err = rows[BUSY]
    assert err["state"] == "error"
    assert "\n" not in err["error"] and len(err["error"]) <= 200
    assert "out of memory" in err["error"]
    # 기존 필드는 그대로
    for key in ("id", "base", "name", "context", "kind", "tools", "dp", "pp", "tp"):
        assert key in rows[PIN]


def test_start_failure_records_error(monkeypatch):
    """_start 가 실패하면 state 'error' 와 함께 errors 에 까닭을 남긴다(카드 부족·장수 초과 모두)."""
    monkeypatch.setattr(fr, "EVICT_WAIT", 0)
    monkeypatch.setattr(fr, "FORCE_EVICT", False)
    monkeypatch.setattr(fr, "PINNED", {PIN})
    pinned = make_backend(PIN, [0, 1, 2, 3])
    r, _ = make_router(monkeypatch, pinned)

    with pytest.raises(RuntimeError):
        r._start(IDLE)
    assert r.state[IDLE] == "error"
    assert "카드 부족" in r.errors[IDLE]

    with pytest.raises(RuntimeError):
        r._start("gpt-oss-20b@dp8")
    assert r.state["gpt-oss-20b@dp8"] == "error"
    assert r.errors["gpt-oss-20b@dp8"]


def _wait_for(pred, timeout=3.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_preload_shows_loading_then_error(monkeypatch):
    """preload 는 즉시 답하고, 올리기가 다른 모델 적재 뒤에 줄 서 있어도 그 모델은 곧바로 loading 으로 보인다.
    뒤에서 실패하면 error 와 짧은 까닭."""
    import threading as _th

    monkeypatch.setattr(fr, "PINNED", set())
    r, _ = make_router(monkeypatch)
    gate = _th.Event()

    def _ensure(model_id):
        gate.wait(5)
        raise RuntimeError("모델 서버가 시작 중에 끝났습니다\nboom: device busy")

    monkeypatch.setattr(r, "ensure", _ensure)
    c = _client(monkeypatch, r)

    res = c.post("/router/preload", json={"model": IDLE})
    assert res.json() == {"ok": True, "model": IDLE}
    assert _rows(c)[IDLE]["state"] == "loading"
    gate.set()
    assert _wait_for(lambda: _rows(c)[IDLE]["state"] == "error")
    assert "boom: device busy" in _rows(c)[IDLE]["error"]


def test_preload_of_up_model_stays_up(monkeypatch):
    """이미 올라온 모델의 preload 는 state 를 건드리지 않는다(up 그대로)."""
    monkeypatch.setattr(fr, "PINNED", {PIN})
    r, _ = make_router(monkeypatch, make_backend(PIN, [0, 1, 2, 3]))
    calls = []
    monkeypatch.setattr(r, "ensure", lambda m: calls.append(m) or 8410)
    c = _client(monkeypatch, r)

    assert c.post("/router/preload", json={"model": PIN}).json() == {"ok": True, "model": PIN}
    assert _rows(c)[PIN]["state"] == "up"
    assert _wait_for(lambda: calls == [PIN])
    assert _rows(c)[PIN]["state"] == "up"


def test_mark_loading_does_not_overwrite_up(monkeypatch):
    """preload 의 mark_loading 이 usable() 을 본 뒤 state 를 쓰기 전에 _start 가 ready·up 을 마치면, 올라온 모델 위에
    'loading' 을 덮어써 LED·콘솔 버튼이 노랑·비활성으로 남았다(검토 지적). 확인과 쓰기, _start 의 ready·up 은 한 ilock 안에서.
    실제 _start 를 돌리되 serve 실행·포트·준비 확인은 가짜로 바꾼다(NPU 를 건드리지 않는다)."""
    import threading as _th

    monkeypatch.setattr(fr, "PINNED", set())
    r, _ = make_router(monkeypatch)
    monkeypatch.setattr(fr.subprocess, "Popen", lambda *a, **k: FakeProc())
    monkeypatch.setattr(fr, "free_port", lambda *a, **k: 18999)
    in_wait, proceed, checked, go = _th.Event(), _th.Event(), _th.Event(), _th.Event()

    def _wait_ready(b, logpath):
        in_wait.set()
        assert proceed.wait(5)

    monkeypatch.setattr(r, "_wait_ready", _wait_ready)
    starter = _th.Thread(target=r._start, args=(IDLE,), daemon=True)
    starter.start()
    assert in_wait.wait(5)
    b = r.running[IDLE]
    orig_usable = b.usable

    def _usable():
        res = orig_usable()        # 아직 준비 전이라 False
        checked.set()
        go.wait(5)                 # 확인과 쓰기 사이에 _start 가 끝나게 기다린다
        return res

    monkeypatch.setattr(b, "usable", _usable)
    marker = _th.Thread(target=r.mark_loading, args=(IDLE,), daemon=True)
    marker.start()
    assert checked.wait(5)
    proceed.set()                  # _start 가 ready·up 을 하러 간다
    starter.join(0.5)              # 고친 뒤에는 mark_loading 이 쥔 ilock 때문에 여기서 끝나지 않는다
    go.set()
    marker.join(5)
    starter.join(5)
    assert not marker.is_alive() and not starter.is_alive()
    assert b.ready is True
    assert r.state[IDLE] == "up"


def test_failure_messages_are_plain_korean(monkeypatch):
    """/router/models 의 error 칸(콘솔 모델 목록)으로 가는 실패 문구: 한국어, 파이썬 목록 표기·설정 이름·영어 디버그 문구 없음."""
    monkeypatch.setattr(fr, "PINNED", {PIN})
    pinned = make_backend(PIN, [0, 1, 2, 3])
    busy = make_backend(BUSY, [2], inflight=1)

    def plain(msg):
        assert "카드 부족" in msg, msg
        for bad in ("[", "]", "need", "free", "force_evict", "ROUTER_", "—"):
            assert bad not in msg, (bad, msg)

    r, _ = make_router(monkeypatch, pinned)
    msg = r._no_cards_msg(4, "unreachable")
    plain(msg)
    assert PIN in msg and "핀 모델" in msg and "4장" in msg

    r, _ = make_router(monkeypatch, busy)
    msg = r._no_cards_msg(4, "busy")
    plain(msg)
    assert BUSY in msg

    monkeypatch.setattr(fr, "PINNED", set())
    r, _ = make_router(monkeypatch)
    plain(r._no_cards_msg(4, "unreachable"))

    # _evict_until 뒤에 카드가 모자란 경우(_start 안)
    r, _ = make_router(monkeypatch)
    monkeypatch.setattr(r, "_evict_until", lambda need: None)
    monkeypatch.setattr(r, "_free_cards", lambda: [])
    with pytest.raises(RuntimeError):
        r._start(IDLE)
    plain(r.errors[IDLE])

    # serve 가 시작 중에 끝남: 한국어 첫 줄 + 로그 꼬리 마지막 줄(실제 오류)
    class DeadProc(FakeProc):
        def poll(self):
            return 1

    log = os.path.join(fr.LOGDIR, "router-test-dead.log")
    with open(log, "w") as f:
        f.write("INFO loading\nRuntimeError: out of memory\n")
    dead = fr.Backend(IDLE, 18998, DeadProc(), [3])
    with pytest.raises(RuntimeError) as ei:
        r._wait_ready(dead, log)
    short = fr.short_error(ei.value)
    assert short.startswith("모델 서버가 시작 중에 끝났습니다"), short
    assert short.endswith("RuntimeError: out of memory"), short
    assert "serve process" not in short

    # 준비 시간 초과: 설정 이름(ROUTER_READY_TIMEOUT)은 화면 문구에 넣지 않는다
    monkeypatch.setattr(fr, "READY_TIMEOUT", 0)
    alive = fr.Backend(IDLE, 18997, FakeProc(), [3])
    with pytest.raises(RuntimeError) as ei:
        r._wait_ready(alive, log)
    msg = str(ei.value)
    assert "준비되지 않았습니다" in msg
    assert "ROUTER_READY_TIMEOUT" not in msg and "serve not ready" not in msg


# furiosa-llm serve 로그의 실제 꼴(tracing, 색 코드, 2026-10-23 router-gpt-oss-120b.log 마지막 3줄)
_REAL_TAIL = (
    "\x1b[2m2026-10-23T22:47:56.36325561+09:00\x1b[0m \x1b[32m INFO\x1b[0m \x1b[2mpe_runtime::context\x1b[0m"
    "\x1b[2m:\x1b[0m Memory dump thread for TpGroupId(npu0-3) started\n"
    "\x1b[2m2026-10-23T22:47:56.363418971+09:00\x1b[0m \x1b[32m INFO\x1b[0m \x1b[2mfuriosa_generator::entrypoint\x1b[0m"
    "\x1b[2m:\x1b[0m Loading DP group 1/1: PP=1, devices=[TpGroupId(npu0-3)]\n"
    "\x1b[2m2026-10-23T22:49:05.304547795+09:00\x1b[0m \x1b[32m INFO\x1b[0m \x1b[2mfuriosa_generator::entrypoint\x1b[0m"
    "\x1b[2m:\x1b[0m   PP rank 1/1: device=TpGroupId(npu0-3), layers=0..36\n"
)


def test_start_exit_error_drops_log_colors_and_timestamps(monkeypatch):
    """serve 가 시작 중에 끝났을 때 콘솔 error 칸: 색 코드·로그 시각·tracing 대상 이름 없이 마지막 줄의 내용만."""
    r, _ = make_router(monkeypatch)

    class DeadProc(FakeProc):
        def poll(self):
            return 1

    log = os.path.join(fr.LOGDIR, "router-test-ansi.log")
    with open(log, "w") as f:
        f.write(_REAL_TAIL)
    dead = fr.Backend(IDLE, 18996, DeadProc(), [3])
    with pytest.raises(RuntimeError) as ei:
        r._wait_ready(dead, log)
    full = str(ei.value)
    assert "\x1b" not in full and "2026-10-23T" not in full, full
    short = fr.short_error(ei.value)
    assert short == "모델 서버가 시작 중에 끝났습니다: PP rank 1/1: device=TpGroupId(npu0-3), layers=0..36", short

    # 꼬리를 날것으로 받아도(다른 길로 들어온 까닭) short_error 가 같은 한 줄로 만든다
    raw = "모델 서버가 시작 중에 끝났습니다\n" + _REAL_TAIL
    assert fr.short_error(raw) == short


def test_clean_log_line_keeps_plain_lines():
    """색·시각이 없는 줄(파이썬 예외·uvicorn 줄)은 그대로 둔다."""
    assert fr.clean_log_line("RuntimeError: out of memory") == "RuntimeError: out of memory"
    assert fr.clean_log_line("\x1b[31mERROR\x1b[0m boom") == "ERROR boom"
    assert fr.clean_log_line("2026-10-23T22:49:05.3+09:00  WARN xgrammar: Provided vocab_size 1 does not match") == \
        "Provided vocab_size 1 does not match"
    assert fr.clean_log_line("2026-10-23 22:49:05,123 device busy") == "device busy"
