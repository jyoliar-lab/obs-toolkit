"""obs.logs — 로그 환원 연산: histogram · templates · around · first_seen (logs 백엔드, GET 만).

원본 로그 수천 줄을 agent 가 읽지 않고 "무엇이 얼마나 언제부터" 를 숫자로 안다. 계산은 서버(Loki)가 하거나 — `count_over_time`
matrix · Patterns API — 서버가 못 내는 것(예시 한 줄 · 첫 발생)만 `query_range` 한 줄 질의로 보탠다.

구조로 거는 것(obs.metrics 와 같다):
  - 순수 함수다 — HTTP 는 `Client` 주입(`Client.get` 만), fixture(서버 환원 응답)만으로 테스트된다.
  - 연산은 `(Evidence[V], 원본)` 을 돌려주고 거부는 `ObsError`(요청 전). 값 dataclass 는 frozen.
  - "데이터 없음" 과 "0" 을 구분한다 — 빈 목록 / None 이 없음이고, 0 은 0 으로 실린다.
  - 상한은 함수 안에서 — histogram 의 행 수(max_groups) · templates 의 군집 수(k) · around 의 쪽별 줄 수(limit).

Loki 응답 파서(`Stream` · `LogResult` · `parse_logs` · `LogEntry`)는 어댑터(cli)에서 이리로 옮겨 왔다 — cli 는 같은 이름을 import 한다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from obs.core import JSON, Client, Evidence, Window, json_int, json_labels, json_list, json_obj, json_pair, json_str
from obs.metrics import Series, series_items

DEFAULT_MAX_GROUPS: int = 20
DEFAULT_TEMPLATES_K: int = 10
DEFAULT_AROUND_LIMIT: int = 50
DEFAULT_AROUND_SPAN: str = "5m"


# ── 응답 dataclass + 파서 — Loki 로그 (이동: obs/adapters/cli.py) ───────────────────

@dataclass(frozen=True)
class Stream:
    """Loki 로그 스트림 — 레이블 집합과 (나노초, 줄) 목록."""
    labels: dict[str, str]
    values: list[tuple[int, str]]


@dataclass(frozen=True)
class LogResult:
    result_type: str            # streams · matrix
    streams: list[Stream]
    series: list[Series]


@dataclass(frozen=True)
class LogEntry:
    """로그 줄 하나 — 스트림을 펼친 것. (이동 · 승격: cli._Entry)"""
    ts_ns: int
    labels: dict[str, str]
    line: str


def parse_logs(body: dict[str, JSON]) -> LogResult:
    data = json_obj(body.get("data"))
    rtype = json_str(data.get("resultType"))
    if rtype == "matrix":
        return LogResult(result_type=rtype, streams=[], series=series_items(data.get("result")))
    streams: list[Stream] = []
    for item in json_list(data.get("result")):
        o = json_obj(item)
        values: list[tuple[int, str]] = []
        for v in json_list(o.get("values")):
            pr = json_pair(v)
            if pr is not None:
                values.append((json_int(pr[0]), pr[1]))
        streams.append(Stream(labels=json_labels(o.get("stream")), values=values))
    return LogResult(result_type=rtype, streams=streams, series=[])


# ── histogram ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class GroupCount:
    """표의 행 하나 — `by` 값 하나(예: detected_level=error)."""
    labels: dict[str, str]
    total: int                          # 창 전체 건수 = counts 의 합
    counts: list[int]                   # 열 — Histogram.times 와 같은 길이 · 점이 없던 구간은 0


@dataclass(frozen=True)
class Histogram:
    """matrix 를 편 표 — 행 = by 값(groups) · 열 = 시간 구간(times)."""
    by: list[str]
    step: str                           # 구간 폭
    times: list[float]                  # 시간축 — 구간 시작 시각(unix 초), 모든 시계열의 시각 합집합 정렬
    groups: list[GroupCount]            # [] = 데이터 없음 (값 0 인 시계열은 total 0 인 행으로 실린다)
    folded: int                         # max_groups 에 밀려 표에서 뺀 행 수 · 0 = 없음


def histogram_query(selector: str, by: Sequence[str], step: str) -> str:
    raise NotImplementedError("histogram_query")


def histogram(client: Client, selector: str, window: Window, by: Sequence[str] = ("detected_level",),
              step: str | None = None, max_groups: int = DEFAULT_MAX_GROUPS) -> tuple[Evidence[Histogram], JSON]:
    raise NotImplementedError("histogram")


# ── templates ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Sample:
    """Patterns API 의 sample 하나 — 한 구간(step)의 건수."""
    ts: float                           # unix 초 — 구간 시작
    count: int


@dataclass(frozen=True)
class Pattern:
    """Patterns API 응답 항목."""
    pattern: str
    samples: list[Sample]


@dataclass(frozen=True)
class Template:
    template: str
    count: int
    ratio: float
    first_ts: float                     # 건수 > 0 인 첫 sample 의 시각 — 정밀도는 step
    last_ts: float
    example: str | None                 # None = 예시 질의가 빈 스트림


@dataclass(frozen=True)
class Templates:
    k: int
    step: str                           # sample 구간 폭
    total: int
    clusters: list[Template]            # [] = 빈 군집
    other: int


def pattern_filter(selector: str, pattern: str) -> str:
    raise NotImplementedError("pattern_filter")


def parse_patterns(body: dict[str, JSON]) -> list[Pattern]:
    raise NotImplementedError("parse_patterns")


def clusters_of(patterns: Sequence[Pattern], k: int, step: str) -> Templates:
    raise NotImplementedError("clusters_of")


def templates(client: Client, selector: str, window: Window, k: int = DEFAULT_TEMPLATES_K,
              step: str | None = None) -> tuple[Evidence[Templates], JSON]:
    raise NotImplementedError("templates")


# ── around ───────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Side:
    """ts 의 한쪽 — 앞(t < ts) · 뒤(t ≥ ts)."""
    shown: int                          # 받아 온 줄 수 (≤ limit)
    truncated: bool                     # 그 쪽 창 안에 보인 것보다 더 있다


@dataclass(frozen=True)
class Context:
    center: float
    span: float
    limit: int                          # 한 쪽 상한 — 앞 ≤ limit · 뒤 ≤ limit
    total: int                          # 창(ts±span) 전체 건수 — count 질의
    before: Side
    after: Side
    entries: list[LogEntry]             # 앞 + 뒤 · 시각 오름차순 · [] = 빈 문맥 · 길이 ≤ 2·limit

    @property
    def shown(self) -> int:
        raise NotImplementedError("Context.shown")

    @property
    def truncated(self) -> bool:
        raise NotImplementedError("Context.truncated")


def count_query(selector: str, seconds: float) -> str:
    raise NotImplementedError("count_query")


def around(client: Client, selector: str, ts: float, span: float, limit: int = DEFAULT_AROUND_LIMIT) -> tuple[Evidence[Context], JSON]:
    raise NotImplementedError("around")


# ── first_seen ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class FirstSeen:
    template: str
    ts: float | None                    # None = 창 안에 없음
    line: str | None


def first_seen(client: Client, selector: str, template: str, window: Window) -> tuple[Evidence[FirstSeen], JSON]:
    raise NotImplementedError("first_seen")
