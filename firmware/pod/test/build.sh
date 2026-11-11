#!/usr/bin/env bash
# Build and run podcrp's host test on the PC (MinGW gcc): the real CRP engine (../src/crp) and
# ../podcrp.c with fake hooks, plus podcrp.h from C++. Arduino never compiles this folder.
#   bash firmware/pod/test/build.sh
# analysis/crp.py --vectors go in too: the pod's bytes against the Python
# reference (P1). PYTHON overrides the venv Python. Exit 0 only if every check passed.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
pod="$here/.."
repo="$(cd "$here/../../.." && pwd)"
out="$here/build"
mkdir -p "$out"

cflags=(-std=gnu11 -Wall -Wextra -Werror -g -O1 -I"$pod")
engine=("$pod/src/crp/crp_engine.c" "$pod/src/crp/crp_packet.c" "$pod/src/crp/crp_region.c" "$pod/src/crp/crp_channel.c")

gcc "${cflags[@]}" -o "$out/host_test" "$here/host_test.c" "$pod/podcrp.c" "${engine[@]}"

# The C++ check: podcrp and the engine as C objects, the check as C++, then link
objs=()
for src in "$pod/podcrp.c" "${engine[@]}"; do
  obj="$out/$(basename "${src%.c}").o"
  gcc "${cflags[@]}" -c "$src" -o "$obj"
  objs+=("$obj")
done
g++ -std=gnu++11 -Wall -Wextra -Werror -g -I"$pod" -o "$out/cpp_check" "$here/cpp_check.cpp" "${objs[@]}"

rc=0
vec=()
py="${PYTHON:-$repo/analysis/.venv/Scripts/python.exe}"
if "$py" "$repo/analysis/crp.py" --vectors > "$out/vectors.json"; then
  vec=("$out/vectors.json")
else
  echo "FAIL  analysis/crp.py --vectors didn't run"
  rc=1
fi
"$out/host_test" ${vec[@]+"${vec[@]}"} || rc=1
"$out/cpp_check" || rc=1
exit $rc
