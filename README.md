# obs-toolkit

홈랩 관측 스택(Prometheus · Loki · Tempo)을 읽는 **코어 하나**와 그 어댑터. 저장소 루트가 Claude Code 플러그인 루트다.

- `obs/core.py` — 설정(논리 이름 → 물리 해석) · GET 유일 클라이언트 · 시간 창 · 근거 · spill · 예산 · 진단. 표준 라이브러리만.
- `obs/metrics.py` — 메트릭 환원 연산 `summarize` · `compare` · `top` (순수 · `Client` 주입 · `Window` → `Evidence`). **계산은 PromQL 이 한다** — 코어는 질의를 조립(서브쿼리 위 `*_over_time` · `deriv` · `offset` · `topk`, 통계는 `label_replace … or …` 로 한 질의)하고 응답을 계약에 담으며 "데이터 없음" · "기준선 없음" · 0 을 가른다. Prometheus 형 응답 파서도 여기. 기준선 규약(`Window.with_baseline` · 기본 `1d`)은 후속 `red` 가 같이 쓴다.
- `obs/traces.py` — 트레이스 환원 연산 `exemplars` · `critical_path` · `service_graph` · `diff` 와 Tempo 파서(검색 `spanSets` · OTLP `batches`/`resourceSpans`). 임계는 spanmetrics 의 엔드포인트별 평소 p95(조사 창 직전 `[7d] offset <창>`) · 임계경로는 CRISP(LFC · 1ms 드리프트 · 부모 경계 절단 · 루트/고아 분리) · 호출 그래프는 spanmetrics 클라이언트 행을 서버 행과 span 이름으로 조인. 보낸 요청과 결과는 `Client.log`(호출 순서 · 실패 포함)에 남고 어댑터가 raw.json 으로 떨어뜨린다.
- `obs/adapters/cli.py` — `promql.py`(+ 서브커맨드 `summarize` · `compare` · `top`) · `logql.py`(+ `histogram` · `templates` · `around` · `first-seen`) · `traceql.py`(+ `exemplars` · `critical-path` · `service-graph` · `diff`) · `obs-doctor.py`. 호스트 특정 코드는 `adapters/` 아래에만.
- `bin/` — 명령 4개(얇은 shim). Claude Code 가 플러그인이 켜진 동안 자동으로 PATH 에 올린다 — hook 없이도 bare name 실행.
- `skills/obs-query/` — 스킬(Agent Skills 스펙 필드만). 명령은 `bin/` 에만 있다.
- `hooks/hooks.json` — SessionStart 가 `bin/` 을 PATH 에, 플러그인 루트를 PYTHONPATH 에 올린다(`--plugin-dir` 대비).
- `tests/` + `fixtures/` — 녹화 fixture 로 도는 pytest. 세 티어(`contracts/` 약속 · `format/` 출력 스냅샷 · `infra/` 하네스)와 작성 규칙은 `.claude/rules/python-tests.md`, 그것을 지키는 메타테스트는 `tests/infra/test_conventions.py`(위반 0 이 기준). GET 유일성 · 설정 해석 순서 · 물리 비노출을 검사한다. fixture 는 **호출 요청(백엔드 · 경로 · query string · 기준 시각 `at`)과 응답을 함께 담은 JSON 봉투**(`tests/record.py` 의 형식)이고, 테스트는 `replay`(conftest)로 녹화 호출을 순서대로 돌려받으며 **나간 요청이 녹화 요청과 같은지 호출마다 검사**한다(`tests/replay.py` — `request_key` 정규화 · 녹화 `at` 주입 · 검사를 끄는 유일한 길은 `unchecked=<이유>`). 녹화기 `tests/record.py` 는 `Client.transport` 래퍼 — 실환경에 한 번 돌리면 파일 이름 · 호출(전부) · 응답이 한 번에 떨어진다.

## 쓰기

```bash
# 로컬 개발
claude --plugin-dir .

# 하네스 — .claude/settings.json
{
  "extraKnownMarketplaces": { "obs-toolkit": { "source": { "source": "github", "repo": "jyoliar-lab/obs-toolkit" } } },
  "enabledPlugins": { "obs-toolkit@obs-toolkit": true }
}
```

토큰은 env `OBS_GRAFANA_TOKEN` 으로만 받는다 — 프로젝트의 `.claude/settings.local.json`(gitignored)의 `env` 에 둔다.
코어가 읽는 env 전부와 뜻은 `.env.example` 에 있다(셸에서 직접 부를 때는 복사해 `.env` 로).
agent 인터페이스에는 URL · uid · 토큰이 없다. 물리는 `obs-doctor.py`(사람용)만 보여준다.

## 테스트

```bash
python -m pip install pytest==9.1.1 pyright==1.1.411
python -m pyright   # strict — Any 없음
python -m pytest
OBS_TEST_UPDATE=1 python -m pytest tests/format       # format 티어의 스냅샷(tests/format/snapshots/) 을 이번 출력으로 갱신

# fixture 재녹화 — 실환경 토큰(OBS_GRAFANA_TOKEN) 필요. 한 실행의 모든 fixture 가 같은 at 을 받는다
python -m tests.record --all                        # 13개 전부(레시피: tests/record.py 의 RECIPES)
python -m tests.record traceql-trace.json           # 하나만. 재현 불가(오류 trace 없음 등)면 ✗ 로 남기고 파일은 건드리지 않는다
```

`obs/metrics.py` 등이 질의 조립을 바꾸면 fixture replay 테스트가 **요청 불일치**로 실패한다(문구에 나간 요청 · 녹화 요청이 나란히) — 그때는 `python -m tests.record <fixture>` 로 다시 녹화한다. 공유 fixture(`promql-instant-empty.json`)만 `unchecked=` 로 검사를 끄고, 그 이유가 테스트 코드에 있다.

fixture 에 `recorded: manual` 표시가 있는 것은 재현할 수 없는 조건의 손 기록이고 `reason` 이 이유를 말한다 — 표시 없는 파일은 전부 녹화기가 만든 것이다. 물리(호스트 · 프록시 경로 · uid · 토큰)는 fixture 에 실리지 않는다(`tests/test_fixtures.py` 가 파일 전체를 검사한다).
