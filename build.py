"""打包脚本：把源码打成 Windows 发行包。

用法：
    python build.py            # 构建 onedir 目录 + zip
    python build.py --clean    # 构建前清理产物

产物：
    dist/BiliLiveSongDrop/            PyInstaller onedir 目录（含 exe）
    dist/BiliLiveSongDrop-windows-x64.zip   上传到 GitHub Release 的包

zip 内部不含顶层目录——解压出来的文件直接就是 exe 和依赖，
和 updater.py 的「解压覆盖安装目录」逻辑一一对应。别改成带壳目录，
一改更新流程就废。
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

from app import APP_NAME, __version__

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
BUILD = ROOT / "build"
APP_DIR = DIST / APP_NAME
ZIP_PATH = DIST / f"{APP_NAME}-windows-x64.zip"

ENTRY = "main.py"
ASSETS = [("gui", "gui"), ("requirements.txt", ".")]


def run(cmd: list[str]) -> None:
    print("  $", " ".join(cmd))
    subprocess.run(cmd, check=True, cwd=str(ROOT))


def clean() -> None:
    print("清理旧产物")
    for d in (BUILD, APP_DIR):
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
    ZIP_PATH.unlink(missing_ok=True)


def ensure_pyinstaller() -> None:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller 未安装，正在安装…")
        run([sys.executable, "-m", "pip", "install", "pyinstaller"])


def build() -> None:
    ensure_pyinstaller()
    print(f"打包 {APP_NAME} v{__version__} (onedir)")

    data_args = []
    for src, dst in ASSETS:
        data_args += ["--add-data", f"{src};{dst}"]

    run([
        sys.executable, "-m", "PyInstaller",
        "--name", APP_NAME,
        "--noconfirm", "--clean",
        "--onedir",
        "--windowed",                       # GUI 程序，不要控制台黑框
        "--hidden-import", "webview.platforms.edgechromium",
        "--hidden-import", "brotli",
        "--collect-submodules", "webview",
        *data_args,
        ENTRY,
    ])

    if not APP_DIR.exists():
        raise SystemExit(f"打包失败：{APP_DIR} 不存在")

    print(f"\n打包完成：{APP_DIR}")
    print(f"主程序：{APP_DIR / (APP_NAME + '.exe')}")


def make_zip() -> None:
    print(f"制作发行包：{ZIP_PATH}")
    if ZIP_PATH.exists():
        ZIP_PATH.unlink()

    with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in APP_DIR.rglob("*"):
            if path.is_file():
                zf.write(path, path.relative_to(APP_DIR))

    size = ZIP_PATH.stat().st_size / 1024 / 1024
    print(f"完成：{ZIP_PATH.name}  {size:.1f} MB")


def main() -> int:
    parser = argparse.ArgumentParser(description=f"打包 {APP_NAME}")
    parser.add_argument("--clean", action="store_true", help="构建前清理旧产物")
    args = parser.parse_args()

    if args.clean:
        clean()

    build()
    make_zip()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
