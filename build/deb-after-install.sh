#!/bin/bash
# Custom deb postinst. electron-builder's `afterInstall` REPLACES the default
# postinst, so we replicate its useful parts (the /usr/bin symlink + mime/
# desktop registration) and then FORCE the SUID chrome-sandbox.
#
# Why force it: electron-builder's default postinst picks SUID (4755) vs plain
# (0755) chrome-sandbox by testing `unshare --user`, but that test runs as root
# during install — so on systems where *unprivileged* user namespaces are
# restricted (Ubuntu 24.04+ with apparmor_restrict_unprivileged_userns=1) it
# guesses wrong and the app can't start without --no-sandbox. The SUID sandbox
# doesn't depend on user namespaces, so forcing it makes the app run sandboxed
# everywhere with no flag.

if type update-alternatives 2>/dev/null >&1; then
    if [ -L '/usr/bin/vision-studio' -a -e '/usr/bin/vision-studio' -a "`readlink '/usr/bin/vision-studio'`" != '/etc/alternatives/vision-studio' ]; then
        rm -f '/usr/bin/vision-studio'
    fi
    update-alternatives --install '/usr/bin/vision-studio' 'vision-studio' '/opt/Vision Studio/vision-studio' 100 || ln -sf '/opt/Vision Studio/vision-studio' '/usr/bin/vision-studio'
else
    ln -sf '/opt/Vision Studio/vision-studio' '/usr/bin/vision-studio'
fi

# Force the SUID chrome-sandbox (does not rely on user namespaces).
chmod 4755 '/opt/Vision Studio/chrome-sandbox' 2>/dev/null || true

if hash update-mime-database 2>/dev/null; then
    update-mime-database /usr/share/mime || true
fi

if hash update-desktop-database 2>/dev/null; then
    update-desktop-database /usr/share/applications || true
fi
