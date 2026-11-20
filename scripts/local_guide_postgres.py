"""使用已安装的 PostgreSQL 维护本项目独立的本机向量库。"""

import argparse
import hashlib
import os
from pathlib import Path
import shutil
import stat
import subprocess
from urllib.parse import urlencode


ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = ROOT / ".runtime" / "guide-postgres"
DATA = STATE_DIR / "data"
SOCKET = Path("/tmp") / ("travel-guide-pg-" + hashlib.sha256(str(ROOT).encode()).hexdigest()[:10])
ROLE = DATABASE = "travel_guides"
PORT = "55433"


def pg_bin() -> Path:
    configured = os.environ.get("PG_BIN")
    binary = Path(configured) if configured else Path(shutil.which("pg_ctl") or "")
    if binary.is_file():
        binary = binary.resolve().parent
    if not (binary / "initdb").is_file():
        raise RuntimeError("请安装 PostgreSQL 16 与 pgvector，或通过 PG_BIN 指定 bin 目录")
    return binary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "stop", "status", "dsn"), default="start", nargs="?")
    args = parser.parse_args()
    if args.action == "dsn":
        print(f"postgresql://{ROLE}@/{DATABASE}?" + urlencode({"host": str(SOCKET), "port": PORT}))
        return 0

    binary = pg_bin()
    def run(command: str, *arguments: str, check: bool = True):
        return subprocess.run([str(binary / command), *arguments], capture_output=True, text=True, check=check)

    running = DATA.is_dir() and run("pg_ctl", "-D", str(DATA), "status", check=False).returncode == 0
    if args.action == "status":
        print("攻略数据库正在运行" if running else "攻略数据库未运行")
        return int(not running)
    if args.action == "stop":
        if running:
            run("pg_ctl", "-D", str(DATA), "-m", "fast", "-w", "stop")
        print("攻略数据库已停止")
        return 0

    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    SOCKET.mkdir(exist_ok=True, mode=0o700)
    info = SOCKET.stat()
    if SOCKET.is_symlink() or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise RuntimeError("数据库 socket 目录必须由当前用户拥有且权限为 0700")
    if not (DATA / "PG_VERSION").is_file():
        run("initdb", "-D", str(DATA), "--username=" + ROLE, "--auth-local=trust",
            "--auth-host=reject", "--encoding=UTF8", "--locale=C")
    if not running:
        # 只监听当前用户可访问的 Unix socket，不开放 TCP 端口。
        options = f"-k {SOCKET} -p {PORT} -c listen_addresses= -c unix_socket_permissions=0700"
        run("pg_ctl", "-D", str(DATA), "-l", str(STATE_DIR / "server.log"), "-o", options, "-w", "start")
    connection = ("-h", str(SOCKET), "-p", PORT, "-U", ROLE)
    exists = run("psql", *connection, "-d", "postgres", "-Atc",
                 "SELECT 1 FROM pg_database WHERE datname='travel_guides'").stdout.strip()
    if not exists:
        run("createdb", *connection, DATABASE)
    run("psql", *connection, "-d", DATABASE, "-v", "ON_ERROR_STOP=1", "-c", "CREATE EXTENSION IF NOT EXISTS vector")
    print("攻略独立 PostgreSQL + pgvector 已就绪")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
