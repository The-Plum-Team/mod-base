#!/usr/bin/env bash
# kit-digest-v1 (SPEC §1.2 step 4): the tree digest every callee prologue recomputes over the pinned
# kit checkout before any kit code runs, compared with the MB_KIT_TREE_DIGEST literal compiled into
# the callee YAML.
#
# The listing is the `sha256sum` output ("<sha256>  ./<path>") of every regular file under src/,
# site/ and requirements/, sorted bytewise by path (LC_ALL=C); the digest is "sha256:" followed by
# the SHA-256 of that listing. The tree is refused when it holds a symlink, a special file, an
# executable file, a __pycache__ entry or a name outside [A-Za-z0-9._-].
#
# The callee prologues inline the function between the markers byte for byte
# (tests/test_tree_digest_literal.py). mod_base.pin.kit_tree_digest and
# tools/update_tree_digest.py compute the same value in Python; the tests pin the parity.
#
# Usage: bash tools/kit_digest.sh KIT_ROOT        prints sha256:<64 hex>
set -euo pipefail

# >>> kit-digest-v1
kit_digest_v1() {
  local root="$1" directory offending listing
  for directory in src site requirements; do
    if [[ ! -d "$root/$directory" || -L "$root/$directory" ]]; then
      printf 'kit-digest-v1: %s/ is not a real directory\n' "$directory" >&2
      return 1
    fi
  done
  if ! offending="$(CDPATH='' cd -- "$root" && LC_ALL=C find ./src ./site ./requirements \( -type l \
      -o \( ! -type f ! -type d \) -o \( -type f \( -perm -100 -o -perm -010 -o -perm -001 \) \) \
      -o -name __pycache__ -o -name '*[!A-Za-z0-9._-]*' \) -print -quit)" || [[ -n "$offending" ]]; then
    printf 'kit-digest-v1: refusing %q (symlink, special file, executable, __pycache__ or unsafe name)\n' \
      "$offending" >&2
    return 1
  fi
  if ! listing="$(CDPATH='' cd -- "$root" && LC_ALL=C find ./src ./site ./requirements -type f -print0 \
      | LC_ALL=C sort -z | while IFS= read -r -d '' path; do
          [[ "$path" =~ ^\./[A-Za-z0-9._/-]+$ ]] || exit 1
          sha256sum -- "$path" || exit 1
        done)" || [[ -z "$listing" ]]; then
    printf 'kit-digest-v1: cannot list the kit tree\n' >&2
    return 1
  fi
  printf 'sha256:%s\n' "$(printf '%s\n' "$listing" | sha256sum | cut -d ' ' -f 1)"
}
# <<< kit-digest-v1

if (( $# != 1 )); then
  printf 'usage: kit_digest.sh KIT_ROOT\n' >&2
  exit 2
fi
kit_digest_v1 "$1"
