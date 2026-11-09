#!/bin/sh
# Self-test for scripts/ensure-env.sh on throwaway files (never touches a real .env).
# Run from the repo root: sh projects/01-enterprise-mcp-gateway/tests/ensure_env_selftest.sh
set -eu
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
printf '%s\n' '# a comment mentioning __GENERATE__ stays' 'A_KEY=__GENERATE__' 'PLAIN=keepme' 'B_KEY=__GENERATE__' > "$T/.env.example"

fail() { echo "FAIL: $*"; exit 1; }
len() { sed -n "s/^$1=//p" "$T/.env" | awk '{ print length($0) }'; }

sh scripts/ensure-env.sh "$T" > /dev/null
[ "$(len A_KEY)" = 48 ] || fail "create: A_KEY not generated"
[ "$(len B_KEY)" = 48 ] || fail "create: B_KEY not generated"
[ "$(sed -n 's/^A_KEY=//p' "$T/.env")" != "$(sed -n 's/^B_KEY=//p' "$T/.env")" ] || fail "create: values repeat"
grep -q '^PLAIN=keepme$' "$T/.env" || fail "create: plain line lost"
grep -q '^# a comment mentioning __GENERATE__ stays$' "$T/.env" || fail "create: comment changed"
[ "$(ls -l "$T/.env" | cut -c1-10)" = "-rw-------" ] || fail "create: permissions"

printf 'PLAIN=mine\nA_KEY=__GENERATE__' > "$T/.env"          # placeholder left, B_KEY missing, no final newline
out=$(sh scripts/ensure-env.sh "$T")
[ "$(len A_KEY)" = 48 ] || fail "backfill: placeholder not replaced"
[ "$(len B_KEY)" = 48 ] || fail "backfill: missing key not appended"
grep -q '^PLAIN=mine$' "$T/.env" || fail "backfill: existing value changed"
[ "$(grep -c '' "$T/.env")" = 3 ] || fail "backfill: unexpected line count"
case "$out" in *A_KEY*B_KEY*) ;; *) fail "backfill: report missing key names";; esac
case "$out" in *"$(sed -n 's/^B_KEY=//p' "$T/.env")"*) fail "backfill: printed a secret";; esac

before=$(cat "$T/.env")
[ -z "$(sh scripts/ensure-env.sh "$T")" ] || fail "rerun: should be silent"
[ "$before" = "$(cat "$T/.env")" ] || fail "rerun: file changed"
# A failing openssl must never leave an empty secret behind, and a later run must repair it.
mkdir "$T/bin"
printf '#!/bin/sh\nexit 1\n' > "$T/bin/openssl"
chmod +x "$T/bin/openssl"
printf 'PLAIN=mine\n' > "$T/.env"
if PATH="$T/bin:$PATH" sh scripts/ensure-env.sh "$T" > /dev/null 2>&1; then fail "broken openssl: should fail"; fi
if grep -q '^A_KEY=$' "$T/.env" || grep -q '^B_KEY=$' "$T/.env"; then fail "broken openssl: wrote an empty secret"; fi
printf 'PLAIN=mine\nA_KEY=\n' > "$T/.env"                      # an empty value left by some earlier failure
sh scripts/ensure-env.sh "$T" > /dev/null
[ "$(len A_KEY)" = 48 ] || fail "empty value not regenerated"
[ "$(len B_KEY)" = 48 ] || fail "missing key not added after repair"
echo "ensure-env self-test: OK"
