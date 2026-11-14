#!/bin/sh
# Install the pinned official Go toolchain into /usr/local/go for the Work image.
# The archive comes from go.dev and must match the pinned SHA-256 (published at
# https://go.dev/dl/); any mismatch fails the image build.
#
# Work commands run offline on a read-only root, so the toolchain is configured in
# $GOROOT/go.env (the lowest-precedence Go configuration, read whatever the command
# environment is):
#   GOTOOLCHAIN=local  never download another toolchain because of a go.mod directive;
#   GOPROXY=off        fail at once with "module lookup disabled" instead of waiting on
#                      a network that does not exist (standard-library code needs none);
#   GOSUMDB=off        no checksum database lookups for the same reason;
#   GOFLAGS=-buildvcs=false  do not shell out to git to stamp binaries: the workspace
#                      is a git repository whose ownership git may refuse to trust.
# Caches follow HOME (GOCACHE=$HOME/.cache/go-build, GOPATH=$HOME/go), which every Work
# container points at a writable directory.
set -eu

GO_VERSION=1.27.4
case "$(dpkg --print-architecture)" in
  amd64) GO_SHA256=7226736c2339719cda28fbf3a54d71aa63bcefe376e98cef00034268dab24840 ;;
  arm64) GO_SHA256=3392582988a273a87ab5043ee1b2c9c17a2c3c00580bf9326c47a2e0aaaeac92 ;;
  *) echo "unsupported architecture: $(dpkg --print-architecture)" >&2; exit 1 ;;
esac
archive="go${GO_VERSION}.linux-$(dpkg --print-architecture).tar.gz"

cd /tmp
curl -fsSL --proto '=https' --tlsv1.2 -o "$archive" "https://dl.google.com/go/$archive"
echo "$GO_SHA256  $archive" | sha256sum -c -
rm -rf /usr/local/go
tar -C /usr/local -xzf "$archive"
rm -f "$archive"
# Not needed to build or test programs: the toolchain's own test suite, API lists,
# documentation and editor/misc files.
rm -rf /usr/local/go/test /usr/local/go/api /usr/local/go/doc /usr/local/go/misc
# go.env already sets GOPROXY, GOSUMDB and GOTOOLCHAIN, and the first occurrence of a
# key wins: drop those lines before appending the sandbox values.
sed -i -E '/^(GOTOOLCHAIN|GOPROXY|GOSUMDB|GOFLAGS)=/d' /usr/local/go/go.env
cat >> /usr/local/go/go.env <<'ENV'

# Atlas Work: offline sandbox defaults (see infra/docker/install-go.sh).
GOTOOLCHAIN=local
GOPROXY=off
GOSUMDB=off
GOFLAGS=-buildvcs=false
ENV
# Work commands run with PATH=/usr/local/bin:/usr/bin:/bin.
ln -sf /usr/local/go/bin/go /usr/local/bin/go
ln -sf /usr/local/go/bin/gofmt /usr/local/bin/gofmt
test "$(go env GOTOOLCHAIN GOPROXY GOSUMDB GOFLAGS | tr '\n' ' ')" = "local off off -buildvcs=false "
go version
