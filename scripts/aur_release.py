#!/usr/bin/env python3
"""发布 AUR 包：算源码哈希、回填 PKGBUILD、重新生成 .SRCINFO。

AUR 的 ``PKGBUILD`` 指向 GitHub 的 tag 源码包，
``https://github.com/Zer0x70e8/albusWall/archive/refs/tags/v<pkgver>.tar.gz``。
这个哈希只能在 tag 推上去之后才算得出来，所以顺序是：

    1. 改版本号 -> 提交 -> 打 tag -> push tag
    2. python scripts/aur_release.py            # 下载 tag 源码包，算 sha256
    3. 把 packaging/aur/{PKGBUILD,.SRCINFO} 同步到 AUR 仓库

用法::

    python scripts/aur_release.py                 # 用 PKGBUILD 里的 pkgver
    python scripts/aur_release.py --version 1.0.0 # 同时改写 pkgver
    python scripts/aur_release.py --tarball x.tar.gz   # 用本地包（离线/预演）
    python scripts/aur_release.py --check         # 只下载并打印哈希，不写文件
    python scripts/aur_release.py --srconly       # 只重生成 .SRCINFO
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUR_DIR = ROOT / "packaging" / "aur"
PKGBUILD = AUR_DIR / "PKGBUILD"
SRCINFO = AUR_DIR / ".SRCINFO"
REPO = "Zer0x70e8/albusWall"
USER_AGENT = f"albuswall-aur-release (+https://github.com/{REPO})"
DOWNLOAD_ATTEMPTS = 3


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _pkgver() -> str:
    match = re.search(r"^pkgver=(\S+)$", _read(PKGBUILD), re.MULTILINE)
    if match is None:
        raise SystemExit("PKGBUILD 里找不到 pkgver=…")
    return match.group(1)


def _set_pkgver(version: str) -> None:
    text = _read(PKGBUILD)
    text = re.sub(r"^pkgver=\S+$", f"pkgver={version}", text, count=1, flags=re.MULTILINE)
    _write(PKGBUILD, text)


def _tarball_url(version: str) -> str:
    return f"https://github.com/{REPO}/archive/refs/tags/v{version}.tar.gz"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download(url: str, dest: Path) -> Path:
    """下载 tag 归档。

    国内网络下 codeload.github.com 经常直接掐断连接（RemoteDisconnected），
    所以带重试 + 明确的失败提示，而不是把 urllib 的堆栈甩给用户。
    """
    last_error: Exception | None = None
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        suffix = "" if attempt == 1 else f"（第 {attempt}/{DOWNLOAD_ATTEMPTS} 次尝试）"
        print(f"下载 {url}{suffix}")
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with (
                urllib.request.urlopen(request, timeout=120) as response,
                dest.open("wb") as out,
            ):
                while chunk := response.read(1024 * 1024):
                    out.write(chunk)
        except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
            last_error = exc
            dest.unlink(missing_ok=True)
            print(f"  失败：{exc!r}", file=sys.stderr)
            if attempt < DOWNLOAD_ATTEMPTS:
                time.sleep(attempt * 3)
            continue
        return dest

    raise SystemExit(
        f"下载失败：{last_error!r}\n"
        "网络受限时可以先手动下载再离线算哈希：\n"
        f"  wget -O albuswall-{_pkgver()}.tar.gz {url}\n"
        f"  python scripts/aur_release.py --tarball albuswall-{_pkgver()}.tar.gz"
    )


def _set_checksum(digest: str) -> None:
    text = _read(PKGBUILD)
    new_text, count = re.subn(
        r"^sha256sums=\('[^']*'\)",
        f"sha256sums=('{digest}')",
        text,
        count=1,
        flags=re.MULTILINE,
    )
    if count != 1:
        raise SystemExit("PKGBUILD 里找不到 sha256sums=(…)")
    _write(PKGBUILD, new_text)


def _print_srcinfo() -> None:
    try:
        result = subprocess.run(
            ["makepkg", "--printsrcinfo"],
            cwd=AUR_DIR,
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        print("找不到 makepkg：请在 Arch 上运行，或手动更新 .SRCINFO", file=sys.stderr)
        raise SystemExit(2) from None
    except subprocess.CalledProcessError as exc:
        print(exc.stderr or exc.output, file=sys.stderr)
        raise SystemExit(exc.returncode) from None

    _write(SRCINFO, result.stdout)
    print(f"已生成 {SRCINFO.relative_to(ROOT)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="回填 AUR 源码哈希并生成 .SRCINFO")
    parser.add_argument("--version", help="改写 PKGBUILD 的 pkgver（默认沿用现值）")
    parser.add_argument("--tarball", type=Path, help="本地源码包：算它的哈希，不联网")
    parser.add_argument("--check", action="store_true", help="只打印哈希，不改文件")
    parser.add_argument("--srconly", action="store_true", help="只重新生成 .SRCINFO")
    args = parser.parse_args(argv)

    if args.version:
        _set_pkgver(args.version)
        print(f"pkgver = {args.version}")
    version = _pkgver()

    if not args.srconly:
        with tempfile.TemporaryDirectory() as tmp:
            source = args.tarball
            if source is None:
                source = _download(_tarball_url(version), Path(tmp) / "source.tar.gz")
            digest = _sha256(source)

        if args.check:
            print(f"sha256 = {digest}")
            return 0

        _set_checksum(digest)
        print(f"sha256 = {digest}")

    _print_srcinfo()
    if not args.check:
        print("\n下一步：把 packaging/aur/{PKGBUILD,.SRCINFO} 推到 AUR 仓库。")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
