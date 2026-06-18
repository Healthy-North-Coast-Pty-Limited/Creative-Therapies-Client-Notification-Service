import azure.functions as func
import csv
import io
import json
import logging
import os
import urllib.error
import urllib.request

from azure.storage.blob import BlobServiceClient

app = func.FunctionApp()

PROVIDER_COL = "Please select the relevant provider:"
ATTENDANCE_COL = "Attendance"
SERVICE_TYPE_COL = "Type of service"
REFERRAL_ID_COL = "Referral ID"
GROUP_REFERRAL_IDS_COL = "Referral IDs of all people attending the group session"

ATTENDED = "Client attended the session"
DNA = "Client did not attend their session and did not cancel in advance"


# Monday 08:00 AEST (UTC+10) = Sunday 22:00 UTC
@app.timer_trigger(
    schedule="0 0 22 * * 0",
    arg_name="timer",
    run_on_startup=False,
    use_monitor=True,
)
def scheduled_notification(timer: func.TimerRequest) -> None:
    logging.info("Creative Therapies weekly notification triggered by schedule.")
    _run_notifications()


@app.route(route="send-notifications", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def on_demand_notification(req: func.HttpRequest) -> func.HttpResponse:
    logging.info("Creative Therapies notification triggered on demand via HTTP.")
    try:
        _run_notifications()
        return func.HttpResponse("Notifications sent successfully.", status_code=200)
    except Exception as exc:
        logging.error("Failed to send notifications: %s", exc)
        return func.HttpResponse(f"Error: {exc}", status_code=500)


def _run_notifications() -> None:
    blob_conn_str = os.environ["BLOB_CONNECTION_STRING"]
    container_name = os.environ["BLOB_CONTAINER_NAME"]
    blob_filename = os.environ["BLOB_FILENAME"]
    webhook_url = os.environ["POWER_AUTOMATE_WEBHOOK_URL"]

    rows = _load_blob_csv(blob_conn_str, container_name, blob_filename)
    provider_stats = _process_data(rows)

    for provider, referral_stats in provider_stats.items():
        _post_to_webhook(webhook_url, provider, referral_stats)


def _load_blob_csv(conn_str: str, container: str, filename: str) -> list[dict]:
    blob_service = BlobServiceClient.from_connection_string(conn_str)
    blob_client = blob_service.get_blob_client(container=container, blob=filename)
    raw = blob_client.download_blob().readall()
    text = raw.decode("utf-8-sig")  # Strip BOM if present
    return list(csv.DictReader(io.StringIO(text)))


def _process_data(rows: list[dict]) -> dict:
    """
    Returns: {provider: {referral_id: {individual_attended, group_attended, dna}}}
    Total sessions is derived as the sum of the three counters at render time.
    """
    provider_stats: dict[str, dict] = {}

    for row in rows:
        provider = (row.get(PROVIDER_COL) or "").strip()
        attendance = (row.get(ATTENDANCE_COL) or "").strip()
        service_type = (row.get(SERVICE_TYPE_COL) or "").strip().lower()
        referral_id = (row.get(REFERRAL_ID_COL) or "").strip()
        group_ids_raw = (row.get(GROUP_REFERRAL_IDS_COL) or "").strip()

        if not provider:
            continue

        referral_ids: set[str] = set()
        if referral_id:
            referral_ids.add(referral_id)
        for gid in group_ids_raw.split(","):
            gid = gid.strip()
            if gid:
                referral_ids.add(gid)

        if not referral_ids:
            continue

        if provider not in provider_stats:
            provider_stats[provider] = {}

        for rid in referral_ids:
            if rid not in provider_stats[provider]:
                provider_stats[provider][rid] = {
                    "individual_attended": 0,
                    "group_attended": 0,
                    "dna": 0,
                }

            stats = provider_stats[provider][rid]

            if attendance == ATTENDED:
                if service_type.startswith("group"):
                    stats["group_attended"] += 1
                else:
                    stats["individual_attended"] += 1
            elif attendance == DNA:
                stats["dna"] += 1

    return provider_stats


def _post_to_webhook(webhook_url: str, provider: str, referral_stats: dict) -> None:
    payload = json.dumps(
        {"provider": provider, "table_html": _build_table_html(referral_stats)}
    ).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            logging.info(
                "Webhook posted for provider '%s'. HTTP %s", provider, resp.status
            )
    except urllib.error.HTTPError as exc:
        logging.error(
            "Webhook HTTP error for provider '%s': %s %s", provider, exc.code, exc.reason
        )
        raise
    except urllib.error.URLError as exc:
        logging.error("Webhook connection error for provider '%s': %s", provider, exc.reason)
        raise


def _build_table_html(referral_stats: dict) -> str:
    th = "padding:10px 14px;border:1px solid #005f56;text-align:left;"
    th_c = "padding:10px 14px;border:1px solid #005f56;text-align:center;"
    td = "padding:8px 14px;border:1px solid #ccc;"
    td_c = "padding:8px 14px;border:1px solid #ccc;text-align:center;"

    rows_html = ""
    for i, referral_id in enumerate(sorted(referral_stats)):
        s = referral_stats[referral_id]
        total = s["individual_attended"] + s["group_attended"] + s["dna"]
        bg = "background-color:#f9f9f9;" if i % 2 == 1 else ""
        rows_html += (
            f'<tr style="{bg}">'
            f'<td style="{td}">{referral_id}</td>'
            f'<td style="{td_c}">{total}</td>'
            f'<td style="{td_c}">{s["individual_attended"]}</td>'
            f'<td style="{td_c}">{s["group_attended"]}</td>'
            f'<td style="{td_c}">{s["dna"]}</td>'
            f"</tr>"
        )

    return (
        f'<table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;font-size:13px;">'
        f"<thead>"
        f'<tr style="background:#007a6e;color:#fff;">'
        f'<th style="{th}">Referral ID</th>'
        f'<th style="{th_c}">Total sessions</th>'
        f'<th style="{th_c}">Individual sessions attended</th>'
        f'<th style="{th_c}">Group sessions attended</th>'
        f'<th style="{th_c}">Session not attended and not cancelled</th>'
        f"</tr>"
        f"</thead>"
        f"<tbody>{rows_html}</tbody>"
        f"</table>"
    )
