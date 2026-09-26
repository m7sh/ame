#!/usr/bin/env bash
#
# update.sh — self-updater for ame (terminal YouTube Music player)
#
set -euo pipefail

# Visual styles
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    BOLD="\033[1m"
    DIM="\033[2m"
    RESET="\033[0m"
    GREEN="\033[32m"
    YELLOW="\033[33m"
    CYAN="\033[36m"
    RED="\033[31m"
else
    BOLD=""
    DIM=""
    RESET=""
    GREEN=""
    YELLOW=""
    CYAN=""
    RED=""
fi

log_title()   { printf "${BOLD}${CYAN}▸─ %s ─────────────────────────◂${RESET}\n\n" "$*"; }
log_step()    { printf "  ${CYAN}▸${RESET} %s\n" "$*"; }
log_success() { printf "  ${GREEN}✓${RESET} %s\n" "$*"; }
log_info()    { printf "  ${CYAN}ℹ${RESET} %s\n" "$*"; }
log_warn()    { printf "  ${YELLOW}!${RESET} %s\n" "$*"; }
log_error()   { printf "  ${RED}✗${RESET} %s\n" "$*"; }

show_help() {
    cat << EOF
ame updater

Usage:
  ame --update [OPTIONS]
  ./update.sh [OPTIONS]

Options:
      --check        Check for available updates without applying them
      --force        Force reinstall dependencies even if unchanged
  -y, --yes          Proceed without interactive confirmation
  -h, --help         Show this help message
EOF
}

CHECK_ONLY=false
FORCE=false
ASSUME_YES=false

while [ $# -gt 0 ]; do
    case "$1" in
        --check)
            CHECK_ONLY=true
            shift
            ;;
        --force)
            FORCE=true
            shift
            ;;
        -y|--yes)
            ASSUME_YES=true
            shift
            ;;
        -h|--help)
            show_help
            exit 0
            ;;
        *)
            log_error "Unknown option: $1"
            printf "\nRun ./update.sh --help for available options.\n" >&2
            exit 1
            ;;
    esac
done

# Resolve repository directory
SOURCE="${BASH_SOURCE[0]:-}"
while [ -h "$SOURCE" ]; do
    DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"
    SOURCE="$(readlink "$SOURCE")"
    [[ $SOURCE != /* ]] && SOURCE="$DIR/$SOURCE"
done
DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"

log_title "ame updater"

if [ ! -d "$DIR/.git" ]; then
    log_error "Not a git repository: $DIR"
    exit 1
fi

command -v git >/dev/null 2>&1 || { log_error "git is required but not installed."; exit 1; }

cd "$DIR"

CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "master")"
CURRENT_HASH="$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")"

log_step "Checking for updates on branch '${CURRENT_BRANCH}'..."

# Fetch updates from origin
if ! git fetch origin "$CURRENT_BRANCH" --quiet 2>/dev/null; then
    if ! git fetch origin --quiet 2>/dev/null; then
        log_error "Failed to fetch updates from remote origin."
        exit 1
    fi
fi

UPSTREAM="origin/$CURRENT_BRANCH"
if ! git rev-parse "$UPSTREAM" >/dev/null 2>&1; then
    UPSTREAM="@{u}"
fi

if ! git rev-parse "$UPSTREAM" >/dev/null 2>&1; then
    log_warn "Could not determine upstream remote branch for '${CURRENT_BRANCH}'."
    exit 1
fi

LOCAL_FULL="$(git rev-parse HEAD)"
REMOTE_FULL="$(git rev-parse "$UPSTREAM")"
REMOTE_HASH="$(git rev-parse --short "$UPSTREAM")"

if [ "$LOCAL_FULL" = "$REMOTE_FULL" ]; then
    log_success "ame is already up to date (${CURRENT_HASH})."
    if [ "$FORCE" = false ]; then
        printf "\n"
        exit 0
    fi
fi

# Count commits behind
BEHIND_COUNT="$(git rev-list --count HEAD.."$UPSTREAM" 2>/dev/null || echo "0")"

if [ "$CHECK_ONLY" = true ]; then
    if [ "$BEHIND_COUNT" -gt 0 ]; then
        log_info "Update available! (${CURRENT_HASH} -> ${REMOTE_HASH}, ${BEHIND_COUNT} new commit(s))"
        printf "\n  ${DIM}Incoming changes:${RESET}\n"
        git log --oneline --no-merges HEAD.."$UPSTREAM" | sed 's/^/    /'
        printf "\nRun 'ame --update' to apply.\n\n"
    else
        log_success "ame is up to date (${CURRENT_HASH}).\n"
    fi
    exit 0
fi

if [ "$BEHIND_COUNT" -gt 0 ]; then
    log_info "New update found: ${BEHIND_COUNT} new commit(s) (${CURRENT_HASH} -> ${REMOTE_HASH})"
    printf "\n  ${DIM}Changes in this update:${RESET}\n"
    git log --oneline --no-merges HEAD.."$UPSTREAM" | sed 's/^/    /'
    printf "\n"
fi

# Handle local uncommitted modifications
STASHED=false
if ! git diff-index --quiet HEAD -- 2>/dev/null; then
    log_warn "Local modifications detected. Stashing changes temporarily..."
    git stash push -m "ame-auto-stash-$(date +%s)" --quiet
    STASHED=true
fi

# Pull updates
log_step "Applying updates..."
if ! git merge --ff-only "$UPSTREAM" --quiet 2>/dev/null; then
    # Fallback to standard pull
    if ! git pull origin "$CURRENT_BRANCH" --quiet 2>/dev/null; then
        log_error "Failed to merge upstream changes cleanly."
        [ "$STASHED" = true ] && git stash pop --quiet 2>/dev/null || true
        exit 1
    fi
fi

NEW_HASH="$(git rev-parse --short HEAD)"
log_success "Updated to commit ${NEW_HASH}."

# Restore stashed modifications if any
if [ "$STASHED" = true ]; then
    log_step "Restoring local modifications..."
    if git stash pop --quiet 2>/dev/null; then
        log_success "Restored local modifications."
    else
        log_warn "Conflicts occurred while restoring local modifications. Stash has been retained."
    fi
fi

# Update dependencies if requirements.txt changed or force requested
REQ_CHANGED=false
if [ "$FORCE" = true ]; then
    REQ_CHANGED=true
elif ! git diff --quiet "$LOCAL_FULL" "$NEW_HASH" -- requirements.txt 2>/dev/null; then
    REQ_CHANGED=true
fi

if [ "$REQ_CHANGED" = true ] && [ -f "$DIR/requirements.txt" ]; then
    log_step "Updating Python dependencies..."
    if [ -x "$DIR/.venv/bin/pip" ]; then
        "$DIR/.venv/bin/pip" install --quiet -r "$DIR/requirements.txt"
        log_success "Python dependencies updated in .venv."
    elif command -v pip3 >/dev/null 2>&1; then
        pip3 install --quiet --user -r "$DIR/requirements.txt" 2>/dev/null || true
        log_success "Python dependencies updated."
    fi
fi

# Ensure launcher symlink is in place
if [ ! -L "$HOME/.local/bin/ame" ] && [ -d "$HOME/.local/bin" ]; then
    ln -sf "$DIR/ame" "$HOME/.local/bin/ame"
    log_success "Linked launcher to ~/.local/bin/ame"
fi

printf "\n${BOLD}${GREEN}ame has been successfully updated!${RESET}\n\n"
