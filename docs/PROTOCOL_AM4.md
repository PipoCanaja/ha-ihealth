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

**Numéro de séquence (vérifié sur le vrai bracelet, fw 137).** Comme dans
`BleCommProtocol.c()`, le numéro de la trame suivante de l'app n'est pas un compteur
indépendant : après l'acquittement d'une trame reçue de séquence `s` et d'index `i`, il
vaut `i×2 + (s+1) + 2`, soit **séquence du dernier fragment + 3** ; après une trame `F0`
(non acquittée) il vaut `s+1`. Le bracelet fait de même à partir des nôtres (notre `B4` en
`09` → sa réponse en `0C`). Pour une commande isolée il accepte un autre numéro, mais **au
milieu d'un transfert fragmenté il ignore un écho mal numéroté** et renvoie le message en
boucle (capture : dernier fragment `AD` en `22`, écho attendu en `25`, écho envoyé en `19`
ignoré). Le transport suit donc ce calcul pour l'AM4 (`follow_seq`).

**Réémission (vérifié).** Des écritures se perdent (rafales de 2 ACK + écho en moins de
1 ms via un proxy ESPHome) : le bracelet réémet alors ses derniers fragments, puis le
message entier avec de nouveaux numéros. Comme `BleCommProtocol` (minuteur : 1 s, puis
toutes les 0,5 s, 3 fois au plus), l'intégration garde chaque trame de commande jusqu'à
l'ACK du bracelet (`A0 03 xx seq+1 AA`) et la renvoie sinon ; 10 ms entre deux écritures.
Un message réémis après un écho perdu est renvoyé en écho mais pas stocké deux fois.

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

Commandes de réglage (envoyées **uniquement** par le bouton « Réinitialiser après
collecte », voir plus bas) — chaque commande d'écriture reprend la disposition de la
réponse de lecture correspondante :

| Lecture → réponse | Écriture | Contenu |
|---|---|---|
| `A2` → `[id u32]` | `A3 [id u32]` | identifiant du compte iHealth (seul critère d'association pour l'app) |
| `B6` → 19 octets (+2 dès fw 138) | `A5` mêmes octets | `âge pas_cm taille_cm sexe(0 = H) kg kg/10 unité objectif u32, objectif/2 u32, objectif/4 u32` (+ objectif natation `hh mm`) |
| — | `B7 [bmr u16]` | métabolisme de base, envoyé juste avant `A5` |
| `01` → `[mode]` | `02 [mode]` | format horaire (0..5) |
| `B3` → `[hh mm actif]` | `AA` mêmes octets | rappel d'inactivité (minutes u16 si l'octet 2 > 59) |
| `A6` → `[n][id…]`, `A7 [id]` → `[id hh mm répét jours actif]` | `A8` mêmes 6 octets | alarmes (jours : bit 0 = dimanche) |
| `10` → `[n]` | `11 [n]` | « picture » (écran affiché ? non vérifié) |
| — | `A1 FF FF FF FF` → `A1 [n]` | réinitialisation usine : efface historique, réglages et identifiant |

BMR (`AmControlBase.a()`) : Harris-Benedict révisée, arrondi au plus proche, moins 2 —
homme `88,362 + 13,397·kg + 4,799·cm − 5,677·âge`, femme
`447,593 + 9,247·kg + 3,098·cm − 4,33·âge`. Le facteur de niveau d'activité, tel que
décompilé, vaut toujours 1.

Jamais envoyées : `09` (code d'appairage), `06` (natation), `A9` (suppression d'alarme), `B5`
(mode).

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

`identify` → `A2` (vérifie que le bracelet est associé au compte : sinon il est présenté
comme non associé) → `02` (format horaire) → `A4` → `05` → `B7` (BMR) → `A5` (profil) →
`0A` → `AC` → `B0` → `BF`. L'app réécrit donc heure, format horaire et profil à chaque
connexion.

## Appairage par l'app (`AMSendRandomActivity`, `AM4SendRandomViewModel.c()`)

1. `09 d1..d6` : 6 chiffres aléatoires choisis par l'app, affichés par le bracelet ;
   l'utilisateur recopie les 2 derniers, la vérification est faite **dans l'app** (le
   bracelet ne contrôle rien).
2. `AC` puis trois étapes non visibles dans l'archive (classe
   `androidx.constraintlayout.core.state.a` absente).
3. `A3` identifiant du compte → liaison côté cloud (`ambinding`).
4. `B7` + `A5` (profil, objectif quotidien, défaut 10 000), `02` (format horaire du
   téléphone), trois autres étapes non visibles, puis `amDeviceBind` (cloud).

Après un `A1` envoyé par l'app, celle-ci retire aussi le bracelet du compte dans le cloud
(`amunbinding`). Un `A1` envoyé par l'intégration ne touche pas au cloud : réécrire le même
identifiant suffit pour que l'app reconnaisse le bracelet.

## Réinitialisation par l'intégration (bouton « Réinitialiser après collecte »)

