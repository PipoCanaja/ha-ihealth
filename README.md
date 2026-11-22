# iHealth pour Home Assistant

Intégration Home Assistant (Bluetooth, locale, sans cloud) pour l'oxymètre de pouls
**iHealth PO3M** (« Air »).

> ⚠️ **Expérimental.** Le protocole a été reconstitué à partir de l'application iHealth
> (voir [docs/PROTOCOL.md](docs/PROTOCOL.md)). Il est vérifié contre le code Java d'origine
> et un appareil simulé, mais pas encore contre un vrai PO3M.

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

Allumez l'oxymètre : il est découvert automatiquement (*Paramètres > Appareils et services*).
Sinon, *Ajouter une intégration > iHealth* pendant qu'il est allumé.

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
