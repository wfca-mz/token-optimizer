#!/bin/bash
#
# install-tokopt-launchd.sh - daily 03:30 `tokopt apply --scheduled` via launchd (macOS)
#
# Usage:
#   pipx install .                                   # first: install the package
#   ./scripts/install-tokopt-launchd.sh              # write plist + load
#   ./scripts/install-tokopt-launchd.sh --uninstall
#   ./scripts/install-tokopt-launchd.sh --status
#
# The job runs unattended, so it refuses to change anything until you have run
# `tokopt apply` (or `apply --yes`) once by hand, and again after any policy change.
# It runs the installed `tokopt` command if there is one on PATH; otherwise an
# absolute python >= 3.11 whose `-m tokopt --version` works. launchd gets the program
# and each argument as separate plist strings: no shell is involved.
# Logs: ~/.local/state/tokopt/logs/ (0700). Written for stock macOS bash 3.2.
#
set -euo pipefail

LABEL="com.tokopt.agent"
PLIST_PATH="${HOME}/Library/LaunchAgents/${LABEL}.plist"
LOG_DIR="${HOME}/.local/state/tokopt/logs"

# Print the absolute path of the newest python >= 3.11 on PATH, or fail.
resolve_python() {
  local c p
  for c in python3.13 python3.12 python3.11 python3; do
    p="$(command -v "$c" 2>/dev/null)" || continue
    if "$p" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
      echo "$p"
      return 0
    fi
  done
  echo "tokopt needs Python >= 3.11 (tried python3.13, python3.12, python3.11, python3); install one, e.g. 'brew install python@3.12'" >&2
  return 1
}

# Group name of a path (seam: tests override it).
dir_group() {
  # shellcheck disable=SC2012  # portable (BSD+GNU) way to print a group NAME; the path is never parsed from ls
  ls -ld "$1" | awk '{print $4}'
}

# Owner name of a path, and the current user's name (seams: tests override them).
dir_owner() {
  # shellcheck disable=SC2012  # see dir_group
  ls -ld "$1" | awk '{print $3}'
}

current_user() {
  id -un
}

# Succeed if the current user belongs to group $1 (seam: tests override it).
user_in_group() {
  local g
  for g in $(id -Gn 2>/dev/null); do
    [ "$g" = "$1" ] && return 0
  done
  return 1
}

