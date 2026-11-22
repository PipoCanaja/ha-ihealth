# Protocole BLE du iHealth PO3 « classique » (profil POV11)

Certains PO3 (firmware ancien) exposent, au lieu du profil « POV12 » décrit dans
[PROTOCOL.md](PROTOCOL.md), un profil plus ancien avec **une seule caractéristique**
servant à la fois à l'écriture et aux notifications.

Reconstitué depuis `AcInsSet`, `IdentifyIns`, `POMethod`, `XXTEA`,
`BleCommProtocol` et `BleUnPackageData` de l'application iHealth.
Implémentation : [`custom_components/ihealth/po3classic/protocol.py`](../custom_components/ihealth/po3classic/protocol.py).

## GATT

| Rôle | UUID |
|---|---|
| Service annoncé | `636f6d2e-6a69-7561-6e2e-504f56313100` (« com.jiuan.POV11 ») |
| Écriture + notification | `7274782e-6a69-7561-6e2e-504f56313100` (« rtx.jiuan.POV11 ») |

## Trame

```
 0       1      2        3      4    5        ...        n
B0/A0 | len  | frag  | seq  | AC | cmd | données… | somme
```

- `len` = nombre d'octets après l'octet `len` ; longueur totale = `len + 3` ;
- `frag` = 0 (trame simple) ou `0xF0` (dernière d'un flux) ; sinon fragment :
  `(nombre−1) << 4 | index`, les fragments partant de l'index le plus haut ;
- chaque trame (et chaque fragment) répète l'octet `AC` (0xAC) en position 4 ;
- messages > 15 octets découpés en fragments de 14 octets de données ;
- `somme` = somme des octets de la position 2 à l'avant-dernier, & 0xFF ;
- **acquittement** : à chaque trame reçue dont `frag` ≠ 0xF0, l'autre partie renvoie
  une trame de 6 octets `B0 03 (0xA0+index) (seq+1) AC somme`. L'appareil attend cet
  acquittement avant d'envoyer le fragment suivant.

Le compteur `seq` commence à 1 et augmente de 2 à chaque trame.

## Authentification

1. app → `AC FA` + `c(R1)` — R1 = 16 octets aléatoires (valeur absolue), `c()` inverse
   chaque mot de 4 octets.
2. appareil → `AC FB` + 48 octets : `ID(16) | R1'(16) | défi(16)`.
3. l'app calcule, avec la clé `Ka` dérivée de la bibliothèque native :
   - `Ka = XXTEA(d(getKey("PO3")), d(F))` — constante ;
   - `clé = XXTEA(c(ID), Ka)` ;
   - réponse = `c(XXTEA(c(défi), clé))`.
4. app → `AC FC` + réponse(16). Appareil → `AC FD` (accepté) ou `AC FE` (refusé).

`getKey("PO3")`, `d()` (inversion des quartets) et `F` proviennent de `libiHealth.so`
et de `IdentifyIns`. `XXTEA` ici est la variante « bloc entier, gros-boutiste » (différente
de l'AES-CTR du profil POV12). Le calcul complet a été vérifié contre le code Java d'origine.

## Commandes (après authentification)

| Code (après AC) | Requête | Réponse |
|---|---|---|
| `C1` | batterie | `[niveau %]` |
| `A2` | régler l'heure : AA MM JJ hh mm ss | `A2` |
| `C8` | lire l'heure | `AA MM JJ hh mm ss` (mois sur le quartet bas) |
| `C6` | infos appareil | `[état batterie]` |
| `A5` | démarrer la mesure | `A6` (prêt) → l'app renvoie `AC A6` → flux `A7` |
| `A7` | (flux) | SpO2, pouls, force, AC (LE16), DC (LE16), 3 × onde (LE16) ; PI = round(AC/DC × 1000)/10 % |
| `A9`/`AA`/`AB`/`AC`/`AD` | mémoire | enregistrements datés (import non encore implémenté) |

Résultat de la mesure : dernière trame `A7` valide reçue après 1,2 s sans nouvelle trame.
