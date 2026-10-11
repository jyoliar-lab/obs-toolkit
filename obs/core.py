"""obs.core — 관측 코어: 설정 · GET 클라이언트 · 시간 창 · 근거 · spill · 예산 · 진단.

표준 라이브러리만 쓴다. 전송 무관이다 — CLI 든 MCP 든 같은 함수를 부른다.

구조로 거는 것:
  - HTTP 메서드는 GET 뿐이다. `_urllib_transport` 가 유일한 HTTP 코드이고 `Request` 에 method 를
    주지 않는다(urllib 기본 = GET). 이 모듈 밖에서 urllib·http 를 import 하는 모듈이 없다 — 테스트가 지킨다.
  - agent 는 물리를 모른다. 호출 인터페이스는 논리 이름(metrics · logs · traces)만 받고, 오류는
    논리 이름 · 상태 · 다음 행동만 말한다. 호스트·uid 는 `doctor` 가 사람에게만 보여준다.
  - 토큰은 env(OBS_GRAFANA_TOKEN)에서만 읽는다 — 설정 파일·인자로 받지 않고, 어느 dataclass 에도 싣지 않는다.
  - 모든 dataclass 는 `to_dict` 로 JSON 직렬화된다 — 나중에 MCP outputSchema · structuredContent 로 그대로 쓴다.
  - `Any` 를 쓰지 않는다. 외부 경계(HTTP 응답 · 설정 파일)는 `JSON` 으로 받고, 파서가 즉시 dataclass 로 옮긴다 —
    그때만 쓰는 접근자(`json_obj` …)가 여기 있다. 환원 연산(obs/metrics.py …)은 파싱된 dataclass 만 본다.

설정 해석 순서(Config): env → 설정 파일(JSON) → 내장 기본값.
  env:  OBS_GRAFANA_URL · OBS_PROXY_PREFIX · OBS_UID_METRICS · OBS_UID_LOGS · OBS_UID_TRACES
  파일: OBS_CONFIG 가 가리키는 경로 → ${CLAUDE_PLUGIN_DATA}/config.json → ~/.config/obs-toolkit/config.json
        {"grafana_url": "...", "proxy_prefix": "...", "uids": {"metrics": "...", "logs": "...", "traces": "..."}}
  기본값: 아래 DEFAULT_* — 사람의 기록용 상수. agent 가 볼 문서에는 적지 않는다.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import json
import os
import re
import shutil
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Generic, Mapping, Sequence, TypeAlias, TypeVar, cast

LOGICAL_BACKENDS: tuple[str, ...] = ("metrics", "logs", "traces")

# 외부 경계의 값 — 파싱된 JSON. 형태를 모르는 것이 사실이라 이 타입이다. 내부에서는 dataclass 로 옮겨 쓴다.
JSON: TypeAlias = "dict[str, JSON] | list[JSON] | str | int | float | bool | None"
# 질의 문자열 인자 — None · "" 은 Client.get 이 버린다
Params: TypeAlias = Mapping[str, "str | int | float | None"]

# ── 물리 기본값 — 사람의 기록용. 설정 해석 계층 안에서만 산다 ────────────────────
DEFAULT_GRAFANA_URL: str = "https://obs.mg.jyo-liar.com"   # agent 게이트웨이(GET 만). 클러스터 내 소비자는 설정으로 바꾼다
DEFAULT_PROXY_PREFIX: str = "/api/datasources/proxy/uid"
DEFAULT_UIDS: dict[str, str] = {
    "metrics": "prometheus",
    "logs": "loki-careerhub",
    "traces": "tempo-careerhub",
}

ENV_TOKEN: str = "OBS_GRAFANA_TOKEN"
ENV_URL: str = "OBS_GRAFANA_URL"
ENV_PROXY_PREFIX: str = "OBS_PROXY_PREFIX"
ENV_CONFIG: str = "OBS_CONFIG"
ENV_UIDS: dict[str, str] = {
    "metrics": "OBS_UID_METRICS",
    "logs": "OBS_UID_LOGS",
    "traces": "OBS_UID_TRACES",
}

TIMEOUT_SEC: float = 30.0
SPILL_KEEP: int = 50          # tmp/obs 에 남기는 최대 호출 디렉터리 수 — 오래된 것부터 지운다
MAX_QUERIES: int = 20         # 한 예산(조사) 안의 질의 횟수 상한
MAX_RESPONSE_BYTES: int = 20_000_000   # 한 예산 안에서 받는 응답 총량 상한


class ObsError(Exception):
    """agent 에게 보일 실패 — 논리 이름 · 상태 · 다음 행동만. 호스트·URL 을 담지 않는다."""


class BudgetExceeded(ObsError):
    """예산 초과 — 좁혀서 다시."""


class TransportError(Exception):
    """전송 실패(연결·TLS·연결 시간 초과). 메시지에 호스트를 담지 않는다 — transport 가 지운다."""


class TransportTimeout(TransportError):
    """닿기는 했는데 제한 시간 안에 답이 오지 않았다(읽기 시간 초과). 연결 실패와 다르다 —
    무거운 질의가 흔한 원인이라 agent 가 범위를 좁혀 고칠 수 있는 것으로 분류한다."""


# ── 외부 경계 접근자 — 파싱된 JSON 을 dataclass 로 옮길 때만 쓴다 ─────────────────

def json_obj(v: JSON) -> dict[str, JSON]:
    return v if isinstance(v, dict) else {}


def json_list(v: JSON) -> list[JSON]:
    return v if isinstance(v, list) else []


def json_str(v: JSON, default: str = "") -> str:
    if v is None:
        return default
    return v if isinstance(v, str) else str(v)


def json_int(v: JSON) -> int:
    if isinstance(v, bool) or v is None:
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str):
        try:
            return int(float(v))
        except ValueError:
            return 0
    return 0


def json_labels(v: JSON) -> dict[str, str]:
    return {k: json_str(x) for k, x in json_obj(v).items()}


def json_strs(v: JSON) -> list[str]:
    return [json_str(x) for x in json_list(v)]


def json_pair(v: JSON) -> tuple[str, str] | None:
    """[ts, value] 쌍. 둘 다 문자열로 — 해석(초·나노초·수치)은 받는 쪽이 한다."""
    items = json_list(v)
    if len(items) < 2:
        return None
    return json_str(items[0]), json_str(items[1])


# ── 직렬화 ──────────────────────────────────────────────────────────────────────

def to_dict(obj: object) -> JSON:
    """dataclass(중첩 포함)를 JSON 값으로. Mapping/Sequence 는 재귀, Path 는 str, 스칼라는 그대로. 그 외는 TypeError."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_dict(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, Mapping):
        return {str(k): to_dict(v) for k, v in cast(Mapping[object, object], obj).items()}
    if isinstance(obj, (list, tuple)):
        return [to_dict(v) for v in cast(Sequence[object], obj)]
    if isinstance(obj, Path):
        return str(obj)
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    raise TypeError("JSON 으로 옮길 수 없는 값: %r" % type(obj).__name__)


