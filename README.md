# obs-toolkit

홈랩 관측 스택(Prometheus · Loki · Tempo)을 읽는 **코어 하나**와 그 어댑터. 저장소 루트가 Claude Code 플러그인 루트다.

- `obs/core.py` — 설정(논리 이름 → 물리 해석) · GET 유일 클라이언트 · 시간 창 · 근거 · spill · 예산 · 진단. 표준 라이브러리만.
- `obs/adapters/cli.py` — `promql.py` · `logql.py` · `traceql.py` · `obs-doctor`. 호스트 특정 코드는 `adapters/` 아래에만.
- `skills/obs-query/` — 스킬(Agent Skills 스펙 필드만). `scripts/` 는 얇은 shim.
- `hooks/hooks.json` — SessionStart 가 `skills/*/scripts` 를 PATH 에, 플러그인 루트를 PYTHONPATH 에 올린다.
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
agent 인터페이스에는 URL · uid · 토큰이 없다. 물리는 `obs-doctor`(사람용)만 보여준다.

## 테스트

```bash
python -m pip install pytest==9.1.1 pyright==1.1.411
python -m pyright   # strict — Any 없음
python -m pytest
```
