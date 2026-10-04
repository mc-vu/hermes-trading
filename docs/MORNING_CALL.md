# Morning Call: Lagebild-Abschnitt (Patch-Vorschlag)

**Nicht eingebaut.** `~/.hermes/scripts/morning_call.sh` ist unverändert (sha256 `31c2b5ef…dd897`
am 04.10.2026 vor und nach der Prüfung). Einbau macht Jarvis nach Freigabe durch MCVu.

## Was der Patch tut

Hängt nach dem Polymarket-Block einen Block an, der das Lagebild des Tages ausgibt:

- `hermes trading report --morning` liest nur die Plugin-DB (keine Kurs- oder Ereignisabrufe), gibt höchstens
  20 Zeilen aus und speichert das Ergebnis in `daily_briefing` (`delivered_via = morning_call`).
- Erste Zeile immer: `LAGEBILD · keine Anlageberatung · <Tag Datum Uhrzeit>`. Jede weitere Zeile endet mit
  einer Quelle als Kurzlink in `[…]`.
- **Depot nur in Prozent.** Mit `--morning` stehen keine Euro-Beträge im Text, weil der Morning-Call-Agent
  den Rohtext an das LLM gibt. Beträge zeigt nur die lokale Ansicht (`report --text` oder `--betraege`).
- Ereignis-, Termin- und Signalzeilen schreibt das LLM über `ctx.llm.complete` (einmal pro Tag). Fehlt das LLM,
  schlägt es fehl oder ist der Tagesaufruf schon verbraucht, erzeugt der Befehl dieselben Zeilen regelbasiert.
- Fehler brechen den Morning Call nicht ab: `timeout 180`, stderr verworfen; kommt nichts zurück, steht dort
  „Lagebild nicht verfügbar …“. Ist die DB erreichbar, aber z. B. das Schema kaputt, druckt der Befehl selbst
  eine Zeile `LAGEBILD · keine Anlageberatung: nicht verfügbar (<Fehler>)`.

## Patch

Datei im Repo: `docs/morning_call.patch` (Unified Diff gegen den Stand vom 04.10.2026).

```diff
--- a/morning_call.sh
+++ b/morning_call.sh
@@ -133,6 +133,14 @@
 PM=$(timeout 60 "$HOME/.local/bin/hermes" polymarket report --morning 2>/dev/null | head -n 15)
 echo "${PM:-Polymarket-Bericht nicht verfügbar (Plugin aktiv? hermes polymarket status)}"
 
+echo; echo "=== LAGEBILD MÄRKTE (keine Anlageberatung) ==="
+# Taegliches Lagebild aus der Plugin-DB von hermes-trading: hoechstens 20 Zeilen, jede mit Quelle.
+# Ein LLM-Aufruf pro Tag ueber ctx.llm, sonst regelbasiert. Depot nur in Prozent (Text geht an das LLM).
+# Daten vorher per Cron holen (prices:update, events:update), hier keine Abrufe.
+# Fehler (Plugin nicht aktiv, DB fehlt) werden als eine Zeile gemeldet, der Morning Call laeuft weiter.
+TR=$(timeout 180 "$HOME/.local/bin/hermes" trading report --morning 2>/dev/null | head -n 20)
+echo "${TR:-Lagebild nicht verfügbar (Plugin aktiv? hermes trading status)}"
+
 # Exa-Verbrauch: nur montags (lokale Log-Zaehlung, kein API-Aufruf)
 if [ "$(date +%u)" = "1" ]; then
   echo; timeout 60 "$HOME/.hermes/scripts/exa_usage.sh" 2>/dev/null || echo "=== EXA VERBRAUCH === nicht ermittelbar"
```

Mit `--morning` stehen auch in `daily_briefing` nur Prozente. Beträge speichert nur ein lokaler Lauf ohne
`--morning` (z. B. `report --text`).

Geprüft (04.10.2026, ohne das Original zu ändern):

- `patch --dry-run -o /dev/null ~/.hermes/scripts/morning_call.sh docs/morning_call.patch` → rc 0
- `bash -n` auf die gepatchte Kopie (`.dev/smoke/morning_call.new.sh`) → rc 0
- Block allein ausgeführt, Plugin noch nicht aktiv → Fallback-Zeile „Lagebild nicht verfügbar …“, rc 0
- `report --morning` gegen die Smoke-DB → 13 Zeilen, siehe `docs/BRIEFING_BEISPIEL.md`

## Einbau (nach Freigabe)

Voraussetzungen:

1. Plugin installiert und aktiviert (wie bei Polymarket: Symlink nach `~/.hermes/plugins`, `hermes plugins
   enable hermes-trading`). Das macht Jarvis bzw. MCVu, nicht dieses Repo.
2. Daten frisch: vor 07:00 laufen `hermes trading prices:update` und `hermes trading events:update`
   (z. B. Cron 06:30). Ohne diesen Lauf ist das Lagebild alt; die Kopfzeile meldet dann
   „Achtung: neuestes Ereignis vom …“, wenn das jüngste Ereignis älter als 36 Stunden ist.
3. Depotdatei importiert (`hermes trading portfolio:import`), sonst steht im Depotteil
   „keine Positionen importiert“.

Schritte:

1. Script patchen (legt `morning_call.sh.orig` als Sicherung an):
   ```bash
   patch -b ~/.hermes/scripts/morning_call.sh ~/projects/hermes-trading/docs/morning_call.patch
   ```
2. Prompt des Morning-Call-Jobs `d1ac24833b65` ergänzen (vollständigen bisherigen Prompt plus diesen Punkt):

   > 7. **Lagebild Märkte**: den Block unter `=== LAGEBILD MÄRKTE` übernehmen, höchstens 10 Zeilen,
   > Kopfzeile „keine Anlageberatung“ immer mitnehmen, Quellen-Kurzlinks behalten. Keine Kauf-,
   > Verkaufs- oder Halteempfehlung ergänzen, nichts hinzuerfinden. Bei Signalen den Meldeverzug nennen.
   > Steht dort „nicht verfügbar“ oder „Achtung: neuestes Ereignis vom …“, das in einem Satz melden.

3. Test ohne Warten auf 07:00: `hermes cron run d1ac24833b65`

Rückbau: `mv ~/.hermes/scripts/morning_call.sh.orig ~/.hermes/scripts/morning_call.sh`

## Laufzeit und Kosten

- Regelbasiert: unter 1 s.
- Mit LLM: ein Aufruf pro Kalendertag (Europe/Berlin), `max_tokens=1500`, `timeout=120` s. Weitere Aufrufe am
  selben Tag (z. B. manuell) laufen regelbasiert (`llm_status = daily_limit`). Der Aufruf läuft über das
  aktive Modell des Nutzers; `provider`/`model` werden nicht überschrieben, daher ist kein Eintrag unter
  `plugins.entries.hermes-trading.llm` nötig.
