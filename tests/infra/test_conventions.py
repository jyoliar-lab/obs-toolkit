"""테스트 규약(.claude/rules/python-tests.md)을 CI 에서 지키는 메타테스트 — tests/ 의 파이썬 파일을 AST(tests/infra/test_get_only.py 의
_imports 방식) 와 tokenize(주석) 로 훑어 다섯 규칙의 위반을 세고, 래칫 ALLOWLIST 와 비교한다.
새 위반은 즉시 실패, 기존 위반은 줄어들기만 — 줄었는데 ALLOWLIST 를 안 줄여도 실패한다. 전부 비면 ALLOWLIST 를 지운다."""
from __future__ import annotations

import ast
import io
import json
import re
import tokenize
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT: Path = Path(__file__).resolve().parents[2]
TESTS: Path = ROOT / "tests"
CONTRACTS: Path = TESTS / "contracts"
CONFTEST: Path = TESTS / "conftest.py"

RULES: dict[str, str] = {
    "docstring": "test_* 함수에 docstring 이 없다 — 한 테스트 = 한 주장, docstring 이 그 주장을 말한다",
    "and": "assert 의 조건이 and 로 이어져 있다 — 실패하면 어느 조건인지 보이지 않는다. 조건마다 assert 한 줄",
    "replay": "tests/contracts/ 에서 replay( · load_response( 를 부른다 — contracts 는 손으로 만든 최소 입력만",
    "physical": "PHYSICAL 이 tests/conftest.py 밖에 정의된다 — 목록은 한 곳",
    "date": "주석에 재녹화 · YYYY-MM-DD 꼴 날짜가 있다 — 출처는 커밋 메시지에",
}

