"""serve-router.sh 의 .env 로더(load-env.sh) 테스트 — bash 로 로더만 source 한다(라우터는 안 띄운다).

실행:
  taskset -c 64-127 <venv>/bin/python -m pytest server/test_serve_router_env.py -q
"""
import os
import subprocess

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
LOADER = os.path.join(HERE, "load-env.sh")


def load(tmp_path, env_text, shell_env, *keys):
    """임시 .env 를 쓰고 `set -euo pipefail` 아래에서 로더를 source 한 뒤 keys 값을 찍는다.
    반환 (returncode, {key: 값 | None(unset)}, stderr). serve-router.sh 와 같은 셸 옵션이다."""
    envfile = tmp_path / ".env"
    envfile.write_text(env_text)
    show = "; ".join(f'if [ -z "${{{k}+x}}" ]; then echo "{k}=<unset>"; else echo "{k}=${{{k}}}"; fi'
                     for k in keys)
    script = f'set -euo pipefail; source "{LOADER}" "{envfile}"; echo LOADED; {show}'
    env = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/"), **shell_env}
    p = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
    vals = {}
    for line in p.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            vals[k] = None if v == "<unset>" else v
    return p.returncode, vals, p.stderr


def test_env_sets_unset_vars(tmp_path):
    rc, v, err = load(tmp_path, "ROUTER_PIN=gpt-oss-20b@dp4\nROUTER_FORCE_EVICT=0\n", {},
                      "ROUTER_PIN", "ROUTER_FORCE_EVICT")
    assert rc == 0, err
    assert v == {"ROUTER_PIN": "gpt-oss-20b@dp4", "ROUTER_FORCE_EVICT": "0"}


def test_shell_value_wins_even_when_empty(tmp_path):
    """셸에서 준 변수는 .env 보다 우선 — 빈 값으로 준 것도 '준 것'이다(ROUTER_PIN= bash serve-router.sh 로
    핀 없이 한 번 띄우는 길). 검토 지적: 예전 `${!k:-}` 는 빈 값을 '안 준 것'으로 봐 .env 핀을 다시 씌웠다."""
    rc, v, err = load(tmp_path, "ROUTER_PIN=gpt-oss-20b@dp4\nPRESET=fromenv\n",
                      {"ROUTER_PIN": "", "PRESET": "fromshell"}, "ROUTER_PIN", "PRESET")
    assert rc == 0, err
    assert v == {"ROUTER_PIN": "", "PRESET": "fromshell"}


def test_last_line_already_set_does_not_abort(tmp_path):
    """마지막 줄이 '셸 값 우선'으로 건너뛰어져도 set -e 아래에서 로더가 1 로 끝나면 안 된다."""
    rc, v, err = load(tmp_path, "A=1\nROUTER_PIN=x", {"ROUTER_PIN": "shell"}, "A", "ROUTER_PIN")
    assert rc == 0, err
    assert v == {"A": "1", "ROUTER_PIN": "shell"}


def test_values_are_literal(tmp_path):
    """값은 글자 그대로 — 따옴표도 값에 들어가고, 두 번째 '=' 부터는 값이다. 빈 줄·주석·CRLF 는 건너뛴다."""
    text = 'QUOTED="x y"\r\n\n# 주석\nEQ=a=b\n  \nSPACE_IN_VALUE=a b c\n'
    rc, v, err = load(tmp_path, text, {}, "QUOTED", "EQ", "SPACE_IN_VALUE")
    assert rc == 0, err
    assert v == {"QUOTED": '"x y"', "EQ": "a=b", "SPACE_IN_VALUE": "a b c"}


@pytest.mark.parametrize("bad", ["SPACED = 1", "NO_EQUALS", "1ABC=2", "a-b=1", "=1"])
def test_malformed_line_fails_naming_file_and_line(tmp_path, bad):
    """꼴이 틀린 줄은 .env 경로·줄 번호를 들어 실패(exit 1) — bash 의 'invalid variable name' 이 아니라.
    실패하면 뒤(라우터 재시작)는 돌지 않는다."""
    rc, v, err = load(tmp_path, f"OK=1\n{bad}\nAFTER=1\n", {}, "OK", "AFTER")
    assert rc != 0
    assert "LOADED" not in err and v.get("AFTER") is None
    assert ".env:2" in err and bad in err, err
    assert "invalid variable name" not in err


def test_missing_env_file_is_fine(tmp_path):
    p = subprocess.run(["bash", "-c", f'set -euo pipefail; source "{LOADER}" "{tmp_path}/nope.env"; echo LOADED'],
                       env={"PATH": os.environ["PATH"]}, capture_output=True, text=True)
    assert p.returncode == 0 and p.stdout.strip() == "LOADED", p.stderr


def test_serve_router_sources_loader():
    """serve-router.sh 는 인라인 로더 대신 load-env.sh 를 같은 폴더의 .env 로 source 한다."""
    src = open(os.path.join(HERE, "serve-router.sh")).read()
    assert 'source "$HERE/load-env.sh" "$HERE/.env"' in src
    assert "${!k" not in src
