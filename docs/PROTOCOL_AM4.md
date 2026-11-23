# Protocole du bracelet d'activité iHealth AM4

Reconstitué à partir du code décompilé de l'application iHealth :
`Am4Control`, `AmControlBase`, `AmProfile`, `AaInsSet`, `IdentifyIns`, `BleConfig`,
et pour l'enchaînement des commandes `AM4ViewModel` / `AM4Devices`.
Code : `custom_components/ihealth/am4/` + `am4_coordinator.py`.

## GATT

| Rôle | UUID | Texte |
|---|---|---|
| Service (annoncé) | `636f6d2e-6a69-7561-6e2e-414d56313200` | `com.jiuan.AMV12` |
| Écriture (app → bracelet) | `7265632e-6a69-7561-6e2e-414d56313200` | `rec.jiuan.AMV12` |
| Notification (bracelet → app) | `7365642e-6a69-7561-6e2e-414d56313200` | `sed.jiuan.AMV12` |
| Firmware | `00002a26-…` (Device Information) | ex. `1.4.0` |

Le numéro de firmware utilisé par l'app est la version sans les points (`"1.3.8"` → 138).

## Transport et authentification

Identiques au PO3 classique (`BleCommProtocol`, voir `PROTOCOL_CLASSIC.md`) avec l'octet de
famille **`AA`** (`AaInsSet.G = -86`) : trames `B0/A0 | len | frag | seq | AA | cmd | … | somme`,
fragmentation à 14 octets, acquittement `B0 03 (A0+index) (seq+1) AA somme` de chaque trame
`frag != F0`.

Authentification `IdentifyIns` (`FA` → `FB` 48 o → `FC` 16 o → `FD`/`FE`) avec
`getKey("AM4")` = `1967a5449c30c3bd20dec808f8ea4d34`, `Ka` dérivée =
`995a085c06eaa5dcf2f4da075df4804b` (voir `device_keys.md`).

## Commandes (après `FD`)

| Cmd | Sens | Données | Réponse |
|---|---|---|---|
| `B4` | état | — | `B4` `[mode][niveau batterie 0..9]` |
| `A4` | mise à l'heure | `yy mm jj hh mi ss jour_sem 00 00 00 00` (jour_sem : 1 = dimanche) | `A4` |
| `BF` | totaux du jour | — | `BF` `[pas u32][calories u16]` (gros-boutiste) |
| `A2` | identifiant de compte | — | `A2` `[id u32]` |
| `0A` | rapports d'étapes | — | `0B` / `0C`… / `0D` |
| `AC` | activité | — | `AB` / `AD`… / `AE` |
| `B0` | sommeil | — | `AF` / `B1`… / `B2` |

Mode (`AmProfile.AM_DEVICE_MODE_*`) : 0 sommeil, 1 activité, 2 avion, 3 conduite.

Batterie (`AaInsSet`, cas `0xB4`) : niveau > 9 = inconnu ; firmware > 137 →
`(niveau + 1) × 10 %` ; sinon `niveau × 10 %`, 9 → 100 %.

Les calories de `BF` sont celles de l'activité ; l'app y ajoute elle-même la part du
métabolisme de base (BMR) écoulée depuis minuit.

**Jamais envoyées par l'intégration** : `A1` (réinitialisation), `A3` (change le compte
associé), `A5` / `B7` (profil, BMR), `A8`/`A9`/`AA` (alarmes, rappels), `02`, `06`, `09`,
`11`, `B5`… qui modifient le bracelet.

## Transferts en masse

Le bracelet envoie *début*, puis zéro ou plusieurs *données*, puis *fin*. L'app répond à
**chacun** par l'écho `AA <cmd>` (en plus de l'acquittement de transport) ; sans cet écho le
bracelet n'envoie pas la suite.

- **début** : ouvre un nouveau bloc. Activité et sommeil : charge utile **sans ses 4
  derniers octets** (`rejectBuffer`). Étapes : la charge utile du début est ignorée.
- **données** : ajoutées au bloc en cours.
- **fin** : clôt le bloc ; décodage.

### Activité (`AaInsSet.i`, AM4)

Un bloc par jour : `[yy][mm][jj][longueur de pas cm][?][?]` puis des points de 8 octets
`[hh][mi][pas u32][calories u16]`, un toutes les 5 min. **Pas et calories sont cumulés
depuis minuit.**

### Sommeil (`AaInsSet.j`)

Un bloc par nuit : `[yy][mm][jj][hh][mi][ss][?][?]` puis un octet de niveau toutes les
5 min à partir de l'heure de début : 0 éveil, 1 léger, 2 profond
(`AMSleepDataParsingUtil`).

### Rapports d'étapes (`AaInsSet.k`, enregistrements de 21 octets sur l'AM4)

Ignorés par l'app si firmware < 134. Octet 0 = type :

| Type | Contenu |
|---|---|
| 0 natation | fin `yy mm jj hh mi ss` (1-6), durée min u16 (7), longueurs (9), bassin (10), nage (11), mouvements u16 (12), calories u16 (14), écarts entrée/sortie u16 (16, 18), drapeau (20) |
| 1 entraînement | fin `yy mm jj hh mi` (1-5), durée min u16 (6), pas u32 (8), distance « o12.o13 » km (12-13, chaîne comme dans l'app), calories u16 (14) |
| 2 sommeil | fin `yy mm jj hh mi` (1-5), durée min u16 (6), efficacité u16 /10 % (8), « 50 min » (10) |
| 3 résumé du jour | date `yy mm jj` (1-3), pas u16 (4), distance u16 (6), calories u16 (8), objectif u16 (10), natation u16 (12) |

## Séquence de l'app (`AM4ViewModel`)

`identify` → `A2` (vérifie que le bracelet est associé au compte) → `02` (format horaire) →
`A4` → `05` → `A5` (profil) → `B7` (BMR) → `0A` → `AC` → `B0` → `BF`.

## Séquence de l'intégration

Toutes les 15 min au plus, tant que le bracelet émet :
auth → `B4` → `A4` → `BF` → *(si l'historique est activé)* `0A` → `AC` → `B0` → déconnexion.

## Inconnues à vérifier sur le vrai bracelet

- Le bracelet efface-t-il ce qu'il a transmis après l'écho de *fin* ? Probable (mémoire
  limitée), d'où l'historique coupé par défaut.
- Sens des 4 octets retirés des messages *début* et des 2 octets inconnus des en-têtes.
- Réponse du bracelet à `AC` / `B0` / `0A` quand il n'a rien : *fin* directe supposée.
