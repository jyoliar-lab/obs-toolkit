# obs-toolkit

홈랩 관측 스택(Prometheus · Loki · Tempo)을 읽는 **코어 하나**와 그 어댑터. 저장소 루트가 Claude Code 플러그인 루트다.

- `obs/core.py` — 설정(논리 이름 → 물리 해석) · GET 유일 클라이언트 · 시간 창 · 근거 · spill · 예산 · 진단. 표준 라이브러리만.
- `obs/metrics.py` — 메트릭 환원 연산 `summarize` · `compare` · `top` · `red` (순수 · `Client` 주입 · `Window` → `Evidence`). **계산은 PromQL 이 한다** — 코어는 질의를 조립(서브쿼리 위 `*_over_time` · `deriv` · `offset` · `topk`, 통계는 `label_replace … or …` 로 한 질의)하고 응답을 계약에 담으며 "데이터 없음" · "기준선 없음" · 0 을 가른다. Prometheus 형 응답 파서도 여기. 기준선 규약(`Window.with_baseline` · 기본 `1d`)은 `compare` 와 `red` 가 같이 쓴다. `red` 는 서비스 하나의 R · E · D(호출 수 · 오류율 · p95)를 spanmetrics 의 서버 span 행에서 지금 창 · 기준선 창으로 한 질의에 받아 "시계열 없음" · "호출 0" · 값을 가른다 — spanmetrics 의 메트릭 · 라벨 상수(`CALLS_METRIC` · `SPAN_METRIC` · `SERVER_KIND` · `CLIENT_KIND`)는 여기 살고 `obs/traces.py` 가 같이 쓴다.
- `obs/logs.py` — 로그 환원 연산 `histogram` · `templates` · `around` · `first_seen` (순수 · `Client` 주입 · `Window` → `Evidence`)과 Loki 로그 파서(`Stream` · `LogResult` · `parse_logs` · `LogEntry` — cli 에서 옮겨 옴). **계산은 Loki 가 한다** — `histogram` 은 `sum by (<by>) (count_over_time(…))` matrix 를 `query_range` 한 질의로, `templates` 는 Patterns API(`pattern_ingester` 필요 — 없으면 404 를 "사람에게" 로) 한 질의 + 군집마다 예시 한 줄(`|>` 패턴 필터 · forward · limit 1), `around` 는 창 전체 건수(count 질의) + 앞 · 뒤 각 `limit` 줄(`limit+1` 로 잘림 판정 · 창 밖 줄과 중심 시각 중복은 클라이언트가 거른다), `first_seen` 은 `|>` 패턴 필터 forward · limit 1. 원본 로그 줄은 예시 · 첫 발생 · 문맥에만 온다. 상한은 함수 안(`max_groups` 20 · `k` 10 · 쪽별 `limit` 50)이고 "데이터 없음"(빈 목록 · None)과 0 을 가른다 — `first_seen` 은 "창 안에 없음".
- `obs/traces.py` — 트레이스 환원 연산 `exemplars` · `critical_path` · `service_graph` · `diff` 와 Tempo 파서(검색 `spanSets` · OTLP `batches`/`resourceSpans`). 임계는 spanmetrics 의 엔드포인트별 평소 p95(조사 창 직전 `[7d] offset <창>`) · 임계경로는 CRISP(LFC · 1ms 드리프트 · 부모 경계 절단 · 루트/고아 분리) · 호출 그래프는 spanmetrics 클라이언트 행을 서버 행과 span 이름으로 조인. 보낸 요청과 결과는 `Client.log`(호출 순서 · 실패 포함)에 남고 어댑터가 raw.json 으로 떨어뜨린다.
- `obs/adapters/cli.py` — `promql.py`(+ 서브커맨드 `summarize` · `compare` · `top` · `red`) · `logql.py`(+ `histogram` · `templates` · `around` · `first-seen`) · `traceql.py`(+ `exemplars` · `critical-path` · `service-graph` · `diff`) · `obs-doctor.py`. 호스트 특정 코드는 `adapters/` 아래에만.
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

## 측정 — mutation testing · 커버리지