def to_json(obj: object) -> str:
    return json.dumps(to_dict(obj), ensure_ascii=False, indent=1)


# ── 설정 ────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Resolved:
    """해석된 값과 출처 — doctor 가 사람에게 "어디서 온 값인가" 를 보이기 위해."""
    value: str
    source: str   # "env:<NAME>" · "file:<path>" · "default"


@dataclass(frozen=True)
class Config:
    """논리 이름 → 물리 해석. 토큰은 여기 없다(env 에서만, Client 가 직접 읽는다)."""
    grafana_url: Resolved
    proxy_prefix: Resolved
    uids: dict[str, Resolved]
    config_path: str | None = None   # 읽은 설정 파일. 없으면 None

    def uid(self, backend: str) -> str:
        if backend not in self.uids:
            raise ObsError("알 수 없는 백엔드 %r — metrics · logs · traces 중 하나" % backend)
        return self.uids[backend].value

    def proxy_url(self, backend: str, path: str) -> str:
        """물리 URL — Client 와 doctor 만 부른다. agent 출력에 싣지 않는다."""
        return "%s%s/%s/%s" % (
            self.grafana_url.value.rstrip("/"),
            self.proxy_prefix.value,
            self.uid(backend),
            path.lstrip("/"),
        )


def _explicit_config(env: Mapping[str, str]) -> str:
    """OBS_CONFIG 의 값 — 공백뿐이면 미설정("")."""
    return env.get(ENV_CONFIG, "").strip()


