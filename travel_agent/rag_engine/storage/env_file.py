"""供外部服务配置显式加载的 .env 解析器。"""

import re
import shlex
from pathlib import Path


def read_env_file(path: Path) -> dict[str, str]:
    """读取基础 KEY=VALUE，支持引号、export 和注释；不展开变量或执行内容。

    不支持多行值；如需完整 dotenv 语法，可后续替换此配置适配器。
    """
    if not path.exists():
        return {}
    values = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f"配置文件第 {number} 行格式错误")
        try:
            parts = shlex.split(value, comments=True, posix=True)
        except ValueError:
            raise ValueError(f"配置文件第 {number} 行引号不完整") from None
        if len(parts) > 1:
            raise ValueError(f"配置文件第 {number} 行包含未加引号的空格")
        values[key] = parts[0] if parts else ""
    return values
