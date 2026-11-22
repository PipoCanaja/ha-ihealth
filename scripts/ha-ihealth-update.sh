#!/usr/bin/env bash
# ha-ihealth-update.sh — installe / met à jour custom_components/ihealth
# depuis le dépôt Git ha-ihealth. Voir « ha-ihealth-update.sh help ».
set -euo pipefail

PROG=$(basename "$0")
REPO_URL=${REPO_URL:-https://github.com/PipoCanaja/ha-ihealth.git}
CONFIG_DIR=${CONFIG_DIR:-/config}
KEEP_BACKUPS=${KEEP_BACKUPS:-5}

COMPONENT=ihealth
SRC_SUBDIR=custom_components/$COMPONENT
TARGET=$CONFIG_DIR/custom_components/$COMPONENT
BACKUP_ROOT=$CONFIG_DIR/.ha-ihealth-backups
INFO_FILE=.install-info

TMP_DIR=""
ASSUME_YES=0
RESTART=0

usage() {
  cat <<EOF
$PROG — installe / met à jour custom_components/$COMPONENT
depuis le dépôt Git, à exécuter dans le shell de Home Assistant OS
(module complémentaire « Terminal & SSH » ou « Advanced SSH & Web Terminal »).

  $PROG status
  $PROG list
  $PROG install [--branch NOM | --pr N | --tag NOM | --ref SHA] [--restart] [--yes]
  $PROG rollback  [--restart] [--yes]
  $PROG uninstall [--restart] [--yes]

Sans option de source, « install » prend la branche main.

Variables d'environnement facultatives :
  REPO_URL    dépôt Git (défaut : https://github.com/PipoCanaja/ha-ihealth.git)
  CONFIG_DIR  dossier de configuration HA (défaut : /config)
  KEEP_BACKUPS nombre de sauvegardes conservées (défaut : 5)
EOF
}

# ------------------------------------------------------------------ outils
die()  { echo "Erreur : $*" >&2; exit 1; }
info() { echo "==> $*"; }
warn() { echo "Attention : $*" >&2; }

cleanup() { [ -n "$TMP_DIR" ] && rm -rf "$TMP_DIR"; return 0; }
trap cleanup EXIT

need() { command -v "$1" >/dev/null 2>&1 || die "commande « $1 » introuvable."; }

confirm() {
  [ "$ASSUME_YES" -eq 1 ] && return 0
  [ -t 0 ] || die "pas de terminal interactif : relancez avec --yes."
  local answer
  read -r -p "$1 [o/N] " answer
  case "$answer" in o|O|oui|OUI|y|Y|yes) return 0 ;; *) echo "Abandon."; exit 1 ;; esac
}

manifest_version() {  # $1 = dossier du composant
  [ -f "$1/manifest.json" ] || { echo "?"; return; }
  sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$1/manifest.json" | head -n 1
}

info_value() {  # $1 = dossier, $2 = clé
  [ -f "$1/$INFO_FILE" ] || return 0
  sed -n "s/^$2=//p" "$1/$INFO_FILE" | head -n 1
}

describe() {  # $1 = dossier du composant
  local src commit date
  src=$(info_value "$1" source)
  commit=$(info_value "$1" commit)
  date=$(info_value "$1" installed_at)
  echo "version $(manifest_version "$1"), source ${src:-inconnue}${commit:+ @ ${commit:0:10}}${date:+, installée le $date}"
}

github_slug() {  # owner/repo si REPO_URL pointe sur GitHub
  echo "$REPO_URL" | sed -n -E 's#^(https://github\.com/|git@github\.com:)([^/]+/[^/]+)$#\2#p' | sed 's/\.git$//'
}

list_backups() {  # plus récente en premier
  [ -d "$BACKUP_ROOT" ] || return 0
  find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d | sort -r
}

prune_backups() {
  local n=0 dir
  while IFS= read -r dir; do
    n=$((n + 1))
    if [ "$n" -gt "$KEEP_BACKUPS" ]; then
      rm -rf "$dir"
    fi
  done < <(list_backups)
}

backup_current() {
  [ -d "$TARGET" ] || return 0
  local dest
  # nom horodaté unique (tri chronologique = tri alphabétique)
  while dest="$BACKUP_ROOT/$(date +%Y%m%d-%H%M%S)"; [ -e "$dest" ]; do sleep 1; done
  mkdir -p "$dest"
  cp -a "$TARGET" "$dest/$COMPONENT"
  info "Sauvegarde de la version actuelle : $dest"
  prune_backups
}

