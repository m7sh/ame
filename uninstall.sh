#!/usr/bin/env bash
#
# uninstall.sh — uninstaller for ame (terminal YouTube Music player)
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
ame uninstaller

Usage:
  ./uninstall.sh [OPTIONS]
  ame --uninstall [OPTIONS]

Options:
  -y, --yes          Assume yes to uninstallation prompts
      --purge        Remove all user data (~/.local/share/ame, ~/.config/ame)
      --keep-data    Retain user favourites and history
      --remove-repo  Remove the cloned ame repository directory
      --keep-repo    Keep the ame repository directory
      --dry-run      Show what would be removed without deleting anything
  -h, --help         Show this help message
EOF
}

ASSUME_YES=false
PURGE_DATA=""
REMOVE_REPO=""
DRY_RUN=false

while [ $# -gt 0 ]; do
    case "$1" in
        -y|--yes)
            ASSUME_YES=true
            shift
            ;;
        --purge)
            PURGE_DATA=true
            shift
            ;;
        --keep-data)
            PURGE_DATA=false
            shift
            ;;
        --remove-repo)
            REMOVE_REPO=true
            shift
            ;;
        --keep-repo)
            REMOVE_REPO=false
            shift
            ;;
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        -h|--help)
            show_help
            exit 0
            ;;
        *)
            log_error "Unknown option: $1"
            printf "\nRun ./uninstall.sh --help for available options.\n" >&2
            exit 1
            ;;
    esac
done

