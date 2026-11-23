# iHealth pour Home Assistant

Intégration Home Assistant (Bluetooth, locale, sans cloud) pour :

- l'oxymètre de pouls **iHealth PO3 / PO3M** (« Air ») ;
- le tensiomètre **iHealth KN-550BT** — voir [plus bas](#tensiomètre-kn-550bt) et
  [docs/PROTOCOL_KN550BT.md](docs/PROTOCOL_KN550BT.md) ;
- le bracelet d'activité **iHealth AM4** — voir [plus bas](#bracelet-dactivité-am4) et
  [docs/PROTOCOL_AM4.md](docs/PROTOCOL_AM4.md).

> ⚠️ **Expérimental.** Les protocoles ont été reconstitués à partir de l'application iHealth.
> Tout est vérifié contre le code Java d'origine et un appareil simulé, mais pas encore
> contre un vrai appareil.

Deux profils Bluetooth sont pris en charge, choisis automatiquement selon le service annoncé :

- **POV12** (PO3M récents) — voir [docs/PROTOCOL.md](docs/PROTOCOL.md) ;
- **POV11** (PO3 plus anciens, une seule caractéristique) — voir
  [docs/PROTOCOL_CLASSIC.md](docs/PROTOCOL_CLASSIC.md).

La clé d'authentification du profil POV11 a été extraite de la bibliothèque native
`libiHealth.so` (`getKey("PO3")`).

## Fonctionnement

Le PO3M n'émet en Bluetooth que lorsqu'il est allumé (doigt inséré). À chaque fois que
Home Assistant le voit, l'intégration :

1. se connecte et réalise l'authentification iHealth (XXTEA + AES-CBC, puis AES-CTR) ;
2. lit la batterie et remet l'oxymètre à l'heure ;
3. lit les infos de l'appareil (firmware, n° de série) au premier passage ;
4. lance une mesure et suit le flux en direct ; le **résultat** est la dernière valeur
   valide reçue quand le flux s'arrête (comme dans l'application) ;
5. lit la mémoire de l'oxymètre (mesures faites sans téléphone) ;
6. se déconnecte (nouvelle session possible 20 s plus tard).

Il faut un adaptateur Bluetooth local **ou un proxy ESPHome capable de connexions actives**
(`bluetooth_proxy: active: true`) à portée de l'oxymètre.

## Entités

| Entité | Description |
|---|---|
| SpO2, Fréquence cardiaque, Indice de perfusion | dernier résultat (mesure en direct ou mémoire) |
| Dernière mesure | horodatage du dernier résultat |
| Batterie | niveau (%) |
| Mesure en cours | `on` pendant le flux en direct |
| Connecté | session Bluetooth ouverte (diagnostic) |
| SpO2 / Fréquence / IP / Force du pouls *en direct* | valeurs pendant la mesure, ~1 par seconde — **désactivées par défaut** |

Les valeurs restent affichées (et sont conservées au redémarrage) quand l'oxymètre est éteint.

### Évènement `ihealth_history_record`

Chaque mesure trouvée dans la mémoire de l'oxymètre (et pas encore importée) déclenche
un évènement :

```yaml
event_type: ihealth_history_record
data:
  address: "AA:BB:CC:DD:EE:FF"
  measured_at: "2026-11-21T09:30:05+01:00"
  spo2: 99
  pulse_rate: 58
  perfusion_index: 5.2
  time_calibrated: true
```

## Tensiomètre KN-550BT

Le KN-550BT mesure seul et garde les mesures en mémoire. Après une mesure, il émet en
Bluetooth un moment ; l'intégration se connecte alors, lit la batterie et **toute la
mémoire**, remet l'appareil à l'heure puis se déconnecte (pas de nouvelle synchro pendant
2 min). La mémoire **n'est jamais effacée** : l'application du téléphone voit toujours
toutes les mesures. Les trois profils Bluetooth du KN-550BT (BPAV21, BPAV10, « stream »)
sont gérés.

| Entité | Description |
|---|---|
| Systolique, Diastolique (mmHg), Fréquence cardiaque | dernière mesure |
| Dernière mesure | horodatage de la dernière mesure |
| Rythme cardiaque irrégulier | drapeau arythmie de la dernière mesure |
| Mouvement pendant la mesure | firmwares récents — **désactivé par défaut** |
| Batterie, Mesures en mémoire | diagnostic |
| Connecté | session Bluetooth ouverte (diagnostic) |
| Connexion automatique | coupé = tensiomètre laissé libre pour l'application |

Quand l'horloge du tensiomètre n'était pas fiable au moment d'une mesure (drapeau posé par
les firmwares récents), l'heure est corrigée avec l'horloge de l'appareil, comme dans
l'application.

Chaque nouvelle mesure trouvée en mémoire déclenche un évènement :