ALLOWLIST: dict[str, dict[str, int]] = {
    "tests/infra/test_get_only.py::test_core_never_sets_http_method": {"and": 1},
    "tests/test_cli.py::test_doctor_lines_show_url_and_hide_token": {"and": 2},
    "tests/test_cli.py::test_emit_always_writes_even_when_fits": {"and": 1},
    "tests/test_cli.py::test_emit_quiet_writes_files_prints_only_path": {"and": 1},
    "tests/test_cli.py::test_emit_truncates_and_spills": {"and": 1},
    "tests/test_cli.py::test_gitignore_and_rules": {"and": 1},
    "tests/test_cli.py::test_logql_streams": {"and": 3, "date": 1},
    "tests/test_cli.py::test_parse_metric_shapes": {"and": 2},
    "tests/test_cli.py::test_parse_spans_error_detection": {"and": 2, "date": 1},
    "tests/test_cli.py::test_plugin_manifests": {"and": 3},
    "tests/test_cli.py::test_promql_instant_up": {"and": 3, "date": 1},
    "tests/test_cli.py::test_promql_points": {"and": 1},
    "tests/test_cli.py::test_promql_range_summary": {"and": 2, "date": 1},
    "tests/test_cli.py::test_scripts_are_thin_executable_shims": {"and": 1},
    "tests/test_cli.py::test_shim_finds_plugin_root_from_any_cwd": {"and": 1},
    "tests/test_cli.py::test_trace_error_span_shows_status_message": {"and": 3},
    "tests/test_cli.py::test_traceql_search_then_trace": {"and": 4, "date": 1},
    "tests/test_config.py::test_defaults_when_nothing_set": {"and": 1},
    "tests/test_config.py::test_env_overrides_file": {"and": 2},
    "tests/test_config.py::test_file_overrides_default": {"and": 3},
    "tests/test_core.py::test_dataclasses_are_json_serializable": {"and": 1},
    "tests/test_core.py::test_doctor_shows_physical_and_probes": {"and": 2},
    "tests/test_core.py::test_doctor_without_token_skips_probes": {"and": 1},
    "tests/test_core.py::test_gateway_errors_are_agent_fixable": {"and": 1},
    "tests/test_core.py::test_http_errors_are_logical": {"and": 1},
    "tests/test_core.py::test_not_found_is_agent_fixable": {"and": 1},
    "tests/test_core.py::test_parse_duration": {"and": 2},
    "tests/test_core.py::test_parse_time": {"and": 2},
    "tests/test_core.py::test_query_error_passes_backend_message": {"and": 1},
    "tests/test_core.py::test_read_timeout_is_agent_fixable": {"and": 1},
    "tests/test_core.py::test_spill_writes_and_prunes": {"and": 1},
    "tests/test_core.py::test_transport_failure_is_logical": {"and": 1},
    "tests/test_core.py::test_transport_never_consults_system_proxy": {"and": 1},
    "tests/test_core.py::test_urllib_read_timeout_maps_to_transport_timeout": {"and": 1},
    "tests/test_core.py::test_window": {"and": 1},
    "tests/test_fixtures.py::test_at_is_explicit_and_not_derived_from_url": {"and": 3},
    "tests/test_fixtures.py::test_every_fixture_has_a_recipe_and_vice_versa": {"and": 1},
    "tests/test_fixtures.py::test_fixture_is_recorded_format_with_request_and_response": {"and": 4},
    "tests/test_fixtures.py::test_metrics_fixtures_record_assembled_queries": {"and": 5},
    "tests/test_fixtures.py::test_query_string_parsing_keeps_repeats_and_blanks": {"and": 1, "docstring": 1},
    "tests/test_fixtures.py::test_recorder_strips_physical_keeps_order_and_writes_envelope": {"and": 5},
    "tests/test_fixtures.py::test_trace_fixture_records_search_then_trace_in_order": {"and": 4},
    "tests/test_fixtures.py::test_unmarked_fixtures_come_from_recorder_and_manual_ones_say_why": {"date": 1},
    "tests/test_metrics.py::test_auto_step_is_subquery_resolution": {"and": 2},
    "tests/test_metrics.py::test_compare_baseline_out_of_retention_says_so": {"and": 3},
    "tests/test_metrics.py::test_compare_both_empty": {"and": 2, "docstring": 1},
    "tests/test_metrics.py::test_compare_default_baseline_is_one_day": {"and": 2},
    "tests/test_metrics.py::test_compare_fixture_one_query_offset": {"and": 7},
    "tests/test_metrics.py::test_compare_missing_series_and_zero_do_not_divide": {"and": 2},
    "tests/test_metrics.py::test_compare_prev_baseline": {"and": 2},
    "tests/test_metrics.py::test_compare_summary_preamble_has_query_window_baseline": {"and": 2},
    "tests/test_metrics.py::test_help_mentions_operations_without_physical": {"and": 4},
    "tests/test_metrics.py::test_metrics_run_dispatch": {"and": 3},
    "tests/test_metrics.py::test_promql_main_dispatches_subcommand": {"and": 5},
    "tests/test_metrics.py::test_summaries_of_requires_n_and_marks_missing_as_nan": {"and": 2},
    "tests/test_metrics.py::test_summarize_empty_is_no_data_not_zero": {"and": 1},
    "tests/test_metrics.py::test_summarize_fixture_one_query": {"and": 7},
    "tests/test_metrics.py::test_summarize_query_is_seven_stats_in_one_expression": {"and": 3},
    "tests/test_metrics.py::test_summarize_requires_query": {"docstring": 1},
    "tests/test_metrics.py::test_top_clips_to_k_even_if_server_returns_more": {"and": 1},
    "tests/test_metrics.py::test_top_empty_is_no_data": {"and": 1, "docstring": 1},
    "tests/test_metrics.py::test_top_wraps_query_server_side": {"and": 3, "date": 1},
    "tests/test_metrics.py::test_window_baseline_prev_and_serialization": {"and": 2},
    "tests/test_replay.py::test_load_call_returns_request_key_and_at": {"and": 2},
    "tests/test_replay.py::test_replay_transport_checks_each_call_in_order_and_shows_both_keys": {"and": 1},
    "tests/test_replay.py::test_replay_transport_fails_on_wrong_query_not_just_path": {"and": 1},
    "tests/test_replay.py::test_request_key_strips_physical_and_ignores_param_order": {"and": 1},
}
"""래칫 — 현재 위반. 키는 `<tests/ 상대 경로>::<최상위 함수 이름>`(함수 밖이면 경로만), 값은 규칙별 개수.
위반을 고치면 여기서 그만큼 뺀다(안 빼면 실패). 실패 문구가 지금 상태를 이 꼴로 찍어 준다. 전부 비면 이 상수를 지운다."""

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


