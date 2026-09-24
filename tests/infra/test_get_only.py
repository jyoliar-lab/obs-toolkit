"""GET 유일성 — obs/ 안에서 urllib·http 를 import 하는 모듈은 core.py 뿐이고, core.py 도 GET 외의 메서드를 만들지 않는다."""
from __future__ import annotations

import ast
from pathlib import Path

ROOT: Path = Path(__file__).resolve().parents[2]
FORBIDDEN: tuple[str, ...] = ("urllib", "http", "socket", "ssl", "requests", "httpx", "aiohttp")


def _imports(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def _py_files() -> list[Path]:
    files = list((ROOT / "obs").rglob("*.py"))
    files += list((ROOT / "skills").rglob("*.py"))
    return files


def test_only_core_imports_http() -> None:
    """GET 유일성의 절반 — HTTP 를 만지는 모듈은 obs/core.py 하나여야 한다.
    obs/** 와 skills/** 의 파이썬 파일을 AST 로 훑어 urllib·http·socket·ssl·requests 류 import 가 core.py 밖에 없음을 확인한다.
    새 어댑터가 자기 HTTP 클라이언트를 들이면 여기서 막힌다."""
    offenders: list[str] = []
    for path in _py_files():
        if path == ROOT / "obs" / "core.py":
            continue
        found = _imports(ast.parse(path.read_text(encoding="utf-8"))) & set(FORBIDDEN)
        if found:
            offenders.append("%s: %s" % (path.relative_to(ROOT), sorted(found)))
    assert not offenders, "HTTP 는 obs/core.py 의 Client 만 만진다: %s" % offenders


def test_core_never_sets_http_method() -> None:
    """urllib.request.Request(...) 호출에 method= · data= 가 없다 — urllib 기본 = GET."""
    tree = ast.parse((ROOT / "obs" / "core.py").read_text(encoding="utf-8"))
    request_calls = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "Request":
            request_calls += 1
            kws = {k.arg for k in node.keywords}
            assert "method" not in kws and "data" not in kws, "Request 에 method/data 를 주지 않는다"
            assert len(node.args) == 1, "Request 위치 인자는 url 하나 — data 를 위치 인자로도 주지 않는다"
    assert request_calls == 1, "HTTP 요청을 만드는 자리는 정확히 하나다"
