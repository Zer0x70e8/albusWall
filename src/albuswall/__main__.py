#! /usr/bin/python3
# -*- coding: utf-8 -*-
""""""

import sys

from albuswall.entrypoint import should_launch_main
from albuswall.cli import run_cli
from albuswall.main import main

if __name__ == "__main__":
    if should_launch_main():
        raise SystemExit(main())
    raise SystemExit(run_cli(sys.argv[1:]))
