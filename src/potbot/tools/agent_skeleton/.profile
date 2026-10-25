# Only executed by login shells; .bashrc owns interactive decoration.
if [ -n "${BASH_VERSION:-}" ] && [ -r "$HOME/.bashrc" ]; then
    . "$HOME/.bashrc"
fi
