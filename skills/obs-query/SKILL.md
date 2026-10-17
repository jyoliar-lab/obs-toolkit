---
name: obs-query
description: 홈랩 관측 스택(Prometheus·Loki·Tempo) 원본 조회 — PromQL·LogQL·TraceQL 을 명령 3종(promql.py·logql.py·traceql.py)으로 던진다. "지금 어떤 상태인가"(actual) — 메트릭 값·최근 로그·느린 트레이스·에러율을 물을 때 쓴다. kubectl 은 리소스 상태까지만 답하고, 그 너머(무엇이 노출·기록되는가)는 이것이 답한다. 결과는 요약이고 원본은 파일로 떨어진다.
license: MIT
compatibility: Claude Code 등 셸이 있는 호스트. Python 3.10+ (표준 라이브러리만). 관측 게이트웨이로의 네트워크 접근과 env OBS_GRAFANA_TOKEN 이 필요하다.
metadata:
  author: jyoliar-lab
  version: "0.2"
---

# obs-query — 관측 스택 읽기

**actual 은 관측 스택으로 읽는다.** desired 는 코드(GitOps), actual 은 메트릭·로그·트레이스다.
`kubectl get` 이 "파드가 떠 있다"까지 답하면, "무엇을 내보내고 무엇이 기록됐는가"는 여기서 답한다.

백엔드는 셋이고 너는 이름만 안다 — **metrics**(PromQL) · **logs**(LogQL) · **traces**(TraceQL).
어디에 있고 어떤 경로로 닿는지는 코어의 설정 계층이 푼다 — 호스트·URL 을 알 필요도, 조립할 필요도 없다.
테넌트는 서버가 얹고, 읽기 전용은 서버가 건다. 코어는 그 위에서 GET 만 쓴다.
MCP 는 쓰지 않는다 — PromQL·LogQL·TraceQL 은 네가 아는 언어다. 도구 정의 없이 질의를 직접 쓴다.

## 준비 — 토큰은 env 로만

`OBS_GRAFANA_TOKEN` 이 비어 있으면 명령이 거부한다.
값은 **사람이** 꺼내 프로젝트의 `.claude/settings.local.json`(gitignored)의 `env` 에 둔다:

```json
{ "env": { "OBS_GRAFANA_TOKEN": "<토큰>" } }
```

agent 는 이 값을 읽지도 옮기지도 않는다 — 어디에도 적지 않고, 출력에도 남기지 않는다.

## 명령 — bare name 으로 실행한다

명령이 not found 이면 사람에게 — 경로를 찾아 우회하지 않는다.

