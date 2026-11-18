#!/usr/bin/env bash
# Set repository configuration only. Does not install the App, change rules,
# enable workflows/merging, or print the private key. Dry-run by default.
set +x
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: bash scripts-release/setup-pin-bot.sh \
  --client-id CLIENT_ID --app-id NUMERIC_ID --slug APP_SLUG \
  --key /absolute/path/to/app.pem [--apply]

Without --apply, previews the changes and makes no GitHub requests.
With --apply, configures ds-tegra-distro, meta-dsensos, sensor-portal, and ds
under DistributedSpectrum. Requires gh authentication with repository admin
access. Merging and notifications are explicitly disabled first.
EOF
}

client_id='' app_id='' app_slug='' key_path='' apply=false
while [ "$#" -gt 0 ]; do
  case "$1" in
    --client-id|--app-id|--slug|--key)
      [ "$#" -ge 2 ] || { usage >&2; exit 2; }
      case "$1" in
        --client-id) client_id=$2 ;;
        --app-id) app_id=$2 ;;
        --slug) app_slug=$2 ;;
        --key) key_path=$2 ;;
      esac
      shift 2 ;;
    --apply) apply=true; shift ;;
    --help|-h) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done

[[ "$client_id" =~ ^[A-Za-z0-9_.-]+$ ]] || { echo 'Invalid client ID' >&2; exit 2; }
[[ "$app_id" =~ ^[1-9][0-9]*$ ]] || { echo 'App ID must be a positive number' >&2; exit 2; }
[[ "$app_slug" =~ ^[a-z0-9]+(-[a-z0-9]+)*$ ]] || { echo 'Use the App slug without [bot]' >&2; exit 2; }
[ -n "$key_path" ] || { echo 'Specify the downloaded PEM file with --key' >&2; exit 2; }

distro=DistributedSpectrum/ds-tegra-distro
meta=DistributedSpectrum/meta-dsensos
repos=("$distro" "$meta" DistributedSpectrum/sensor-portal DistributedSpectrum/ds)

if [ "$apply" != true ]; then
  echo 'PREVIEW ONLY: no changes made.'
  echo "$distro: DS_PIN_BOT_MODE=disabled"
  echo "$meta: DS_PIN_BOT_NOTIFY=false"
  for repo in "${repos[@]}"; do
    echo "$repo: DS_PIN_BOT_CLIENT_ID=$client_id"
    echo "$repo: upload DS_PIN_BOT_PRIVATE_KEY from the specified PEM file"
  done
  echo "$distro: DS_PIN_BOT_APP_ID=$app_id"
  echo "$distro: DS_PIN_BOT_APP_SLUG=$app_slug"
  echo 'Run again with --apply to store this configuration.'
  exit 0
fi

[ -r "$key_path" ] && [ -s "$key_path" ] || { echo 'PEM file is unreadable or empty' >&2; exit 2; }
openssl rsa -in "$key_path" -check -noout -passin pass: >/dev/null 2>&1 \
  || { echo 'Expected an unencrypted RSA private key downloaded from the App settings' >&2; exit 2; }
gh auth status --hostname github.com >/dev/null 2>&1 \
  || { echo 'Authenticate first with gh auth login --hostname github.com' >&2; exit 2; }
for repo in "${repos[@]}"; do
  [ "$(gh api "repos/$repo" --jq '.permissions.admin')" = true ] \
    || { echo "Repository admin access required: $repo" >&2; exit 2; }
done

# Stop automation before writing credentials. If a later command fails, the
# script exits immediately and leaves the automation disabled. Safe to rerun.
gh variable set DS_PIN_BOT_MODE --repo "$distro" --body disabled
gh variable set DS_PIN_BOT_NOTIFY --repo "$meta" --body false
for repo in "${repos[@]}"; do
  gh variable set DS_PIN_BOT_CLIENT_ID --repo "$repo" --body "$client_id"
  # gh encrypts the value locally; plaintext is passed only through stdin.
  gh secret set DS_PIN_BOT_PRIVATE_KEY --app actions --repo "$repo" < "$key_path"
done
gh variable set DS_PIN_BOT_APP_ID --repo "$distro" --body "$app_id"
gh variable set DS_PIN_BOT_APP_SLUG --repo "$distro" --body "$app_slug"
echo 'Repository configuration saved. Merging and notifications remain disabled.'
