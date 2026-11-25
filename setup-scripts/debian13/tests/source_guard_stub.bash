# shellcheck shell=bash
# `ls` stub for lib.sh's source guard (require_root_owned_source and
# _source_guard_check_env_file). Used by the server and worker suites.
#
# The guard reads owner and mode with `ls -lnd -- <path>` and only acts as root.
# A test that simulates root (an `id -u` stub that prints 0) cannot create
# root-owned files, so it prepends this stub to PATH; it answers `ls -lnd`:
#   - paths under SOURCE_GUARD_FIXTURE: the file's real type and permissions,
#     with the suite user's uid changed to 0, unless the path is listed (one per
#     line) in the SOURCE_GUARD_FOREIGN file: there the uid is 1001 (a service
#     user);
#   - any other existing path: owner root and mode 755 (or 644), with the real
#     type. This way tests that do not exercise the guard pass with no setup;
#   - a nonexistent path: fails, like ls.
# Any other ls invocation is delegated to the real ls.
#
# Only the tests use it: lib.sh does not read these variables.

write_ls_stub() {  # $1=stubs directory (prepended to PATH)
    mkdir -p "$1"
    cat > "$1/ls" <<'STUB'
#!/usr/bin/env bash
real_ls=/bin/ls
[ -x "$real_ls" ] || real_ls=/usr/bin/ls
if [ "${1:-}" != "-lnd" ] || [ "${2:-}" != "--" ] || [ "$#" -ne 3 ]; then
    exec "$real_ls" "$@"
fi
p="$3"
line="$("$real_ls" -lnd -- "$p" 2>/dev/null)" || exit 1
read -r perms _ uid _ <<< "$line"
under=0
if [ -n "${SOURCE_GUARD_FIXTURE:-}" ]; then
    case "$p/" in "${SOURCE_GUARD_FIXTURE%/}/"*) under=1 ;; esac
fi
if [ "$under" = 1 ]; then
    perms="${perms:0:11}"
    # macOS marks extended attributes with '@': it is not an ACL.
    [ "${perms:10:1}" = "@" ] && perms="${perms:0:10}"
    [ "$uid" = "$(/usr/bin/id -u)" ] && uid=0
    if [ -n "${SOURCE_GUARD_FOREIGN:-}" ] && [ -f "$SOURCE_GUARD_FOREIGN" ] \
        && grep -qxF -- "$p" "$SOURCE_GUARD_FOREIGN"; then
        uid=1001
    fi
else
    case "${perms:0:1}" in
        d) perms="drwxr-xr-x" ;;
        l) perms="lrwxrwxrwx" ;;
        *) perms="${perms:0:1}rw-r--r--" ;;
    esac
    uid=0
fi
printf '%s 1 %s 0 0 Jan  1 00:00 %s\n' "$perms" "$uid" "$p"
STUB
    chmod +x "$1/ls"
}

# `id` stub that simulates root for `id -u` and delegates the rest.
write_root_id_stub() {  # $1=stubs directory
    mkdir -p "$1"
    cat > "$1/id" <<'STUB'
#!/usr/bin/env bash
if [ "${1:-}" = "-u" ] && [ "$#" -eq 1 ]; then echo 0; else exec /usr/bin/id "$@"; fi
STUB
    chmod +x "$1/id"
}
