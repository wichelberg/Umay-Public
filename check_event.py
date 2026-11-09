from datetime import datetime

import alarm_ring
import clock_logger
from config_loader import add_interval


def is_event(config, stop_event):
    if not config.active:
        print(f"Event '{config.name}' is not active, skipping.")
        return

    print(f"Event '{config.name}' checking started at: {datetime.now()}")

    next_check = config.start_date
    while next_check < datetime.now():
        advanced = add_interval(next_check, config.frequency)
        if advanced <= next_check:
            print(f"Event '{config.name}' has a non-advancing frequency, cannot schedule.")
            return
        next_check = advanced

    while not stop_event.is_set():
        seconds_to_wait = max((next_check - datetime.now()).total_seconds(), 0)
        if stop_event.wait(seconds_to_wait):
            break

        current_time = clock_logger.time_puller()

        if current_time > config.end_date:
            print(f"Event '{config.name}' window closed, stopping.")
            break

        print(f"Event '{config.name}' triggered at: {current_time}")
        alarm_ring.ring(config.name, config.sound)
        if not config.loop:
            break

        next_check = add_interval(next_check, config.frequency)

    if stop_event.is_set():
        print(f"Event '{config.name}' stopped.")
