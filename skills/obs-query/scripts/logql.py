#!/usr/bin/env python3
"""logql.py — 얇은 shim. 본체는 obs/adapters/cli.py 의 logql_main 이다.
플러그인 루트(이 파일의 세 단계 위)를 sys.path 에 올려 PYTHONPATH 없이도 돈다."""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from obs.adapters.cli import logql_main, main_guard  # noqa: E402

if __name__ == "__main__":
    main_guard(logql_main)
