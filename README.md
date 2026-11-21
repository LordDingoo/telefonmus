# Telefonmus 📱🖱️

Styr din Windows-PC's mus og tastatur fra din telefon over WiFi, og se et live-billede af skærmen.

Du skal ikke installere noget på telefonen. Du scanner en QR-kode, og så kører det i telefonens browser.

## Funktioner

- **Touchpad**
  - Flyt musen med én finger.
  - Tryk med én finger for at venstreklikke, og med to fingre for at højreklikke.
  - Scroll med to fingre eller langs højre kant af touchpad'en.
- **Knapper** til venstreklik og højreklik, og en **Hold**-knap til at trække ting rundt.
- **Tastatur**
  - Skriv tekst direkte på PC'en. Æ, ø, å og emoji virker også.
  - Knapper til slet og Enter.
- **Live skærmbillede**
  - Viser et zoomet udsnit omkring musen, så teksten kan læses.
  - Op til ca. 60 fps.
  - Sender kun billeder, når noget ændrer sig på skærmen.
- **Flere skærme**
  - Billedet følger musen over på alle skærme.
  - ⇆-knappen hopper musen til næste skærm.
- **Tryk på skærmbilledet** for at flytte musen derhen.

## Kom i gang

### Den nemme måde: hent exe'en

1. Hent `Telefonmus.exe` fra [Releases](../../releases).
2. Dobbeltklik på den. Der åbner et lille vindue med en QR-kode.
   - Windows SmartScreen advarer måske, fordi filen ikke er signeret. Vælg **"Flere oplysninger" → "Kør alligevel"**.
3. **Første gang:** tryk på **"Tillad i firewall"** og sig ja til administrator-spørgsmålet. Ellers blokerer Windows forbindelsen fra telefonen.
4. Scan QR-koden med telefonens kamera. Telefonen skal være på **samme WiFi** som PC'en.

Tip: læg siden på telefonens hjemmeskærm. Adressen er den samme hver gang, så længe PC'en beholder sin IP-adresse.

### Kør fra kildekoden

Kræver Windows og Python 3.10 eller nyere.

```
pip install qrcode pillow
python telefonmus.py
```

Byg din egen exe med `build.bat`. Den kommer ud i `dist\Telefonmus.exe`.

## Sikkerhed

- **Kun din egen PC.** Telefonmus kører kun på din egen computer og dit eget lokale netværk. Der er ingen server på internettet, og der er ingen konto.
- **Hver installation har sin egen kode.** Første gang programmet starter, laver det en tilfældig hemmelig kode og gemmer den i `%APPDATA%\Telefonmus\kode.txt`. Koden står i adressen og i QR-koden, og uden den afviser programmet alle forbindelser.
- **Del ikke QR-koden eller adressen.** Alle, der har den og er på samme netværk, kan styre din mus og se din skærm. Hvis koden er sluppet ud, så slet `kode.txt`. Så laver programmet en ny næste gang det starter.
- **Forbindelsen er ikke krypteret.** Den kører over almindelig `http` på dit lokale netværk. Brug ikke programmet på offentlige netværk, du ikke stoler på, som café- eller hotel-WiFi.
- **Firewall-reglen** åbner TCP-port 8765. Du kan fjerne den igen med denne kommando i en administrator-terminal:
  ```
  netsh advfirewall firewall delete rule name="Telefonmus"
  ```

## Fejlfinding

| Problem | Løsning |
|---|---|
| Telefonen kan ikke åbne siden | Tjek at telefonen og PC'en er på samme WiFi, og tryk på "Tillad i firewall". Nogle routere har "AP-isolering" eller et gæstenetværk, der forhindrer enheder i at tale med hinanden. |
| Der står "Afbrudt – prøver igen…" | Er programmet lukket, eller er PC'ens IP-adresse ændret? Scan QR-koden igen. |
| Noget andet | Se logfilen i `%APPDATA%\Telefonmus\log.txt`. |

## Sådan virker det

`telefonmus.py` er en lille webserver, der kun bruger Pythons standardbibliotek, plus `qrcode` og `Pillow`.

- **Telefonsiden.** Serveren sender siden `pad.html` til telefonens browser.
- **Forbindelsen.** Telefonen og PC'en taler sammen over en WebSocket.
- **Mus og tastatur.** Bevægelser og tastetryk bliver omsat til rigtige mus- og tastaturhændelser med Windows' `SendInput`.
- **Skærmbilledet.** Programmet tager billede af udsnittet med GDI, laver det til JPEG og sender det til telefonen. Der er højst 3 billeder på vej ad gangen, så der ikke hober sig forsinkelse op.

## Licens

[MIT](LICENSE)
