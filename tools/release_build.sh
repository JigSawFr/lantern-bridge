#!/usr/bin/env bash
# The release's Python files, built reproducibly (RELEASING.md): the same
# commit, SOURCE_DATE_EPOCH and tools give the same bytes, wherever the
# checkout is. The sdist and the wheel of lantern-bridge, and their SBOM
# (CycloneDX): the environment the Dockerfile builds, of the wheel's locked
# dependencies.
#
#   tools/release_build.sh [-o DIR]
#
# Environment:
#   LANTERN_REF   the commit or tag to build (default HEAD)
#   PYTHON        the Python with the release's tools (default python3):
#                 pip install --require-hashes -r tools/release-requirements.txt
#   SOURCE_DATE_EPOCH  default: the commit's time
# uv (the version the CI uses) installs the locked dependencies for the SBOM.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
OUT=dist
while getopts "o:" opt; do
  case "$opt" in
    o) OUT=$OPTARG ;;
    *) sed -n '2,15p' "$0"; exit 2 ;;
  esac
done

REF=${LANTERN_REF:-HEAD}
COMMIT=$(git -C "$ROOT" rev-parse --verify "$REF^{commit}")
export SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH:-$(git -C "$ROOT" log -1 --format=%ct "$COMMIT")}
PYTHON=${PYTHON:-python3}
export TZ=UTC LC_ALL=C
umask 022

"$PYTHON" -c "import build, hatchling, cyclonedx_py" 2>/dev/null || {
  echo "release_build.sh: $PYTHON lacks the release's tools:" \
    "pip install --require-hashes -r tools/release-requirements.txt" >&2
  exit 1
}

mkdir -p "$OUT"
OUT=$(cd "$OUT" && pwd)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
mkdir "$WORK/src"
# The commit's files, and the SDK's folder the lock points at (a submodule: git archive does not enter it)
git -C "$ROOT" archive --format=tar "$COMMIT" | tar -x -C "$WORK/src"
sdk=$(git -C "$ROOT" rev-parse "$COMMIT:sdk")
if [ ! -f "$WORK/src/sdk/python/pyproject.toml" ]; then
  git -C "$ROOT/sdk" archive --format=tar "$sdk" python | tar -x -C "$WORK/src/sdk"
fi

# Without isolation: the build backend is the one installed, pinned by hash
(cd "$WORK/src" && "$PYTHON" -m build --no-isolation --outdir "$WORK/py" . > /dev/null)
cp "$WORK"/py/* "$OUT/"
wheel=$(cd "$WORK/py" && ls -- *.whl)
version=${wheel#lantern_bridge-}
version=${version%%-*}

# The SBOM: the locked, non-development environment, installed as the image's is
UV_PROJECT_ENVIRONMENT="$WORK/env" UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy \
  uv sync --quiet --frozen --no-dev --no-editable --project "$WORK/src"
sbom="$OUT/lantern-bridge-$version.pypi.cdx.json"
"$PYTHON" -m cyclonedx_py environment --pyproject "$WORK/src/pyproject.toml" --mc-type application \
  --output-reproducible --output-format JSON --spec-version 1.6 -o "$sbom" "$WORK/env/bin/python"
# The scratch folder's name is not part of what the release is made of
sed -i "s#file://$WORK/src#file://.#g" "$sbom"

{
  echo "commit: $COMMIT"
  echo "SOURCE_DATE_EPOCH: $SOURCE_DATE_EPOCH"
  "$PYTHON" --version
  "$PYTHON" -m build --version | cut -d' ' -f1-2
  uv --version
} > "$OUT/build-info.txt"
ls -1 "$OUT"