# Succeed only for a location that is local, private and not writable by others. A directory writable by the
# `admin` group is accepted only when it is owned by the current user, who is a member of admin (macOS/Homebrew
# default: 775 user:admin). `staff` (effectively every local user), other groups, root/other-owned group-writable
# directories, group-writable files and world-writable paths are refused.
check_safe_path() {
  local p="$1" g w gw
  case "$p" in
    *Dropbox*|*/Library/CloudStorage/*|*"/Library/Mobile Documents/"*)
      echo "Refusing: ${p} is in a cloud-synced location; install from a local, private checkout" >&2
      return 1 ;;
  esac
  # fail closed: if find itself errors, the permissions are unknown and the path is refused
  if ! w="$(find "$p" -maxdepth 0 -perm -002 2>/dev/null)" || ! gw="$(find "$p" -maxdepth 0 -perm -020 2>/dev/null)"; then
    echo "Refusing: cannot determine the permissions of ${p}" >&2
    return 1
  fi
  if [ -n "$w" ]; then
    echo "Refusing: ${p} is world-writable (a scheduled job would run code others can modify)" >&2
    return 1
  fi
  if [ -n "$gw" ]; then
    if [ -d "$p" ]; then
      g="$(dir_group "$p")"
      if [ "$g" = "admin" ] && [ "$(dir_owner "$p")" = "$(current_user)" ] && user_in_group admin; then
        return 0
      fi
    fi
    echo "Refusing: ${p} is group-writable (only a directory you own, group admin, that you are a member of, is accepted; a scheduled job would run code others can modify)" >&2
    return 1
  fi
  return 0
}

xml_escape() {
  printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'
}

# Print the physical absolute path of a file: symlinks followed, directories resolved with pwd -P.
# With a second argument ("check"), the physical directory chain of the path as given AND of every symlink hop
# must be safe and owned by you or root (whoever can write a directory holding a link can re-point it).
resolve_exe() {
  local p="$1" d n i=0 t
  while [ -L "$p" ] && [ "$i" -lt 20 ]; do
    if [ -n "${2:-}" ]; then
      d="$(cd "$(dirname "$p")" && pwd -P)" || return 1
      check_dir_chain "$d" || return 1
    fi
    t="$(readlink "$p")"
    case "$t" in /*) p="$t" ;; *) p="$(dirname "$p")/$t" ;; esac
    i=$((i + 1))
  done
  [ -L "$p" ] && return 1  # still a link after 20 hops: a loop or a chain too long to vouch for
  d="$(cd "$(dirname "$p")" && pwd -P)" || return 1
  if [ -n "${2:-}" ]; then
    check_dir_chain "$d" || return 1
  fi
  n="$(basename "$p")"
  if [ "$d" = "/" ]; then printf '/%s\n' "$n"; else printf '%s/%s\n' "$d" "$n"; fi
}

# Succeed only if $1 is owned by the current user or root (nobody else may have created or replaced it).
check_owner() {
  local p="$1" uid
  uid="$(id -u)"
  if [ -n "$(find "$p" -maxdepth 0 \( -user "$uid" -o -user 0 \) 2>/dev/null)" ]; then
    return 0
  fi
  echo "Refusing: ${p} is not owned by you (uid ${uid}) or root" >&2
  return 1
}

# The resolved program file and every directory above it must be safe and owned by you or root.
validate_real() {
  local real="$1"
  check_safe_path "$real" || return 1
  check_owner "$real" || return 1
  check_dir_chain "$(dirname "$real")"
}

# The program a scheduled job will run must be an absolute, existing file that only we (or root) can change,
# reached through directories only we (or root) can change: the given path, every symlink hop, the target.
validate_exe() {
  local given="$1" real
  case "$given" in
    /*) ;;
    *) echo "Refusing: '${given}' is not an absolute path" >&2; return 1 ;;
  esac
  if [ ! -e "$given" ]; then
    echo "Refusing: ${given} does not exist" >&2
    return 1
  fi
  real="$(resolve_exe "$given" check)" || { echo "Refusing: cannot resolve ${given} through safe directories" >&2; return 1; }
  validate_real "$real"
}

# Every directory from $1 up to / must be safe and owned by you or root.
check_dir_chain() {
  local d="$1"
  while :; do
    check_safe_path "$d" || return 1
    check_owner "$d" || return 1
    [ "$d" = "/" ] && break
    d="$(dirname "$d")"
  done
  return 0
}

# Validate a program and its shebang interpreter and print its RESOLVED real path. The path is resolved ONCE
# (hops checked on the way) and everything after works on that result, so nothing is re-resolved between check and use.
pinned_exe() {
  local given="$1" real
  case "$given" in
    /*) ;;
    *) echo "Refusing: '${given}' is not an absolute path" >&2; return 1 ;;
  esac
  [ -e "$given" ] || { echo "Refusing: ${given} does not exist" >&2; return 1; }
  real="$(resolve_exe "$given" check)" || { echo "Refusing: cannot resolve ${given} through safe directories" >&2; return 1; }
  validate_real "$real" || return 1
  validate_shebang "$real" || return 1
  printf '%s\n' "$real"
}

# A script is only as trustworthy as its interpreter: validate the shebang interpreter exactly like the program.
# `#!/usr/bin/env X` resolves through PATH at run time, so it is refused. Non-scripts (binaries) pass.
validate_shebang() {
  local f="$1" line interp wrap
  [ "$(head -c 2 "$f" 2>/dev/null)" = "#!" ] || return 0
  line="$(head -n 1 "$f")"
  line="${line#\#!}"
  line="${line# }"
  interp="${line%% *}"
  case "$interp" in
    */env)
      echo "Refusing: ${f} starts with '#!${line}': the interpreter would be found through PATH; reinstall with pipx" >&2
      return 1 ;;
  esac
  # pip console scripts: `#!/bin/sh` then a second line  '''exec' "/path/to/python" "$0" "$@"
  # (a shell that re-execs the quoted interpreter). That interpreter is what really runs: validate it, or refuse.
  case "$interp" in
    */sh|*/bash)
      line="$(sed -n 2p "$f")"
      wrap="'''exec'"
      case "$line" in
        "${wrap} \"/"*)
          line="${line#*\"}"
          validate_exe "${line%%\"*}" || return 1 ;;
        "${wrap}"*)
          echo "Refusing: ${f} re-execs an interpreter I cannot parse from its second line" >&2
          return 1 ;;
      esac ;;
  esac
  validate_exe "$interp"
}

