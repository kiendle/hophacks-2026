"""Download the hackathon datasets from the public S3 bucket (no credentials, no AWS CLI).

Run: python download_data.py [firehose|congress|all]

Each source lands in its own directory. Re-running resumes: complete files are
skipped by size and partial files continue from their last byte.
"""
import shutil
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import quote

BUCKET = "https://calcifer-hot.s3.us-east-2.amazonaws.com"
PREFIX = "hopkins-hackathon-2026/"
NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
ROOT = Path(__file__).resolve().parent
WORKERS = 8
# source -> (local directory, S3 names under PREFIX; a trailing slash means every parquet below it)
SOURCES = {
    "firehose": (ROOT / "twitter-firehose", ["twitter-firehose-last-month/"]),
    "congress": (ROOT / "congress-tweets", [
        "congress-tweets-unified.parquet",
        "roster.json",
        "handles.txt",
        "gov-profiles_current.parquet",
        "gov-bio_history.parquet",
        "gov-follow_edges.parquet",
        "README.md",
    ]),
}


def list_keys(name):
    token = None
    while True:
        url = f"{BUCKET}/?list-type=2&prefix={quote(PREFIX + name)}"
        if token:
            url += f"&continuation-token={quote(token, safe='')}"
        with urllib.request.urlopen(url, timeout=60) as response:
            tree = ET.parse(response)
        for item in tree.iterfind("s3:Contents", NS):
            key = item.findtext("s3:Key", namespaces=NS)
            size = int(item.findtext("s3:Size", namespaces=NS))
            if key.endswith(".parquet") if name.endswith("/") else key == PREFIX + name:
                yield key, size
        token = tree.findtext("s3:NextContinuationToken", namespaces=NS)
        if not token:
            return


def fetch(key, size, dest):
    """Return bytes transferred; 0 when the file was already complete."""
    if dest.exists() and dest.stat().st_size == size:
        return 0
    part = dest.with_name(dest.name + ".part")
    transferred = 0
    for attempt in range(8):
        have = part.stat().st_size if part.exists() else 0
        if have > size:
            part.unlink()
            have = 0
        try:
            if have < size:
                request = urllib.request.Request(f"{BUCKET}/{quote(key)}", headers={"Range": f"bytes={have}-"})
                with urllib.request.urlopen(request, timeout=120) as response, part.open("ab") as out:
                    shutil.copyfileobj(response, out, 1 << 20)
            else:
                part.touch()
            transferred += part.stat().st_size - have
            if part.stat().st_size == size:
                part.replace(dest)
                return transferred
        except OSError as error:
            transferred += (part.stat().st_size if part.exists() else 0) - have
            print(f"  retry {attempt + 1}/8 {dest.name}: {error}", flush=True)
            time.sleep(min(60, 2 ** attempt))
    raise RuntimeError(f"gave up on {key}")


def download(source):
    directory, names = SOURCES[source]
    directory.mkdir(exist_ok=True)
    jobs = [(key, size, directory / key.rsplit("/", 1)[1]) for name in names for key, size in list_keys(name)]
    total = sum(size for _, size, _ in jobs)
    print(f"{source}: {len(jobs)} files, {total / 1e9:.2f} GB -> {directory}", flush=True)
    start = time.monotonic()
    done = moved = 0
    failed = []
    with ThreadPoolExecutor(WORKERS) as pool:
        futures = {pool.submit(fetch, *job): job for job in jobs}
        for future in as_completed(futures):
            key, size, dest = futures[future]
            try:
                moved += future.result()
            except Exception as error:
                failed.append(dest.name)
                print(f"  FAILED {dest.name}: {error}", flush=True)
                continue
            done += size
            rate = moved / 1e6 / max(time.monotonic() - start, 1e-9)
            print(f"  {done / 1e9:7.2f}/{total / 1e9:.2f} GB  {rate:6.1f} MB/s  {dest.name}", flush=True)
    print(f"{source}: finished in {(time.monotonic() - start) / 60:.1f} min, {len(failed)} failed {failed}", flush=True)
    return not failed


if __name__ == "__main__":
    choice = sys.argv[1] if len(sys.argv) > 1 else "all"
    ok = all([download(source) for source in (SOURCES if choice == "all" else [choice])])
    sys.exit(0 if ok else 1)