`tests/` 가 약속 위반을 잡는지 재는 척도 둘이다. 커버리지는 "어느 줄이 한 번도 안 돌았나", mutation 은 "돈 줄의 결과를 테스트가 검사하나" — 서로를 대체하지 못한다. 둘 다 CI 밖이다 — 로컬에서 돌리고 산출물(`mutants/` · `.coverage` · `htmlcov/`)은 커밋하지 않는다(gitignore). 설정은 `pyproject.toml` 의 `[tool.mutmut]` · `[tool.coverage.*]`, 핀은 `[dependency-groups] measure`. **두 측정은 같은 테스트 선택 위에서 돈다** — contracts · format 두 티어(infra 티어는 하네스 검사라 obs 함수를 지나지 않고, `test_core_never_sets_http_method` 는 소스를 AST 로 읽어 변이 사본과 양립하지 않아 `--deselect`). mutmut 쪽은 `pytest_add_cli_args_test_selection` 에, 커버리지 쪽은 아래 명령에 같은 인자가 있다 — 한쪽을 바꾸면 다른 쪽도 바꾼다. 소스 · 테스트는 바꾸지 않는다.

### mutation testing (mutmut)

```bash
python -m pip install mutmut==3.8.0          # 또는 python -m pip install --group measure (pip ≥ 25.1 · 핀은 pyproject 의 [dependency-groups])
unset OBS_TEST_UPDATE                        # 켜져 있으면 format 티어가 스냅샷을 다시 쓰고 통과해 렌더러 변이가 전부 살아남는다
rm -rf mutants && mutmut run                 # 빈 mutants/ 에서 시작 — 전제 검증 세 단계(stats · clean tests · forced fail)를 지나 전체를 돈다. 워커 수 기본 = CPU 수(--max-children N 으로 고정)
mutmut results                               # killed 를 뺀 목록 — survived · no tests · timeout · suspicious
mutmut show <mutant 이름>                    # 한 mutant 의 diff. mutmut browse 는 터미널 UI
mutmut run "obs.core.x_load_config*"         # 이름 패턴으로 일부만 다시 — 바뀐 함수만 재검사되므로 전체 재실행도 짧다
mutmut print-time-estimates                  # 예상 시간 — 첫 실행 뒤(mutants/ 가 있을 때)에만 동작한다
```

`python -m mutmut` 이 아니라 **`mutmut run`** 이다. 워커는 fork 라 macOS · Linux 에서 돈다(Windows 는 WSL). 상태의 뜻 — **killed**(테스트가 변이를 잡았다) · **survived**(테스트 전부 통과 — 그 결과를 검사하는 테스트가 없다) · **no tests**(그 함수를 지나는 테스트가 없다 — 함수 단위 미실행) · **timeout** · **suspicious**(측정 자체의 문제 — 설정 · 테스트를 먼저 본다). 점수는 `killed / total` 이고 `no tests` 를 뺀 분모도 함께 적는다. 측정 조건(Python 버전 · 워커 수 · 실행 시간)을 결과와 같이 남긴다 — 수치는 기록이지 목표가 아니다. 생존 mutant 는 고칠 목록이 아니라 읽고 분류할 목록이다.

**워커 수와 결정론.** 병렬 실행(워커 = CPU 수)은 killed 에 변이와 무관한 가짜가 실행당 1 ~ 4개 섞인다 — 뒤집힌 mutant 를 단독으로 돌리면 전부 survived 였다. survived · no tests 목록은 결정론적이다. 그래서 **일상 실행은 병렬로 하고 결과는 survived · no tests 목록으로 읽는다**. 점수나 killed 수를 전 · 후로 비교할 때는 `mutmut run --max-children 1`(약 6배 시간)로 기준선을 잡고, 어긋난 mutant 는 이름을 지정해 단독 재실행으로 가린다.

### 커버리지 (coverage.py)

```bash
python -m pip install coverage==7.16.2       # mutmut 이 이미 끌어온 것과 같은 버전 — 또는 --group measure
unset OBS_TEST_UPDATE
coverage erase && coverage run -m pytest tests/contracts tests/format --deselect tests/contracts/test_get_only.py::test_core_never_sets_http_method
coverage report                              # 모듈별 문장(Stmts · Miss) · 분기(Branch · BrPart) 커버리지와 미실행 줄(Missing)
coverage html                                # htmlcov/index.html — 줄 단위로 볼 때
```

`coverage run` 은 pytest 플러그인이 아니라 pytest 호출을 밖에서 감싼다 — `addopts` · CI · mutmut 의 mutant 당 pytest 에 영향이 없다. 분기 커버리지(`branch = true`)가 켜져 있어 문장 · 분기가 한 표에 나온다 — 둘 다 적는다. 미실행 줄은 "테스트가 비어 있는 자리" 의 목록이고, mutmut 의 `no tests` 함수와 같은 자리를 가리키는지 대조한다. 수치는 기록이지 목표가 아니다.
