# Violet

[English](README.md) | [简体中文](README.zh-CN.md) | [繁體中文](README.zh-TW.md) | [日本語](README.ja.md) | [한국어](README.ko.md) | [Español](README.es.md) | [Français](README.fr.md) | **Deutsch** | [Русский](README.ru.md) | [Português](README.pt-BR.md)

Violet ist eine KI-Begleiterin, die auf deinem eigenen Server läuft. Sie hat eine beständige Persönlichkeit und einen eigenen Alltag, merkt sich, was du ihr erzählst, meldet sich von selbst, wenn der Moment passt, und du kannst mit ihr über WeChat, QQ und Telegram chatten. Alles wird in einem Web-Panel eingerichtet; es gibt keine Konfigurationsdateien zu bearbeiten.

> Inspiriert von [Lynnette177/Project-Eve](https://github.com/Lynnette177/Project-Eve) (von [Lynnette177](https://github.com/Lynnette177)). Violet hat den Großteil neu geschrieben und erweitert und ist jetzt ein eigenständiges Projekt. Danke an den ursprünglichen Autor für die Idee.

Die Oberfläche des Panels ist derzeit auf Chinesisch.

## Was sie kann

**Chat**

- WeChat (ClawBot / iLink, per QR-Code), ein persönliches QQ-Konto (SnowLuma, Anmeldung per Scan), ein offizieller QQ-Bot und Telegram. Mehrere können gleichzeitig verbunden sein.
- Sie antwortet nur dir, der Person, mit der sie verknüpft ist. Privatnachrichten von Fremden bleiben unbeantwortet, und ihr Text wird nicht gespeichert.
- Mit einem persönlichen QQ-Konto kann sie Gruppen beitreten: Sie mischt sich ein, wenn jemand ihren Namen sagt, sie mit @ erwähnt oder sie anstupst, und bringt ab und zu selbst ein Thema auf, wenn es in der Gruppe still wird. Mit der Zeit lernt sie die Leute in der Gruppe kennen. Sie kann sich auch Bilder in der Gruppe ansehen, Sticker sammeln und senden und in QQ Zone posten (sie zeigt dir den Beitrag vorher).
- Sie kann mehrere Nachrichten hintereinander schicken, Sprachnachrichten (optional: eine Stimme aus einer einzigen Aufnahme klonen, läuft auf der Server-CPU) und Bilder, die sie selbst malt.

**Gedächtnis**

- Sie merkt sich getrennt deine langfristige Situation, aktuelle Ereignisse, wie sie mit dir reden soll, und die Versprechen, die ihr euch gegeben habt. Altes verblasst langsam, und eine halbe Bemerkung wird nicht zur Aufgabe, nach der sie immer wieder fragt.
- Auf der Seite Gedächtnis im Panel kannst du Erinnerungen suchen (Stichwörter hervorgehoben), ansehen, korrigieren und löschen oder ihr verbieten, eine davon zu verwenden.
- „Erinner mich morgen um acht daran, meine Hausaufgaben abzugeben" wird wirklich notiert, und sie erinnert dich pünktlich.

**Personas und Beziehungen**

- Mitgelieferte Personas: Violet (Standard), sanfte Begleiterin, fröhliche Mitschülerin, ruhig und verlässlich, eine, die weiß, dass sie eine KI ist, Big Fat Fish, die ursprüngliche Eve-Persona und eine leere. Du kannst auch deine eigene schreiben.
- Beziehungen: nahestehend, Freundschaft, Partnerschaft, Tochter, Entwickler.
- Jeder Chat-Kanal kann eine andere Persona verwenden. Jede Persona hat ihren eigenen Chatverlauf, ihr Gedächtnis, ihr Leben und ihre Stimmung, sodass beim Wechseln nichts verloren geht und nichts durcheinandergerät.
- Sie hat einen eigenen virtuellen Alltag und eigene Stimmungen: Ihre Tage verlaufen ruhig, und es können mehrere Tage ohne besondere Ereignisse vergehen; wenn du grob zu ihr bist, ist sie ein bisschen gekränkt. Sie erfindet nie Erlebnisse, die nicht in ihrem Lebensprotokoll stehen.

**Sich von selbst melden**

- Sie teilt etwas, sagt, dass sie dich vermisst, greift ein früheres Thema auf oder beschließt, gar nichts zu schicken.
- Es gibt Ruhezeiten, ein Tageslimit und eine Regel „wenn du nicht geantwortet hast, erst mal warten". Sag ihr „schreib mir nicht so oft", und sie meldet sich seltener.
- Optional: Grüße zu Feiertagen passend zu eurer Beziehung (chinesisches Neujahr, Mondfest, dein Geburtstag; bei Paaren auch Valentinstag, Qixi und mehr).
- Optional: Mit verbundenem Handy (siehe Violet MCP unten) weiß sie, ob dein Bildschirm an ist, welche App du gerade nutzt, wie voll dein Akku ist und wie lange du heute am Bildschirm warst, und kann dir morgens beim ersten Entsperren des Handys guten Morgen sagen.

**Werkzeuge** (alle optional, standardmäßig aus, im Panel einschalten)

- Websuche und Seiten lesen: Ein-Klick-Installation eines privaten SearXNG, das nur auf dem Server selbst lauscht. Kein Docker nötig.
- Stundenplan: Sie stört dich nicht im Unterricht und macht dir vor Prüfungen Mut. Import aus CSV / JSON.
- Bilder ansehen und malen: Ihre Bilder sind beiläufige „schnell mit dem Handy geknipste" Alltagsfotos, und sie behauptet nie, dass es echte Fotos sind.
- MCP-Werkzeuge: Werkzeuge mit reinem Lesezugriff werden direkt genutzt; Werkzeuge, die etwas verändern, fragen dich jedes Mal zuerst und laufen erst, nachdem du im Chat zugestimmt hast.

**Web-Panel**

- Start (was noch fehlt), Übersicht, Ablauf (welche Prompts, Werkzeuge und Modelle jede Antwort verwendet hat, einschließlich des vollständigen Inhalts, der an das Modell ging).
- Modelldienste (ein anderes Modell pro Agent und pro Chat-Plattform), Chat-Kanäle, Werkzeuge & Handy, Bilder, Personas & Leben, Stundenplan, Einstellungen.
- Chatverlauf, Gedächtnis, Diagnose, Protokolle, Änderungsverlauf, Kontosicherheit.

**Umzug von Project Eve**

Unter Chatverlauf → Chatverlauf und Gedächtnis importieren gibt dir das Panel einen Befehl zum Verpacken. Führe ihn auf deinem Eve-Server aus und lade die erzeugte Datei ins Panel hoch. Beim Import wählst du den Chat-Kanal (auch einen, den du noch nicht hinzugefügt hast), die Persona und die Beziehung; Chatverlauf und Gedächtnis werden beide übernommen.

## Installation

Auf einem Linux-Server, ein einziger Befehl:

```bash
curl -fsSL https://github.com/aileboeth/violet-companion/releases/latest/download/install.sh | bash
```

- Derselbe Befehl funktioniert auf Debian, Ubuntu, Rocky und anderen. Als root installiert er direkt (auch auf einem minimalen Debian ohne sudo); als normaler Benutzer startet er sich über sudo neu; ein normaler Benutzer ohne sudo wechselt zuerst mit `su -` zu root.
- Der Installer richtet Python und die Abhängigkeiten ein, registriert einen systemd-Dienst, wählt einen zufälligen Port und erzeugt wie 1Panel einen zufälligen Sicherheitseingang, Benutzernamen und ein Anfangspasswort. Am Ende zeigt er die Panel-Adresse im Format `https://SERVER_IP:PORT/EINGANG`.
  - Das Zertifikat ist selbstsigniert. Wenn der Browser meldet, die Seite sei nicht sicher, wähle Fortfahren.
  - Auf einem Cloud-Server muss dieser Port zusätzlich in der Sicherheitsgruppe / Firewall des Anbieters geöffnet werden.
- Nach der Installation:
  1. Öffne das Panel und trage auf der Startseite die Adresse und den Schlüssel deines Modelldienstes ein.
  2. Verbinde auf der Seite Chat-Kanäle eine beliebige Chat-Plattform und fang an zu chatten.
- Adresse oder Benutzername vergessen: `/opt/violet/violet info`. Passwort vergessen: `/opt/violet/violet reset-password`.

**Voraussetzungen**:

- Eine OpenAI-kompatible Modell-API.
- Mindestens 512 MB RAM werden empfohlen. Sprachausgabe braucht 8 GB RAM und 12 GB Speicherplatz.

**Panel nur auf dem Server selbst erreichbar** (Verwaltung über einen SSH-Tunnel):

- Neuinstallation: hänge `-s -- --access local` an den Installationsbefehl an.
- Bereits installiert: führe den Installationsbefehl erneut mit dem gewünschten Zugriffsmodus aus (`local` / `lan` / `public` / `proxy`). Daten, Konto und Port bleiben erhalten.

```bash
curl -fsSL https://github.com/aileboeth/violet-companion/releases/latest/download/install.sh | bash -s -- --access local
```

Mehr Details (Zugriffsmodi, Firewall, Wartungsbefehle) in [violet/INSTALL.md](violet/INSTALL.md) (auf Englisch).

## Updates

- Standardmäßig automatisch: Wenn eine neue Version erscheint, aktualisiert sich der Server selbst. Vorher wird ein Backup angelegt, und schlägt das Update fehl, wird automatisch zur alten Version zurückgekehrt.
- Unter Einstellungen → Versionsupdates kannst du auf „nur benachrichtigen" oder „manuell prüfen" umstellen.
- Ein Klick auf Jetzt aktualisieren auf der Startseite zeigt den Fortschritt.
- Oder aktualisiere von Hand auf dem Server:

```bash
/opt/violet/violet update
```

## Deinstallation

Der folgende Befehl entfernt Violet vollständig. **Das lässt sich nicht rückgängig machen.** Er löscht:

- das Programm, die Dienste, die automatischen Updates und den Hintergrund-Aufgabendienst;
- das Handy-MCP-Relay, die Websuche, das QQ-Konto, die Sprachausgabe;
- Konfiguration, Daten (Chatverlauf und Gedächtnis) und Backups;
- das Systemkonto `violet`, das Verzeichnis `/opt/violet` und alle Firewall-Regeln, die Violet geöffnet hat.

```bash
curl -fsSL https://github.com/aileboeth/violet-companion/releases/latest/download/install.sh | bash -s -- uninstall --purge --yes
```

Er räumt auch auf, was nach einer halb fertigen Installation, einem verlorenen Installationsprotokoll oder einer früheren Deinstallation, die nur das Programm entfernt hat, übrig ist. Über den Paketmanager des Systems installiertes Python und openssl bleiben erhalten, weil andere Programme sie nutzen könnten.

## Stand

- **Automatische Tests**:
  - Jede Funktion hat Offline-Tests, insgesamt etwa 3000, die mit simulierten Modellen und Chat-Plattformen ohne Netzwerk laufen.
  - Jede Änderung am Installer führt Installation, automatischen Neustart, Backup, Rückkehr nach einem fehlgeschlagenen Update und Deinstallation vollständig auf echtem Linux in GitHub Actions aus (Ubuntu 22.04 / 24.04, Debian 12, Rocky Linux 9).
- **Echter Einsatz**:
  - Alle vier Chat-Kanäle (WeChat, persönliches QQ, offizieller QQ-Bot, Telegram) wurden auf den echten Plattformen getestet.
  - Der Import von Chatverlauf und Gedächtnis aus Project Eve wurde mit echten Daten durchgeführt.
  - Die Ein-Klick-Installation des persönlichen QQ-Kontos hat auch in einem Netz in Festlandchina funktioniert.
- **Wer sie ist**: Das ist ein Begleiter-Projekt. Violet weiß, dass sie eine fiktive Figur ist, und gibt sich nie als echter Mensch aus; von der KI gemalte Bilder werden nie als echte Fotos ausgegeben.

## Was im Repository liegt

| Pfad | Inhalt |
|---|---|
| `violet/ai/` | Violet selbst: Chat, Gedächtnis, Personas, Stimmung, virtuelles Leben, sich melden, Gruppenchat, Werkzeuge, Malen, Stundenplan |
| `violet/ai/channels/` | Verbindungen zu WeChat, QQ und Telegram |
| `violet/wx_ilink_py/` | Das Web-Panel (Flask) und der WeChat-iLink-Client |
| `violet/persona/` | Persona-Vorlagen und Prompts |
| `violet/tools/` | Installer, Release, Installationsskripte für die optionalen Teile (Websuche, QQ-Konto, Sprachausgabe, Handy-Relay) und Testwerkzeuge für die Entwicklung |
| `violet/tests/` | Offline-Tests |
| `violet/deploy/` | Abnahmeskripte für die Installation und ein alternativer Weg, SearXNG mit Docker bereitzustellen |

## Verwandtes Projekt

[Violet MCP](https://github.com/aileboeth/violet-mcp) ist eine optionale Android-App. Sobald sie auf deinem Handy ist, kann Violet innerhalb der Grenzen, die du erlaubst, den Zustand des Handys kennen, etwa ob der Bildschirm an ist, welche App du nutzt, Akku, Schritte, Kalender und die gerade laufende Musik. Jeder Punkt lässt sich auf dem Handy einzeln ein- und ausschalten.

## Lizenz

MIT, siehe [LICENSE](LICENSE).
