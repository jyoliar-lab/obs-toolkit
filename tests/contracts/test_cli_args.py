"""argv → *Args 의 약속 — 선언(obs.adapters.cli_argparse 의 *_parser)은 기본값을 모르고, 매핑(map_args)은 Namespace 에 있는 필드만 옮기며
기본값은 *Args dataclass 한 곳에서 온다. Client · 요청 · env 없이 검사한다."""
from __future__ import annotations

import argparse
from dataclasses import fields
from typing import Callable

import pytest

from obs.adapters import cli, cli_argparse

ParserFn = Callable[[], argparse.ArgumentParser]

MINIMAL_ARGV: list[tuple[str, ParserFn, list[str], set[str]]] = [
    # (화면, 파서, 옵션 전부 생략한 최소 argv, Namespace 에 남아야 하는 속성 — 준 위치 인자와 서브커맨드 이름뿐)
    ("promql", cli_argparse.promql_parser, [], set()),
    ("summarize", cli_argparse.metrics_parser, ["summarize", "up"], {"cmd", "query"}),
    ("compare", cli_argparse.metrics_parser, ["compare", "up"], {"cmd", "query"}),
    ("top", cli_argparse.metrics_parser, ["top", "up"], {"cmd", "query"}),
    ("red", cli_argparse.metrics_parser, ["red", "svc"], {"cmd", "service"}),
    ("logql", cli_argparse.logql_parser, ['{a="b"}'], {"query"}),
    ("histogram", cli_argparse.logs_parser, ["histogram", '{a="b"}'], {"cmd", "selector"}),
    ("templates", cli_argparse.logs_parser, ["templates", '{a="b"}'], {"cmd", "selector"}),
    ("around", cli_argparse.logs_parser, ["around", '{a="b"}'], {"cmd", "selector"}),
    ("first-seen", cli_argparse.logs_parser, ["first-seen", '{a="b"}', "<_> x"], {"cmd", "selector", "template"}),
    ("traceql", cli_argparse.traceql_parser, ["search"], {"cmd"}),
    ("exemplars", cli_argparse.traces_parser, ["exemplars"], {"cmd"}),
    ("critical-path", cli_argparse.traces_parser, ["critical-path", "t"], {"cmd", "trace_id"}),
    ("service-graph", cli_argparse.traces_parser, ["service-graph"], {"cmd"}),
    ("diff", cli_argparse.traces_parser, ["diff", "s", "n"], {"cmd", "slow_id", "normal_id"}),
]


@pytest.mark.parametrize("parser,argv,expected", [c[1:] for c in MINIMAL_ARGV], ids=[c[0] for c in MINIMAL_ARGV])
def test_parsers_leave_omitted_options_unset(parser: ParserFn, argv: list[str], expected: set[str]) -> None:
    """옵션을 전부 생략한 argv 의 Namespace 에는 준 위치 인자와 서브커맨드 이름(cmd)뿐이다 — 생략한 옵션의 속성이 없다(SUPPRESS), 기본값이 파서에 없다."""
    # given — 열다섯 화면 각각의 최소 argv
    # when
    ns = parser().parse_args(argv)
    # then
    assert set(vars(ns)) == expected


MappedArgs = cli.PromqlArgs | cli.MetricsArgs | cli.LogqlArgs | cli.LogsArgs | cli.TraceqlArgs | cli.TracesArgs

PRESENT_CASES: list[tuple[str, ParserFn, list[str], Callable[[argparse.Namespace], MappedArgs], MappedArgs]] = [
    # (옵션 종류, 파서, 그 종류를 명시한 argv(+ --max-lines 3 --json — Namespace 의 여분 속성), 매핑, 기대 — 명시한 필드만 값이고 나머지는 dataclass 기본값)
    ("positional", cli_argparse.logql_parser, ['{a="b"}', "--max-lines", "3", "--json"],
     lambda ns: cli.map_args(cli.LogqlArgs, ns), cli.LogqlArgs(query='{a="b"}')),
    ("flag", cli_argparse.promql_parser, ["up", "--labels", "--points", "--max-lines", "3", "--json"],
     lambda ns: cli.map_args(cli.PromqlArgs, ns), cli.PromqlArgs(query="up", labels=True, points=True)),
    ("valued", cli_argparse.traceql_parser, ["search", "--limit", "7", "--min-duration", "2s", "--max-lines", "3", "--json"],
     lambda ns: cli.map_args(cli.TraceqlArgs, ns), cli.TraceqlArgs(cmd="search", limit=7, min_duration="2s")),
    ("subcommand-valued", cli_argparse.traces_parser,
     ["exemplars", "--k", "2", "--quantile", "0.9", "--baseline-range", "3d", "--limit", "5", "--max-lines", "3", "--json"],
     lambda ns: cli.map_args(cli.TracesArgs, ns), cli.TracesArgs(cmd="exemplars", k=2, quantile=0.9, baseline_range="3d", limit=5)),
]


