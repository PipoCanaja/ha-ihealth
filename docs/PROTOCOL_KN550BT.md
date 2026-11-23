# Protocole BLE du tensiomètre iHealth KN-550BT

Reconstitué à partir de l'application iHealth décompilée :

- `com.ihealth.communication.control.Bp550BTControl` (commandes exposées, restrictions par firmware)
- `com.ihealth.communication.ins.A1InSet_KN550BT` / `A1InsSetCommon` (trames, décodage)
- `com.ihealth.communication.ins.IdentifyIns` / `IdentifyIns2` (authentification)
- `com.ihealth.communication.base.protocol.BleCommProtocol` (transport « classique »)
- `com.ihealth.communication.base.protocol2.BleCommProtocol2` + `BleUnPackageData2` (transport « stream »)
- `com.ihealth.business.device.bp.viewmodle.Kn550BtOptimizationBaseViewModel` (déroulé d'une synchro, correction d'heure)

Code : `custom_components/ihealth/kn550bt/` (`protocol.py` sans dépendance HA, `client.py` bleak).

## Principe

Le KN-550BT **mesure seul** et garde les mesures en mémoire. L'application ne lance pas de
mesure : elle télécharge la mémoire. L'intégration fait de même.

## Profils BLE

Choisis selon le service annoncé (comme `iHealthDevicesManager`) :

| Service | UUID | Transport | Auth | Clé |
|---|---|---|---|---|
| `com.jiuan.BPAV21` | `636f6d2e-6a69-7561-6e2e-425041563231` | classique | IdentifyIns | `KN-550BT_BPAV21` |
| `com.jiuan.BPAV10` (+ nom contenant « 550 ») | `636f6d2e-…-425041563130` | classique | IdentifyIns | `KN-550BT` |
| `com.jiuan.dev` (+ nom « KN-550BT ») | `636f6d2e-…-646576000000` | stream | IdentifyIns2 | clé fixe `IdentifyIns2.n` |

Caractéristiques : écriture `7265632e-…` (« rec. »), notification `7365642e-…` (« sed. »),
même suffixe que le service. Les services BPAV10 et `dev` sont partagés avec d'autres
appareils (KD-926, KD-723, BG5S) : on les distingue par le nom.

Avant l'auth, l'app lit le *Device Information* : chaîne de protocole (`2a30` ou `ff01`)
et firmware (`2a26`). Le firmware conditionne plusieurs commandes (voir plus bas) ;
`A1InSet_KN550BT.f()` le convertit en nombre : `"2.0.1"` → 201.

## Transport

Toutes les commandes commencent par l'octet de type `A1`.

- **Classique** : identique au PO3 classique (`docs/PROTOCOL_CLASSIC.md`) — trames
  `B0/A0 | len | frag | seq | …`, fragmentation et **acquittement obligatoire** par
  fragment. Seule différence : l'ACK porte `A1` au lieu de `AC`.
- **Stream** : `B0 | n+2 | 00 | seq | charge | somme`, somme = octets 2…fin-1, `seq` part à 1
  et avance de 2. La trame est découpée en morceaux de 20 octets **sans acquittement**.
  En réception : `A0 | len | …`, longueur totale `len+3`, commande à l'index 5, données à
  partir de l'index 6, trames à somme fausse ignorées.

## Authentification

