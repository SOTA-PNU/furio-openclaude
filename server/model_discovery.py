"""로컬 HF 스냅숏과 모델 폴더의 JSON/FXB 메타데이터만 읽어 카탈로그를 만든다.

모델 로드, 외부 다운로드, 실행 파일 호출은 하지 않는다. FXB는 ZIP의 manifest.json만 읽는다.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import zipfile

MAX_METADATA = 2 * 1024 * 1024
TP_VALUES = (1, 2, 4, 8, 16, 32)
PARSERS = {
    "gemma4": ("gemma4", "gemma4"), "gemma4_text": ("gemma4", "gemma4"),
    "llama": ("llama3_json", None), "qwen2": ("hermes", None),
    "qwen3": ("hermes", "qwen3"), "qwen3_moe": ("hermes", "qwen3"),
    "gpt_oss": ("openai", None), "exaone4": ("hermes", "exaone4"),
    "solar_open": ("solar_open", "solar_open"),
}


def _json(path):
    if path.stat().st_size > MAX_METADATA:
        raise ValueError("메타데이터가 너무 큽니다")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("메타데이터가 객체가 아닙니다")
    return value


def _dict(value):
    return value if isinstance(value, dict) else {}


def _positive(value):
    return type(value) is int and value > 0


def runtime_info(binary):
    """서빙 venv의 버전과 설치된 파서 이름. 패키지는 import하지 않는다."""
    root = Path(binary).expanduser().resolve().parent.parent
    release, tools, reasoning = None, set(), set()
    for package in sorted(root.glob("lib/python*/site-packages/furiosa_llm")):
        version = package / "git_version.txt"
        if version.is_file():
            match = re.match(r"(\d+\.\d+)", version.read_text().strip())
            if match:
                release = "v" + match[1]
        for kind, result in (("tool", tools), ("reasoning", reasoning)):
            for parser in (package / "server" / (kind + "_parsers")).glob("*.py"):
                try:
                    result.update(re.findall(r'register_module\(["\']([^"\']+)["\']', parser.read_text()))
                except (OSError, UnicodeError):
                    pass
    return release, tools, reasoning


def _describe(folder, tool_parsers, reasoning_parsers):
    config_path = folder / "config.json"
    config = _json(config_path) if config_path.is_file() else {}
    text = _dict(config.get("text_config")) or config
    fxbs = sorted(folder.glob("*.fxb"))
    if fxbs:
        # 서로 다른 빌드 중 하나를 임의로 고르면 TP와 실제 실행 파일이 어긋날 수 있다.
        if len(fxbs) != 1:
            raise ValueError("FXB가 여러 개입니다. 사용할 빌드를 별도 폴더로 나누세요")
        with zipfile.ZipFile(fxbs[0]) as bundle:
            info = bundle.getinfo("manifest.json")
            if info.file_size > MAX_METADATA:
                raise ValueError("FXB 메타데이터가 너무 큽니다")
            manifest = json.loads(bundle.read(info))
        manifest = _dict(manifest)
        model = _dict(manifest.get("model"))
        parallel = _dict(manifest.get("parallel_config"))
    elif (folder / "artifact.json").is_file():
        artifact = _json(folder / "artifact.json")
        model = _dict(artifact.get("model"))
        metadata = _dict(model.get("model_metadata"))
        text = _dict(metadata.get("hf_configs")) or text
        parallel = _dict(model.get("parallel_config"))
        # v2 MoE는 기존 서빙 경로에서 답이 조용히 틀릴 수 있어 자동 등록하지 않는다.
        if "moe" in str(text.get("model_type", "")).lower() or any(
                _positive(text.get(key)) for key in ("num_experts", "num_local_experts")):
            raise ValueError("MoE v2 결과물은 자동 등록하지 않습니다. FXB를 사용하세요")
        model = text
    else:
        raise ValueError("실행 가능한 FXB 또는 v2 결과물이 없습니다")
    tp = parallel.get("tensor_parallel_size")
    if type(tp) is not int or tp not in TP_VALUES:
        raise ValueError("빌드의 TP 정보를 확인할 수 없습니다")
    if parallel.get("pipeline_parallel_size", 1) != 1:
        raise ValueError("PP가 고정된 빌드는 수동 등록이 필요합니다")
    ctx = model.get("max_position_embeddings") or text.get("max_position_embeddings")
    if not _positive(ctx):
        raise ValueError("컨텍스트 한도를 확인할 수 없습니다")
    architectures = text.get("architectures") or config.get("architectures") or []
    architecture = model.get("architecture") or (architectures[0] if isinstance(architectures, list) and architectures else "")
    if not isinstance(architecture, str) or not architecture.endswith(("ForCausalLM", "ForConditionalGeneration")):
        raise ValueError("채팅 모델 아키텍처가 아닙니다")
    family = text.get("model_type") or config.get("model_type")
    tool, reasoning = PARSERS.get(family, (None, None))
    if "coder" in folder.name.lower() and "qwen3_coder" in tool_parsers:
        tool, reasoning = "qwen3_coder", None
    tool = tool if tool in tool_parsers else None
    reasoning = reasoning if reasoning in reasoning_parsers else None
    return {"path": str(folder), "tp": tp, "cards": max(1, (tp + 7) // 8), "pp": 1,
            "tool": tool, "reasoning": reasoning, "ctx": ctx, "kind": "chat", "fxb": bool(fxbs)}


def _snapshots(repo, revision):
    snapshots = repo / "snapshots"
    if not snapshots.is_dir():
        return []
    paths = []
    for ref in (revision, "main"):
        if not ref:
            continue
        try:
            sha = (repo / "refs" / ref).read_text().strip()
            if not re.fullmatch(r"[A-Za-z0-9_-]+", sha):
                continue
            path = snapshots / sha
            if path.is_dir() and path not in paths:
                paths.append(path)
        except OSError:
            pass
    for path in sorted(snapshots.iterdir(), key=lambda p: p.name):
        if path.is_dir() and path not in paths:
            paths.append(path)
    return paths


def _local_folders(root):
    """모델 폴더는 두 단계까지만 본다. 가중치·캐시 blob 폴더 아래로 내려가지 않는다."""
    root = Path(root).expanduser()
    if root.is_file() and root.suffix == ".fxb":
        root = root.parent
    if not root.is_dir():
        return
    for here, directories, files in os.walk(root, followlinks=False):
        depth = len(Path(here).relative_to(root).parts)
        directories[:] = sorted(d for d in directories if not d.startswith(".") and d not in ("blobs", "snapshots", "node_modules")) if depth < 2 else []
        if "artifact.json" in files or "config.json" in files or any(f.endswith(".fxb") for f in files):
            yield Path(here)
            directories[:] = []


def discover(hubs, roots, known, *, runtime=(None, (), ())):
    """찾은 등록 항목·도구 등급·진단. known(내장/수동)은 같은 경로를 중복 등록하지 않는다."""
    revision, tool_parsers, reasoning_parsers = runtime
    registries, support, skipped = {}, {}, []
    known_paths = {os.path.realpath(r["path"]) if os.path.isabs(r["path"]) else r["path"] for r in known.values()}
    seen_repos, seen_paths = set(), set()

    def add(folder, name, source):
        real = os.path.realpath(folder)
        if real in known_paths or real in seen_paths:
            return True
        try:
            entry = _describe(folder, tool_parsers, reasoning_parsers)
        except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile, UnicodeError) as error:
            if len(skipped) < 50:
                skipped.append({"path": str(folder), "reason": str(error)[:240]})
            return False
        mid = name
        if mid in known or mid in registries:
            mid = source + "/" + name
        if "@" in mid or mid in known or mid in registries:
            if len(skipped) < 50:
                skipped.append({"path": str(folder), "reason": "같은 모델 이름이 이미 등록돼 있습니다"})
            return False
        registries[mid] = entry
        support[mid] = "weak" if entry["tool"] else "no"
        seen_paths.add(real)
        return True

    for hub in hubs:
        try:
            repos = sorted(Path(hub).glob("models--*"))
        except OSError:
            continue
        for repo in repos:
            parts = repo.name.split("--", 2)
            if len(parts) != 3:
                continue
            owner, name = parts[1:]
            repo_id = owner + "/" + name
            if repo_id in known_paths or repo_id in seen_repos:
                continue
            try:
                candidates = _snapshots(repo, revision)
            except OSError:
                continue
            for folder in candidates:
                if add(folder, name if owner == "furiosa-ai" else repo_id, owner):
                    seen_repos.add(repo_id)
                    break
    for root in roots:
        for folder in _local_folders(root):
            add(folder, folder.name, "local")
    return registries, support, {"found": list(registries), "skipped": skipped}