```yaml
event_type: ihealth_bp_record
data:
  address: "AA:BB:CC:DD:EE:FF"
  measured_at: "2026-11-23T07:42:10+01:00"
  systolic: 128
  diastolic: 82
  pulse_rate: 67
  irregular_heartbeat: false
  body_movement: false
  time_fixed: false
```

## Bracelet d'activité AM4

L'AM4 émet en Bluetooth en permanence. Tant qu'il est à portée, l'intégration s'y connecte
**au plus toutes les 15 minutes** : authentification, mode et batterie, mise à l'heure,
**pas et calories du jour**, puis déconnexion.

L'interrupteur **« Télécharger l'historique »** (coupé par défaut) ajoute à chaque session
le téléchargement des rapports d'étapes (entraînements, natation, résumés de sommeil), des
points d'activité (toutes les 5 min) et du sommeil détaillé. ⚠️ Le bracelet ne garde
probablement pas ce qu'il a déjà envoyé : si vous utilisez encore l'application iHealth,
laissez-le coupé, sinon elle ne recevra plus ces données.

L'intégration n'envoie jamais de commande qui modifie le bracelet (réinitialisation,
identifiant de compte, profil, alarmes…) : il reste associé à votre compte iHealth.

| Entité | Description |
|---|---|
| Pas, Calories | totaux du jour (remis à zéro par le bracelet à minuit) |
| Dernière synchronisation | horodatage de la dernière lecture réussie (diagnostic) |
| Batterie, Mode (activité / sommeil / avion / conduite) | diagnostic |
| Durée de sommeil, Sommeil profond, Sommeil léger, Éveil pendant le sommeil | dernière nuit — **historique uniquement** |
| Début / Fin du sommeil | dernière nuit — **historique uniquement** |
| Connecté | session Bluetooth ouverte (diagnostic) |
| Connexion automatique | coupé = bracelet laissé libre pour l'application |
| Télécharger l'historique | voir ci-dessus |

Avec l'historique, trois évènements sont émis (seulement pour les données nouvelles) :

```yaml
event_type: ihealth_am4_activity      # un par jour ; pas/calories cumulés du jour
data: {address: …, date: "2026-11-23", steps: 5230, calories: 214, step_length: 70,
       points: [{time: "2026-11-23T07:05:00+01:00", steps: 120, calories: 6}, …]}

event_type: ihealth_am4_sleep         # une nuit ; un niveau toutes les 5 min
data: {address: …, start: …, end: …, awake_minutes: 20, light_minutes: 190,
       deep_minutes: 150, levels: [0, 1, 2, …]}   # 0 éveil, 1 léger, 2 profond

event_type: ihealth_am4_stage         # rapport calculé par le bracelet
data: {address: …, type: workout, start: …, end: …, duration_minutes: 30,
       steps: 4200, distance: 3.2, calories: 210}
# type: workout | swim | sleep (efficiency, is_50min) | summary (résumé du jour)
```

## Installation

### Avec le script `ha-ihealth-update.sh` (recommandé pour tester branches et PR)

Dans le module complémentaire « Terminal & SSH » ou « Advanced SSH & Web Terminal » :

```sh
mkdir -p /config/scripts
curl -fsSL -o /config/scripts/ha-ihealth-update.sh \
  https://raw.githubusercontent.com/PipoCanaja/ha-ihealth/main/scripts/ha-ihealth-update.sh
chmod +x /config/scripts/ha-ihealth-update.sh

/config/scripts/ha-ihealth-update.sh install --restart
```

```
ha-ihealth-update.sh status
ha-ihealth-update.sh list
ha-ihealth-update.sh install [--branch NOM | --pr N | --tag NOM | --ref SHA] [--restart] [--yes]
ha-ihealth-update.sh rollback  [--restart] [--yes]
ha-ihealth-update.sh uninstall [--restart] [--yes]
```

- sans option de source, `install` prend la branche `main` ;
- chaque `install` / `uninstall` sauvegarde la version en place dans
  `/config/.ha-ihealth-backups` (`KEEP_BACKUPS`, 5 par défaut) ; `rollback` restaure la
  plus récente et la retire de la liste ;
- `--restart` lance `ha core check` puis `ha core restart` ;
- `list` affiche les titres des PR ouvertes si `curl` et `jq` sont présents.

Variables : `REPO_URL`, `CONFIG_DIR` (défaut `/config`), `KEEP_BACKUPS`.

### Avec HACS

Ajoutez `https://github.com/PipoCanaja/ha-ihealth` comme dépôt personnalisé (catégorie
*Intégration*), installez « iHealth », redémarrez.

## Configuration

Allumez l'oxymètre, faites une mesure avec le tensiomètre ou approchez le bracelet AM4 :
l'appareil est découvert automatiquement (*Paramètres > Appareils et services*). Sinon,
*Ajouter une intégration > iHealth* pendant qu'il émet.

## Débogage

```yaml
logger:
  logs:
    custom_components.ihealth: debug
```

## Développement

```sh
pip install pytest cryptography
pytest -q tests
```
