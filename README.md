# Lyndrix Meeting Bingo Plugin

**Version:** 1.2.0  
**Autor:** Lyndrix

Ein Multiplayer Bullshit-Bingo Plugin, das nahtlos in die Lyndrix-Plattform integriert ist. Perfekt, um langatmige Meetings mit etwas Gamification aufzulockern.

## 📋 Features

- **Multiplayer Lobby**: Erstelle Bingo-Sessions mit anpassbarer Feldgröße (3x3 bis 5x5).
- **Echtzeit-Synchronisation**: Sieh live, wie deine Kollegen ihre Felder markieren (und wer kurz vor dem Sieg steht).
- **Wall of Shame**: Ein optionales Scoreboard, das die Gewinner persistent im Lyndrix Vault speichert.
- **Sarkastische Kommentare**: Das System kommentiert deine Leistung (oder deren Fehlen).
- **Integrierte Begriffe**: Kommt mit einer kuratierten Liste an Buzzwords (`terms.txt`), die pro Session angepasst werden kann.

## 🚀 Installation

Da es sich um ein Plugin für `lyndrix-core` handelt, muss es im Plugin-Verzeichnis der Hauptanwendung installiert werden.

1. Navigiere in das Plugin-Verzeichnis deiner `lyndrix-core` Installation:
   ```bash
   cd /pfad/zu/lyndrix-core/plugins
   ```

2. Klone dieses Repository:
   ```bash
   git clone https://github.com/lyndrix/lyndrix-meeting-bingo.git lyndrix.plugin.bingo
   ```
   *Hinweis: Der Zielordnername `lyndrix.plugin.bingo` wird empfohlen, damit die ID im Manifest sauber matcht.*

3. Starte die Lyndrix-Anwendung neu. Das Plugin wird automatisch geladen und ist unter der Route `/bingo` erreichbar.

## ⚙️ Konfiguration

Das Plugin nutzt die interne `ctx` API von Lyndrix für Einstellungen und Secrets.

### Scoreboard (Wall of Shame)
Standardmäßig ist das dauerhafte Speichern von Gewinnern deaktiviert (aus "ethischen" Gründen).
Um es zu aktivieren:
1. Öffne das Plugin in der UI.
2. Scrolle zu den Einstellungen.
3. Aktiviere den Switch **"Scoreboard aktivieren"**.
4. Die Daten werden sicher im Vault unter dem Key `bingo_scoreboard` abgelegt.

## 🛠 Entwicklung & Struktur

- `__init__.py`: Enthält die komplette Logik, UI (NiceGUI) und das Plugin-Manifest.
- `terms.txt`: Standardliste der Buzzwords (wird neu erstellt, falls gelöscht).

### Abhängigkeiten
Das Plugin verlässt sich auf Bibliotheken, die in `lyndrix-core` bereitgestellt werden:
- `nicegui`
- `core.modules.models`
- `ui.layout` / `ui.theme`

## 📝 Lizenz
Internes Tool. Nutzung auf eigene Gefahr während offizieller Meetings.