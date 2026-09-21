from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
import zipfile
from pathlib import Path

import requests

BASE = "https://assets.kraken.com/marketing/institutions"
FULL_PARTS = [
    f"{BASE}/Kraken_OHLCVT_Full_2026Q2.zip.part{i:02d}" for i in range(5)
]
FULL_SHA256 = "fc81b54cba6e12af3e9422dde9416179e6ef76af4831d48d839fbdb43018eaa4"
Q2 = f"{BASE}/Kraken_OHLCVT_2026Q2.zip"


def download(url: str, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() and dst.stat().st_size > 0:
        print(f"skip existing {dst.name} ({dst.stat().st_size:,} bytes)")
        return
    print(f"download {url}")
    tmp = dst.with_suffix(dst.suffix + ".partial")
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        done = 0
        with tmp.open("wb") as f:
            for chunk in r.iter_content(chunk_size=8 * 1024 * 1024):
                if not chunk:
                    continue
                f.write(chunk)
                done += len(chunk)
                if total:
                    print(f"  {done/total*100:5.1f}%  {done/1024/1024:,.0f} MB", end="\r")
    tmp.replace(dst)
    print(f"  saved {dst} ({dst.stat().st_size/1024/1024:,.1f} MB)")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            b = f.read(8 * 1024 * 1024)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def join_parts(parts: list[Path], dst: Path) -> None:
    print(f"join -> {dst}")
    with dst.open("wb") as out:
        for part in parts:
            with part.open("rb") as src:
                shutil.copyfileobj(src, out, length=16 * 1024 * 1024)


def extract(z: Path, dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    print(f"extract {z.name} -> {dst}")
    with zipfile.ZipFile(z) as arc:
        arc.extractall(dst)


def full_history(root: Path, keep_parts: bool = False) -> None:
    part_dir = root / "parts"
    parts = []
    for url in FULL_PARTS:
        p = part_dir / url.rsplit("/", 1)[-1]
        download(url, p)
        parts.append(p)
    archive = root / "Kraken_OHLCVT_Full_2026Q2.zip"
    if not archive.exists():
        join_parts(parts, archive)
    digest = sha256(archive)
    print(f"sha256={digest}")
    if digest.lower() != FULL_SHA256.lower():
        raise SystemExit("SHA256 mismatch; archive not extracted.")
    # Once the joined archive is verified, the individual parts are redundant.
    # Remove them before extraction to keep peak disk usage materially lower.
    if not keep_parts:
        for p in parts:
            p.unlink(missing_ok=True)
    extract(archive, root / "full")
    print("Full Kraken OHLCVT history ready.")


def q2_update(root: Path) -> None:
    archive = root / "Kraken_OHLCVT_2026Q2.zip"
    download(Q2, archive)
    extract(archive, root / "q2")
    print("Kraken 2026Q2 OHLCVT update ready.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Download official Kraken OHLCVT archives.")
    ap.add_argument("--root", default="data/kraken_history")
    ap.add_argument("--full", action="store_true", help="Download complete multi-part history through 2026-06-30 (~multi-GB).")
    ap.add_argument("--q2", action="store_true", help="Download only the 2026Q2 incremental archive.")
    ap.add_argument("--keep-parts", action="store_true")
    args = ap.parse_args()
    root = Path(args.root)
    if args.full:
        full_history(root, args.keep_parts)
    elif args.q2:
        q2_update(root)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
