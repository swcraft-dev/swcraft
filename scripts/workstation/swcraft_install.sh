#!/bin/sh
# Install the SWCraft toolchain from this unpacked archive.
#
# The archive carries its own Python; this file gets install.py one to run on.
# First, where `xattr` exists, it refuses a folder still carrying macOS's
# download quarantine: macOS silently kills the unsigned Python (exit 137).
# Then it checks runtime/python.tar.gz against SHA256SUMS, unpacks it into
# .bootstrap/python (dropping the vendor's leading directory) and runs
# install.py on it with every argument; install.py removes .bootstrap last.
# "preparing the installer ... " is closed by "done", or by "failed" before the
# refusal on stderr, so the refusal stays the last line.
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
member=runtime/python.tar.gz
python="$here/.bootstrap/python/bin/python3"
printf '%s' "preparing the installer ... "
if command -v xattr >/dev/null 2>&1; then
    for marked in "$here/swcraft_install.sh" "$here/$member"; do
        if [ -e "$marked" ] && xattr -p com.apple.quarantine "$marked" >/dev/null 2>&1; then
            echo failed
            echo "this folder still carries macOS's download quarantine, which stops the Python it holds from running, so nothing was installed: run   xattr -dr com.apple.quarantine \"$here\"   and run this again" >&2
            exit 1
        fi
    done
fi
expected=$(awk -v name="$member" '$2 == name { print $1 }' "$here/SHA256SUMS" 2>/dev/null || true)
actual=""
if [ -f "$here/$member" ]; then
    if command -v shasum >/dev/null 2>&1; then
        actual=$(shasum -a 256 "$here/$member" | awk '{ print $1 }')
    elif command -v sha256sum >/dev/null 2>&1; then
        actual=$(sha256sum "$here/$member" | awk '{ print $1 }')
    else
        # Otherwise an empty digest would blame the download, which a new download cannot fix.
        echo failed
        echo "neither shasum nor sha256sum was found on PATH, so $member could not be checked and nothing was installed: install one of the two and run this again" >&2
        exit 1
    fi
fi
if [ -z "$expected" ] || [ "$actual" != "$expected" ]; then
    echo failed
    echo "$member does not match its line in SHA256SUMS, so nothing was installed: download the archive again" >&2
    exit 1
fi
if ! { rm -rf "$here/.bootstrap/python" && mkdir -p "$here/.bootstrap/python"; }; then
    echo failed
    echo ".bootstrap/python could not be made beside this file, so nothing was installed: unpack the archive into a directory you can write to" >&2
    exit 1
fi
if ! tar -xzf "$here/$member" -C "$here/.bootstrap/python" --strip-components 1; then
    echo failed
    echo "$member could not be unpacked into .bootstrap/python, so nothing was installed: unpack the archive again" >&2
    exit 1
fi
echo done
exec "$python" "$here/install.py" "$@"
