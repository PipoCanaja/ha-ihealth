# Protocole BLE du iHealth PO3M

Reconstitué à partir de l'application Android iHealth (`com.ihealth.sdk.command.po3m.Po3mCommand`,
`AuthCommand`, `base.flow.ProtocolWrapper`, `ProtocolParser`, `communication.ins.XXTEA2`).
Implémentation : [`custom_components/ihealth/po3m/protocol.py`](../custom_components/ihealth/po3m/protocol.py).

## GATT

| Rôle | UUID |
|---|---|
| Service (annoncé, nom contenant « Pulse Oximeter ») | `0000fc83-0000-1000-8000-00805f9b34fb` |
| Écriture (app → PO3M), avec réponse | `7265632e-6a69-7561-6e2e-504f56313200` (« rec.jiuan.POV12 ») |
| Notification (PO3M → app), CCCD 0x2902 | `7365642e-6a69-7561-6e2e-504f56313200` (« sed.jiuan.POV12 ») |

L'application ne demande pas de MTU : les trames reçues peuvent être fragmentées en
notifications de 20 octets et doivent être réassemblées grâce au champ longueur.

## Trame

```
 0      1..2        3        4 .. 4+L-1        +4        +4        +1
B0/A0 | L (LE16) | hdr | cmd + données (L) | TAG (4) | CNT (4) | somme
```

- `B0` = app → PO3M, `A0` = PO3M → app ; longueur totale = L + 13 ;
- `hdr` = `(L_lo ^ L_hi) & 0xFE`, bit 0 = chiffré AES-CTR ;
- TAG = CRC-32 standard (zlib) de `cmd + données`, petit-boutiste ;
- chiffré : `cmd + données + TAG` passe en AES-CTR, CNT = compteur (LE32) ;
  en clair : CNT = `00000000` ;
- somme = somme de tous les octets précédents & 0xFF.

Seules `FA` / `FB` sont en clair (au sens CTR) ; tout le reste est chiffré, `FC` compris.

## Authentification

1. app → `FA` + R1 (16 octets aléatoires 0..127).
2. PO3M → `FB` : les 64 octets `cmd…TAG` sont chiffrés AES-CBC (NoPadding) avec
   `Key1 = R1[0:8] + "com.jiua"`, `IV1 = R1[8:16] + "n.v1.0.0"`. Contenu déchiffré :
   `FB | R2 (16) | R1' (20) | ID (16) | 7 × 00 | CRC32 (4)`.
3. Avec la clé fixe de l'application
   `APP_KEY = 16 52 12 A2 BD 0B 09 3F 2D 1E 96 8D 99 D4 3C BF` :
   - `K = XXTEA(ID, APP_KEY)` (20 octets ; XXTEA « iHealth » : longueur ajoutée en dernier mot) ;
   - vérifier `XXTEA⁻¹(R1', K) == R1` ;
   - `R2' = XXTEA(R2, K)` ;
   - `Key2 = AES-ECB(K[0:16]).encrypt(R2'[0:16])` ;
   - `nonce = R2[0:12]`, `Count_Init = LE32(R2[12:16])`.
4. app → `FC` + R2' (chiffré CTR). PO3M → `FD` (succès) ou `FE` (échec).

## AES-CTR

Bloc compteur = `nonce (12) || BE32(compteur)`, +1 par bloc de 16 octets.

- Envoi : `start = Count_Tx + 1`, puis `Count_Tx = start + ⌈(L+4)/16⌉`. Le CNT de la trame vaut `start`.
- Réception : le CNT peut désigner le premier ou le dernier bloc ; l'application essaie
  `CNT − blocs + 1` puis `CNT`, et garde celui dont le TAG est correct.

## Commandes

| Code | Requête | Réponse (données après l'octet commande) |
|---|---|---|
| `20` | lire l'heure | AA MM JJ hh mm ss (AA = année − 2000) |
| `21` | régler l'heure : AA MM JJ hh mm ss | — |
| `22` | batterie | `[état][niveau %]` |
| `24` | (non sollicitée) | `[état batterie]` |
| `25` | infos | état, niveau, heure (6), puis si ≥ 94 octets : protocole@8, nom@24, firmware@40 (3 car.), matériel@43, fabricant@46, modèle@62, série@78 (chaînes de 16) |
| `40` | nombre de mesures en mémoire | LE16 |
| `41` | lire la mémoire (à répéter) | `[?][suite ≠ 0]` + n × 12 : date (6), SpO2, pouls, PI×10, heure calée, 2 × réservé |
| `42` | effacer la mémoire | — |
| `50` | démarrer la mesure | accusé, puis flux `52` |
| `52` | (flux) | SpO2, pouls, force du pouls, AC (LE16), DC (LE16), 3 × onde (LE16) ; PI = round(AC/DC × 1000)/10 %, ignoré hors 0,2..20 |

Règles de l'application : trames `52` rejetées si SpO2 > 100 ou pouls > 250 ; résultat =
dernière trame `52` après 1,2 s sans nouvelle trame ; renvoi d'une commande toutes les
2 s, abandon après 3 essais.