def config_file_candidates(env: Mapping[str, str], home: Path | None = None) -> list[Path]:
    """설정 파일 후보 — 순서대로. OBS_CONFIG 가 있으면 그것만."""
    explicit = _explicit_config(env)
    if explicit:
        return [Path(explicit).expanduser()]
    out: list[Path] = []
    data = env.get("CLAUDE_PLUGIN_DATA", "").strip()
    if data:
        out.append(Path(data) / "config.json")
    out.append((home or Path.home()) / ".config" / "obs-toolkit" / "config.json")
    return out


def _read_config_file(env: Mapping[str, str], home: Path | None) -> tuple[dict[str, JSON], str | None]:
    explicit = bool(_explicit_config(env))
    for p in config_file_candidates(env, home):
        if not p.is_file():
            if explicit:
                raise ObsError("%s 가 가리키는 경로가 설정 파일이 아니다(%s) — 사람에게" % (ENV_CONFIG, p))
            continue
        try:
            data: JSON = json.loads(p.read_text(encoding="utf-8"))
        except ValueError as e:
            raise ObsError("설정 파일이 JSON 이 아니다(%s): %s — 사람에게" % (p, e))
        if not isinstance(data, dict):
            raise ObsError("설정 파일 최상위는 객체여야 한다(%s) — 사람에게" % p)
        return data, str(p)
    return {}, None


def load_config(env: Mapping[str, str] | None = None, home: Path | None = None) -> Config:
    """env → 설정 파일 → 내장 기본값. 각 값의 출처를 Resolved 로 남긴다."""
    env = os.environ if env is None else env
    file_data, file_path = _read_config_file(env, home)
    raw_uids = file_data.get("uids")
    if raw_uids is not None and not isinstance(raw_uids, dict):
        raise ObsError("설정 파일의 uids 는 객체여야 한다 — 사람에게")
    file_uids: dict[str, JSON] = raw_uids or {}

    def resolve(env_name: str, file_key: str, file_val: JSON, default: str) -> Resolved:
        v = env.get(env_name, "").strip()
        if v:
            return Resolved(v, "env:" + env_name)
        if file_val is None or file_val == "":
            return Resolved(default, "default")
        if not isinstance(file_val, str):
            raise ObsError("설정 파일의 %s 는 문자열이어야 한다 — 사람에게" % file_key)
        return Resolved(file_val, "file:%s" % file_path)

    uids = {
        name: resolve(ENV_UIDS[name], "uids.%s" % name, file_uids.get(name), DEFAULT_UIDS[name])
        for name in LOGICAL_BACKENDS
    }
    return Config(
        grafana_url=resolve(ENV_URL, "grafana_url", file_data.get("grafana_url"), DEFAULT_GRAFANA_URL),
        proxy_prefix=resolve(ENV_PROXY_PREFIX, "proxy_prefix", file_data.get("proxy_prefix"), DEFAULT_PROXY_PREFIX),
        uids=uids,
        config_path=file_path,
    )


