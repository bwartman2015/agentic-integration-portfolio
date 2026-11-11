"""Download a model into the `models` volume, verified by SHA-256 (`make models P=01 [MODEL=chat]`).

  embeddings  nomic-embed-text-v1.5, Q8_0 GGUF (146 MB, Apache-2.0): long-term memory (Milestone 6)
  chat        Qwen2.5-1.5B-Instruct, Q4_K_M GGUF (1.1 GB, Apache-2.0): the free local model (Milestone 7)

Each file comes from a pinned revision of its publisher's Hugging Face repository. It is written as
<name>.part and renamed only after its size and checksum match, so llama.cpp never loads a partial or
altered file. Already present and matching: nothing to do.
"""
from __future__ import annotations

import hashlib
import os
import pathlib
import sys
import urllib.request
from dataclasses import dataclass


@dataclass(frozen=True)
class Model:
    name: str
    url: str
    sha256: str
    size: int


def _hf(repo: str, revision: str, name: str) -> str:
    return f"https://huggingface.co/{repo}/resolve/{revision}/{name}"


MODELS = {
    "embeddings": Model("nomic-embed-text-v1.5.Q8_0.gguf",
                        _hf("nomic-ai/nomic-embed-text-v1.5-GGUF", "0188c9bf409793f810680a5a431e7b899c46104c",
                            "nomic-embed-text-v1.5.Q8_0.gguf"),
                        "3e24342164b3d94991ba9692fdc0dd08e3fd7362e0aacc396a9a5c54a544c3b7", 146146432),
    "chat": Model("qwen2.5-1.5b-instruct-q4_k_m.gguf",
                  _hf("Qwen/Qwen2.5-1.5B-Instruct-GGUF", "91cad51170dc346986eccefdc2dd33a9da36ead9",
                      "qwen2.5-1.5b-instruct-q4_k_m.gguf"),
                  "6a1a2eb6d15622bf3c96857206351ba97e1af16c30d7a74ee38970e434e9407e", 1117320736),
}


def sha256_of(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def fetch(model: Model, directory: pathlib.Path) -> int:
    target = directory / model.name
    if target.is_file() and target.stat().st_size == model.size and sha256_of(target) == model.sha256:
        print(f"{model.name}: present, checksum ok")
        return 0
    part = target.with_suffix(".part")
    print(f"downloading {model.name} ({model.size // 1_000_000} MB) ...", flush=True)
    digest, done = hashlib.sha256(), 0
    try:
        with urllib.request.urlopen(model.url, timeout=60) as resp, part.open("wb") as out:
            for block in iter(lambda: resp.read(1 << 20), b""):
                out.write(block)
                digest.update(block)
                done += len(block)
                if done > model.size:
                    break
    except OSError as exc:
        part.unlink(missing_ok=True)
        print(f"download failed ({type(exc).__name__}); run `make models P=01` again", file=sys.stderr)
        return 1
    if done != model.size or digest.hexdigest() != model.sha256:
        part.unlink(missing_ok=True)
        print(f"{model.name}: size or SHA-256 does not match the pinned value; file discarded", file=sys.stderr)
        return 1
    part.replace(target)
    print(f"{model.name}: downloaded, checksum ok")
    return 0


def main(argv: list[str] | None = None) -> int:
    names = (argv if argv is not None else sys.argv[1:]) or ["embeddings"]
    unknown = [n for n in names if n not in MODELS]
    if unknown:
        print(f"unknown model {', '.join(unknown)}; choose from {', '.join(MODELS)}", file=sys.stderr)
        return 2
    directory = pathlib.Path(os.environ.get("MODELS_DIR", "/models"))
    return max(fetch(MODELS[n], directory) for n in names)


if __name__ == "__main__":
    raise SystemExit(main())
