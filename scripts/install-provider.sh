#!/usr/bin/env bash
# Install a released FDP OpenTofu provider into a local filesystem mirror, after verifying it.
#
#   scripts/install-provider.sh VERSION MIRROR_DIR [OS_ARCH]
#   scripts/install-provider.sh 0.3.0 "$HOME/.fdp-providers"            # this machine
#   scripts/install-provider.sh 0.3.0 "$HOME/.fdp-providers" linux_amd64
#
# Downloads the release from GitHub, checks the GPG signature of its SHA256SUMS against the key in this
# checkout (terraform/provider-fdp/signing-key.asc) and the archive's checksum, then places the archive
# where OpenTofu's filesystem_mirror finds it. Uses a throwaway keyring; your GnuPG setup is not touched.
set -euo pipefail

usage="usage: $0 VERSION MIRROR_DIR [OS_ARCH]"
version=${1:?$usage}
version=${version#v}
mirror=${2:?$usage}
os=$(uname -s | tr '[:upper:]' '[:lower:]')
arch=$(uname -m | sed -e 's/^x86_64$/amd64/' -e 's/^aarch64$/arm64/')
platform=${3:-${os}_${arch}}

key="$(cd "$(dirname "$0")/.." && pwd)/terraform/provider-fdp/signing-key.asc"
name="terraform-provider-fdp_${version}"
archive="${name}_${platform}.zip"
base="https://github.com/Floren-App/terraform-provider-fdp/releases/download/v${version}"

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

for file in "${name}_SHA256SUMS" "${name}_SHA256SUMS.sig" "$archive"; do
  curl -fsSL --proto '=https' -o "$work/$file" "$base/$file"
done

gpg --dearmor < "$key" > "$work/signing-key.gpg"
gpgv --keyring "$work/signing-key.gpg" "$work/${name}_SHA256SUMS.sig" "$work/${name}_SHA256SUMS" 2>/dev/null \
  || { echo "signature check failed for ${name}_SHA256SUMS" >&2; exit 1; }

expected=$(awk -v f="$archive" '$2 == f { print $1 }' "$work/${name}_SHA256SUMS")
[ -n "$expected" ] || { echo "no checksum for $archive in the release" >&2; exit 1; }
if command -v sha256sum >/dev/null; then
  actual=$(sha256sum "$work/$archive" | awk '{ print $1 }')
else
  actual=$(shasum -a 256 "$work/$archive" | awk '{ print $1 }')
fi
[ "$actual" = "$expected" ] || { echo "checksum mismatch for $archive" >&2; exit 1; }

dest="$mirror/registry.opentofu.org/floren-app/fdp"
mkdir -p "$dest"
cp "$work/$archive" "$dest/$archive"
echo "Verified and installed $archive in $dest"
