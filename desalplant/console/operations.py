"""Operations that change the state of the line."""

from .http import Request, Response, json_response


def start(console, request: Request) -> Response:
    """Bring the line up."""
    request.body()
    event = console.plant.start()
    return json_response({"status": "started", "event": event.to_dict()})


def shutdown(console, request: Request) -> Response:
    """Stop the line."""
    request.body()
    event = console.plant.shutdown()
    return json_response({"status": "stopped", "event": event.to_dict()})


def switch(console, request: Request) -> Response:
    """Move production to the standby tank."""
    request.body()
    event = console.plant.switch_tank()
    return json_response({"status": "switched", "event": event.to_dict()})


def maintain(console, request: Request) -> Response:
    """Run the maintenance routine."""
    request.body()
    event = console.plant.maintain()
    return json_response({"status": "maintained", "event": event.to_dict()})


def trip_recovery(console, request: Request) -> Response:
    """Raise the recovery alarm and put the valve in emergency."""
    body = request.body()
    rate = body.get("rate") if isinstance(body, dict) else None
    event = console.plant.trip_recovery_alarm(rate=rate)
    return json_response({"status": "tripped", "event": event.to_dict()})


def exit_recovery(console, request: Request) -> Response:
    """Advance the audited emergency release by one step."""
    request.body()
    plant = console.plant
    event = plant.exit_recovery_emergency()
    return json_response({
        "status": plant.emergency_exit.state.value,
        "mode": plant.emergency_exit.mode.value,
        "valve_emergency": plant.valve.emergency,
        "event": event.to_dict(),
    })


def retry_recovery(console, request: Request) -> Response:
    """Retry the emergency release after a rollback once water is healthy."""
    request.body()
    plant = console.plant
    event = plant.retry_recovery_exit()
    return json_response({
        "status": plant.emergency_exit.state.value,
        "mode": plant.emergency_exit.mode.value,
        "valve_emergency": plant.valve.emergency,
        "event": event.to_dict(),
    })