def token_from_env(env: Mapping[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    return env.get(ENV_TOKEN, "").strip()


# ── 시간 ────────────────────────────────────────────────────────────────────────

_DUR = re.compile(r"^(\d+(?:\.\d+)?)(ms|[smhdw])$")
_UNIT: dict[str, float] = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(text: str) -> float:
    """'30s' '5m' '1h' '2d' '500ms' → 초. Prometheus/Loki 표기와 같다."""
    m = _DUR.match(text.strip())
    if not m:
        raise ObsError("기간 표기가 아니다: %r (예: 30s, 5m, 1h, 2d)" % text)
    return float(m.group(1)) * _UNIT[m.group(2)]


def parse_time(text: str | None, default: float) -> float:
    """'now' · '-1h'(now 기준 상대) · unix 초 · RFC3339 → unix 초."""
    if text is None or text.strip() in ("", "now"):
        return default
    t = text.strip()
    if t.startswith("-") and _DUR.match(t[1:]):
        return default - parse_duration(t[1:])
    if re.match(r"^\d+(\.\d+)?$", t):
        return float(t)
    try:
        dt = _dt.datetime.fromisoformat(t.replace("Z", "+00:00"))
    except ValueError:
        raise ObsError("시각 표기가 아니다: %r (now · -1h · unix초 · RFC3339)" % t)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return dt.timestamp()


def now() -> float:
    return _dt.datetime.now(_dt.timezone.utc).timestamp()


def fmt_ts(unix_sec: float) -> str:
    """요약 줄의 시각 — UTC 이고 그렇다고 표기한다(Z). 사람이 코멘트에 옮길 때 tz 를 헷갈리지 않게."""
    return _dt.datetime.fromtimestamp(unix_sec, _dt.timezone.utc).strftime("%m-%d %H:%M:%SZ")


def fmt_rfc3339(unix_sec: float) -> str:
    return _dt.datetime.fromtimestamp(unix_sec, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


BASELINE_PREV: str = "prev"   # with_baseline 의 spec — 직전 창(같은 길이로 바로 앞). 급변 감지용


@dataclass(frozen=True)
class Window:
    """시작·끝(unix 초)과 선택적 기준선. "언제" 가 근거에 자동으로 붙는다.

    기준선 창의 규약(메트릭 환원 연산 `compare` · `red` 가 같이 쓴다): `baseline` 은 **같은 길이** 의 창이고
    `with_baseline(spec)` 으로 단다 — spec 은 기간(`1d` = 1일 전 같은 시각 · `1w` = 1주 전) 또는 `prev`(직전 창).
    기본값은 obs.metrics.DEFAULT_BASELINE(`1d`)이 정한다."""
    start: float
    end: float
    baseline: Window | None = None

    @classmethod
    def last(cls, since: str, end: str | None = None, at: float | None = None) -> Window:
        """`since='1h'` [`end='-10m'`] → 창. `at` 은 now 의 대체(테스트용)."""
        base = now() if at is None else at
        end_ts = parse_time(end, base)
        return cls(start=end_ts - parse_duration(since), end=end_ts)

    @property
    def seconds(self) -> float:
        return self.end - self.start

    def shifted(self, seconds: float) -> Window:
        return Window(start=self.start + seconds, end=self.end + seconds)

    def with_baseline(self, spec: str) -> Window:
        """같은 길이의 창을 앞으로 옮겨 기준선으로 단다. `spec` 은 기간('1d' · '1w' — 그만큼 앞의 같은 시각) 또는
        'prev'(직전 창 — 창 길이만큼 앞). 표기가 아니면 ObsError."""
        if spec.strip() == BASELINE_PREV:
            offset = self.seconds
        else:
            try:
                offset = parse_duration(spec)
            except ObsError:
                raise ObsError("기준선 표기가 아니다: %r — 기간(1d · 1w · 12h …) 또는 %s(직전 창)" % (spec, BASELINE_PREV))
        return Window(start=self.start, end=self.end, baseline=self.shifted(-offset))

    def label(self) -> str:
        return "%s ~ %s" % (fmt_rfc3339(self.start), fmt_rfc3339(self.end))


# ── 근거 ────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Lines:
    """가장 단순한 환원물 — 사람·agent 가 읽는 요약 줄. 후속 환원 연산이 다른 환원물 dataclass 를 더한다."""
    lines: list[str]


V = TypeVar("V")


@dataclass(frozen=True)
class Evidence(Generic[V]):
    """한 연산의 결과 — 환원물 · 원본 포인터.

    `V` 는 환원물 dataclass(Lines · Summary …) — JSON 직렬화 가능해야 한다."""
    value: V
    raw_ref: str | None = None  # 어댑터가 정한다 — 파일 경로(CLI) / bundle_id(MCP)


# ── spill — 원본은 파일로, 컨텍스트에는 포인터만 ─────────────────────────────────

def spill_root(env: Mapping[str, str] | None = None, cwd: Path | None = None) -> Path:
    """${CLAUDE_PROJECT_DIR}/tmp/obs — 없으면 cwd 의 tmp/obs."""
    env = os.environ if env is None else env
    proj = env.get("CLAUDE_PROJECT_DIR", "").strip()
    base = Path(proj) if proj else (cwd or Path.cwd())
    return base / "tmp" / "obs"


SPILL_RAW: str = "raw.json"          # 호출 디렉터리 안 — 백엔드 응답 원본
SPILL_SUMMARY: str = "summary.txt"   # 호출 디렉터리 안 — 잘리지 않은 요약 전체
SPILL_HEAD_CHARS: int = 1_000        # 예산 초과 응답의 기록에 남기는 본문 앞부분 길이(문자)


def spill(kind: str, payload: object, summary: Sequence[str] = (), env: Mapping[str, str] | None = None,
          cwd: Path | None = None, keep: int = SPILL_KEEP) -> Path:
    """한 호출의 산출물을 <spill_root>/<UTC시각.마이크로초>-<kind>/ 디렉터리 하나에 떨어뜨리고 그 경로를 돌려준다 —
    raw.json(응답 원본) · summary.txt(요약 전체, stdout 절단과 무관). 최근 keep 개 디렉터리만 남긴다.
    이름은 재사용되지 않는다 — 한 번 안내한 경로는 그 호출만 가리킨다."""
    root = spill_root(env, cwd)
    root.mkdir(parents=True, exist_ok=True)
    # 마이크로초까지 — 같은 초 안의 spill 이 지워진 이름을 다시 받아, 앞서 안내한 경로가 다른 호출을 가리키는 일을 막는다
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    path = root / ("%s-%s" % (stamp, kind))
    n = 1
    while path.exists():
        n += 1
        path = root / ("%s-%s-%d" % (stamp, kind, n))
    path.mkdir()
    (path / SPILL_RAW).write_text(json.dumps(to_dict(payload), ensure_ascii=False, indent=1), encoding="utf-8")
    (path / SPILL_SUMMARY).write_text("".join(line + "\n" for line in summary), encoding="utf-8")
    prune(root, keep, protect=path)
    return path


def prune(root: Path, keep: int = SPILL_KEEP, protect: Path | None = None) -> int:
    """오래된 항목(호출 디렉터리 — 예전 형식의 낱개 파일도)부터 지워 keep 개만 남긴다. 지운 수를 돌려준다.

    `protect`(방금 쓴 것)는 후보에서 뺀다 — mtime 이 같은 항목들 사이의 정렬 순서에 기대지 않고
    "방금 쓴 것은 지워지지 않는다" 를 구조로 보장한다."""
    entries = sorted((p for p in root.iterdir() if p != protect), key=lambda p: (p.stat().st_mtime, p.name))
    removed = 0
    for old in entries[: max(0, len(entries) - (keep - 1 if protect is not None else keep))]:
        try:
            if old.is_dir():
                shutil.rmtree(old)
            else:
                old.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def spill_ref(path: Path, env: Mapping[str, str] | None = None, cwd: Path | None = None) -> str:
    """spill 경로(호출 디렉터리)를 사람·agent 가 부를 상대 경로로."""
    env = os.environ if env is None else env
    proj = env.get("CLAUDE_PROJECT_DIR", "").strip()
    base = Path(proj) if proj else (cwd or Path.cwd())
    try:
        return str(path.relative_to(base))
    except ValueError:
        return str(path)


# ── 예산 ────────────────────────────────────────────────────────────────────────

@dataclass
class Budget:
    """한 조사 안에서 나갈 질의 횟수와 받는 응답 총량의 상한. 넘으면 멈추고 "좁혀라" 고 말한다."""
    max_queries: int = MAX_QUERIES
    max_response_bytes: int = MAX_RESPONSE_BYTES
    queries: int = 0
    response_bytes: int = 0

    def charge_query(self) -> None:
        if self.queries >= self.max_queries:
            raise BudgetExceeded("예산 초과 — 질의 %d회 상한. 범위를 좁혀 다시(창·selector·limit)." % self.max_queries)
        self.queries += 1

    def charge_bytes(self, n: int) -> None:
        self.response_bytes += n
        if self.response_bytes > self.max_response_bytes:
            raise BudgetExceeded("예산 초과 — 응답 총량 %d bytes 상한. 범위를 좁혀 다시(창·selector·limit)."
                                 % self.max_response_bytes)


# ── 클라이언트 — GET 유일 진입점 ────────────────────────────────────────────────

Transport = Callable[[str, Mapping[str, str], float], tuple[int, bytes]]
"""(url, headers, timeout) → (status, body). HTTP 오류도 (status, body) 로 돌려준다. 연결 실패는 TransportError."""


@dataclass(frozen=True)
class Response:
    """보낸 호출 하나의 요청과 결과 — 성공 · HTTP 오류 · 전송 실패 · 예산 초과 전부 같은 꼴."""
    backend: str    # 논리 이름
    path: str       # 백엔드 네이티브 경로(api/v1/query 등) — 호스트·uid 없음
    status: int | None   # None = 응답 없음(전송 실패)
    body: JSON      # 파싱된 JSON — 어댑터가 즉시 dataclass 로 옮긴다. JSON 이 아니면 str. None = 응답 없음 · 예산 초과면 앞부분 문자열
    params: dict[str, str | int | float] = field(default_factory=dict[str, str | int | float])   # 실제로 보낸 질의 파라미터(None · "" 를 뺀 뒤). {} = 파라미터 없이 보냈다
    error: str | None = None     # None = 전송됐다 · 전송 실패의 오류 문구(Client.get 이 올리는 ObsError 와 같은 것)
    size: int | None = None      # 받은 본문 바이트 수 — None = 받은 것 없음
    truncated: bool = False      # True = body 가 앞부분뿐(예산 초과)


def _scrub(text: str, secrets: tuple[str, ...]) -> str:
    """오류 문구에서 호스트·토큰 같은 물리 문자열을 지운다."""
    for s in secrets:
        if s:
            text = text.replace(s, "<redacted>")
    return text


def _urllib_transport(url: str, headers: Mapping[str, str], timeout: float) -> tuple[int, bytes]:
    """유일한 HTTP 코드. Request 에 method 를 주지 않는다 — urllib 기본 = GET.

    프록시는 env(HTTP_PROXY·HTTPS_PROXY·NO_PROXY)만 본다. urllib 기본은 macOS 시스템 설정까지 조회하는데(_scproxy),
    그 경로가 CoreFoundation 의 argv "argument domain" 파싱을 깨워 `--series '{a="b"}'` 같은 인자를 plist 로 읽으려다
    stderr 에 CFPropertyList 경고를 흘린다 — agent 출력에 섞인다."""
    req = urllib.request.Request(url, headers=dict(headers))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(urllib.request.getproxies_environment()))
    try:
        with opener.open(req, timeout=timeout) as resp:
            return int(resp.status), resp.read()
    except urllib.error.HTTPError as e:
        return int(e.code), e.read()
    except urllib.error.URLError as e:
        # 연결 단계 실패 — 연결 시간 초과도 reason=TimeoutError 로 여기 온다. 닿지 못한 것이라 사람의 것.
        host = urllib.parse.urlsplit(url).hostname or ""
        raise TransportError(_scrub(str(e.reason), (host,)))
    except TimeoutError as e:
        # 연결은 됐고 응답을 기다리다 초과 — urllib 가 감싸지 않고 그대로 올린다. 무거운 질의의 모양.
        host = urllib.parse.urlsplit(url).hostname or ""
        raise TransportTimeout(_scrub(str(e) or "timed out", (host,)))
    except OSError as e:
        host = urllib.parse.urlsplit(url).hostname or ""
        raise TransportError(_scrub(str(e) or e.__class__.__name__, (host,)))


def http_error_message(backend: str, code: int, body: str) -> str:
    """상태별로 논리 이름 · 상태 · 다음 행동만. agent 가 고칠 수 있는 것(400·422 질의 오류, 404 없는 자원,
    502·503·504 제때 못 답함 — 무거운 질의)은 "다시" 로 끝난다. 사람의 것(401·403·405·그 밖의 5xx)은 obs-doctor.py 로 보낸다."""
    if code == 401:
        return "%s 백엔드 인증 실패(401). 사람에게: %s 확인(obs-doctor.py)." % (backend, ENV_TOKEN)
    if code == 404:
        msg = _json_message(body)
        return ("%s 백엔드에 그 자원이 없다(404)%s — traceID·이름을 확인하고 다시(만료됐거나 오타일 수 있다). "
                "무엇을 물어도 404 면 사람에게(obs-doctor.py)." % (backend, (": " + msg) if msg else ""))
    if code in (403, 405):
        return "%s 백엔드가 요청을 거부했다(%d). 읽기 경로 밖이다 — 사람에게(obs-doctor.py)." % (backend, code)
    if code in (400, 422):
        msg = _json_message(body)
        return "%s 백엔드가 질의를 거부했다(%d)%s — 질의를 고쳐 다시." % (backend, code, (": " + msg) if msg else "")
    if code == 429:
        return "%s 백엔드가 속도 제한을 걸었다(429). 잠시 후 다시, 범위를 좁혀서." % backend
    if code in (502, 503, 504):
        return ("%s 백엔드가 제때 답하지 못했다(%d) — 무거운 질의가 흔한 원인이다: 창(--since)·구간([1d])·선택자를 좁혀 다시. "
                "가벼운 질의(--since 5m)도 그러면 사람에게(obs-doctor.py)." % (backend, code))
    return "%s 백엔드 오류(%d). 잠시 후 다시, 반복되면 사람에게(obs-doctor.py)." % (backend, code)


def _json_message(body: str) -> str:
    try:
        obj: JSON = json.loads(body)
    except ValueError:
        return body.strip()[:300]
    if isinstance(obj, dict):
        for k in ("error", "message"):
            v = obj.get(k)
            if v:
                return str(v)[:300]
    return ""


@dataclass
class Client:
    """Grafana 데이터소스 프록시 GET — 유일한 HTTP 진입점. 논리 이름으로 부른다."""
    config: Config
    token: str = field(default="", repr=False)
    budget: Budget = field(default_factory=Budget)
    transport: Transport | None = None   # 테스트·다른 전송 주입. None 이면 urllib GET
    timeout: float = TIMEOUT_SEC
    log: list[Response] = field(default_factory=list[Response])   # 전송을 시도한 호출 전부, 호출 순서 — 보내기 전 거부(모르는 backend · 질의 횟수 예산)는 없다

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, budget: Budget | None = None,
                 transport: Transport | None = None) -> Client:
        env = os.environ if env is None else env
        token = token_from_env(env)
        if not token:
            raise ObsError(
                "%s 이 비어 있다 — 사람에게: 토큰을 .claude/settings.local.json 의 env 에 둔다(gitignored). "
                "agent 는 값을 다루지 않는다." % ENV_TOKEN)
        return cls(config=load_config(env), token=token, budget=budget or Budget(), transport=transport)

    def get(self, backend: str, path: str, params: Params | None = None) -> Response:
        """GET <proxy>/<uid>/<path>?params. 오류는 ObsError 로 — 논리 이름만 말한다."""
        if backend not in LOGICAL_BACKENDS:
            raise ObsError("알 수 없는 백엔드 %r — metrics · logs · traces 중 하나" % backend)
        self.budget.charge_query()
        query: dict[str, str | int | float] = {k: v for k, v in (params or {}).items() if v is not None and v != ""}
        url = self.config.proxy_url(backend, path)
        if query:
            url += "?" + urllib.parse.urlencode(query, doseq=True)
        headers = {"Authorization": "Bearer " + self.token, "Accept": "application/json"}
        transport = self.transport or _urllib_transport
        try:
            status, raw = transport(url, headers, self.timeout)
        except TransportTimeout:
            raise ObsError("%s 백엔드가 %.0f초 안에 답하지 않았다 — 무거운 질의가 흔한 원인이다: 창(--since)·구간([1d])·선택자를 좁혀 다시. "
                           "가벼운 질의(--since 5m)도 그러면 사람에게(obs-doctor.py)." % (backend, self.timeout))
        except TransportError as e:
            raise ObsError("%s 백엔드에 닿지 못했다(%s). 사람에게: 게이트웨이·VPN 확인(obs-doctor.py)." % (backend, e))
        self.budget.charge_bytes(len(raw))
        if status >= 400:
            raise ObsError(http_error_message(backend, status, raw.decode("utf-8", "replace")))
        text = raw.decode("utf-8", "replace")
        body: JSON
        try:
            body = json.loads(text) if text else {}
        except ValueError:
            body = text
        return Response(backend=backend, path=path, status=status, body=body, params=query)


# ── 진단 — 사람용. 여기서만 물리(URL·uid)가 보인다 ──────────────────────────────

_PROBES: dict[str, str] = {
    "metrics": "api/v1/status/buildinfo",
    "logs": "loki/api/v1/labels",
    "traces": "api/echo",
}


@dataclass(frozen=True)
class Probe:
    backend: str
    uid: str
    url: str
    ok: bool
    status: int | None
    detail: str


@dataclass(frozen=True)
class DoctorReport:
    grafana_url: Resolved
    proxy_prefix: Resolved
    uids: dict[str, Resolved]
    config_path: str | None
    config_candidates: list[str]
    token_present: bool
    spill_dir: str
    probes: list[Probe]

    @property
    def ok(self) -> bool:
        return self.token_present and all(p.ok for p in self.probes)


def doctor(config: Config, token: str, env: Mapping[str, str] | None = None,
           transport: Transport | None = None, cwd: Path | None = None) -> DoctorReport:
    """해석된 물리 경로 · 연결 · 인증 상태. 토큰 값은 보이지 않는다(있음/없음만)."""
    env = os.environ if env is None else env
    probes: list[Probe] = []
    client = Client(config=config, token=token, budget=Budget(max_queries=len(_PROBES) + 1), transport=transport)
    for backend in LOGICAL_BACKENDS:
        path = _PROBES[backend]
        url = config.proxy_url(backend, path)
        if not token:
            probes.append(Probe(backend, config.uid(backend), url, False, None, "토큰 없음 — 연결 확인 생략"))
            continue
        try:
            r = client.get(backend, path)
            probes.append(Probe(backend, config.uid(backend), url, True, r.status, "ok"))
        except ObsError as e:
            probes.append(Probe(backend, config.uid(backend), url, False, None, str(e)))
    return DoctorReport(
        grafana_url=config.grafana_url,
        proxy_prefix=config.proxy_prefix,
        uids=dict(config.uids),
        config_path=config.config_path,
        config_candidates=[str(p) for p in config_file_candidates(env)],
        token_present=bool(token),
        spill_dir=str(spill_root(env, cwd)),
        probes=probes,
    )
