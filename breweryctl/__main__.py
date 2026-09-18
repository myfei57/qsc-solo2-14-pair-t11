"""``python -m breweryctl`` 的进程入口。"""

from breweryctl.runtime.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
