"""obs.adapters.cli_argparse — promql · logql · traceql 의 argparse 선언. **선언만** — 분기 · 환경 · Client 가 없다.

기본값은 여기 없다 — 모든 `add_argument` 의 `default` 는 `argparse.SUPPRESS` 라 생략한 옵션은 Namespace 에 자리가 없고,
`obs.adapters.cli` 의 `*Args` dataclass 가 기본값을 안다(`map_args`). 도움말 문구의 "기본 …" 은 그 상수를 찍는다.
배선(`*_main` · `*_run` · `finish`)은 `obs.adapters.cli` 에 있다 — 이 파일은 사용자가 보는 usage · `--help` 의 전부다.
"""
from __future__ import annotations

import argparse

DEFAULT_MAX_LINES: int = 40
DEFAULT_LOG_LIMIT: int = 30
DEFAULT_SINCE: str = "1h"

METRICS_SUBCOMMANDS: tuple[str, ...] = ("summarize", "compare", "top", "red")
LOGS_SUBCOMMANDS: tuple[str, ...] = ("histogram", "templates", "around", "first-seen")
TRACES_SUBCOMMANDS: tuple[str, ...] = ("exemplars", "critical-path", "service-graph", "diff")


def output_options(ap: argparse.ArgumentParser) -> None:
    """--max-lines · --json — 여섯 명령이 공유하는 stdout 절제 옵션(dest max_lines · as_json). default 는 SUPPRESS."""
    raise NotImplementedError("output_options")


def promql_parser() -> argparse.ArgumentParser:
    raise NotImplementedError("promql_parser")


def metrics_parser() -> argparse.ArgumentParser:
    raise NotImplementedError("metrics_parser")


def logql_parser() -> argparse.ArgumentParser:
    raise NotImplementedError("logql_parser")


def logs_parser() -> argparse.ArgumentParser:
    raise NotImplementedError("logs_parser")


def traceql_parser() -> argparse.ArgumentParser:
    raise NotImplementedError("traceql_parser")


def traces_parser() -> argparse.ArgumentParser:
    raise NotImplementedError("traces_parser")
