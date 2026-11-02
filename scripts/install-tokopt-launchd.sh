#!/bin/bash
# Checkout entry point: the installer lives in the package (tokopt/install-tokopt-launchd.sh) so pipx installs ship it.
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export TOKOPT_REPO_ROOT="${TOKOPT_REPO_ROOT:-$(cd "${here}/.." && pwd)}"
exec /bin/bash "${here}/../tokopt/install-tokopt-launchd.sh" "$@"