| 명령 | 백엔드 | 하는 일 |
|---|---|---|
| `promql.py '<PromQL>'` | metrics | 즉시 조회. `--range 1h [--step]` 구간 조회(시계열별 요약). `--labels` · `--label-values <name>` · `--series` 로 탐색 |
| `promql.py summarize '<PromQL>' --since 1h` | metrics | **환원** — 시계열별 n · min · p50 · p95 · max · last · 기울기. 계산은 백엔드(서브쿼리 위 `*_over_time` · `deriv`), 질의 1회 |
| `promql.py compare '<PromQL>' --since 1h [--baseline 1d]` | metrics | **환원** — 같은 식의 p95 를 지금 창과 `offset` 기준선 창에서 한 질의로 받아 시계열별 차이(×비율 · Δ)만. 기준선 `1d`(기본 — 1일 전 같은 시각) · `1w` · `prev`(직전 창). 기준선에 없으면 "기준선 없음" |
| `promql.py red <service> --since 24h [--baseline 1d]` | metrics | **환원** — 서비스 하나의 R · E · D 삼각측량: 창 안 호출 수 · 오류율 · p95(초)를 지금 창과 기준선 창에서 한 질의로 받아 행마다 `×비율 · now · base`. 기준선은 `compare` 와 같다(`1d` 기본 · `1w` · `prev`). 세 상태를 가른다 — "시계열 없음"(그 서비스가 span 을 낸 적이 없다) · "호출 0"(시계열은 있고 창 안 호출이 없다 — D 는 산출 불가) · 값. "기준선 없음" · "기준선 0" 이면 비율이 없다. **창은 24h 이상으로** — 홈랩 트래픽은 하루 한 번 도는 cron 이 거의 전부라 1h 창은 전 서비스가 호출 0 이다 |
| `promql.py top '<PromQL>' --by pod [--k 10] [--agg sum]` | metrics | **환원** — 레이블 축 상위 k, `--end` 시각의 즉시 조회(창 없음). `--by` 는 쉼표로 여러 축(`namespace,pod`), `--agg` 는 `sum`(기본) · `max` · `min` · `avg` · `count`. `topk` 로 감싸 서버가 k 개만 돌려준다(카디널리티 폭발 없음) |
| `logql.py '<LogQL>'` | logs | 로그 줄. `--since 1h` `--limit 30`(기본 — 낮다). 메트릭 질의면 시계열 요약. `--labels` · `--label-values` · `--series` |
| `traceql.py search [--q '<TraceQL>']` | traces | 최근 트레이스 목록. `--min-duration 500ms` |
| `traceql.py trace <traceID>` | | 트레이스 요약 — 서비스·span 수·오류·느린 span. 원본은 항상 파일로. **느린 span top 은 병목이 아니다** — 루트는 자식을 포함해 늘 제일 길다. 병목은 `critical-path` 로 |
| `traceql.py tags` · `tag-values <tag>` | | 검색 가능한 태그 탐색 |
| `traceql.py exemplars [--q '<조건식>'] --since 1h [--k 3]` | traces + metrics | **환원** — 서버 span 을 느림 · 오류 · 정상 세 묶음으로 나눠 묶음마다 대표 트레이스 k 개. "느림" 의 임계는 **엔드포인트마다 평소 p95**(조사 창 직전 `--baseline-range 7d` 의 spanmetrics 분포, `--quantile 0.95`) — 없으면 조사 창 p95 → 표본 p95 순 폴백이고 건마다 적용한 임계와 출처(`baseline` · `window` · `sample`)가 붙는다. 요약 줄 "표본 N · 임계 초과 M · 평소 기대 5%" 가 묶음을 읽는 맥락. 트레이스를 열지 않는다(GET 2회) |
| `traceql.py critical-path <traceID>` | traces | **환원** — CRISP 임계경로: 마지막에 끝난 자식을 따라 내려가며 span 마다 `+시작 오프셋 · 길이 · self(자식에 귀속되지 않은 시간)`, 아래에 **병목 = self 내림차순 상위 3**. 부모 경계를 넘친 자식은 잘리고 넘친 길이가 표시된다(`+400.0ms`). 루트가 둘 이상 · 고아(부모 유실) · 루트 없음은 헤더에 표시 — 불완전한 트레이스를 숨기지 않는다 |
| `traceql.py service-graph --since 1h` | metrics | **환원** — 호출 그래프를 spanmetrics 클라이언트 span 행에서: `호출한 서비스 → 불린 서비스` 아래 엔드포인트마다 `×배율(조사 창 p95 / 평소 p95) · 건수 · 오류율 · 클라이언트 p95(평소) · 서버 p95`. 배율 내림차순이라 **첫 줄이 가장 늘어난 홉**. 불린 서비스는 같은 이름의 서버 행으로 조인 — 없거나 모호하면 "미확인". 창 안 호출 0 은 "호출 0", 행 자체가 없으면 "데이터 없음" |
| `traceql.py diff <느린 traceID> <정상 traceID>` | traces | **환원** — 같은 루트 이름의 두 트레이스를 (서비스, span 이름) 으로 짝지어 `Δ = 느린 − 정상` 만. 한쪽에만 있는 span 은 "한쪽에만 있음". 루트 이름이 다르면 거부 |
| `obs-doctor.py` | (전부) | **사람용** 진단 — 해석된 경로·연결·인증. 명령이 "사람에게" 라고 하면 사람이 이것을 돌린다 |

`--help` 에 전체 옵션이 있다. 시간은 `--since 2h` · `--end -30m` · RFC3339 · unix초.