replace_target() {  # $1 = dossier source du composant
  mkdir -p "$(dirname "$TARGET")"
  rm -rf "$TARGET.new"
  cp -a "$1" "$TARGET.new"
  find "$TARGET.new" -name __pycache__ -type d -prune -exec rm -rf {} +
  rm -rf "$TARGET"
  mv "$TARGET.new" "$TARGET"
}

maybe_restart() {
  if [ "$RESTART" -ne 1 ]; then
    echo "Redémarrez Home Assistant pour appliquer le changement (ou relancez avec --restart)."
    return 0
  fi
  if ! command -v ha >/dev/null 2>&1; then
    warn "commande « ha » introuvable : redémarrez Home Assistant manuellement."
    return 0
  fi
  info "Vérification de la configuration…"
  if ! ha core check; then
    die "la vérification de la configuration a échoué : redémarrage annulé."
  fi
  info "Redémarrage de Home Assistant…"
  ha core restart
}

# ------------------------------------------------------------------ commandes
cmd_status() {
  echo "Dépôt       : $REPO_URL"
  echo "Destination : $TARGET"
  if [ -d "$TARGET" ]; then
    echo "Installé    : $(describe "$TARGET")"
  else
    echo "Installé    : non"
  fi
  local dir found=0
  echo "Sauvegardes ($BACKUP_ROOT, $KEEP_BACKUPS max) :"
  while IFS= read -r dir; do
    found=1
    echo "  $(basename "$dir") — $(describe "$dir/$COMPONENT")"
  done < <(list_backups)
  [ "$found" -eq 1 ] || echo "  aucune"
}

cmd_list() {
  need git
  local refs
  refs=$(git ls-remote "$REPO_URL") || die "impossible de lire $REPO_URL"

  echo "Branches :"
  echo "$refs" | sed -n 's#^\([0-9a-f]\{10\}\)[0-9a-f]*[[:space:]]*refs/heads/\(.*\)$#  \2  (\1)#p'

  echo "Tags :"
  local tags
  tags=$(echo "$refs" | sed -n 's#^\([0-9a-f]\{10\}\)[0-9a-f]*[[:space:]]*refs/tags/\([^^]*\)$#  \2  (\1)#p')
  echo "${tags:-  aucun}"

  echo "Pull requests :"
  local slug json=""
  slug=$(github_slug)
  if [ -n "$slug" ] && command -v curl >/dev/null 2>&1 && command -v jq >/dev/null 2>&1; then
    json=$(curl -fsSL -H "Accept: application/vnd.github+json" \
      "https://api.github.com/repos/$slug/pulls?state=open&per_page=50" 2>/dev/null) || json=""
  fi
  if [ -n "$json" ]; then
    local out
    out=$(echo "$json" | jq -r '.[] | "  #\(.number)  \(.title)  [\(.head.ref), @\(.user.login)]"')
    echo "${out:-  aucune PR ouverte}"
  else
    local prs
    prs=$(echo "$refs" | sed -n 's#^\([0-9a-f]\{10\}\)[0-9a-f]*[[:space:]]*refs/pull/\([0-9]*\)/head$#\2 \1#p' \
      | sort -rn | head -n 20 | sed 's/^\([0-9]*\) \(.*\)$/  #\1  (\2)/')
    echo "${prs:-  aucune}"
    [ -z "$prs" ] || echo "  (ouvertes et fermées ; installez curl et jq pour voir les titres des PR ouvertes)"
  fi
}

fetch_source() {  # $1 = type, $2 = valeur ; remplit TMP_DIR/repo
  need git
  TMP_DIR=$(mktemp -d)
  local repo="$TMP_DIR/repo" refspec
  git init -q "$repo"
  git -C "$repo" remote add origin "$REPO_URL"
  case "$1" in
    branch) refspec="refs/heads/$2" ;;
    pr)     refspec="refs/pull/$2/head" ;;
    tag)    refspec="refs/tags/$2" ;;
    ref)    refspec="$2" ;;
  esac
  info "Téléchargement : $SOURCE_LABEL…"
  if ! git -C "$repo" fetch -q --depth 1 origin "$refspec" 2>/dev/null; then
    if [ "$1" != ref ]; then
      die "introuvable : $SOURCE_LABEL (voir « $PROG list »)."
    fi
    # SHA abrégé : on récupère l'historique complet puis on résout
    git -C "$repo" fetch -q origin '+refs/heads/*:refs/remotes/origin/*' \
      '+refs/pull/*/head:refs/remotes/pr/*' '+refs/tags/*:refs/tags/*' \
      || die "impossible de lire $REPO_URL"
    git -C "$repo" rev-parse -q --verify "$2^{commit}" >/dev/null \
      || die "commit introuvable : $2"
    git -C "$repo" checkout -q "$2"
  else
    git -C "$repo" checkout -q FETCH_HEAD
  fi
  [ -f "$repo/$SRC_SUBDIR/manifest.json" ] \
    || die "$SRC_SUBDIR/manifest.json absent ($SOURCE_LABEL)."
}

