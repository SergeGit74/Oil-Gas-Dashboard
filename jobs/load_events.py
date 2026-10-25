"""Load the event calendar: config/events.yaml plus EIA release schedules. Logs `load_events` to job_runs."""
import logging

import cost_tracker
import event_calendar
import storage


def run_load_events():
    counts, errors = event_calendar.load_events()
    storage.log_job("load_events", "degraded" if errors else "success",
                    "; ".join(errors) if errors else ", ".join(f"{k}: {v}" for k, v in counts.items()))
    return counts


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    with cost_tracker.cost_run("load_events"):
        print(run_load_events())
