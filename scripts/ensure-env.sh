#!/bin/sh
# Create or backfill <project>/.env from <project>/.env.example. Used by `make up P=NN`. POSIX sh
# (macOS ships bash 3.2), no arrays.
#   - No .env yet: copy .env.example, replacing every `KEY=__GENERATE__` line with a fresh random secret.
#   - .env exists: append any `KEY=__GENERATE__` key from .env.example that .env lacks, with a fresh
#     random value, and regenerate any line in .env that is still the literal placeholder or empty.
# If openssl fails, the script stops before writing anything for that key (never an empty secret).
# Existing values are never changed or printed; only key names are reported. Owner-only permissions.
set -eu

dir=${1:?usage: scripts/ensure-env.sh <project-dir>}
example="$dir/.env.example"
env_file="$dir/.env"
[ -f "$example" ] || exit 0
umask 077

rand() {
  r=$(openssl rand -hex 24) || { echo "openssl failed" >&2; exit 1; }
  [ "${#r}" -ge 32 ] || { echo "openssl returned a short value" >&2; exit 1; }
  printf '%s' "$r"
}

keys=$(sed -n 's/^\([A-Z0-9_][A-Z0-9_]*\)=__GENERATE__$/\1/p' "$example")
tmp="$env_file.tmp"
trap 'rm -f "$tmp" "$tmp.2"' EXIT

if [ ! -f "$env_file" ]; then
  cp "$example" "$tmp"
  for key in $keys; do
    value=$(rand)
    awk -v k="$key" -v v="$value" '$0 == k "=__GENERATE__" { print k "=" v; next } { print }' "$tmp" > "$tmp.2"
    mv "$tmp.2" "$tmp"
  done
  mv "$tmp" "$env_file"
  echo "created $env_file from .env.example (random values for: $(echo $keys | tr ' ' ','))"
  exit 0
fi

changed=""
for key in $keys; do
  if grep -q "^${key}=__GENERATE__\$" "$env_file" || grep -q "^${key}=\$" "$env_file"; then
    # Still the literal placeholder (e.g. .env was made with cp) or empty: replace just that line.
    value=$(rand)
    awk -v k="$key" -v v="$value" '$0 == k "=__GENERATE__" || $0 == k "=" { print k "=" v; next } { print }' \
      "$env_file" > "$tmp"
    mv "$tmp" "$env_file"
    changed="$changed $key(regenerated)"
  elif ! grep -q "^${key}=" "$env_file"; then
    value=$(rand)                     # assigned first so a failing openssl stops the script (set -e)
    if [ -s "$env_file" ] && [ "$(tail -c1 "$env_file" | od -An -c | tr -d ' ')" != '\n' ]; then
      printf '\n' >> "$env_file"
    fi
    printf '%s=%s\n' "$key" "$value" >> "$env_file"
    changed="$changed $key"
  fi
done
chmod 600 "$env_file"
[ -z "$changed" ] || echo "updated $env_file: generated$changed"