def scan(path: Path, source: str) -> list[Violation]:
    """파일 하나의 위반 전부 — 규칙 1~4 는 AST, 규칙 5 는 주석 토큰."""
    tree = ast.parse(source)
    out: list[Violation] = []
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


def _allowed() -> Counter[tuple[str, str]]:
    return Counter({(key, rule): n for key, rules in ALLOWLIST.items() for rule, n in rules.items()})


def _as_allowlist(counts: Counter[tuple[str, str]]) -> str:
    """ALLOWLIST 꼴로 찍는다 — 실패 문구에서 그대로 복사한다."""
    by_key: dict[str, dict[str, int]] = {}
    for (key, rule), n in sorted(counts.items()):
        by_key.setdefault(key, {})[rule] = n
    body = "".join("    %s: %s,\n" % (json.dumps(k, ensure_ascii=False), json.dumps(v, ensure_ascii=False)) for k, v in by_key.items())
    return "{\n%s}" % body


def _explain(pairs: dict[tuple[str, str], tuple[int, int]]) -> str:
    return "\n".join("  %s  [%s] 실제 %d · allowlist %d — %s" % (key, rule, actual, allowed, RULES[rule])
                     for (key, rule), (actual, allowed) in sorted(pairs.items()))


def test_no_violation_beyond_allowlist() -> None:
    """tests/ 의 규약 위반이 ALLOWLIST 에 적힌 수를 넘지 않는다 — 넘는 항목은 새 위반이고, 고치는 것이 답이다(ALLOWLIST 를 늘리지 않는다)."""
    actual, allowed = _violations(), _allowed()
    grown = {kr: (actual[kr], allowed[kr]) for kr in actual if actual[kr] > allowed[kr]}
    assert not grown, "새 규약 위반(.claude/rules/python-tests.md):\n%s\n\n지금 상태 전체:\n%s" % (_explain(grown), _as_allowlist(actual))


def test_allowlist_is_not_looser_than_reality() -> None:
    """ALLOWLIST 의 항목이 실제 위반보다 크지 않다 — 위반을 고쳤으면 ALLOWLIST 도 그만큼 줄인다. 느슨한 allowlist 는 래칫이 아니다."""
    actual, allowed = _violations(), _allowed()
    shrunk = {kr: (actual[kr], allowed[kr]) for kr in allowed if actual[kr] < allowed[kr]}
    assert not shrunk, "위반이 줄었다 — ALLOWLIST 를 줄인다:\n%s\n\n지금 상태 전체:\n%s" % (_explain(shrunk), _as_allowlist(actual))


SAMPLES: dict[str, tuple[Path, str]] = {
    # 규칙 → (파일 자리, 그 규칙 하나만 어기는 소스)
    "docstring": (TESTS / "test_x.py", 'def test_a() -> None:\n    assert 1\n'),
    "and": (TESTS / "test_x.py", 'def test_a() -> None:\n    """a."""\n    assert 1 and 2\n'),
    "replay": (CONTRACTS / "test_x.py", 'def test_a(replay: object) -> None:\n    """a."""\n    replay("f.json")\n'),
    "physical": (TESTS / "test_x.py", 'PHYSICAL: tuple[str, ...] = ("a",)\n'),
    "date": (TESTS / "test_x.py", 'x = 1  # 1999-12-31 에 봤다\n'),
}


@pytest.mark.parametrize("rule", sorted(RULES))
def test_each_rule_detects_its_sample(rule: str) -> None:
    """다섯 규칙이 각각 자기 표본을 잡고 다른 규칙은 잡지 않는다 — 메타테스트가 실제로 보는지의 확인."""
    path, source = SAMPLES[rule]
    assert [v.rule for v in scan(path, source)] == [rule]


def test_conftest_may_define_physical() -> None:
    """규칙 4 의 예외 — PHYSICAL 의 자리인 tests/conftest.py 는 위반이 아니다."""
    assert scan(CONFTEST, 'PHYSICAL: tuple[str, ...] = ("a",)\n') == []
