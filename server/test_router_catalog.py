"""라우터 모델 목록 테스트: 올릴 수 있는지(available·reason), 추가 모델 파일(ROUTER_EXTRA_MODELS), POST /router/rescan.

NPU 없이 돈다. furiosa-llm 실행·furiosa-smi 호출은 막고, HF 캐시·결과물 폴더는 임시 폴더로 흉내 낸다.

실행:
  taskset -c 64-127 <venv>/bin/python -m pytest server/test_router_catalog.py -q
"""
import json
import os
import sys
import tempfile
import time

os.environ.setdefault("FURIO_LOGDIR", tempfile.mkdtemp(prefix="router-catalog-"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest

import furiosa_router as fr

PIN = "gpt-oss-20b@dp4"


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
def no_npu(monkeypatch, tmp_path):
    """NPU 를 건드리는 길을 막고, 모델 목록을 내장 목록으로 되돌린 뒤 추가 파일·HF 캐시를 임시 폴더로 돌린다."""

    def _forbidden(*a, **k):
        raise AssertionError("NPU 경로 호출 금지: furiosa-llm/furiosa-smi 가 호출됐다")

    monkeypatch.setattr(fr.subprocess, "Popen", _forbidden)
    monkeypatch.setattr(fr.subprocess, "run", _forbidden)
    monkeypatch.setattr(fr, "_smi_read", lambda: {})
    monkeypatch.setattr(fr, "free_port", _forbidden)
    monkeypatch.setattr(fr, "wait_cards_free", lambda cards, timeout=0: True)
    monkeypatch.delenv("SDI_API_KEY", raising=False)
    monkeypatch.delenv("FURIOSA_API_KEY", raising=False)
    hub = tmp_path / "hub"
    hub.mkdir()
    monkeypatch.setattr(fr, "hub_caches", lambda: [str(hub)])
    monkeypatch.setattr(fr, "EXTRA_MODELS", str(tmp_path / "extra-models.yaml"))
    monkeypatch.setattr(fr, "AVAIL_TTL", 0.0)
    fr.rebuild_registry()
    yield
    monkeypatch.undo()
    fr.rebuild_registry()


def _client(monkeypatch, r=None):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(fr, "ROUTER", r or fr.Router())
    return TestClient(fr.build_app())


def _rows(c):
    return {m["id"]: m for m in c.get("/router/models").json()["data"]}


def hub_repo(tmp_path, repo, files, *, rev="abc123"):
    """HF 캐시 흉내: hub/models--org--name/snapshots/<rev>/<files>. files 는 {이름: 내용}."""
    snap = tmp_path / "hub" / ("models--" + repo.replace("/", "--")) / "snapshots" / rev
    snap.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        (snap / name).write_text(body, encoding="utf-8")
    return snap


def v2_dir(path):
    """완전한 v2 결과물 폴더(artifact.json·binary_bundle.zip)."""
    os.makedirs(path, exist_ok=True)
    for name in ("artifact.json", "binary_bundle.zip", "config.json"):
        with open(os.path.join(path, name), "w") as f:
            f.write("{}")
    return path


def write_extra(tmp_path, models, *, name="extra-models.yaml"):
    import yaml
    p = tmp_path / name
    if name.endswith(".json"):
        p.write_text(json.dumps({"models": models}), encoding="utf-8")
    else:
        p.write_text(yaml.safe_dump({"models": models}, allow_unicode=True), encoding="utf-8")
    return p


def bump(path):
    """mtime 을 확실히 바꾼다(같은 초 안에 두 번 써도 다르게)."""
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))


# ── 올릴 수 있는지 ─────────────────────────────────────────────────────────────
def test_artifact_complete_rules(tmp_path):
    """v2 = artifact.json + binary_bundle.zip, FXB 묶음 = *.fxb + 색인의 가중치 조각 전부(없으면 safetensors 하나 이상)."""
    v2 = v2_dir(str(tmp_path / "v2"))
    assert fr.artifact_complete(v2)
    os.remove(os.path.join(v2, "binary_bundle.zip"))
    assert not fr.artifact_complete(v2)

    fxb = tmp_path / "fxb"
    fxb.mkdir()
    (fxb / "m.fxb").write_text("x")
    (fxb / "model.safetensors.index.json").write_text(json.dumps(
        {"weight_map": {"a": "model-1.safetensors", "b": "model-2.safetensors"}}))
    (fxb / "model-1.safetensors").write_text("x")
    assert not fr.artifact_complete(str(fxb))           # 조각 하나가 아직 없다
    (fxb / "model-2.safetensors").write_text("x")
    assert fr.artifact_complete(str(fxb))

    only_config = tmp_path / "cfg"
    only_config.mkdir()
    (only_config / "config.json").write_text("{}")
    assert not fr.artifact_complete(str(only_config))
    assert not fr.artifact_complete(str(tmp_path / "nope"))


