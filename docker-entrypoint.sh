#!/bin/sh
# Prepare the volume, install the rmapi token from a variable on first boot, start the app.
set -eu

DATA=/data
if [ ! -d "$DATA" ] || ! touch "$DATA/.write-test" 2>/dev/null; then
  echo "No writable volume at $DATA. Attach a Railway volume mounted at /data (see DEPLOY.md)." >&2
  exit 1
fi
rm -f "$DATA/.write-test"
mkdir -p "$DATA/secrets" "$DATA/cache" "$DATA/out" "$DATA/home"
chmod 700 "$DATA/secrets"

# rmapi refreshes its token in place, so the volume's copy wins once it exists.
TOKEN="$DATA/secrets/rmapi.conf"
if [ ! -s "$TOKEN" ] && [ -n "${RMAPI_TOKEN_B64:-}" ]; then
  echo "$RMAPI_TOKEN_B64" | base64 -d > "$TOKEN"
  chmod 600 "$TOKEN"
  echo "Installed the rmapi token from RMAPI_TOKEN_B64."
fi
[ -s "$TOKEN" ] || echo "Warning: no rmapi token yet. Set RMAPI_TOKEN_B64, or run 'rmtasks auth' in a Railway shell." >&2

for v in RMTASKS_PASSWORD ANTHROPIC_API_KEY TYPESAFE_API_KEY; do
  eval "val=\${$v:-}"
  [ -n "$val" ] || echo "Warning: $v is not set." >&2
done

if [ -n "${RMTASKS_NOTEBOOK:-}" ]; then
  exec rmtasks --notebook "$RMTASKS_NOTEBOOK" serve --host 0.0.0.0
fi
exec rmtasks serve --host 0.0.0.0
