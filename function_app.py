import logging

import azure.functions as func

from backup.core import run_backup

app = func.FunctionApp()


@app.timer_trigger(
    schedule="0 0 2 * * *",  # 02:00 UTC daily
    arg_name="timer",
    run_on_startup=False,  # avoid spurious backup every time Function App cold-starts
    use_monitor=True,  # Az persists checkpoint; won't re-fire on mid-schedule restart
)
def ats_backup(timer: func.TimerRequest) -> None:
    """Daily backup of Azure Table Storage to blob."""
    if timer.past_due:
        logging.warning("Timer is past due.")
    run_backup()


@app.route(route="dev-backup", auth_level=func.AuthLevel.FUNCTION)
def dev_ats_backup(req: func.HttpRequest) -> func.HttpResponse:
    """Manual trigger for dev ATS backup testing."""
    run_backup()
    return func.HttpResponse("Dev backup completed", status_code=200)
