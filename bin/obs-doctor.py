#!/usr/bin/env python3
"""obs-doctor.py — 얇은 shim. 본체는 obs/adapters/cli.py 의 doctor_main 이다.
플러그인 루트(obs/core.py 가 있는 조상)를 찾아 sys.path 에 올린다 — 캐시·--plugin-dir 어디에 설치돼도 같다."""
import os
import sys

_d = os.path.dirname(os.path.realpath(__file__))
while not os.path.isfile(os.path.join(_d, "obs", "core.py")):
    _d = os.path.dirname(_d)
sys.path.insert(0, _d)

from obs.adapters.cli import doctor_main, main_guard  # noqa: E402

if __name__ == "__main__":
    main_guard(lambda: doctor_main(sys.argv[1:], os.environ, None))
