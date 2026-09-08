#!/bin/sh
# SessionStart — 플러그인의 스킬 scripts/ 를 PATH 에, 플러그인 루트를 PYTHONPATH 에 올린다.
# hook 프로세스 안의 export 는 세션에 전파되지 않는다 — CLAUDE_ENV_FILE 에 쓴 export 만
# 이후 모든 Bash 호출 전에 source 된다. CLAUDE_PLUGIN_ROOT 가 hook 밖 Bash 에서 보이는지는
# 미확인이라, 값은 여기서 파일로 넘긴다(스크립트 자체는 자기 위치로 루트를 찾으므로 PATH 만 있어도 돈다).
[ -n "$CLAUDE_ENV_FILE" ] || exit 0
root="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
dirs=$(find "$root/skills" -type d -name scripts 2>/dev/null | paste -sd: -)
[ -n "$dirs" ] && printf 'export PATH="%s:$PATH"\n' "$dirs" >> "$CLAUDE_ENV_FILE"
printf 'export PYTHONPATH="%s${PYTHONPATH:+:$PYTHONPATH}"\n' "$root" >> "$CLAUDE_ENV_FILE"
exit 0