# Agent config locations the job must see exactly as the installing shell did (launchd has a bare environment).
extra_env_xml() {
  if [ -n "${CLAUDE_CONFIG_DIR:-}" ]; then
    printf '      <key>CLAUDE_CONFIG_DIR</key>\n      <string>%s</string>\n' "$(xml_escape "${CLAUDE_CONFIG_DIR}")"
  fi
  if [ -n "${CODEX_HOME:-}" ]; then
    printf '      <key>CODEX_HOME</key>\n      <string>%s</string>\n' "$(xml_escape "${CODEX_HOME}")"
  fi
}

# Logs can hold file paths and agent names: owner-only, whether or not the directory already existed.
prepare_log_dir() {
  # shellcheck disable=SC2174
  mkdir -p -m 700 "${LOG_DIR}"
  chmod 700 "${LOG_DIR}" "$(dirname "${LOG_DIR}")"
}

main() {
  local script_dir repo_root exe py args_xml a t
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  repo_root="${TOKOPT_REPO_ROOT:-$(cd "${script_dir}/.." && pwd)}"

  case "${1:-}" in
    --status|-s)
      launchctl list "${LABEL}" 2>/dev/null || echo "Not loaded: ${LABEL}"
      # one plist parser (plistlib, inside tokopt) so XML entities decode; best effort, never fatal
      if [ -f "${PLIST_PATH}" ]; then
        if t="$(command -v tokopt 2>/dev/null)"; then
          "${t}" --check-job || true
        elif py="$(resolve_python 2>/dev/null)"; then
          "${py}" -I -m tokopt --check-job || true
        fi
      fi
      find "${LOG_DIR}" -maxdepth 1 -type f 2>/dev/null | head -3 || true
      return 0 ;;
    --uninstall|-u)
      launchctl unload "${PLIST_PATH}" 2>/dev/null || true
      rm -f "${PLIST_PATH}"
      echo "Removed ${LABEL}"
      return 0 ;;
    "" ) ;;
    *) echo "Usage: $0 [--uninstall|--status]"; return 2 ;;
  esac

  case "${repo_root}" in
    */.claude/worktrees/*) echo "Refusing: ${repo_root} is a worktree; set TOKOPT_REPO_ROOT to the canonical checkout" >&2; return 1 ;;
  esac
  check_safe_path "${repo_root}" || return 1

  if exe="$(command -v tokopt 2>/dev/null)"; then
    exe="$(pinned_exe "${exe}")" || return 1
    "${exe}" --version >/dev/null || { echo "'${exe} --version' failed" >&2; return 1; }
    set -- "${exe}" apply --scheduled
  else
    py="${TOKOPT_PYTHON:-$(resolve_python)}" || return 1
    py="$(pinned_exe "${py}")" || return 1
    "${py}" -I -m tokopt --version >/dev/null 2>&1 || {
      echo "'${py} -I -m tokopt --version' failed: install the package first (pipx install .)" >&2
      return 1
    }
    set -- "${py}" -I -m tokopt apply --scheduled
  fi

  prepare_log_dir
  mkdir -p "$(dirname "${PLIST_PATH}")"
  launchctl unload "${PLIST_PATH}" 2>/dev/null || true

  args_xml=""
  for a in "$@"; do
    args_xml="${args_xml}      <string>$(xml_escape "${a}")</string>
"
  done

  cat > "${PLIST_PATH}" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key>
    <string>${LABEL}</string>
    <key>ProgramArguments</key>
    <array>
${args_xml}    </array>
    <key>StartCalendarInterval</key>
    <dict>
      <key>Hour</key>
      <integer>3</integer>
      <key>Minute</key>
      <integer>30</integer>
    </dict>
    <key>StandardOutPath</key>
    <string>$(xml_escape "${LOG_DIR}")/tokopt.out.log</string>
    <key>StandardErrorPath</key>
    <string>$(xml_escape "${LOG_DIR}")/tokopt.err.log</string>
    <key>EnvironmentVariables</key>
    <dict>
      <key>PATH</key>
      <string>/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin</string>
      <key>HOME</key>
      <string>$(xml_escape "${HOME}")</string>
$(extra_env_xml)
    </dict>
  </dict>
</plist>
PLIST

  launchctl load "${PLIST_PATH}"
  echo "Installed ${LABEL}: daily 03:30 -> $* (logs ${LOG_DIR})"
}

# Run only when executed; sourcing (tests) just defines the functions.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main "$@"
fi
