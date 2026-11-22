# Clés d'authentification iHealth (extraites de `libiHealth.so`)

Extraites de la fonction native `GenerateKap.getKey(<type>)` (ARM aarch64,
BuildID `1328e02a720e0f801c4c1b500229d3132a11c1a3`), désassemblée avec `llvm-objdump`.

Pour chaque type d'appareil :
- **getKey** = les 16 octets renvoyés par la fonction native ;
- **Ka** = clé de session dérivée, `XXTEA(d(getKey), d(F))`, avec `d()` = inversion des
  quartets et `F = "Ch/HQ4LzItYT42s="`. C'est la clé utilisée par `IdentifyIns.deciphering`.

Le calcul `getKey → Ka` a été vérifié à l'octet près contre le code Java pour PO3.
Les `Ka` des autres appareils sont calculés par la même formule mais non vérifiés individuellement.
La dérivation `Ka` suppose le chemin d'authentification classique (`IdentifyIns`) ; certains
appareils récents utilisent un autre schéma (par ex. le profil POV12 en AES-CTR). En cas de doute,
la donnée de référence reste les octets **getKey** bruts extraits de la bibliothèque.

| Type | getKey (natif) | Ka (dérivée) |
|---|---|---|
| `BP3M` | `487679762de525d6a397d3a9c0b4dd26` | `7eae8738826228230cd4bae097b6c503` |
| `BP5` | `487679762de525d6a397d3a9c0b4dd26` | `7eae8738826228230cd4bae097b6c503` |
| `BP7` | `487679762de525d6a397d3a9c0b4dd26` | `7eae8738826228230cd4bae097b6c503` |
| `BPweixin` | `70e3fb742028d21a8ef30d28c1fa7127` | `92e0835bf97fc9a3aabd466a1898e983` |
| `BPW3` | `78ba554272051828b7a6b0ef7243da5f` | `3ab2d317bdf93575673bfa9626a59922` |
| `ABPM` | `78ba554272051828b7a6b0ef7243da5f` | `3ab2d317bdf93575673bfa9626a59922` |
| `ABP100` | `78ba554272051828b7a6b0ef7243da5f` | `3ab2d317bdf93575673bfa9626a59922` |
| `BP3L` | `eacfdf093dcc98f4ba7740bf30a00d92` | `ff2c525a5a3cd3e8c7d9b591a6adcb34` |
| `BP5S` | `65d2bf0206435f00baaf3d5ed32bbd70` | `7da13aba5b31bb68767994c4d7992c01` |
| `BP7S` | `5835d27714e6e2da4de3795f6565ceb4` | `37d0db86635359c0e51227b543f39c1b` |
| `550bt` | `5835d27714e6e2da4de3795f6565ceb4` | `37d0db86635359c0e51227b543f39c1b` |
| `KN-550BT` | `5835d27714e6e2da4de3795f6565ceb4` | `37d0db86635359c0e51227b543f39c1b` |
| `KD926` | `5835d27714e6e2da4de3795f6565ceb4` | `37d0db86635359c0e51227b543f39c1b` |
| `KD723` | `5835d27714e6e2da4de3795f6565ceb4` | `37d0db86635359c0e51227b543f39c1b` |
| `KD-926` | `5835d27714e6e2da4de3795f6565ceb4` | `37d0db86635359c0e51227b543f39c1b` |
| `KD-723` | `5835d27714e6e2da4de3795f6565ceb4` | `37d0db86635359c0e51227b543f39c1b` |
| `AM3` | `6918b8b2980d8a16010ce4079fc33686` | `460773f107d006059a50ea1a2c5db5b0` |
| `AM3S` | `1e6ea723573048c02ddc320a0e336595` | `376ad1a9227ce32cff03ee31bdec6f5e` |
| `AM4` | `1967a5449c30c3bd20dec808f8ea4d34` | `995a085c06eaa5dcf2f4da075df4804b` |
| `HS4` | `5a640e94c760d7a153fcbb6ac93580f1` | `8af0c35d7eabbd8284ebcf78a32bd98e` |
| `HS4S` | `5a640e94c760d7a153fcbb6ac93580f1` | `8af0c35d7eabbd8284ebcf78a32bd98e` |
| `HS2` | `f29e081d7f416675093efe02fc5c0925` | `c0ec1d05fac2cd03b337cc8f79787b4d` |
| `HS5` | `52119af4c1cefa89858ce846260ba8c1` | `b2db8a301e33e6bac30d42d94b26e853` |
| `PO3` | `bf4b051142d270a3932d2daacca9bd1e` | `faa35392cd6f290f33594bcc15bfc725` |
| `BG5` | `1108781187f7f1d5f10e35f87a3bcb98` | `ffd57a6575487e7163cded4b5d93119c` |
| `BG5L` | `48e05e3231bbc447a066d8e9a2927b4e` | `748fa3a542cb02a746ffbfe07c61b866` |
| `BG1304` | `cdd03d65291f60b43f5aeae0c051578d` | `62f7c2b5324d88d8c6777180f35f347b` |
| `BG1305` | `2944688c0d2ec8326f5fa3c687d5b44e` | `b66bac29585690a2bad3fb2b9be6a34c` |
| `BPabi` | `5db63849fe118fe7f9364b9b4f235ba0` | `2a64ba194f51505b26b7e305fea5917f` |
| `BGAG680` | `0d78e515421ee2e02fdbaf9aaf7fbc02` | `9e982a1b558d6665b5e84e16310d655c` |
| `KN-550BT_BPAV21` | `b51dfe0438ba4f897837979124e23fcc` | `19010796f2231a688b5434628c57eb19` |