cmd_install() {
  local kind=branch value=main
  while [ $# -gt 0 ]; do
    case "$1" in
      --branch) kind=branch; value=${2:?--branch attend un nom}; shift ;;
      --pr)     kind=pr; value=${2:?--pr attend un numéro}; shift
                case "$value" in ''|*[!0-9]*) die "--pr attend un numéro" ;; esac ;;
      --tag)    kind=tag; value=${2:?--tag attend un nom}; shift ;;
      --ref)    kind=ref; value=${2:?--ref attend un SHA}; shift ;;
      --restart) RESTART=1 ;;
      --yes|-y) ASSUME_YES=1 ;;
      *) die "option inconnue : $1" ;;
    esac
    shift
  done
  case "$kind" in
    branch) SOURCE_LABEL="branche $value" ;;
    pr)     SOURCE_LABEL="PR #$value" ;;
    tag)    SOURCE_LABEL="tag $value" ;;
    ref)    SOURCE_LABEL="commit $value" ;;
  esac

  fetch_source "$kind" "$value"
  local repo="$TMP_DIR/repo" sha subject new_version
  sha=$(git -C "$repo" rev-parse HEAD)
  subject=$(git -C "$repo" log -1 --format='%s (%cs)')
  new_version=$(manifest_version "$repo/$SRC_SUBDIR")

  echo
  echo "Source   : $SOURCE_LABEL"
  echo "Commit   : ${sha:0:10} — $subject"
  echo "Version  : $new_version"
  if [ -d "$TARGET" ]; then
    echo "Actuelle : $(describe "$TARGET")"
    [ "$(info_value "$TARGET" commit)" = "$sha" ] && echo "(ce commit est déjà installé)"
  else
    echo "Actuelle : non installée"
  fi
  echo
  confirm "Installer dans $TARGET ?"

  backup_current
  replace_target "$repo/$SRC_SUBDIR"
  cat > "$TARGET/$INFO_FILE" <<EOF
source=$kind:$value
commit=$sha
repo=$REPO_URL
installed_at=$(date '+%Y-%m-%d %H:%M:%S')
EOF
  info "Installé : $(describe "$TARGET")"
  maybe_restart
}

parse_common() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --restart) RESTART=1 ;;
      --yes|-y)  ASSUME_YES=1 ;;
      *) die "option inconnue : $1" ;;
    esac
    shift
  done
}

cmd_rollback() {
  parse_common "$@"
  local latest
  latest=$(list_backups | head -n 1)
  [ -n "$latest" ] || die "aucune sauvegarde dans $BACKUP_ROOT."
  [ -d "$latest/$COMPONENT" ] || die "sauvegarde invalide : $latest"
  if [ -d "$TARGET" ]; then
    echo "Actuelle    : $(describe "$TARGET")"
  else
    echo "Actuelle    : non installée"
  fi
  echo "Restaurée   : $(describe "$latest/$COMPONENT") [$(basename "$latest")]"
  confirm "Remplacer la version actuelle par cette sauvegarde ?"
  replace_target "$latest/$COMPONENT"
  rm -rf "$latest"
  info "Restauré : $(describe "$TARGET")"
  maybe_restart
}

cmd_uninstall() {
  parse_common "$@"
  [ -d "$TARGET" ] || die "$TARGET n'existe pas."
  echo "Installée : $(describe "$TARGET")"
  confirm "Supprimer $TARGET (une sauvegarde sera conservée) ?"
  backup_current
  rm -rf "$TARGET"
  info "Désinstallé. Supprimez aussi l'intégration dans Paramètres > Appareils et services."
  maybe_restart
}

# ------------------------------------------------------------------ main
case "${KEEP_BACKUPS}" in ''|*[!0-9]*) die "KEEP_BACKUPS doit être un nombre." ;; esac

cmd=${1:-help}
[ $# -gt 0 ] && shift
case "$cmd" in
  status)    cmd_status ;;
  list)      cmd_list ;;
  install)   cmd_install "$@" ;;
  rollback)  cmd_rollback "$@" ;;
  uninstall) cmd_uninstall "$@" ;;
  help|-h|--help) usage ;;
  *) usage; exit 1 ;;
esac
