import json
import os
from datetime import datetime
from worker import rclone_sync_task

# Här sparar vi alla jobb framöver
JOBS_FILE = os.path.expanduser("~/.oci/jobs.json")
LOCK_FILE = os.path.expanduser("~/.oci/run_backups.lock")
STATE_FILE = os.path.expanduser("~/.oci/run_backups_state.json")


def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return {}


def save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2)

def main():
    # Best-effort lock: prevent concurrent runs (e.g. if a timer overlaps)
    try:
        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    except FileExistsError:
        return

    try:
        state = load_state()

        if not os.path.exists(JOBS_FILE):
            return  # Filen finns inte än (inga jobb skapade i UI:t), avbryt tyst.

        # Läs in jobben
        with open(JOBS_FILE, "r", encoding="utf-8") as f:
            try:
                jobs = json.load(f)
            except json.JSONDecodeError:
                return  # Filen var tom eller trasig

        # Vad är klockan och vilken dag är det just nu?
        now = datetime.now()
        current_time = now.strftime("%H:%M")          # T.ex. "02:00"
        current_weekday = now.strftime("%A").lower()  # T.ex. "sunday"
        current_day_of_month = str(now.day)           # T.ex. "15"

        for job in jobs:
            # Hoppa över jobb som är pausade i gränssnittet
            if not job.get("is_active", True):
                continue

            schedule = job.get("schedule", {})
            freq = schedule.get("frequency")  # "daily", "weekly", "monthly"
            run_time = schedule.get("time")   # "02:00"

            # Kolla om minuten är slagen för detta jobb
            if run_time == current_time:
                should_run = False

                if freq == "daily":
                    should_run = True
                elif freq == "weekly" and schedule.get("day_of_week") == current_weekday:
                    should_run = True
                elif freq == "monthly" and schedule.get("day_of_month") == current_day_of_month:
                    should_run = True

                # Om stjärnorna står rätt, tryck in jobbet i kön!
                if should_run:
                    job_name = job.get("name", "default")
                    # Deduplicera per minut per jobbnamn
                    state_key = f"{job_name}::{now.strftime('%Y-%m-%d %H:%M')}"
                    if state.get(state_key):
                        continue
                    state[state_key] = True

                    print(f"Triggering scheduled job: {job.get('name')}")
                    rclone_sync_task.delay(
                        job["source_remote"],
                        job["dest_profile"],
                        job["dest_bucket"],
                        job.get("sync_mode", "copy"),
                        # Rclone tuning-parametrar skickas med (med säkra standardvärden ifall de saknas)
                        job.get("transfers", 4),
                        job.get("checkers", 8),
                        job.get("buffer_size", "16M"),
                        # Viktigt: så att schemalagda jobb får sin egen loggfil
                        job_name.replace(' ', '_')
                    )

        # Håll state liten: endast senaste ~2 dagar
        cutoff = datetime.now().timestamp() - (2 * 24 * 3600)
        pruned = {}
        for key in state.keys():
            # key: "<name>::YYYY-mm-dd HH:MM"
            try:
                _, ts_str = key.split("::", 1)
                dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M")
                if dt.timestamp() >= cutoff:
                    pruned[key] = True
            except Exception:
                continue

        save_state(pruned)

    finally:
        try:
            os.remove(LOCK_FILE)
        except FileNotFoundError:
            pass

if __name__ == "__main__":
    main()
