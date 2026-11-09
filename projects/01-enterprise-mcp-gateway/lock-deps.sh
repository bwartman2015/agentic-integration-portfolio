#!/usr/bin/env bash
# Regenerate <component>/requirements.lock: exact versions + sha256 hashes for linux amd64 and arm64 wheels.
# Resolves inside the same digest-pinned base image the component's Dockerfile uses, so the lock
# matches the runtime.
# Usage (from the project root): ./lock-deps.sh gateway   |   ./lock-deps.sh mock_apis
set -euo pipefail
cd "$(dirname "$0")"

COMPONENT=${1:?usage: ./lock-deps.sh <gateway|mock_apis>}
test -f "$COMPONENT/Dockerfile" && test -f "$COMPONENT/pyproject.toml" || { echo "no Dockerfile/pyproject.toml in $COMPONENT"; exit 1; }

BASE_IMAGE=$(sed -n 's/^FROM \(python:[^ ]*\) AS base$/\1/p' "$COMPONENT/Dockerfile")
TOP_LEVEL=$(sed -n 's/^ *"\([A-Za-z0-9_.-]*==[^"]*\)".*/\1/p' "$COMPONENT/pyproject.toml" | tr '\n' ' ')
test -n "$BASE_IMAGE" && test -n "$TOP_LEVEL" || { echo "could not read base image or pins"; exit 1; }
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

docker run --rm -v "$WORK:/out" "$BASE_IMAGE" sh -c "
set -e
pip install -q --disable-pip-version-check --root-user-action=ignore $TOP_LEVEL
pip freeze --exclude pip > /out/versions.txt
for arch in x86_64 aarch64; do
  pip download -q --disable-pip-version-check --no-deps --only-binary=:all: --python-version 3.12 \
    --implementation cp --abi cp312 --abi abi3 --abi none \
    --platform manylinux2014_\$arch --platform manylinux_2_17_\$arch --platform manylinux_2_28_\$arch --platform any \
    -r /out/versions.txt -d /out/wheels
done
"

python3 - "$WORK" "$COMPONENT" <<'EOF'
import hashlib, pathlib, re, sys
out, component = pathlib.Path(sys.argv[1]), sys.argv[2]
norm = lambda n: re.sub(r"[-_.]+", "_", n).lower()
wheels: dict[str, list[str]] = {}
for w in sorted((out / "wheels").glob("*.whl")):
    wheels.setdefault(norm(w.name.split("-")[0]), []).append(hashlib.sha256(w.read_bytes()).hexdigest())
lines = [f"# Pinned, hashed dependencies for the {component} image (python:3.12, linux amd64 + arm64).",
         f"# Generated from {component}/pyproject.toml by ./lock-deps.sh {component}; do not edit by hand.", ""]
for spec in sorted(out.joinpath("versions.txt").read_text().split(), key=str.lower):
    name, ver = spec.split("==")
    hashes = wheels.get(norm(name))
    assert hashes, f"no wheel for {name}"
    lines.append(f"{name}=={ver} \\")
    lines += [f"    --hash=sha256:{h}" + (" \\" if i < len(hashes) - 1 else "") for i, h in enumerate(hashes)]
pathlib.Path(component, "requirements.lock").write_text("\n".join(lines) + "\n")
print(f"wrote {component}/requirements.lock: {len(wheels)} packages, {sum(map(len, wheels.values()))} hashes")
EOF
