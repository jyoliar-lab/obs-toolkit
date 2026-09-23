"""tests/replay.py — fixture replay: 나간 요청 == 녹화 요청 검사.

테스트가 fixture 로 응답을 돌려줄 때(replay), **실제로 나간 요청이 fixture 에 녹화된 요청과 같은지** 호출마다 검사한다.
경로 조각 라우팅(`"api/v1/query?"` 가 어떤 질의든 통과)은 코어가 질의 조립을 바꿔도 낡은 응답으로 테스트를 통과시킨다 —
이 검사가 그 조용한 어긋남을 실패로 만든다.

- `request_key(url, config)` — 독립 부품. 물리(호스트 · 프록시 접두 · uid)를 지우고 네이티브 경로 + 정렬된 query 파라미터로 정규화한다.
  파라미터 순서에 무관. 녹화기(tests/record.py)가 지운 것과 같은 층(`Response.path`)이라 두 키가 만난다.
  나중에 파일명 없이 요청으로 fixture 를 찾는 replay 의 인덱스 키이기도 하다(그 일은 미정).
- `ReplayTransport(expected, config, unchecked=None)` — `(url, headers, timeout) → (status, body)`. 녹화된 호출 목록을 **순서대로** 돌려주며,
  호출마다 `request_key(나간 url)` 과 녹화 키를 비교해 다르면 AssertionError(두 요청이 나란히 보인다). 녹화보다 많이 부르면 실패.
  검사를 끄는 유일한 길은 `unchecked=<이유>` — 이유가 코드에 남는다(공유 fixture `promql-instant-empty.json` 용). 기본값은 검사한다.
  `recorded: manual` fixture 는 요청이 추정이라 비교하지 않고 경고를 낸다.
- 시계는 하나다: 테스트는 고정 `at` 대신 fixture 의 `at`(녹화 시점)을 `*_run(…, at=at)` 에 주입한다 — 그래야 녹화와 같은 질의가 나간다.
  같은 replay 의 fixture 들은 `at` 이 같아야 한다(다르면 ValueError).

실환경이 만들 수 없는 조건(연결 실패 · 읽기 시간 초과 · 404 · 서버가 k 보다 많이 돌려주는 경우 · 손으로 만든 응답)은 그대로 `FakeTransport`(conftest) 다.
"""
from __future__ import annotations

import json
import urllib.parse
import warnings
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from obs.core import Config
from tests.record import FIXTURES, Fixture, RecordedCall, normalize_key, read_fixture, strip_physical


def readable(key: str) -> str:
    """실패 문구용 — 키의 URL 인코딩을 풀어 사람이 두 질의를 나란히 읽게 한다(비교는 인코딩된 키로 한다)."""
    return urllib.parse.unquote_plus(key)


def request_key(url: str, config: Config) -> str:
    """물리 URL → 정규화한 요청 키(`<네이티브 경로>?<정렬된 파라미터>`). 접두를 모르는 URL 은 ValueError."""
    rq = strip_physical(url, config, None)
    return normalize_key(rq.path, rq.params)


@dataclass
class Call:
    """transport 가 받은 호출 — 테스트가 URL 을 단언한다."""
    url: str
    headers: dict[str, str]
    timeout: float


FixtureSpec = str | tuple[str, str]
"""replay 할 호출 — 파일 이름(호출이 하나) 또는 (파일 이름, 경로 조각)(호출이 여럿인 파일에서 하나를 고른다)."""


@dataclass(frozen=True)
class Loaded:
    """fixture 에서 꺼낸 녹화 호출 하나와 그 출처."""
    name: str
    fixture: Fixture
    call: RecordedCall

    @property
    def manual(self) -> bool:
        return self.fixture.manual


def load_call(spec: FixtureSpec) -> Loaded:
    """요청 키 · at · 응답을 가진 녹화 호출 — 응답만 꺼내는 `load_response`(conftest) 옆의 진입점."""
    name, path = (spec, None) if isinstance(spec, str) else spec
    fx = read_fixture(FIXTURES / name)
    calls = fx.calls if path is None else [c for c in fx.calls if path in c.request.path]
    if len(calls) != 1:
        raise ValueError("%s: 호출 %d개 — path=%r 로 하나를 고를 수 없다. 있는 경로: %s"
                         % (name, len(calls), path, [c.request.path for c in fx.calls]))
    return Loaded(name=name, fixture=fx, call=calls[0])


def load_fixture_calls(name: str) -> list[Loaded]:
    """파일의 호출 전부 — 순서대로(traces: api/search → api/traces/<id>)."""
    fx = read_fixture(FIXTURES / name)
    return [Loaded(name=name, fixture=fx, call=c) for c in fx.calls]


def shared_at(loaded: Sequence[Loaded]) -> float:
    """replay 에 쓸 하나의 at — 모든 녹화 호출의 at 이 같아야 한다. manual(at 불명)은 제외."""
    ats = {l.call.request.at for l in loaded if l.call.request.at is not None}
    if len(ats) != 1:
        raise ValueError("replay 의 at 이 하나가 아니다: %s — 같은 녹화 실행의 fixture 끼리만 한 replay 에 둔다" % sorted(ats))
    return ats.pop()


@dataclass
class ReplayTransport:
    """녹화 호출을 순서대로 돌려주고, 호출마다 나간 요청 키를 녹화 키와 비교한다."""
    expected: list[Loaded]
    config: Config
    unchecked: str | None = None          # 검사를 끄는 이유 — 있으면 비교하지 않는다. 기본은 검사
    calls: list[Call] = field(default_factory=list[Call])

    def __post_init__(self) -> None:
        for l in self.expected:
            if l.manual and self.unchecked is None:
                warnings.warn("%s 은 recorded: manual — 요청이 추정이라 비교하지 않는다(%s)" % (l.name, l.fixture.reason), stacklevel=3)

    @property
    def at(self) -> float:
        return shared_at(self.expected)

    def __call__(self, url: str, headers: Mapping[str, str], timeout: float) -> tuple[int, bytes]:
        self.calls.append(Call(url=url, headers=dict(headers), timeout=timeout))
        i = len(self.calls) - 1
        if i >= len(self.expected):
            raise AssertionError("녹화보다 많은 호출 — %d번째 호출인데 녹화는 %d개(%s): %s"
                                 % (i + 1, len(self.expected), [l.name for l in self.expected], readable(request_key(url, self.config))))
        l = self.expected[i]
        if self.unchecked is None and not l.manual:
            sent, recorded = request_key(url, self.config), l.call.request.key
            if sent != recorded:
                raise AssertionError("요청 불일치 — 호출 %d/%d (%s) — 코드가 질의 조립을 바꿨거나 at 이 녹화와 다르다\n  나간:  %s\n  녹화:  %s"
                                     % (i + 1, len(self.expected), l.name, readable(sent), readable(recorded)))
        body = l.call.response.body
        raw = body.encode("utf-8") if isinstance(body, str) else json.dumps(body).encode("utf-8")
        return l.call.response.status, raw

    def assert_exhausted(self) -> None:
        """녹화된 호출을 전부 썼는지 — 덜 불렀으면 실패."""
        if len(self.calls) != len(self.expected):
            raise AssertionError("녹화 %d개 중 %d개만 불렀다(%s)" % (len(self.expected), len(self.calls), [l.name for l in self.expected]))