```bash
promql.py 'up == 0'
promql.py 'sum by (pod) (rate(container_cpu_usage_seconds_total{namespace="careerhub-prod"}[5m]))' --range 1h
promql.py compare 'sum by (pod) (rate(container_cpu_usage_seconds_total{namespace="careerhub-prod"}[5m]))' --since 1h --baseline 1d
promql.py top 'container_memory_working_set_bytes' --by pod --k 10
promql.py red posting-service --since 24h --baseline 1d
logql.py '{namespace="careerhub-prod"} |= "error"' --since 30m
logql.py 'sum by (app) (count_over_time({namespace="careerhub-prod"} |= "error" [5m]))' --since 2h
traceql.py search --q '{ status = error }' --since 1h
traceql.py trace 3f2a9c…
traceql.py exemplars --q 'resource.service.name = "posting-service"' --since 6h --k 3
traceql.py critical-path 3f2a9c…
traceql.py diff 3f2a9c… 8b1d04…
traceql.py service-graph --since 6h
```

**트레이스 조사의 순서** — "어느 홉이 늘었나" 는 span 트리를 읽지 않고 안다:
1. `exemplars` 로 느린 · 정상 대표를 고른다(묶음 머리의 `없음` 은 0 건, `데이터 없음` 은 표본 자체가 없는 것 — 창을 넓힌다).
2. 느린 대표의 `critical-path` 로 병목(self 가 큰 span)을 본다 — `trace --top` 의 "제일 긴 span" 이 아니다.
3. 느린 대표와 정상 대표를 `diff` 로 — 어느 span 의 시간이 늘었는지만 남는다.
4. 표본 1~2 건에서 본 홉이 집단에서도 그런지 `service-graph` 로 일반화한다(같은 창 · 평소 7일 대비 배율 · 건수).

## 출력 규약 — 원본을 주 컨텍스트에 쏟지 않는다

- **"높은가" 는 절대값이 아니라 기준선 대비로 판단한다.** 시계열을 두 번 조회해 눈으로 비교하지 말고 `promql.py compare` 를 쓴다 — 차이(×비율 · Δ)만 돌아온다. "데이터 없음" 과 `0` 은 다르다 — 시계열이 없으면 그렇게 표시된다. 서비스 하나가 "이상한가" 는 `promql.py red <service>` 가 표준 진입이다 — R · E · D 세 행을 기준선 대비로 한 번에.
- **"느린가" 도 같다 — 엔드포인트의 평소 p95 대비다.** `exemplars` 의 느림 묶음은 그 기준으로 골라진 것이고, 건마다 붙은 `×배율 · 임계(출처)` 가 근거다. 읽을 때 셋을 본다: (1) 출처 — `baseline` 이 평소 분포, `window` 는 조사 창 자신(창 전체가 느려졌으면 못 잡는다), `sample` 은 검색 표본(가장 약하다) (2) 요약 줄의 "표본 N 중 초과 M(평소 기대 5%)" — M/N 이 5% 를 얼마나 넘는가가 신호의 크기 (3) **평소 p95 가 아주 작은 값(1ms 미만)이면 배율이 수백 배로 부풀 수 있다** — 홈랩은 호출이 드물어 7일 분포가 몇 건으로 만들어진다. 그때는 배율이 아니라 지속시간 절대값과 `service-graph` 의 건수로 읽고, 열화의 측정은 `promql.py compare` 의 것이다.
- **병목은 self 시간이다.** 트레이스에서 "제일 긴 span" 은 루트다 — 자식을 포함하기 때문. `critical-path` 의 병목 줄(self 내림차순)을 인용하고, 잘림(`+Nms`) · 고아 · 루트 없음 표시가 있으면 그 트레이스는 불완전하다고 함께 적는다.
- 요약 줄의 시각은 **UTC** 이고 `Z` 를 붙여 표기한다(`09-15 05:20:11Z`). 코멘트에 옮길 때 그대로 적는다.
- 명령 한 번이 **호출 디렉터리 하나** `tmp/obs/<시각>-<종류>/` 를 만든다 — `raw.json`(응답 원본) · `summary.txt`(머리에 창·기준선, 그 아래 잘리지 않은 요약 전체). stdout 은 **요약** 이고 `--max-lines`(기본 40) 에서 잘리며, 마지막 줄이 그 경로다. `--json` 은 stdout 에 요약을 찍지 않고 경로만.
- `raw.json` 은 어느 명령이든 **보낸 호출 전부를 호출 순서로** 담은 목록 `[{backend, path, params, status, body, error, size, truncated}, …]` 이다 — 호출이 하나인 명령도 원소 하나짜리 목록이고, 원소 수가 그 명령이 보낸 GET 수다. 응답 본문은 `.[].body`, 어느 질의의 응답인지는 `.[].params` 로 꺼낸다. 실패한 호출도 원소다: HTTP 오류는 `status` 와 본문 전체가, 전송 실패(연결 실패 · 시간 초과)는 `status: null` 과 `error`(오류 문구)가, 응답 총량 예산을 넘긴 응답은 `size` 와 앞부분만(`truncated: true`) 실린다.
- **명령이 실패해도 호출 디렉터리는 남는다** — `tmp/obs/<시각>-<명령>-failed/`(명령 = `promql` · `promql-summarize` · `logql-templates` …). `raw.json` 에 그때까지 보낸 호출(성공한 앞 호출 + 실패한 호출)이 순서대로, `summary.txt` 에 오류 문구가 있고, stderr 의 오류 문구 끝이 그 경로다. 보낸 호출이 없는 실패(질의 없음 등)는 디렉터리를 만들지 않는다.
- 파일을 읽을 때는 통째로 열지 말고 `jq`·`grep` 으로 필요한 부분만 꺼낸다.
- Loki 는 `--limit` 기본 30 이다. 넓게 보고 싶으면 먼저 메트릭 질의(`count_over_time`)로 분포를 본 뒤 좁혀 들어간다.
- `tmp/obs/` 는 인박스다 — 프로젝트(`CLAUDE_PROJECT_DIR`) 아래, 최근 50개 호출만 남고 오래된 것은 코어가 지운다. 커밋하지 않는다.

