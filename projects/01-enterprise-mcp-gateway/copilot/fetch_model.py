"""Download the embedding model into the `models` volume, verified by SHA-256 (`make models P=01`).

nomic-embed-text-v1.5, Q8_0 GGUF (146 MB, Apache-2.0), from a pinned revision of nomic-ai's Hugging Face
repository. The file is written as <name>.part and renamed only after its checksum matches, so the
embeddings server never loads a partial or altered file. Already present and matching: nothing to do.
"""
from __future__ import annotations

import hashlib
import os
import pathlib
import sys
import urllib.request

NAME = "nomic-embed-text-v1.5.Q8_0.gguf"
REVISION = "0188c9bf409793f810680a5a431e7b899c46104c"
URL = f"https://huggingface.co/nomic-ai/nomic-embed-text-v1.5-GGUF/resolve/{REVISION}/{NAME}"
SHA256 = "3e24342164b3d94991ba9692fdc0dd08e3fd7362e0aacc396a9a5c54a544c3b7"
SIZE = 146146432


def sha256_of(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    target = pathlib.Path(os.environ.get("MODELS_DIR", "/models")) / NAME
    if target.is_file() and target.stat().st_size == SIZE and sha256_of(target) == SHA256:
        print(f"{NAME}: present, checksum ok")
        return 0
    part = target.with_suffix(".part")
    print(f"downloading {NAME} ({SIZE // 1_000_000} MB) ...", flush=True)
    digest, done = hashlib.sha256(), 0
    try:
        with urllib.request.urlopen(URL, timeout=60) as resp, part.open("wb") as out:
            for block in iter(lambda: resp.read(1 << 20), b""):
                out.write(block)
                digest.update(block)
                done += len(block)
                if done > SIZE:
                    break
    except OSError as exc:
        part.unlink(missing_ok=True)
        print(f"download failed ({type(exc).__name__}); run `make models P=01` again", file=sys.stderr)
        return 1
    if done != SIZE or digest.hexdigest() != SHA256:
        part.unlink(missing_ok=True)
        print(f"{NAME}: size or SHA-256 does not match the pinned value; file discarded", file=sys.stderr)
        return 1
    part.replace(target)
    print(f"{NAME}: downloaded, checksum ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
