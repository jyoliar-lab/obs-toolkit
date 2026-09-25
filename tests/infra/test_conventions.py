"""테스트 규약(.claude/rules/python-tests.md)을 CI 에서 지키는 메타테스트 — tests/ 의 파이썬 파일을 AST(tests/contracts/test_get_only.py 의
_imports 방식) 와 tokenize(주석) 로 훑어 여섯 규칙의 위반을 센다. 위반은 0 이 기준이다 — 재편(ops-workflow#107)으로 래칫 allowlist 가
비어 삭제됐다. 실패 문구가 위반을 자리 · 규칙별로 찍는다."""
from __future__ import annotations

import ast
import io
import re
import tokenize
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT: Path = Path(__file__).resolve().parents[2]
TESTS: Path = ROOT / "tests"
CONTRACTS: Path = TESTS / "contracts"
TIERS: frozenset[str] = frozenset({"contracts", "format", "infra"})
CONFTEST: Path = TESTS / "conftest.py"

RULES: dict[str, str] = {
    "docstring": "test_* 함수에 docstring 이 없다 — 한 테스트 = 한 주장, docstring 이 그 주장을 말한다",
    "and": "assert 의 조건이 and 로 이어져 있다 — 실패하면 어느 조건인지 보이지 않는다. 조건마다 assert 한 줄",
    "replay": "tests/contracts/ 에서 replay( · load_response( 를 부른다 — contracts 는 손으로 만든 최소 입력만",
    "physical": "PHYSICAL 이 tests/conftest.py 밖에 정의된다 — 목록은 한 곳",
    "date": "주석에 재녹화 · YYYY-MM-DD 꼴 날짜가 있다 — 출처는 커밋 메시지에",
    "tier": "test_*.py 가 세 티어(tests/contracts · format · infra) 밖에 있다 — 테스트 파일은 티어 안에",
}

DATE_COMMENT: re.Pattern[str] = re.compile(r"재녹화|\d{4}-\d{2}-\d{2}")
REPLAY_CALLS: frozenset[str] = frozenset({"replay", "load_response"})


@dataclass(frozen=True)
class Violation:
    """위반 하나 — 어느 자리(키)에서 어느 규칙인가."""
    key: str
    rule: str


def _py_files() -> list[Path]:
    return sorted(TESTS.rglob("*.py"))


def _owner(tree: ast.Module, lineno: int) -> str | None:
    """줄이 속한 최상위 함수의 이름 — 함수 밖이면 None."""
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.lineno <= lineno <= (node.end_lineno or node.lineno):
            return node.name
    return None


def _key(path: Path, tree: ast.Module, lineno: int) -> str:
    owner = _owner(tree, lineno)
    rel = path.relative_to(ROOT).as_posix()
    return rel if owner is None else "%s::%s" % (rel, owner)


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _assigned_names(node: ast.Assign | ast.AnnAssign) -> set[str]:
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    return {t.id for t in targets if isinstance(t, ast.Name)}


def _outside_tiers(path: Path) -> bool:
    """tests/ 아래 test_*.py 인데 첫 디렉터리가 세 티어 중 하나가 아니다(tests/ 직속 포함)."""
    rel = path.relative_to(TESTS)
    return path.name.startswith("test_") and rel.parts[0] not in TIERS


def scan(path: Path, source: str) -> list[Violation]:
    """파일 하나의 위반 전부 — 규칙 1~4 는 AST, 규칙 5 는 주석 토큰, 규칙 6 은 경로."""
    tree = ast.parse(source)
    out: list[Violation] = []
    if _outside_tiers(path):
        out.append(Violation(path.relative_to(ROOT).as_posix(), "tier"))
    in_contracts = CONTRACTS in path.parents
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("test_") and not ast.get_docstring(node):
                out.append(Violation(_key(path, tree, node.lineno), "docstring"))
        elif isinstance(node, ast.Assert):
            if isinstance(node.test, ast.BoolOp) and isinstance(node.test.op, ast.And):
                out.append(Violation(_key(path, tree, node.lineno), "and"))
        elif isinstance(node, ast.Call):
            if in_contracts and _call_name(node) in REPLAY_CALLS:
                out.append(Violation(_key(path, tree, node.lineno), "replay"))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            if path != CONFTEST and "PHYSICAL" in _assigned_names(node):
                out.append(Violation(_key(path, tree, node.lineno), "physical"))
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.COMMENT and DATE_COMMENT.search(tok.string):
            out.append(Violation(_key(path, tree, tok.start[0]), "date"))
    return out


def _violations() -> Counter[tuple[str, str]]:
    counts: Counter[tuple[str, str]] = Counter()
    for path in _py_files():
        for v in scan(path, path.read_text(encoding="utf-8")):
            counts[(v.key, v.rule)] += 1
    return counts


def _explain(counts: Counter[tuple[str, str]]) -> str:
    return "\n".join("  %s  [%s] ×%d — %s" % (key, rule, n, RULES[rule]) for (key, rule), n in sorted(counts.items()))


def test_no_convention_violations() -> None:
    """tests/ 에 규약 위반이 없다 — 있으면 자리 · 규칙 · 개수를 말한다. 고치는 것이 답이다."""
    actual = _violations()
    assert not actual, "규약 위반(.claude/rules/python-tests.md):\n%s" % _explain(actual)


INFRA: Path = TESTS / "infra"
SAMPLES: dict[str, tuple[Path, str]] = {
    # 규칙 → (파일 자리, 그 규칙 하나만 어기는 소스). 자리는 티어 안 — 규칙 6 에 걸리지 않게
    "docstring": (INFRA / "test_x.py", 'def test_a() -> None:\n    assert 1\n'),
    "and": (INFRA / "test_x.py", 'def test_a() -> None:\n    """a."""\n    assert 1 and 2\n'),
    "replay": (CONTRACTS / "test_x.py", 'def test_a(replay: object) -> None:\n    """a."""\n    replay("f.json")\n'),
    "physical": (INFRA / "test_x.py", 'PHYSICAL: tuple[str, ...] = ("a",)\n'),
    "date": (INFRA / "test_x.py", 'x = 1  # 1999-12-31 에 봤다\n'),
    "tier": (TESTS / "test_x.py", 'def test_a() -> None:\n    """a."""\n'),
}


@pytest.mark.parametrize("rule", sorted(RULES))
def test_each_rule_detects_its_sample(rule: str) -> None:
    """여섯 규칙이 각각 자기 표본을 잡고 다른 규칙은 잡지 않는다 — 메타테스트가 실제로 보는지의 확인."""
    path, source = SAMPLES[rule]
    assert [v.rule for v in scan(path, source)] == [rule]


def test_nested_directory_outside_tiers_is_a_violation() -> None:
    """규칙 6 은 tests/ 직속만이 아니다 — 티어가 아닌 하위 디렉터리의 test_*.py 도 위반이다."""
    assert [v.rule for v in scan(TESTS / "misc" / "test_x.py", 'def test_a() -> None:\n    """a."""\n')] == ["tier"]


def test_conftest_may_define_physical() -> None:
    """규칙 4 의 예외 — PHYSICAL 의 자리인 tests/conftest.py 는 위반이 아니다."""
    assert scan(CONFTEST, 'PHYSICAL: tuple[str, ...] = ("a",)\n') == []
