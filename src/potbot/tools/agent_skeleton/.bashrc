# Potbot agent skeleton v1; non-interactive commands must stay quiet.
case "$-" in
    *i*) ;;
    *) return ;;
esac
export PATH="$HOME/.local/bin:/usr/bin:/bin"
if [ "${POTBOT_WELCOME:-1}" != 0 ] && [ -t 1 ] && [ -x "$HOME/.local/bin/potbot-welcome" ]; then
    "$HOME/.local/bin/potbot-welcome"
fi