def test_router_models_available_and_reason(monkeypatch, tmp_path):
    """/router/models 각 행에 available, false 면 reason. 올릴 수 없는 모델도 목록에 남는다."""
    hub_repo(tmp_path, "furiosa-ai/Qwen3-8B-FP8", {
        "config.json": "{}", "q.fxb": "x", "model-1.safetensors": "x"})
    hub_repo(tmp_path, "furiosa-ai/Qwen3-4B-FP8", {"config.json": "{}"})       # 받다 만 것
    c = _client(monkeypatch)

    rows = _rows(c)
    assert rows["Qwen3-8B-FP8"]["available"] is True and rows["Qwen3-8B-FP8"]["reason"] is None
    assert rows["Qwen3-8B-FP8@dp2"]["available"] is True
    assert (rows["Qwen3-4B-FP8"]["available"], rows["Qwen3-4B-FP8"]["reason"]) == (False, "일부만 내려받음")
    assert (rows["gpt-oss-120b"]["available"], rows["gpt-oss-120b"]["reason"]) == (False, "내려받지 않음")
    # 목록에서 빠지지 않는다(콘솔이 흐리게 보인다)
    assert PIN in rows and rows[PIN]["available"] is False


def test_local_artifact_missing_is_file_missing(monkeypatch, tmp_path):
    """결과물 폴더(절대 경로)가 없으면 '파일 없음', 있으면 올릴 수 있다. 다른 파일과 상관없이 그 변형의 경로만 본다."""
    art = str(tmp_path / "art" / "my-tp8")
    write_extra(tmp_path, [{"id": "My-Model", "path": art, "tp": 8, "ctx": 32768, "tool": "hermes"}])
    c = _client(monkeypatch)
    assert c.post("/router/rescan").status_code == 200

    rows = _rows(c)
    assert (rows["My-Model"]["available"], rows["My-Model"]["reason"]) == (False, "파일 없음")
    os.makedirs(art)
    (tmp_path / "art" / "my-tp8" / "config.json").write_text("{}")
    assert _rows(c)["My-Model"]["reason"] == "일부 파일 없음"
    v2_dir(art)
    rows = _rows(c)
    assert rows["My-Model"]["available"] is True and rows["My-Model"]["reason"] is None


def test_availability_is_cached_briefly(monkeypatch, tmp_path):
    """같은 경로를 짧은 시간 안에 여러 번 묻으면 디스크를 다시 보지 않는다. rescan 은 그 기억을 지운다."""
    monkeypatch.setattr(fr, "AVAIL_TTL", 60.0)
    art = str(tmp_path / "a")
    write_extra(tmp_path, [{"id": "A", "path": art, "tp": 8, "ctx": 4096}])
    c = _client(monkeypatch)
    c.post("/router/rescan")
    assert _rows(c)["A"]["available"] is False
    v2_dir(art)
    assert _rows(c)["A"]["available"] is False           # 아직 기억한 값
    c.post("/router/rescan")
    assert _rows(c)["A"]["available"] is True


def test_preload_refuses_missing_local_files(monkeypatch, tmp_path):
    """결과물 폴더가 없는 모델 preload 는 카드를 비우기 전에 409 unavailable(떠 있는 모델을 헛되이 내리지 않게).
    내려받지 않은 허브 모델은 그대로 받는다(serve 가 내려받는다)."""
    write_extra(tmp_path, [{"id": "Gone", "path": str(tmp_path / "gone"), "tp": 8, "ctx": 4096}])
    r = fr.Router()
    called = []
    monkeypatch.setattr(r, "ensure", lambda m: called.append(m))
    c = _client(monkeypatch, r)
    c.post("/router/rescan")

    res = c.post("/router/preload", json={"model": "Gone"})
    assert res.status_code == 409
    body = res.json()
    assert (body["ok"], body["reason"], body["error"]["message"]) == (False, "unavailable", "파일 없음")
    assert r.state.get("Gone") is None

    res = c.post("/router/preload", json={"model": "Qwen3-8B-FP8"})
    assert res.status_code == 200
    assert wait_for(lambda: called == ["Qwen3-8B-FP8"])


def wait_for(pred, timeout=5.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.02)
    return False


