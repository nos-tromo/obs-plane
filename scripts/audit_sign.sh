#!/usr/bin/env bash
# Sign audit exports and anchors (docs/audit.md) with an SSH key:
#   scripts/audit_sign.sh <dir> <file>...
# With AUDIT_SIGNING_KEY set to a private key, writes <file>.sig next to each
# file (ssh-keygen -Y sign, namespace "audit"; an older .sig is replaced).
# Without it, prints the command to sign the file elsewhere. Check with:
#   ssh-keygen -Y verify -f allowed_signers -I <identity> -n audit -s <file>.sig < <file>
set -euo pipefail

dir=$1
shift
for name in "$@"; do
  file="$dir/$name"
  if [[ -n "${AUDIT_SIGNING_KEY:-}" ]]; then
    ssh-keygen -Y sign -f "$AUDIT_SIGNING_KEY" -n audit < "$file" > "$file.sig.tmp"
    mv "$file.sig.tmp" "$file.sig"
    echo ">> signed $file -> $file.sig"
  else
    echo ">> $file is unsigned. Sign it with the key kept off this host:"
    echo "   ssh-keygen -Y sign -f <private key> -n audit < $file > $file.sig"
  fi
done
