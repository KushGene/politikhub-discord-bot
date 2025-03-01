import time
import psutil

class Monitoring:
    def __init__(self):
        # Reaktionen
        self.reaction_add_count = 0
        self.reaction_remove_count = 0
        
        # Starboard-Updates
        self.starboard_updates = 0
        self.update_durations = []  # in Sekunden
        # Zeitreihe der Starboard-Updates (für Chart)
        self.history = []  # Liste aus (timestamp, starboard_updates)

        # DB-Nutzung
        self.db_query_count = 0
        self.db_total_time = 0.0
        # Optional: Liste aus (timestamp, query_duration) für zeitbasierte DB-Charts
        self.db_history = []

        # Systemnutzung
        self.system_usage = []  # Liste aus (timestamp, cpu_percent, mem_used_mb)

    # --- Reaktionen ---
    def record_reaction_add(self):
        self.reaction_add_count += 1

    def record_reaction_remove(self):
        self.reaction_remove_count += 1

    # --- Starboard-Updates ---
    def record_update(self, duration):
        self.starboard_updates += 1
        self.update_durations.append(duration)
        # Speichere Zeitpunkt und aktuellen Zähler für ein Zeitreihen-Diagramm
        self.history.append((time.time(), self.starboard_updates))

    # --- Datenbank ---
    def record_db_query(self, duration):
        self.db_query_count += 1
        self.db_total_time += duration
        # Optional: Zeitreihe der DB-Laufzeiten
        self.db_history.append((time.time(), duration))

    # --- System-Ressourcen ---
    def record_system_usage(self):
        """Sammelt CPU- und RAM-Daten mithilfe von psutil."""
        timestamp = time.time()
        cpu_percent = psutil.cpu_percent()
        mem_info = psutil.virtual_memory()
        mem_used_mb = mem_info.used / (1024 * 1024)
        self.system_usage.append((timestamp, cpu_percent, mem_used_mb))

    # --- Stats abrufen ---
    def get_stats(self):
        """Gibt eine Momentaufnahme der wichtigsten Kennzahlen zurück."""
        avg_update_time = (sum(self.update_durations) / len(self.update_durations)
                           if self.update_durations else 0.0)
        max_update_time = max(self.update_durations) if self.update_durations else 0.0

        return {
            "reaction_add_count": self.reaction_add_count,
            "reaction_remove_count": self.reaction_remove_count,
            "starboard_updates": self.starboard_updates,
            "avg_update_time": avg_update_time,
            "max_update_time": max_update_time,
            "db_query_count": self.db_query_count,
            "db_total_time": self.db_total_time
        }

# Eine globale Instanz, die du in deiner Hauptdatei importierst.
monitor = Monitoring()
