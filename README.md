# obs-toolkit

홈랩 관측 스택(Prometheus · Loki · Tempo)을 읽는 **코어 하나**와 그 어댑터. 저장소 루트가 Claude Code 플러그인 루트다.

- `obs/core.py` — 설정(논리 이름 → 물리 해석) · GET 유일 클라이언트 · 시간 창 · 근거 · spill · 예산 · 진단. 표준 라이브러리만.
- `obs/metrics.py` — 메트릭 환원 연산 `summarize` · `compare` · `top` (순수 · `Client` 주입 · `Window` → `Evidence`). **계산은 PromQL 이 한다** — 코어는 질의를 조립(서브쿼리 위 `*_over_time` · `deriv` · `offset` · `topk`, 통계는 `label_replace … or …` 로 한 질의)하고 응답을 계약에 담으며 "데이터 없음" · "기준선 없음" · 0 을 가른다. Prometheus 형 응답 파서도 여기. 기준선 규약(`Window.with_baseline` · 기본 `1d`)은 후속 `red` 가 같이 쓴다.
- `obs/adapters/cli.py` — `promql.py`(+ 서브커맨드 `summarize` · `compare` · `top`) · `logql.py` · `traceql.py` · `obs-doctor.py`. 호스트 특정 코드는 `adapters/` 아래에만.
- `bin/` — 명령 4개(얇은 shim). Claude Code 가 플러그인이 켜진 동안 자동으로 PATH 에 올린다 — hook 없이도 bare name 실행.
- `skills/obs-query/` — 스킬(Agent Skills 스펙 필드만). 명령은 `bin/` 에만 있다.
- `hooks/hooks.json` — SessionStart 가 `bin/` 을 PATH 에, 플러그인 루트를 PYTHONPATH 에 올린다(`--plugin-dir` 대비).
- `tests/` + `fixtures/` — 녹화 응답으로 도는 pytest. GET 유일성 · 설정 해석 순서 · 물리 비노출을 검사한다.

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
OBS_TEST_DUMP=1 python -m pytest tests/test_cli.py   # 각 테스트가 받은 Evidence.lines 를 tmp/test-output/ 에 덤프
```
