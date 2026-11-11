"""扩展入口：工坊按清单跑 python -m voice（cwd = 扩展文件夹，PYTHONPATH 带 SDK）。stdout 是协议管子，日志只进 stderr。"""
from __future__ import annotations

import logging
import sys


def main() -> int:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    from wuxiankit import Extension

    from .config import load
    from .core import Voice
    from .ext import build

    voice = Voice(load())
    ext = Extension()
    build(ext, voice)
    try:
        ext.run(setup=voice.setup)
    finally:
        voice.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
