# Bundled repo launcher

`repo` is the Android Open Source Project standalone launcher, copied from the
existing verified Debian package. Upstream: https://android.googlesource.com/tools/repo/.
Copyright 2008 Google Inc.; Apache-2.0. Only its Python interpreter shebang is
adjusted to `/usr/bin/env python3`. Its SHA-256 is recorded in toolchain.lock.json.
It does not contain a repository checkout, SDK, firmware, or credentials.