@pytest.mark.parametrize("parser,argv,mapper,expected", [c[1:] for c in PRESENT_CASES], ids=[c[0] for c in PRESENT_CASES])
def test_map_args_takes_present_options_by_field_name(parser: ParserFn, argv: list[str], mapper: Callable[[argparse.Namespace], MappedArgs],
                                                      expected: MappedArgs) -> None:
    """명시한 옵션은 같은 이름의 필드로 옮겨지고(타입 변환 포함) 나머지 필드는 dataclass 기본값이다 — Namespace 의 여분 속성(max_lines · as_json)은 예외 없이 무시된다."""
    # given — 옵션 종류 하나를 명시한 argv 의 Namespace(--max-lines · --json 이 함께 있다)
    ns = parser().parse_args(argv)
    assert hasattr(ns, "max_lines")
    assert hasattr(ns, "as_json")
    # when
    got = mapper(ns)
    # then
    assert got == expected
    assert type(got) is type(expected)


@pytest.mark.parametrize("argv,expected", [(['{a="b"}', "--max-lines", "3", "--json"], cli.OutputArgs(max_lines=3, as_json=True)),
                                           (['{a="b"}'], cli.OutputArgs())], ids=["given", "omitted"])
def test_map_args_output_options(argv: list[str], expected: cli.OutputArgs) -> None:
    """--max-lines · --json 은 OutputArgs 로 같은 길을 지난다 — 주면 그 값, 생략하면 dataclass 기본값(DEFAULT_MAX_LINES · False). Namespace 의 query 는 무시된다."""
    # given — logql 의 Namespace(query 가 함께 있다)
    ns = cli_argparse.logql_parser().parse_args(argv)
    assert hasattr(ns, "query")
    # when
    got = cli.map_args(cli.OutputArgs, ns)
    # then
    assert got == expected
    assert cli.OutputArgs().max_lines == cli.DEFAULT_MAX_LINES
    assert cli.OutputArgs().as_json is False


DEFAULT_CASES: list[tuple[str, ParserFn, list[str], Callable[[argparse.Namespace], MappedArgs], MappedArgs]] = [
    # (화면, 파서, 옵션 전부 생략한 argv, 매핑, 기대 — 준 위치 인자 외 전부 dataclass 기본값)
    ("promql", cli_argparse.promql_parser, [], lambda ns: cli.map_args(cli.PromqlArgs, ns), cli.PromqlArgs()),
    ("summarize", cli_argparse.metrics_parser, ["summarize", "up"], lambda ns: cli.map_args(cli.MetricsArgs, ns), cli.MetricsArgs(cmd="summarize", query="up")),
    ("red", cli_argparse.metrics_parser, ["red", "svc"], lambda ns: cli.map_args(cli.MetricsArgs, ns), cli.MetricsArgs(cmd="red", service="svc")),
    ("around", cli_argparse.logs_parser, ["around", '{a="b"}'], lambda ns: cli.map_args(cli.LogsArgs, ns), cli.LogsArgs(cmd="around", selector='{a="b"}')),
]


@pytest.mark.parametrize("parser,argv,mapper,expected", [c[1:] for c in DEFAULT_CASES], ids=[c[0] for c in DEFAULT_CASES])
def test_map_args_defaults_come_from_dataclass_only(parser: ParserFn, argv: list[str], mapper: Callable[[argparse.Namespace], MappedArgs],
                                                    expected: MappedArgs) -> None:
    """옵션을 전부 생략하면 준 위치 인자 외의 모든 필드가 그 dataclass 선언의 기본값이다 — 서브커맨드에 없는 옵션(red 의 query · around 의 since)도 빠지지 않는다."""
    # given — 옵션 전부 생략한 argv 의 Namespace
    ns = parser().parse_args(argv)
    given = set(vars(ns))
    # when
    got = mapper(ns)
    # then
    assert got == expected
    for f in fields(expected):
        if f.name not in given:
            assert getattr(got, f.name) == f.default, f.name