À la session qui suit l'appui : téléchargement complet (`0A`, `AC`, `B0` tous allés au bout,
sinon c'est reporté) → lecture `A2`, `B6`, `01`, `B3`, `10`, `A6`/`A7` (rien n'est fait si
l'identifiant vaut 0) → sauvegarde sur disque → `A1` → `A3` → `A4` → `B7` → `A5` → `02` →
`AA` → `A8`… → `11` → `A2` (doit redonner l'identifiant). Si la session est coupée après
`A1`, la réécriture est refaite en début de session suivante.

## Séquence de l'intégration

Toutes les 15 min au plus, tant que le bracelet émet :
auth → *(attente de la fin d'un transfert repris par le bracelet)* → `B4` → `A4` → `BF` →
*(historique activé, au plus toutes les 3 h)* `0A` → `AC` → `B0` → attente des derniers ACK →
déconnexion.

## Constats sur le vrai bracelet (fw « 137 », lu tel quel sur `2A26`, sans points)

- Authentification OK avec la clé AM4. Juste après la connexion, le lien reste parfois
  bloqué dans le sens app → bracelet (il acquitte le 1er fragment du `FA` puis plus rien,
  ni réémissions ni nouveaux `FA`, jusqu'à la déconnexion). Réessayer sur le même lien n'a
  jamais marché, une nouvelle connexion oui : l'intégration abandonne en 3 s et se
  reconnecte 5 s plus tard.
- Écritures **sans réponse**, comme l'app (la caractéristique offre les deux). Essai avec
  réponse via un proxy ESPHome : chaque écriture attend un aller-retour, les ACK arrivent
  après le délai de réémission du bracelet (~400 ms) et il renvoie tout en boucle.
- Le bracelet réémet souvent un message **avec les mêmes numéros** : il est acquitté mais
  pas traité une seconde fois. Attention, la répétition se reconnaît à la trame **identique**
  (numéro *et* contenu) : le bracelet numérote le message suivant à partir de l'écho qu'il a
  accepté, qui peut être antérieur au dernier envoyé, et réutilise alors les numéros du
  message précédent (capture du 24/11 00:13:36 : message `0c26…` en 0x22→0x2C, puis `0d1c…`
  en 0x26→0x30). Comparer les seuls numéros faisait ignorer le nouveau message.
- Dès qu'un nouveau message arrive, les échos encore en attente d'ACK sont abandonnés : le
  bracelet n'avance qu'après avoir accepté un écho, et un vieil écho (`AA AD`, toujours
  identique) renvoyé pourrait passer pour celui du message en cours.
- Un transfert interrompu reprend à la connexion suivante : le bracelet renvoie de lui-même
  le dernier message non acquitté (ici un `AD`) juste après l'authentification.
- `B4` → `01 09` (activité, 100 %) ; `A4` répond `00 01 01 00 31 06` ; `BF` → zéro pas.
- `0A` : `0B 00 00` puis `0D` directement quand il n'y a pas de rapport.
- `AC` : `AB` = `19 08 1b 4c 08 06` + 4 octets (27/08/2025, pas de 76 cm), puis `AD` en 5
  fragments de 7 points (23:25 → 23:56, 9335 pas cumulés, 414 kcal) : le bracelet garde
  donc des jours anciens non synchronisés.

- Après plusieurs transferts interrompus, le bracelet peut **cesser d'émettre** (plus aucune
  annonce, rien dans HA). Un redémarrage le débloque sans rien effacer : le poser sur son
  chargeur USB et maintenir le bouton du chargeur ~10 s jusqu'à « Restart Complete ».

- Débit constaté (proxy ESPHome, écritures sans réponse + réémission) : ~20 s par jour
  d'activité (≈ 30 messages `AD` de 10 points). Le bracelet envoie les jours du plus ancien
  au plus récent ; ceux d'août 2025 étaient encore en mémoire en novembre 2026.

- **Le bracelet n'efface pas** ce qu'il a transmis, même après un transfert complet dont
  l'écho de fin (`AE`) a été acquitté : la session suivante renvoie tout depuis le plus
  ancien jour (27/08/2025). D'où l'historique limité à un téléchargement toutes les 3 h.
- Un transfert coupé (déconnexion) **reprend tout seul** juste après l'authentification
  suivante ; les commandes envoyées pendant ce temps restent sans réponse (capture du 24/11
  00:55 : `BF` perdu). L'intégration attend la fin du transfert repris avant ses commandes.
- Une « nuit » est ouverte (`AF` avec la date/heure du moment) même sans mesure : en-tête
  seul, ignoré.

## Inconnues à vérifier sur le vrai bracelet

- Capacité de la mémoire et ce qui se passe quand elle est pleine (anneau ?).
- Sens des 4 octets retirés des messages *début* et des 2 octets inconnus des en-têtes.
- Réponse du bracelet à `AC` / `B0` quand il n'a rien (pour `0A` : *début* vide puis *fin*).