**넓은 탐색은 subagent 로.** 원인을 모르는 채 여러 질의를 던져 보는 조사(로그를 여러 창으로 읽기, 시계열을 훑기)는 이 명령들을 **subagent 에게 시켜** 결론만 받는다 — 시도 과정의 출력이 주 대화에 남지 않는다.
답이 한 번의 질의로 나오는 것(`up`, 특정 파드 로그 30줄, 트레이스 하나)은 직접 부른다.

## 경계

- **읽기만.** 코어에 GET 외의 메서드가 없고, 서버 쪽이 같은 것을 강제한다.
- **경로를 조립하지 않는다.** 명령이 거부하면(401·연결 실패) 메시지가 말하는 대로 사람에게 — `curl` 로 직접 닿으려 하지 않는다. 사람은 `obs-doctor.py` 로 원인을 본다.
- 질의 오류(400)와 없는 자원(404 — 만료·오타 traceID)은 백엔드 메시지가 그대로 오고 "다시" 로 끝난다 — 네가 고칠 수 있는 것이다. "사람에게" 로 끝나는 것만 사람에게.
- **제때 답하지 못한 것**(읽기 시간 초과 · 502/503/504)도 먼저 네 것이다 — 무거운 질의가 흔한 원인이다. 창(`--since`)·구간(`[1d]`)·선택자를 좁혀 다시. 가벼운 질의(`--since 5m`)도 같으면 그때 사람에게.
- 트레이스는 드물다 — `search` · `exemplars` · `service-graph` 가 "데이터 없음" 이면 `--since 24h` · `7d` 로 넓힌다. 하루 수십 건의 트래픽이라 `service-graph` 의 건수가 작다 — 건수와 함께 읽는다.
- `exemplars` 의 검색은 `--limit`(기본 100) 에서 절단되고 Tempo 검색은 순서 보장이 없다 — "절단" 표시가 있으면 재실행마다 표본이 달라질 수 있다. 조건식(`--q`)으로 서비스를 좁혀 절단을 없앤 뒤 인용한다.
- 관측 결과는 **관찰**이다 — 티켓 코멘트에 쌓이는 원자료지, 문서가 아니다. 해석은 증거(질의·시각·값)와 함께 적는다.
