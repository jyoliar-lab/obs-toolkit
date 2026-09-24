"""FakeTransport 의 라우팅 — (backend, native_path) 정확 일치 · query string 무관 · 미등록 요청은 등록된 키를 나열하며 실패."""
from __future__ import annotations

import pytest

from tests.conftest import ClientFactory


def test_route_matches_backend_and_exact_native_path(make_client: ClientFactory) -> None:
    """등록한 (backend, path) 키와 같은 요청이 그 응답을 받는다."""
    client, _ = make_client({("metrics", "api/v1/query"): (200, {"status": "success"})})
    assert client.get("metrics", "api/v1/query", {"query": "up"}).body == {"status": "success"}


def test_query_string_takes_no_part_in_routing(make_client: ClientFactory) -> None:
    """같은 경로의 다른 질의는 같은 라우트다 — 질의별 응답은 replay 의 일이다."""
    client, t = make_client({("metrics", "api/v1/query"): (200, {"status": "success"})})
    client.get("metrics", "api/v1/query", {"query": "up"})
    client.get("metrics", "api/v1/query", {"query": "sum(up)"})
    assert len(t.calls) == 2


def test_path_fragment_does_not_match(make_client: ClientFactory) -> None:
    """경로 조각은 라우트가 아니다 — api/traces/ 를 등록해도 api/traces/abc 는 미등록이다."""
    client, _ = make_client({("traces", "api/traces/"): (200, {})})
    with pytest.raises(AssertionError):
        client.get("traces", "api/traces/abc")


def test_unregistered_request_fails_listing_registered_keys(make_client: ClientFactory) -> None:
    """미등록 요청은 404 가 아니라 AssertionError 이고, 문구에 등록된 키가 나란히 보인다."""
    client, _ = make_client({("metrics", "api/v1/query"): (200, {})})
    with pytest.raises(AssertionError, match=r"\('metrics', 'api/v1/query'\)"):
        client.get("metrics", "api/v1/query_range")
