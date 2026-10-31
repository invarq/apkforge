#!/usr/bin/env bash
# A machine with no Java, to see what apkforge does on a bare Docker image without needing Docker:
# a PATH that holds only basic tools (python3, openssl, tar, ...), an empty HOME and no JAVA_HOME.
#
#   tools/bare_env.sh setup <name>              create the environment and show that no java tool is visible in it
#   tools/bare_env.sh run <name> <command...>   run a command in it, for example
#         tools/bare_env.sh run fresh python3 -m apkforge doctor
#         tools/bare_env.sh run fresh python3 dist/apkforge-1.3.0.pyz build ./myapp
#   tools/bare_env.sh java <name>               where a java tool is found in it (nowhere, until apkforge has downloaded a JDK)
#
# The environments live in $APKFORGE_BARE_DIR (default ${TMPDIR:-/tmp}/apkforge-bare).  HOME is <dir>/<name>/home, so the toolchain apkforge downloads
# (~/.apkforge) stays inside it, and `setup` again is a factory reset.  Proxy and CA variables are passed through when they are set.
set -u
B=${APKFORGE_BARE_DIR:-${TMPDIR:-/tmp}/apkforge-bare}
cmd=${1:-}; name=${2:-}
usage() { sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }
[ -n "$cmd" ] && [ -n "$name" ] || usage
case "$cmd" in
  setup)
    rm -rf "${B:?}/$name"; mkdir -p "$B/$name/bin" "$B/$name/home"
    for t in python3 openssl sh bash env tar gzip unzip zip uname ldd ls cat cp mv rm mkdir rmdir chmod ln sed grep awk sort head tail tr wc dirname basename readlink realpath mktemp date sleep id touch find xargs diff cut uniq tee true false test printf echo pwd stat df du file sha256sum md5sum; do
      p=$(command -v "$t" 2>/dev/null) && [ -n "$p" ] && ln -sf "$p" "$B/$name/bin/$t"
    done
    "$0" java "$name" ;;
  java)
    [ -d "$B/$name/bin" ] || { echo "no such environment: $0 setup $name" >&2; exit 2; }
    env -i PATH="$B/$name/bin" HOME="$B/$name/home" /bin/sh -c 'for t in java javac keytool jarsigner; do printf "%-10s %s\n" $t "$(command -v $t || echo "not found")"; done; echo "JAVA_HOME=[${JAVA_HOME:-}]"' ;;
  run)
    [ -d "$B/$name/bin" ] || { echo "no such environment: run '$0 setup $name' first" >&2; exit 2; }
    shift 2; [ $# -gt 0 ] || usage
    c=$1; shift
    case "$c" in */*) ;; *) [ -x "$B/$name/bin/$c" ] && c="$B/$name/bin/$c" ;; esac
    pass=(); for v in HTTP_PROXY HTTPS_PROXY NO_PROXY http_proxy https_proxy no_proxy SSL_CERT_FILE REQUESTS_CA_BUNDLE; do [ -n "${!v:-}" ] && pass+=("$v=${!v}"); done
    exec env -i PATH="$B/$name/bin" HOME="$B/$name/home" LANG=C.UTF-8 PYTHONDONTWRITEBYTECODE=1 ${pass[@]+"${pass[@]}"} "$c" "$@" ;;
  *) usage ;;
esac
