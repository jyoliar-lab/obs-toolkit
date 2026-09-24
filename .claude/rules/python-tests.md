# Python 테스트 — 세 티어와 작성 규칙

`tests/` 는 세 티어로 나뉜다. 티어의 정의는 **"실패하면 사람이 무엇을 하나"** 다. 규약은 `tests/infra/test_conventions.py`(메타테스트)가 CI 에서 지킨다 — 새 위반은 즉시 실패하고, 기존 위반은 래칫 allowlist 로 줄어들기만 한다.

## 세 티어 — 운영적 정의

| 티어 | 자리 | 실패의 뜻 | 사람이 하는 일 |
|---|---|---|---|
| **contracts** | `tests/contracts/` | **약속이 깨졌다** | 생각한다. 스냅샷 갱신으로 지울 수 없다 — 코드가 틀렸거나 약속이 바뀐 것이고, 후자면 테스트를 고쳐 쓴다. |
| **format** | `tests/format/` | **출력이 바뀌었다** | 의도한 변경이면 스냅샷을 갱신한다(`OBS_TEST_UPDATE=1 python -m pytest …`). 아니면 버그다. |
| **infra** | `tests/infra/` | **하네스가 깨졌다** — 녹화기 · replay · 플러그인 골격 · 규약 | 하네스를 고친다. 소스(`obs/`)의 문제가 아니다. |

### 어느 티어인가 — 판정 기준

- 단언이 보는 것이 **dataclass 의 값 · 예외 · 나간 요청**(`t.calls` 의 수 · URL 의 파라미터)이면 contracts. 렌더링 함수(`*_lines`)가 만든 **줄의 모양**이면 format.
- 렌더링된 줄에 거는 단언이라도 **성질**("물리 문자열이 없다" · "10줄 이하다")이면 contracts 에 둘 수 있다 — 모양이 바뀌어도 깨지지 않는 단언이 성질이다.
- 출력 모양이 바뀌었을 때 손대는 파일은 `tests/format/snapshots/` 뿐이어야 한다. 그 밖의 테스트가 깨지면 그 테스트는 잘못된 층에 있다.
- **format 은 안전망이 아니다.** 스냅샷 diff 가 약속 위반을 드러냈다면 답은 갱신이 아니라 contracts 에 그 성질의 단언을 더하는 것이다 — 그래야 갱신으로 지울 수 없는 실패가 남는다.

### contracts — 약속

- 입력은 **손으로 만든 최소 입력**뿐이다 — `_vector(…)` 류의 응답 조립 함수, 명시 라우트(`(backend, path)` 키)의 `FakeTransport`.
- 단언은 **성질**만 — "없다" · "거부한다" · "0 이 아니다" · "1회다". 값을 문자 그대로 박지 않는다.
- **replay 를 쓰지 않는다** — `replay(` · `load_response(` 가 있으면 메타테스트가 거부한다. 녹화 데이터는 약속의 증거가 아니라 출력의 표본이다.

### format — 출력의 모양

- 입력은 **녹화 fixture 의 replay** (`replay("….json")` · `at=t.at`).
- 단언은 둘 중 하나 — **스냅샷 파일**(`assert_snapshot(request, lines)` → `tests/format/snapshots/<테스트 이름>.txt`) 또는 **fixture 를 파서로 다시 읽어 유도한 불변식**(줄 수 = 스트림의 값 수 합, 정렬 순서 등).
- **fixture 의 값을 문자 그대로 단언에 박지 않는다** — `len(lines) == 65` 가 아니라 스냅샷이거나 파서로 센 수다. 재녹화가 단언을 깨뜨리면 그 단언은 잘못 놓인 것이다.
- 스냅샷 파일은 커밋한다. 파일이 없으면 첫 실행이 쓴다 — 새 format 테스트는 스냅샷과 함께 올린다.

### infra — 하네스

- 녹화기(`tests/record.py`) · replay(`tests/replay.py`) · `FakeTransport` · 스냅샷 헬퍼 · 플러그인 골격(bin/ · manifests) · 규약 메타테스트가 대상이다.
- 소스를 고치지 않고 하네스만 고쳐서 통과해야 하는 테스트만 여기 둔다.

### 층마다 검사할 것은 그 층이 더한 것뿐

- `metrics.top` 의 **성질**(k 로 자른다 · 내림차순 · 빈 응답은 "없음")은 contracts.
- `top_lines` 의 **모양**(줄의 형식 · 헤더)은 format 스냅샷.
- `promql_main` 은 **디스패치와 stdout 절제**만 — 계산 · 모양을 다시 검사하지 않는다.

## 작성 규칙 — 메타테스트가 거부하는 여섯 가지

1. **`test_*` 함수에 docstring 이 있다.** 한 테스트 = 한 주장. 이름과 docstring 이 그 주장 하나를 **현재형**으로 말한다.
2. **assert 를 `and` 로 잇지 않는다.** 실패하면 pytest 가 어느 조건인지 보여주지 못한다 — 조건마다 assert 한 줄.
3. **`tests/contracts/` 에서 `replay(` · `load_response(` 를 부르지 않는다.**
4. **`PHYSICAL`(물리 문자열 목록)은 `tests/conftest.py` 한 곳에만 정의한다.** `_no_physical(text)` 도 conftest 의 것을 쓴다.
5. **주석에 `재녹화` · `YYYY-MM-DD` 꼴 날짜를 두지 않는다.** 티켓 AC 번호 · 재녹화 날짜는 docstring · 주석의 것이 아니다 — 출처는 커밋 메시지다.
6. **`test_*.py` 는 세 티어 디렉터리 안에만 둔다.** `tests/` 직속이든 다른 하위 디렉터리든 티어 밖의 테스트 파일은 위반이다 — 어느 티어인지 정하지 않은 테스트는 없다.

그 밖의 관례:

- **replay 검사를 끄는 유일한 길은 `unchecked=<이유>`** 이고 이유는 비어 있을 수 없다. 공유 fixture 의 이유는 conftest 의 `UNCHECKED_SHARED_EMPTY` 상수를 쓴다.
- `FakeTransport` 라우트 키는 `(backend, native_path)` **정확 일치**다 — 경로 조각 · `?` 트릭이 없다. 등록되지 않은 요청은 실패 문구에 등록된 키를 나열한다.
- 스냅샷 파일 이름은 `request.node.name`(파라미터 포함) 그대로다.

## 래칫 allowlist

`tests/infra/test_conventions.py` 의 `ALLOWLIST` 가 현재 위반을 `{"tests/test_cli.py::test_promql_instant_up": {"and": 3}, …}` 꼴로 든다.

- 실제 위반이 allowlist 보다 **많으면** 실패(새 위반).
- **적으면** allowlist 를 줄이라고 실패(느슨한 채로 남지 않게) — 위반을 고쳤으면 그 항목을 그만큼 뺀다.
- 같으면 통과. **전부 비면 `ALLOWLIST` 를 지운다** — 재편의 완료 기준이 "allowlist 가 빈다" 다.