- **Classique** (IdentifyIns) : `A1 FA` + c(R1) → `A1 FB` + 48 octets (ID | R1' | défi) →
  `A1 FC` + `c(XXTEA(c(défi), XXTEA(c(ID), Ka)))` → `A1 FD` (OK) / `A1 FE` (échec).
  `Ka = XXTEA(d(getKey), d(F))` comme pour le PO3. Clé selon la chaîne de protocole :
  BPAV21/BPAV22 → `KN-550BT_BPAV21`, BPV23 → `BPWEIXIN`, sinon `KN-550BT`.
- **Stream** (IdentifyIns2, XXTEA2 petit-boutiste avec mot de longueur) : `A1 FA` + R1 (16) →
  `A1 FB` + 52 octets (graine R2 16 | R1' 20 | ID 16). `Ka = XXTEA2(ID, n)` ; l'appareil
  prouve son identité si `XXTEA2⁻¹(R1', Ka) == R1` ; réponse `A1 FC` + `XXTEA2(R2, Ka)` (20).
  La clé fixe `n` est égale à la Ka dérivée de `KN-550BT_BPAV21` (contrôle croisé).

## Commandes

| Cmd | Requête | Réponse | Notes |
|---|---|---|---|
| `20` | `A1 20 00 00 00` | `[batterie %]` (0 ou >100 → 100) | stream : refusé si fw = 2.0.0 ou chiffre de patch pair |
| `21` | `A1 21 aa mm jj hh mi ss` | capacités | **règle aussi l'horloge** (heure locale du téléphone) |
| `40` | `A1 40 01 00 00` | `[?, nombre]` | nombre de mesures en mémoire |
| `46` | `A1 46 01 00 00` | `[?, suite, enreg. 10 o…]` | format ancien ; `suite ≠ 0` → redemander |
| `4A` | `A1 4A 01 00 00` | `[?, suite, enreg. 11 o…]` | protocole BPAV21 |
| `47` | `A1 47 00 00 00` | — | « transfert terminé » : **efface la mémoire** |
| `60`/`61` | affichage (rétroéclairage, horloge) | | BPAV21 uniquement |
| `62` | `A1 62` | `aa mm jj hh mi ss` | horloge de l'appareil ; stream et fw > 2.0.0 uniquement |
| `38` | — | `[code]` | erreur (table `ERRORS` dans `protocol.py`) |

### Enregistrements

- **10 octets** (`46`) : `aa|arythmie<<7`, `mm|hsd<<7`, `jj`, `hh`, `mi`, `sys−dia`, `dia`,
  `pouls`, -, -. Pas de secondes.
- **11 octets** (`4A`) : `aa`, `mm`, `jj`, `hh`, `mi`, `ss`, `sys−dia`, `dia`, `pouls`,
  `schéma`, `drapeaux` — `0x80` arythmie, `0x10` mouvement, `0x01` **heure à corriger**
  (pris en compte seulement si fw > 2.0.0).

### Correction d'heure

Quand le drapeau `0x01` est présent, l'horloge de l'appareil n'était pas fiable au moment
de la mesure (p. ex. après un changement de piles). L'app lit l'horloge (`62`) en début de
session et recalcule : `heure réelle = heure enregistrée + (maintenant − horloge appareil)`.
L'intégration fait pareil (`fix_timestamp`) ; sans horloge lisible (profil classique),
l'heure enregistrée est gardée.

L'app a en plus une correction manuelle pour une fenêtre de dates connue par
version matérielle/firmware (`TimeRepairManualUtils`) ; elle n'est pas reproduite.

## Effacement de la mémoire

- firmware < 2.0.1 : l'app envoie `47` **automatiquement** après chaque téléchargement ;
- firmware plus récent : seulement sur « supprimer les données » (`transferFinished`).

L'intégration **n'envoie jamais `47`** : la mémoire reste intacte pour l'application du
téléphone. Les mesures déjà importées sont reconnues par leur horodatage brut et leurs
valeurs (les 400 dernières sont mémorisées).

## Déroulé d'une session (intégration)

connexion → Device Information → auth → batterie → horloge (`62`, si dispo) → nombre (`40`)
→ mémoire (`4A` ou `46`, pages jusqu'à `suite = 0` ; si pas de réponse, essai de l'autre
format) → mise à l'heure (`21`) → déconnexion. Après une synchro réussie, pas de nouvelle
session pendant 120 s (le tensiomètre continue d'émettre un moment après une mesure).
