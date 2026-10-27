"""원격 관리 길(/router/health · /router/logs · /router/restart) 시험 — NPU 없이 돈다.

furiosa-smi 는 가짜 _run_tool 로, hwmon 은 임시 폴더로, 재시작 spawn 은 기록용 함수로 바꾼다.
진짜 spawn 함수 시험은 임시 폴더의 가짜 스크립트만 띄운다(serve-router.sh 는 절대 실행하지 않는다).

실행:
  taskset -c 64-127 <venv>/bin/python -m pytest server/test_router_remote.py -q
"""
import json
import os
import subprocess
import sys
import tempfile
import time

os.environ.setdefault("FURIO_LOGDIR", tempfile.mkdtemp(prefix="router-remote-"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest

import furiosa_router as fr

REAL_SPAWN = fr.spawn_restart          # 가짜 스크립트 시험용(아래 autouse 픽스처가 막기 전의 진짜 함수)
REAL_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "serve-router.sh")


class FakeProc:
    def __init__(self, pid=4242):
        self.pid = pid

    def poll(self):
        return None

    def terminate(self):
        pass

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


def smi_status_json(n=4, util=0.0, used=0):
    return json.dumps([{"arch": "RNGD", "device": f"npu{i}",
                        "memory": {"used_size": used, "total_size": 51002736640, "used_ratio": 0.0},
                        "pe_utilizations": [{"pe_core": k, "pe_occupancy": False, "pe_utilization": util}
                                            for k in range(8)]} for i in range(n)])


def make_hwmon(root, n=4, power_uw=39000000, peak=36505):
    for i in range(n):
        d = root / f"hwmon{i + 3}"
        d.mkdir(parents=True)
        (d / "name").write_text(f"rngd{i}\n")
        (d / "power1_average").write_text(f"{power_uw}\n")
        (d / "temp1_label").write_text("MEMORY0\n")
        (d / "temp1_input").write_text("31000\n")
        (d / "temp4_label").write_text("PEAK\n")
        (d / "temp4_input").write_text(f"{peak}\n")
    other = root / "hwmon0"
    other.mkdir()
    (other / "name").write_text("nvme\n")
    (other / "temp1_input").write_text("50000\n")
    return root


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    """NPU·진짜 재시작 길을 막고, 로그 폴더·hwmon·시계를 임시로."""

    def _forbidden(*a, **k):
        raise AssertionError("NPU 경로·실제 프로세스 실행 금지")

    def _no_spawn(*a, **k):
        raise AssertionError("재시작 spawn 은 시험마다 가짜로 바꿔야 한다")

    monkeypatch.setattr(fr.subprocess, "run", _forbidden)
    monkeypatch.setattr(fr, "_smi_read", lambda: {})
    monkeypatch.setattr(fr, "spawn_restart", _no_spawn)
    monkeypatch.setattr(fr, "_run_tool", _forbidden)
    logdir = tmp_path / "logs"
    logdir.mkdir()
    monkeypatch.setattr(fr, "LOGDIR", str(logdir))
    monkeypatch.setattr(fr, "ROUTER_LOG", str(logdir / "router.log"))
    monkeypatch.setattr(fr, "RESTART_LOG", str(logdir / "restart.log"))
    monkeypatch.setattr(fr, "HWMON_ROOT", str(tmp_path / "hwmon-none"))
    monkeypatch.setattr(fr, "RESTART_SCRIPT", str(tmp_path / "no-such-script.sh"))
    monkeypatch.setattr(fr, "_ACCEL_CACHE", {"t": None, "v": None})
    monkeypatch.setattr(fr, "_RESTART", {"at": None})
    monkeypatch.setattr(fr, "ROUTER", fr.Router())
    monkeypatch.delenv("SDI_API_KEY", raising=False)
    monkeypatch.delenv("FURIOSA_API_KEY", raising=False)
    yield logdir


def client():
    from fastapi.testclient import TestClient
    return TestClient(fr.build_app())


def fake_tool(monkeypatch, outputs, calls=None):
    """argv[1](status·info) → 출력 문자열 또는 던질 예외."""

    def run(argv, timeout):
        if calls is not None:
            calls.append(list(argv))
        out = outputs.get(argv[1])
        if isinstance(out, BaseException):
            raise out
        if out is None:
            raise FileNotFoundError(argv[0])
        return out

    monkeypatch.setattr(fr, "_run_tool", run)


# ── /router/health ─────────────────────────────────────────────────────────
def test_health_shape_with_smi_and_hwmon(monkeypatch, tmp_path):
    fake_tool(monkeypatch, {"status": smi_status_json(util=0.5, used=2 * 1024 ** 3)})
    monkeypatch.setattr(fr, "HWMON_ROOT", str(make_hwmon(tmp_path / "hwmon")))
    b = fr.Backend("gpt-oss-20b@dp4", 8411, FakeProc(pid=os.getpid()), [0, 1, 2, 3])
    b.ready = True
    fr.ROUTER.running[b.model_id] = b
    fr.ROUTER.state[b.model_id] = "up"

    res = client().get("/router/health")

    assert res.status_code == 200
    h = res.json()
    assert h["ok"] is True
    assert isinstance(h["server_time"], str) and "T" in h["server_time"]
    assert isinstance(h["uptime_s"], int) and h["uptime_s"] >= 0
    host = h["host"]
    assert host["mem_total"] > 0 and 0 <= host["mem_avail"] <= host["mem_total"]
    assert isinstance(host["load1"], float)
    assert host["disk"][0]["path"] == "/"
    assert all(set(d) == {"path", "total", "free"} for d in host["disk"])
    assert "accel_error" not in h
    assert [a["id"] for a in h["accel"]] == ["npu0", "npu1", "npu2", "npu3"]
    a0 = h["accel"][0]
    assert a0 == {"id": "npu0", "kind": "npu", "util_pct": 50.0, "mem_used": 2 * 1024 ** 3,
                  "mem_total": 51002736640, "power_w": 39.0, "temp_c": 36.5}
    assert h["backends"] == [{"model": "gpt-oss-20b@dp4", "pid": os.getpid(), "port": 8411, "state": "up",
                              "rss": h["backends"][0]["rss"]}]
    assert isinstance(h["backends"][0]["rss"], int) and h["backends"][0]["rss"] > 0


def test_health_util_already_percent_kept(monkeypatch):
    """pe_utilization 은 0~1 비율로 본다(ntop.py 와 같다). 1 을 넘는 값이 있으면 이미 백분율이다."""
    fake_tool(monkeypatch, {"status": smi_status_json(n=1, util=37.5), "info": "[]"})
    h = client().get("/router/health").json()
    assert h["accel"][0]["util_pct"] == 37.5


def test_health_power_temp_from_smi_info_without_hwmon(monkeypatch):
    info = json.dumps([{"dev_name": "npu0", "temperature": "36.51°C", "power": "39 W"}])
    fake_tool(monkeypatch, {"status": smi_status_json(n=1), "info": info})
    a = client().get("/router/health").json()["accel"][0]
    assert (a["power_w"], a["temp_c"]) == (39.0, 36.5)


def test_health_missing_smi_no_hwmon_is_empty_not_500(monkeypatch):
    fake_tool(monkeypatch, {})          # furiosa-smi 없음(FileNotFoundError)
    res = client().get("/router/health")
    assert res.status_code == 200
    h = res.json()
    assert h["accel"] == []
    assert "furiosa-smi" in h["accel_error"] and len(h["accel_error"]) <= 200


def test_health_bad_json_with_hwmon_keeps_power_temp(monkeypatch, tmp_path):
    """smi 가 깨진 값을 내도 hwmon 의 전력·온도는 보인다(나머지는 null) + accel_error."""
    fake_tool(monkeypatch, {"status": "not json {"})
    monkeypatch.setattr(fr, "HWMON_ROOT", str(make_hwmon(tmp_path / "hwmon", n=2)))
    h = client().get("/router/health").json()
    assert h["accel_error"]
    assert [a["id"] for a in h["accel"]] == ["npu0", "npu1"]
    assert h["accel"][0] == {"id": "npu0", "kind": "npu", "util_pct": None, "mem_used": None,
                             "mem_total": None, "power_w": 39.0, "temp_c": 36.5}


def test_health_smi_timeout(monkeypatch):
    fake_tool(monkeypatch, {"status": subprocess.TimeoutExpired(["furiosa-smi"], 8)})
    h = client().get("/router/health").json()
    assert h["accel"] == [] and "시간" in h["accel_error"]


def test_health_accel_cached_5s(monkeypatch):
    calls = []
    now = [1000.0]
    monkeypatch.setattr(fr, "_monotonic", lambda: now[0])
    fake_tool(monkeypatch, {"status": smi_status_json(n=1), "info": "[]"}, calls)
    c = client()
    c.get("/router/health")
    n1 = len(calls)
    now[0] += 4.9
    c.get("/router/health")
    assert len(calls) == n1
    now[0] += 0.2
    c.get("/router/health")
    assert len(calls) == 2 * n1


def test_health_disk_dedup_by_filesystem(monkeypatch, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    disk = fr.disk_usage(["/", str(a), str(b), str(tmp_path / "missing")])
    paths = [d["path"] for d in disk]
    assert paths[0] == "/"
    assert len(paths) == len({os.stat(p).st_dev for p in paths})   # 같은 파일시스템은 한 번만
    assert str(tmp_path / "missing") not in paths


def test_health_requires_key_when_set(monkeypatch):
    monkeypatch.setenv("SDI_API_KEY", "k1")
    fake_tool(monkeypatch, {})
    c = client()
    assert c.get("/router/health").status_code == 401
    assert c.get("/router/health", headers={"authorization": "Bearer k1"}).status_code == 200
    assert c.get("/router/logs?name=router").status_code == 401
    assert c.post("/router/restart", json={}).status_code == 401


# ── /router/logs ───────────────────────────────────────────────────────────
def write_log(path, lines):
    with open(path, "wb") as f:
        for ln in lines:
            f.write(ln if isinstance(ln, bytes) else ln.encode() + b"\n")


def test_logs_names_and_tail(isolate):
    write_log(isolate / "router.log", [f"line {i}" for i in range(500)])
    write_log(isolate / "router-gpt-oss-20b_dp4.log", ["\x1b[32mINFO\x1b[0m ready", "bad \xff byte".encode("latin-1") + b"\n"])
    write_log(isolate / "router-unknown-thing.log", ["x"])          # 모델 목록에 없는 파일은 이름에 안 나온다
    c = client()

    res = c.get("/router/logs", params={"name": "router", "n": 30})
    assert res.status_code == 200
    j = res.json()
    assert j["name"] == "router"
    assert j["lines"] == [f"line {i}" for i in range(470, 500)]
    assert j["names"][0] == "router"
    assert "gpt-oss-20b@dp4" in j["names"]
    assert not any("unknown" in n for n in j["names"])

    j = c.get("/router/logs", params={"name": "gpt-oss-20b@dp4"}).json()
    assert j["lines"][0] == "INFO ready"
    assert j["lines"][1].startswith("bad ") and j["lines"][1].endswith(" byte")
    json.dumps(j, ensure_ascii=False).encode("utf-8")


def test_logs_n_clamped(isolate):
    write_log(isolate / "router.log", [f"l{i}" for i in range(3000)])
    c = client()
    assert len(c.get("/router/logs", params={"name": "router", "n": 1}).json()["lines"]) == 20
    assert len(c.get("/router/logs", params={"name": "router", "n": 99999}).json()["lines"]) == 2000
    assert len(c.get("/router/logs", params={"name": "router"}).json()["lines"]) == 200
    assert len(c.get("/router/logs", params={"name": "router", "n": "abc"}).json()["lines"]) == 200


@pytest.mark.parametrize("name", ["../router", "/etc/passwd", "router.log", "nope", "", "logs/router"])
def test_logs_unknown_name_404(isolate, name):
    write_log(isolate / "router.log", ["a"])
    res = client().get("/router/logs", params={"name": name})
    assert res.status_code == 404
    assert "router" in res.json()["names"]


def test_logs_router_missing_file_not_listed(isolate):
    res = client().get("/router/logs", params={"name": "router"})
    assert res.status_code == 404
    assert res.json()["names"] == []


def test_logs_lists_running_backend_and_prev(isolate):
    write_log(isolate / "router.prev.log", ["old run"])
    write_log(isolate / "restart.log", ["restart requested: x"])
    write_log(isolate / "router-Qwen3-4B-FP8.log", ["loading"])
    j = client().get("/router/logs", params={"name": "router.prev"}).json()
    assert j["lines"] == ["old run"]
    assert {"router.prev", "restart", "Qwen3-4B-FP8"} <= set(j["names"])


# ── /router/restart ────────────────────────────────────────────────────────
def test_restart_202_spawns_after_response_and_guards(monkeypatch, capsys):
    spawned = []
    now = [5000.0]
    monkeypatch.setattr(fr, "_monotonic", lambda: now[0])
    monkeypatch.setattr(fr, "spawn_restart", lambda **kw: spawned.append(kw) or FakeProc())
    c = client()

    res = c.post("/router/restart", json={"reason": "콘솔에서\n다시 시작"})
    assert res.status_code == 202
    assert res.json() == {"ok": True, "restarting": True}
    assert len(spawned) == 1
    out = capsys.readouterr().out
    assert "restart requested: 콘솔에서 다시 시작" in out

    now[0] += 59
    res = c.post("/router/restart", json={})
    assert res.status_code == 409
    assert res.json()["code"] == "restart_in_progress"
    assert len(spawned) == 1

    now[0] += 2
    res = c.post("/router/restart")
    assert res.status_code == 202
    assert len(spawned) == 2


def test_restart_bad_body_400(monkeypatch):
    monkeypatch.setattr(fr, "spawn_restart", lambda **kw: pytest.fail("spawn 하면 안 된다"))
    c = client()
    assert c.post("/router/restart", content=b"not json").status_code == 400
    assert c.post("/router/restart", json=[1, 2]).status_code == 400


def test_restart_spawn_failure_clears_guard(monkeypatch):
    calls = []

    def boom(**kw):
        calls.append(kw)
        raise OSError("no bash")

    monkeypatch.setattr(fr, "spawn_restart", boom)
    c = client()
    assert c.post("/router/restart", json={"reason": "a"}).status_code == 202
    assert c.post("/router/restart", json={"reason": "b"}).status_code == 202   # 실패했으니 다시 받는다
    assert len(calls) == 2


def test_restart_env_drops_dotenv_values(monkeypatch, tmp_path):
    """.env 에서 온 값(시작 때 .env 값과 같은 것)은 빼서 serve-router.sh 가 .env 를 새로 읽게 한다.
    셸에서 다른 값을 준 키는 그대로 둔다(셸 값 우선은 처음 시작과 같다)."""
    monkeypatch.setattr(fr, "_DOTENV_AT_START", {"ROUTER_PIN": "gpt-oss-20b@dp4", "SDI_BIND": "0.0.0.0"})
    monkeypatch.setenv("ROUTER_PIN", "gpt-oss-20b@dp4")
    monkeypatch.setenv("SDI_BIND", "127.0.0.1")
    monkeypatch.setenv("KEEP_ME", "1")
    env = fr.restart_env()
    assert "ROUTER_PIN" not in env
    assert env["SDI_BIND"] == "127.0.0.1"
    assert env["KEEP_ME"] == "1"


def test_read_dotenv_rules(tmp_path):
    p = tmp_path / ".env"
    p.write_text('A=1\r\n# c\n\nB=x=y\nQ="q"\nbad line\n', encoding="utf-8")
    assert fr.read_dotenv(str(p)) == {"A": "1", "B": "x=y", "Q": '"q"'}
    assert fr.read_dotenv(str(tmp_path / "none")) == {}


def test_restart_script_default_is_own_serve_router():
    """기본 재시작 스크립트는 이 파일 옆의 serve-router.sh(경로만 확인 — 실행하지 않는다)."""
    assert fr._default_restart_script() == REAL_SCRIPT


def test_spawn_restart_detaches_fake_script(monkeypatch, tmp_path):
    """진짜 spawn 함수로 가짜 스크립트만 띄운다: 새 세션(setsid)·로그 폴더의 restart.log 에 출력·주어진 env."""
    monkeypatch.setattr(fr.subprocess, "run", subprocess.run)
    marker = tmp_path / "marker"
    script = tmp_path / "fake-serve-router.sh"
    script.write_text(f'#!/usr/bin/env bash\necho "fake start $FAKE_FLAG"\n'
                      f'ps -o sid= -p $$ | tr -d " " > "{marker}.tmp"\nmv "{marker}.tmp" "{marker}"\n')
    logpath = tmp_path / "logs" / "restart.log"
    env = dict(os.environ, FAKE_FLAG="yes")

    p = REAL_SPAWN(script=str(script), logpath=str(logpath), env=env)
    p.wait(timeout=10)
    deadline = time.time() + 10
    while not marker.exists() and time.time() < deadline:
        time.sleep(0.05)
    assert marker.exists(), "가짜 스크립트가 돌지 않았다"
    assert int(marker.read_text().strip()) != os.getsid(0)          # 새 세션
    deadline = time.time() + 5
    while "fake start yes" not in logpath.read_text() and time.time() < deadline:
        time.sleep(0.05)
    assert "fake start yes" in logpath.read_text()


def test_serve_router_keeps_previous_log():
    """serve-router.sh 는 새로 띄우기 전에 지난 router.log 를 router.prev.log 로 남긴다(재시작 까닭을 볼 수 있게)."""
    src = open(REAL_SCRIPT, encoding="utf-8").read()
    i_mv = src.index('router.prev.log')
    i_trunc = src.index(': > "$LOG"')
    assert i_mv < i_trunc