# ── 추가 모델 파일과 rescan ─────────────────────────────────────────────────────
def test_rescan_adds_and_removes_extra_models(monkeypatch, tmp_path):
    """POST /router/rescan: 내장 목록 + 추가 파일을 다시 읽어 {"ok", "count", "added", "removed"}."""
    c = _client(monkeypatch)
    base = c.post("/router/rescan").json()
    assert base["ok"] is True and base["added"] == [] and base["removed"] == []
    n0 = base["count"]
    assert n0 == len(fr.all_model_ids()) == len(_rows(c))

    write_extra(tmp_path, [{"id": "New-8B", "path": "furiosa-ai/New-8B", "tp": 8, "ctx": 32768,
                            "tool": "hermes", "reasoning": "qwen3", "tools": "ok",
                            "extra": ["--max-model-len", "32768"]}])
    res = c.post("/router/rescan").json()
    assert res["ok"] is True
    assert res["added"][0] == "New-8B" and "New-8B@dp4" in res["added"]
    assert res["removed"] == []
    assert res["count"] == n0 + len(res["added"])

    rows = _rows(c)
    row = rows["New-8B"]
    assert (row["context"], row["tools"], row["cards"], row["kind"]) == (32768, "ok", 1, "chat")
    assert row["available"] is False and row["reason"] == "내려받지 않음"
    assert "New-8B" in [m["id"] for m in c.get("/v1/models").json()["data"]]
    reg = fr.REGISTRY["New-8B"]
    assert (reg["tool"], reg["reasoning"], reg["extra"], reg["cards"]) == ("hermes", "qwen3",
                                                                         ["--max-model-len", "32768"], 1)

    os.remove(tmp_path / "extra-models.yaml")
    res = c.post("/router/rescan").json()
    assert res["added"] == [] and "New-8B" in res["removed"] and res["count"] == n0
    assert "New-8B" not in _rows(c)


def test_extra_file_change_reloads_on_next_router_models(monkeypatch, tmp_path):
    """추가 파일을 고치면(mtime) rescan 없이 다음 /router/models 때 저절로 다시 읽는다. JSON 도 된다."""
    monkeypatch.setattr(fr, "EXTRA_MODELS", str(tmp_path / "extra.json"))
    c = _client(monkeypatch)
    assert "J1" not in _rows(c)

    p = write_extra(tmp_path, [{"id": "J1", "path": "org/J1", "tp": 32, "ctx": 8192}], name="extra.json")
    rows = _rows(c)
    assert rows["J1"]["cards"] == 4 and "J1@dp2" not in rows      # tp32 는 4장 독점, 변형 없음

    write_extra(tmp_path, [{"id": "J2", "path": "org/J2", "tp": 8, "ctx": 8192}], name="extra.json")
    bump(p)
    rows = _rows(c)
    assert "J2" in rows and "J1" not in rows


def test_extra_can_override_builtin(monkeypatch, tmp_path):
    """같은 id 는 추가 파일이 내장 항목을 바꾼다(예: 결과물 경로). 파일을 지우면 내장 값으로 돌아간다."""
    art = v2_dir(str(tmp_path / "llama-local"))
    write_extra(tmp_path, [{"id": "Qwen3-4B-FP8", "path": art, "tp": 8, "ctx": 16384, "tool": "hermes"}])
    c = _client(monkeypatch)
    res = c.post("/router/rescan").json()
    assert "Qwen3-4B-FP8" not in res["added"] and "Qwen3-4B-FP8" not in res["removed"]
    row = _rows(c)["Qwen3-4B-FP8"]
    assert (row["context"], row["available"]) == (16384, True)

    os.remove(tmp_path / "extra-models.yaml")
    c.post("/router/rescan")
    assert _rows(c)["Qwen3-4B-FP8"]["context"] == 40960


@pytest.mark.parametrize("models, why", [
    ([{"path": "org/x", "tp": 8, "ctx": 1}], "id"),
    ([{"id": "x", "tp": 8, "ctx": 1}], "path"),
    ([{"id": "x", "path": "org/x", "tp": 3, "ctx": 1}], "tp"),
    ([{"id": "x", "path": "org/x", "tp": 8}], "ctx"),
    ([{"id": "x", "path": "org/x", "tp": 8, "ctx": 1, "extra": "--a"}], "extra"),
    ([{"id": "x", "path": "org/x", "tp": 8, "ctx": 1, "tools": "great"}], "tools"),
    ([{"id": "x", "path": "org/x", "tp": 8, "ctx": 1, "kind": "image"}], "kind"),
    ([{"id": "x", "path": "org/x", "tp": 8, "ctx": 1}, {"id": "x", "path": "org/y", "tp": 8, "ctx": 1}], "x"),
    ([{"id": "x@dp2", "path": "org/x", "tp": 8, "ctx": 1}], "@"),
])
def test_bad_extra_file_keeps_previous_list(monkeypatch, tmp_path, models, why):
    """틀린 추가 파일: rescan 은 400 과 까닭, 목록은 앞의 것 그대로. 저절로 다시 읽기도 앞의 목록을 지킨다."""
    write_extra(tmp_path, [{"id": "Good", "path": "org/Good", "tp": 8, "ctx": 4096}])
    c = _client(monkeypatch)
    assert "Good" in c.post("/router/rescan").json()["added"]

    p = write_extra(tmp_path, models)
    bump(p)
    assert "Good" in _rows(c)                            # 저절로 다시 읽기가 실패해도 목록은 그대로
    res = c.post("/router/rescan")
    assert res.status_code == 400
    body = res.json()
    assert body["ok"] is False and body["reason"] == "bad_catalog"
    assert why in body["error"]["message"]
    assert "Good" in _rows(c)


