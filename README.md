# Brigarde Init Panel

Ein lokales Webpanel mit einem echten DayZ-Missionsskript in Enforce Script. Es zeigt Spieler und Fahrzeuge im X/Z-Raster der laufenden Mission und kann unterstützte Befehle über das Missionsskript ausführen.

**Für eine eigene DayZ-Dedicated-Testmission mit ausführbarer `init.c`.** Auf dem Nitrado-PlayStation-Dienst `18237193` ist eigene Skriptausführung weiterhin nicht als unterstützte Option belegt. Die Dateien dieses Pakets aktivieren diese Berechtigung nicht. [Nitrado-Konsoleneinstellungen](https://server.nitrado.net/de-DE/guides/einstellungen-dayz-konsole/) · [CoDCrafted README](https://github.com/CoDCrafted/Web-Based-DayZ-Admin-Panel/blob/09439eba6bf63ca7680b4a25a385919f737e8123/README.md)

## Lokal starten

Installiere Python **3.10 oder neuer**. Für das Panel sind keine Python-Pakete, Datenbanken oder Nitrado-Zugangsdaten erforderlich.

1. Entpacke das ZIP und öffne ein Terminal im Ordner `Brigarde_Init_Panel`.
2. Starte das Panel.

   ```bash
   python brigarde_init_panel.py
   ```

   Falls dein System Python als `python3` bereitstellt, verwende `python3 brigarde_init_panel.py`.
3. Öffne [http://127.0.0.1:8787](http://127.0.0.1:8787) im Browser. Unter **Verbindung** kannst du die Missionskonfiguration herunterladen. Wenn der DayZ-Testserver auf demselben Gerät läuft, lass die lokale Vorgabe `http://127.0.0.1:8788` stehen.

| Adresse | Zweck |
|---|---|
| `127.0.0.1:8787` | Browserpanel, Konfiguration und Aufträge |
| `127.0.0.1:8788` | Missionsverbindung über `POST /dayz/poll` und Erreichbarkeit über `GET /health` |

Der Python-Prozess muss nur während der Nutzung laufen. Mit `Strg+C` beendest du ihn. Beim ersten Start legt das Programm in `data/bridge.key` einen eigenen zufälligen Verbindungsschlüssel an. Die exportierte JSON enthält denselben Schlüssel. Halte beide Dateien privat und außerhalb geteilter Quellen. Für einen neuen Schlüssel beendest du das Panel, benennst `data/bridge.key` um, startest erneut und exportierst eine neue Missionskonfiguration.

## In eine Testmission einsetzen

Der PC-Dedicated-Server muss bereits mit der gewünschten Karte startbar sein. Nutze eine Kopie seiner Missionsdateien und eigene Test-Spielstände. Bei einer normalen PC-Installation liegt der Missionsordner unter `mpmissions/`. Maßgeblich ist der Ordner der im Dedicated-Server ausgewählten Mission.

1. Wähle die Kopie als Testmission. Sichere deren bisherige `init.c`.
2. Kopiere aus diesem Paket `mission/init.c` in die Wurzel der Testmission und `mission/custom/brigardeAdmin.c` in deren `custom/`.
3. Lade im Panel unter **Verbindung** die Datei `BrigardeInitPanel.json` herunter. Lege sie unter dem exakten Namen in demselben `custom/`-Ordner ab. Die Testmission enthält danach diese drei relativen Pfade.

   - `init.c`
   - `custom/brigardeAdmin.c`
   - `custom/BrigardeInitPanel.json`

4. Starte deinen Testserver. Im Script-Log muss `[Brigarde Init Panel] Mission script loaded.` erscheinen. Beim ersten erfolgreichen Abruf erscheint `Connected to the panel.` und der Browser zeigt **Mission verbunden**.
5. Führe im Browser **Verbindung testen** aus. Warte im Auftragsverlauf auf **Ausgeführt · Antwort der Mission erhalten**. Tritt der Testmission danach mit einem Charakter bei und teste zuerst einen Verband. Nutze für den Teleport und einen Fahrzeugspawn eine freie, trockene Fläche.

Die mitgelieferte `init.c` initialisiert die Central Economy und die Bridge für einen Dedicated-Server. Sie ist eine vollständige Startdatei für die Testmission. Hat deine Mission eigene Starterausrüstung, Spawnregeln oder andere Hooks, behalte für diese Mission deren Initialisierung und integriere den Include, das Bridge-Feld und die Start-/Stoppaufrufe aus `BrigardeTestMission` in ihre vorhandene Missionsklasse.

## Externe Testinstanz zeitweise verbinden

Bei einer externen **skriptfähigen** Testinstanz braucht deren DayZ-Prozess während der Nutzung einen HTTPS-Weg zur Bridge. Ein temporärer Cloudflare Quick Tunnel kann diesen Weg vom eigenen Gerät aus bereitstellen. Er ist für Tests ausgelegt. [Cloudflare Quick Tunnels](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/)

1. Starte das Python-Panel auf deinem Gerät.
2. Installiere `cloudflared` gegebenenfalls von Cloudflare und starte in einem zweiten Terminal einen Tunnel zum Bridge-Port.

   ```bash
   cloudflared tunnel --url http://127.0.0.1:8788
   ```

3. Kopiere die HTTPS-Basisadresse, die Cloudflare ausgibt, in das Feld **Bridge-Adresse** im Panel. Der Aufruf von `/health` unter dieser Adresse liefert bei erreichbarer Bridge die Protokollversion.
4. Lade die Missionskonfiguration erneut herunter und lege sie im `custom/`-Ordner der Testmission ab. Das laufende Skript liest die Datei regelmäßig neu. Ändert sich die Tunneladresse beim nächsten Start, wiederhole den Export mit der neuen HTTPS-Adresse.
5. Beende nach der Nutzung den Tunnel und den lokalen Python-Prozess. Das Browserpanel bleibt auf Port `8787`. Der Tunnelbefehl verwendet ausschließlich Port `8788`.

Das Paket kann auch auf einem eigenen Python-Host mit HTTPS-Reverse-Proxy für Port `8788` laufen. Für den Zugang zum Bedienpanel auf dem Host eignet sich eine lokale SSH-Portweiterleitung zu dessen Port `8787`. GitHub dient als Versionsverwaltung für die Quelldateien. GitHub Pages liefert statische Seiten aus und kann den Python-Prozess nicht ausführen. [GitHub Pages](https://docs.github.com/en/pages/getting-started-with-github-pages/creating-a-github-pages-site)

## Funktionen der ersten Version

| Aktion | Verhalten |
|---|---|
| Verbindung testen | Das Missionsskript beantwortet einen Testauftrag mit `pong`. |
| Spieler und Positionen | Das Missionsskript sendet rund alle 2,5 Sekunden Namen, Positionen und Status verbundener Charaktere. Das Panel zeigt das Alter des letzten Abrufs und nutzt die vom Spiel gemeldete Kartengröße. |
| Nachricht | Ingame-Benachrichtigung an den ausgewählten Spieler. |
| Teleport | Verschiebt einen lebenden Charakter an die Geländehöhe der angegebenen X/Z-Position. Bei Wasserflächen und Charakteren in Fahrzeugen oder auf Leitern lehnt die Mission den Auftrag ab. |
| Item geben | Legt zwischen einem und acht Items aus dem mitgelieferten Katalog ins Spielerinventar. Eine teilweise Ausführung wird mit der tatsächlichen Anzahl gemeldet. |
| Fahrzeug erzeugen | Unterstützt Ada 4x4, Olga 24, Gunter 2 und Sarka 120. Die Mission verwendet die jeweilige Fahrzeugroutine für Anbauteile und Flüssigkeiten und setzt das Fahrzeug als Testobjekt ohne Weltpersistenz. |
| Testfahrzeug entfernen | Die Liste umfasst die in dieser Missionssitzung über das Panel erzeugten Fahrzeuge. Die Mission lehnt das Entfernen eines besetzten Fahrzeugs ab. |

Das Panel zeigt Aufträge als **Wartet**, **Übermittelt**, **Ausgeführt**, **Fehlgeschlagen**, **Abgelaufen** oder **Ausführung offen**. Es sendet einen übermittelten Auftrag bei ausbleibender Quittung nicht automatisch erneut. Prüfe bei **Ausführung offen** zuerst die Spielwelt. Aufträge und Positionsdaten liegen im Arbeitsspeicher und beginnen beim Neustart des Panel-Prozesses neu.

## Dateibestand und Prüfstatus

- `brigarde_init_panel.py` startet das lokale Panel und die geschützte Bridge.
- `panel.html` enthält Oberfläche, X/Z-Raster und Formularlogik ohne externe Ressourcen.
- `mission/init.c` und `mission/custom/brigardeAdmin.c` sind die vollständigen Missionsskripte.
- Der lokale Start, die HTTP-Zugriffstrennung und der Auftragsfluss wurden mit einem simulierten Missionsclient geprüft. Die JavaScript-Bedienlogik wurde mit einem simulierten DOM getestet. Ein Enforce-Script-Compiler und ein DayZ-Dedicated-Server standen für die Prüfung nicht zur Verfügung. Der Kompilier- und Ingame-Test erfolgt auf der eigenen Testinstanz.

Die Bridge basiert auf den veröffentlichten Bohemia-Schnittstellen für REST, JSON, Spielerpositionen und Fahrzeugerzeugung. Die Architektur orientiert sich an der Grundidee des CoDCrafted-Panels. [Bohemia REST-API](https://github.com/BohemiaInteractive/DayZ-Script-Diff/blob/main/scripts/3_game/http/restapi.c) · [Bohemia JSON](https://github.com/BohemiaInteractive/DayZ-Script-Diff/blob/main/scripts/3_game/tools/jsonfileloader.c) · [Bohemia Game API](https://github.com/BohemiaInteractive/DayZ-Script-Diff/blob/main/scripts/3_game/global/game.c)
