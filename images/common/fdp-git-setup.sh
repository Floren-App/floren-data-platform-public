#!/bin/sh
# Keeps git settings and SSH keys on the workspace volume, so they survive workspace restarts. Run as the
# workspace user (uid 1000) before the editor starts. Points git at it via GIT_CONFIG_GLOBAL (set by Floren
# Data Platform for every workspace). HTTPS tokens are never stored here: git-credential-fdp asks the
# platform for your own token (Account → Git credentials) each time git needs one.
set -eu

DC=/workspace/.fdp
G="$DC/git"
mkdir -p "$G" "$DC/ssh"
chmod 700 "$DC/ssh"

if [ ! -f "$G/config" ]; then
  cat > "$G/config" <<EOF
[user]
	name = ${FDP_USER:-analyst}
[credential]
	helper =
	helper = /usr/local/bin/git-credential-fdp
[core]
	excludesFile = $G/ignore
[init]
	defaultBranch = main
[pull]
	rebase = false
EOF
fi

# Who commits: the name and email of the workspace's Git credential (chosen in the app), when set there.
[ -n "${FDP_GIT_NAME:-}" ] && git config --file "$G/config" user.name "$FDP_GIT_NAME"
[ -n "${FDP_GIT_EMAIL:-}" ] && git config --file "$G/config" user.email "$FDP_GIT_EMAIL"

# Volumes from before the platform helper used git's plaintext store: switch them over (the old file is left as is).
if git config --file "$G/config" --get-all credential.helper | grep -q '^store '; then
  git config --file "$G/config" --unset-all credential.helper
  git config --file "$G/config" --add credential.helper ''
  git config --file "$G/config" --add credential.helper /usr/local/bin/git-credential-fdp
fi

# Never commit run consoles/outputs, environment caches or editor droppings.
if [ ! -f "$G/ignore" ]; then
  printf '%s\n' '.fdp/' '__pycache__/' '.ipynb_checkpoints/' '.Rproj.user/' '.Rhistory' '.RData' > "$G/ignore"
fi

# ~/.ssh lives on the volume too (keys for git over SSH).
if [ ! -e "$HOME/.ssh" ] || [ -L "$HOME/.ssh" ]; then
  ln -sfn "$DC/ssh" "$HOME/.ssh"
fi

# Repositories of the workspace's template ("name<TAB>branch<TAB>url" lines): cloned once, in the background so the
# editor starts right away. A clone that fails (no token yet, no access) leaves a note and is retried next start.
if [ -n "${FDP_GIT_CLONE:-}" ]; then
  (
    printf '%s\n' "$FDP_GIT_CLONE" | while IFS="$(printf '\t')" read -r name branch url; do
      [ -n "$name" ] && [ -n "$url" ] || continue
      grep -qxF "$name" "$G/cloned" 2>/dev/null && continue
      if [ -e "/workspace/$name" ]; then echo "$name" >> "$G/cloned"; continue; fi
      if GIT_TERMINAL_PROMPT=0 git clone --branch "$branch" -- "$url" "/workspace/$name" > "$G/clone.log" 2>&1; then
        echo "$name" >> "$G/cloned"
        rm -f "/workspace/$name.clone-failed.txt"
      else
        host=$(printf '%s' "$url" | sed -E 's#^[a-z]+://([^/@]*@)?([^/]+).*#\2#')
        cat > "/workspace/$name.clone-failed.txt" <<NOTE
$url could not be cloned into $name:

$(tail -n 5 "$G/clone.log")

For a private repository, add your own token for $host in the app under Account → Git credentials,
then run in a terminal:   git clone --branch $branch $url $name
(or restart the workspace). Delete this note when done.
NOTE
      fi
    done
  ) &
fi