def test_broken_yaml_keeps_previous_list(monkeypatch, tmp_path):
    write_extra(tmp_path, [{"id": "Good", "path": "org/Good", "tp": 8, "ctx": 4096}])
    c = _client(monkeypatch)
    c.post("/router/rescan")
    p = tmp_path / "extra-models.yaml"
    p.write_text("models: [ {id: x\n", encoding="utf-8")
    bump(p)
    assert "Good" in _rows(c)
    res = c.post("/router/rescan")
    assert res.status_code == 400
    msg = res.json()["error"]["message"]
    assert "\n" not in msg and "YAML" in msg and "째 줄" in msg


def test_rescan_keeps_running_model(monkeypatch, tmp_path):
    """떠 있는 모델은 추가 파일에서 빠져도 내려갈 때까지 목록에 남는다(evict 가 404 로 막히지 않게). 내려간 뒤에는 빠진다."""
    write_extra(tmp_path, [{"id": "Run", "path": "org/Run", "tp": 8, "ctx": 4096}])
    r = fr.Router()
    stopped = []

    def _stop(b):
        stopped.append(b.model_id)
        r.running.pop(b.model_id, None)
        r.state.pop(b.model_id, None)

    monkeypatch.setattr(r, "_stop", _stop)
    c = _client(monkeypatch, r)
    c.post("/router/rescan")
    b = fr.Backend("Run@dp2", 8411, FakeProc(), [0, 1])
    b.ready, b.ready_at = True, time.time()
    r.running["Run@dp2"] = b
    r.state["Run@dp2"] = "up"

    os.remove(tmp_path / "extra-models.yaml")
    res = c.post("/router/rescan").json()
    assert "Run" not in res["removed"] and "Run@dp2" not in res["removed"]
    assert res["kept"] == ["Run"]
    assert _rows(c)["Run@dp2"]["state"] == "up"

    assert c.post("/router/evict", json={"model": "Run@dp2"}).json() == {"ok": True}
    assert stopped == ["Run@dp2"]
    rows = _rows(c)                                      # 내려갔으니 다음 목록에서 빠진다
    assert "Run" not in rows and "Run@dp2" not in rows


def test_rescan_uses_router_key(monkeypatch):
    """라우터 키가 있으면 preload 처럼 rescan 도 Bearer 가 있어야 한다."""
    from fastapi.testclient import TestClient
    monkeypatch.setenv("SDI_API_KEY", "k")
    monkeypatch.setattr(fr, "ROUTER", fr.Router())
    c = TestClient(fr.build_app())
    assert c.post("/router/rescan").status_code == 401
    res = c.post("/router/rescan", headers={"authorization": "Bearer k"})
    assert res.status_code == 200 and res.json()["ok"] is True


def test_missing_extra_file_is_ignored(monkeypatch, tmp_path):
    """추가 파일이 없으면 내장 목록만. 오류가 아니다."""
    c = _client(monkeypatch)
    res = c.post("/router/rescan")
    assert res.status_code == 200
    assert res.json()["count"] == len(fr.all_model_ids())
    assert set(fr.REGISTRY) == set(fr._BUILTIN_REGISTRY)


def test_pin_accepts_extra_model(monkeypatch, tmp_path):
    """추가 파일의 모델도 핀으로 고를 수 있다(파일을 고친 뒤 rescan 없이도)."""
    monkeypatch.setattr(fr, "PINNED", set())
    c = _client(monkeypatch)
    write_extra(tmp_path, [{"id": "Pin-Me", "path": "org/Pin-Me", "tp": 8, "ctx": 4096}])
    res = c.post("/router/pin", json={"models": ["Pin-Me@dp4"]})
    assert res.status_code == 200 and res.json()["pinned"] == ["Pin-Me@dp4"]


def test_example_extra_file_loads(monkeypatch):
    """server/extra-models.example.yaml 은 그대로 읽힌다(예시가 낡지 않게)."""
    ex = os.path.join(os.path.dirname(os.path.abspath(__file__)), "extra-models.example.yaml")
    regs, tools = fr.load_extra_models(ex)
    reg = regs["Qwen3-32B-FP8-local"]
    assert (reg["tp"], reg["ctx"], reg["tool"], reg["reasoning"], reg["cards"]) == (8, 40960, "hermes", "qwen3", 1)
    assert tools["Qwen3-32B-FP8-local"] == "ok"
