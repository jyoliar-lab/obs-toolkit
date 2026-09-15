---
name: obs-query
description: 홈랩 관측 스택(Prometheus·Loki·Tempo) 원본 조회 — PromQL·LogQL·TraceQL 을 명령 3종(promql.py·logql.py·traceql.py)으로 던진다. "지금 어떤 상태인가"(actual) — 메트릭 값·최근 로그·느린 트레이스·에러율을 물을 때 쓴다. kubectl 은 리소스 상태까지만 답하고, 그 너머(무엇이 노출·기록되는가)는 이것이 답한다. 결과는 요약이고 원본은 파일로 떨어진다.
license: MIT
compatibility: Claude Code 등 셸이 있는 호스트. Python 3.10+ (표준 라이브러리만). 관측 게이트웨이로의 네트워크 접근과 env OBS_GRAFANA_TOKEN 이 필요하다.
metadata:
  author: jyoliar-lab
  version: "0.1"
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

`promql.py` 가 not found 이면 사람에게 — 경로를 찾아 우회하지 않는다.

| 명령 | 백엔드 | 하는 일 |
|---|---|---|
| `promql.py '<PromQL>'` | metrics | 즉시 조회. `--range 1h [--step]` 구간 조회(시계열별 요약). `--labels` · `--label-values <name>` · `--series` 로 탐색 |
| `logql.py '<LogQL>'` | logs | 로그 줄. `--since 1h` `--limit 30`(기본 — 낮다). 메트릭 질의면 시계열 요약. `--labels` · `--label-values` · `--series` |
| `traceql.py search [--q '<TraceQL>']` | traces | 최근 트레이스 목록. `--min-duration 500ms` |
| `traceql.py trace <traceID>` | | 트레이스 요약 — 서비스·span 수·오류·느린 span. 원본은 항상 파일로 |
| `traceql.py tags` · `tag-values <tag>` | | 검색 가능한 태그 탐색 |
| `obs-doctor.py` | (전부) | **사람용** 진단 — 해석된 경로·연결·인증. 명령이 "사람에게" 라고 하면 사람이 이것을 돌린다 |

`--help` 에 전체 옵션이 있다. 시간은 `--since 2h` · `--end -30m` · RFC3339 · unix초.

```bash
promql.py 'up == 0'
promql.py 'sum by (pod) (rate(container_cpu_usage_seconds_total{namespace="careerhub-prod"}[5m]))' --range 1h
logql.py '{namespace="careerhub-prod"} |= "error"' --since 30m
logql.py 'sum by (app) (count_over_time({namespace="careerhub-prod"} |= "error" [5m]))' --since 2h
traceql.py search --q '{ status = error }' --since 1h
traceql.py trace 3f2a9c…
```

## 출력 규약 — 원본을 주 컨텍스트에 쏟지 않는다

- 요약 줄의 시각은 **UTC** 이고 `Z` 를 붙여 표기한다(`09-15 05:20:11Z`). 코멘트에 옮길 때 그대로 적는다.
- stdout 은 **요약** 이고 `--max-lines`(기본 40) 에서 잘린다. 잘리면 원본 전체가 `tmp/obs/<시각>-<종류>.json` 으로 떨어지고 **경로만** 돌아온다. `--json` 은 처음부터 파일로.
- 파일을 읽을 때는 통째로 열지 말고 `jq`·`grep` 으로 필요한 부분만 꺼낸다.
- Loki 는 `--limit` 기본 30 이다. 넓게 보고 싶으면 먼저 메트릭 질의(`count_over_time`)로 분포를 본 뒤 좁혀 들어간다.
- `tmp/obs/` 는 인박스다 — 프로젝트(`CLAUDE_PROJECT_DIR`) 아래, 최근 50개만 남고 오래된 것은 코어가 지운다. 커밋하지 않는다.

**넓은 탐색은 subagent 로.** 원인을 모르는 채 여러 질의를 던져 보는 조사(로그를 여러 창으로 읽기, 시계열을 훑기)는 이 명령들을 **subagent 에게 시켜** 결론만 받는다 — 시도 과정의 출력이 주 대화에 남지 않는다.
답이 한 번의 질의로 나오는 것(`up`, 특정 파드 로그 30줄, 트레이스 하나)은 직접 부른다.

## 경계

- **읽기만.** 코어에 GET 외의 메서드가 없고, 서버 쪽이 같은 것을 강제한다.
- **경로를 조립하지 않는다.** 명령이 거부하면(401·연결 실패) 메시지가 말하는 대로 사람에게 — `curl` 로 직접 닿으려 하지 않는다. 사람은 `obs-doctor.py` 로 원인을 본다.
- 질의 오류(400)와 없는 자원(404 — 만료·오타 traceID)은 백엔드 메시지가 그대로 오고 "다시" 로 끝난다 — 네가 고칠 수 있는 것이다. "사람에게" 로 끝나는 것만 사람에게.
- 트레이스는 드물다 — `search` 가 비면 `--since 24h` · `7d` 로 넓힌다.
- 관측 결과는 **관찰**이다 — 티켓 코멘트에 쌓이는 원자료지, 문서가 아니다. 해석은 증거(질의·시각·값)와 함께 적는다.