# Resolve the repository directory
SCRIPT_DIR=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -e "${BASH_SOURCE[0]}" ]; then
    SOURCE="${BASH_SOURCE[0]}"
    while [ -h "$SOURCE" ]; do
        DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"
        SOURCE="$(readlink "$SOURCE")"
        [[ $SOURCE != /* ]] && SOURCE="$DIR/$SOURCE"
    done
    SCRIPT_DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"
fi

REPO_DIR=""
if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/app.py" ] && [ -f "$SCRIPT_DIR/player.py" ]; then
    REPO_DIR="$SCRIPT_DIR"
fi

# Locate binary symlinks / executables
BIN_CANDIDATES=(
    "$HOME/.local/bin/ame"
    "$HOME/bin/ame"
    "/usr/local/bin/ame"
    "/usr/bin/ame"
)

if [ -n "${AME_CALLER_PATH:-}" ]; then
    BIN_CANDIDATES+=("$AME_CALLER_PATH")
fi

if command -v ame >/dev/null 2>&1; then
    CMD_AME="$(command -v ame)"
    BIN_CANDIDATES+=("$CMD_AME")
fi

FOUND_BINS=()
for b in "${BIN_CANDIDATES[@]}"; do
    if [ -L "$b" ] || [ -f "$b" ]; then
        # Skip the repo's own launcher script (only remove symlinks/installed copies)
        if [ -n "$REPO_DIR" ] && [ "$(readlink -f "$b" 2>/dev/null || true)" = "$(readlink -f "$REPO_DIR/ame" 2>/dev/null || true)" ] && [ ! -L "$b" ]; then
            continue
        fi

        # Check if already added
        ALREADY_ADDED=false
        for fb in "${FOUND_BINS[@]:-}"; do
            if [ "$fb" = "$b" ]; then
                ALREADY_ADDED=true
                break
            fi
        done
        if [ "$ALREADY_ADDED" = false ]; then
            FOUND_BINS+=("$b")
            # If REPO_DIR is still empty, see if this symlink points to the repo
            if [ -z "$REPO_DIR" ] && [ -L "$b" ]; then
                TARGET="$(readlink -f "$b" 2>/dev/null || true)"
                if [ -n "$TARGET" ] && [ -f "$TARGET" ]; then
                    PARENT="$(dirname "$TARGET")"
                    if [ -f "$PARENT/app.py" ] && [ -f "$PARENT/player.py" ]; then
                        REPO_DIR="$PARENT"
                    fi
                fi
            fi
        fi
    fi
done

DATA_DIR="$HOME/.local/share/ame"
CONFIG_DIR="$HOME/.config/ame"
CACHE_DIR="$HOME/.cache/ame"

prompt_yes_no() {
    local prompt="$1"
    local default="$2"
    local response

    if [ "$ASSUME_YES" = true ]; then
        return 0
    fi

    if [ ! -t 0 ]; then
        if [ "$default" = "y" ]; then
            return 0
        else
            return 1
        fi
    fi

    local hint="[y/N]"
    [ "$default" = "y" ] && hint="[Y/n]"

    while true; do
        printf "  ${BOLD}%s %s: ${RESET}" "$prompt" "$hint"
        read -r response
        response="${response:-$default}"
        case "$response" in
            [yY]|[yY][eE][sS]) return 0 ;;
            [nN]|[nN][oO])     return 1 ;;
            *) printf "  Please answer yes or no.\n" ;;
        esac
    done
}

log_title "ame uninstaller"

if [ "$DRY_RUN" = true ]; then
    printf "  ${BOLD}[DRY-RUN]${RESET} The following actions would be performed:\n\n"
    if [ ${#FOUND_BINS[@]} -gt 0 ]; then
        for b in "${FOUND_BINS[@]}"; do
            printf "    - Remove binary: %s\n" "$b"
        done
    else
        printf "    - (No binary symlinks found)\n"
    fi

    if [ "$PURGE_DATA" = true ]; then
        [ -d "$DATA_DIR" ] && printf "    - Remove user data directory: %s\n" "$DATA_DIR"
        [ -d "$CONFIG_DIR" ] && printf "    - Remove config directory: %s\n" "$CONFIG_DIR"
    elif [ "$PURGE_DATA" = false ]; then
        [ -d "$DATA_DIR" ] && printf "    - Keep user data directory: %s\n" "$DATA_DIR"
        [ -d "$CONFIG_DIR" ] && printf "    - Keep config directory: %s\n" "$CONFIG_DIR"
    else
        [ -d "$DATA_DIR" ] && printf "    - Prompt to remove user data directory: %s\n" "$DATA_DIR"
        [ -d "$CONFIG_DIR" ] && printf "    - Prompt to remove config directory: %s\n" "$CONFIG_DIR"
    fi

    [ -d "$CACHE_DIR" ] && printf "    - Remove cache directory: %s\n" "$CACHE_DIR"

    if [ -n "$REPO_DIR" ] && [ -d "$REPO_DIR" ]; then
        if [ "$REMOVE_REPO" = true ]; then
            printf "    - Remove repository directory: %s\n" "$REPO_DIR"
        elif [ "$REMOVE_REPO" = false ]; then
            printf "    - Keep repository directory: %s\n" "$REPO_DIR"
        else
            printf "    - Prompt to remove repository directory: %s\n" "$REPO_DIR"
        fi
    fi
    printf "\n"
    exit 0
fi

# Confirmation prompt
if [ "$ASSUME_YES" = false ]; then
    if ! prompt_yes_no "Are you sure you want to uninstall ame?" "n"; then
        log_warn "Uninstallation cancelled."
        exit 0
    fi
    printf "\n"
fi

# Determine whether to purge user data
if [ -z "$PURGE_DATA" ]; then
    if [ -d "$DATA_DIR" ] || [ -d "$CONFIG_DIR" ]; then
        if prompt_yes_no "Remove user data (favourites and history in ~/.local/share/ame)?" "n"; then
            PURGE_DATA=true
        else
            PURGE_DATA=false
        fi
        printf "\n"
    else
        PURGE_DATA=false
    fi
fi

# Determine whether to remove repository directory
if [ -n "$REPO_DIR" ] && [ -d "$REPO_DIR" ]; then
    if [ -z "$REMOVE_REPO" ]; then
        if prompt_yes_no "Remove the cloned ame repository directory (${REPO_DIR})?" "n"; then
            REMOVE_REPO=true
        else
            REMOVE_REPO=false
        fi
        printf "\n"
    fi
else
    REMOVE_REPO=false
fi

log_step "Uninstalling ame..."

# 1. Remove binary symlinks
if [ ${#FOUND_BINS[@]} -gt 0 ]; then
    for b in "${FOUND_BINS[@]}"; do
        if [ -w "$b" ] || [ -w "$(dirname "$b")" ]; then
            rm -f "$b"
            log_success "Removed binary: $b"
        elif command -v sudo >/dev/null 2>&1; then
            sudo rm -f "$b"
            log_success "Removed binary (with sudo): $b"
        else
            log_error "Could not remove $b: permission denied"
        fi
    done
else
    log_info "No binary symlinks found to remove."
fi

# 2. Clean temporary sockets if any
rm -f /tmp/ame_mpv_*.sock 2>/dev/null || true
rm -f /tmp/ame_cava_*.conf 2>/dev/null || true

# 3. Clean cache
if [ -d "$CACHE_DIR" ]; then
    rm -rf "$CACHE_DIR"
    log_success "Removed cache directory: $CACHE_DIR"
fi

# 4. Clean user data and config if requested
if [ "$PURGE_DATA" = true ]; then
    if [ -d "$DATA_DIR" ]; then
        rm -rf "$DATA_DIR"
        log_success "Removed user data: $DATA_DIR"
    fi
    if [ -d "$CONFIG_DIR" ]; then
        rm -rf "$CONFIG_DIR"
        log_success "Removed config directory: $CONFIG_DIR"
    fi
else
    if [ -d "$DATA_DIR" ]; then
        log_info "Preserved user data: $DATA_DIR"
    fi
    if [ -d "$CONFIG_DIR" ]; then
        log_info "Preserved config directory: $CONFIG_DIR"
    fi
fi

# 5. Remove repository directory if requested
if [ "$REMOVE_REPO" = true ] && [ -n "$REPO_DIR" ] && [ -d "$REPO_DIR" ]; then
    log_step "Removing repository directory: $REPO_DIR"
    # Execute through a separate shell process from HOME to safely delete the running script's directory
    exec bash -c 'cd "$1" && rm -rf "$2" && printf "  \033[32m✓\033[0m Removed repository: %s\n\n\033[1;32mame has been successfully uninstalled.\033[0m\n" "$2"' _ "$HOME" "$REPO_DIR"
fi

printf "\n${BOLD}${GREEN}ame has been successfully uninstalled.${RESET}\n\n"
if [ "$PURGE_DATA" = false ] && [ -d "$DATA_DIR" ]; then
    printf "  ${DIM}Note: Your favourites and history were kept in %s${RESET}\n" "$DATA_DIR"
fi
