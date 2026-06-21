"""
main.py
Entry point: load config → run pipeline → schedule recurring reports
Usage:
    python main.py --run-now              # Run report immediately
    python main.py --schedule             # Start scheduler (daily/weekly cron)
    python main.py --run-now --source csv --file data/sample.csv
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "backend"))
import argparse
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path

import schedule
import yaml
from dotenv import load_dotenv

from report_engine import (
    fetch_google_sheet,
    load_csv,
    fetch_from_sql,
    compute_summary,
    detect_anomalies,
    generate_report_with_llm,
    send_to_slack,
    send_email,
    save_report_locally,
)

load_dotenv()
log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)


# ── Config Loader ─────────────────────────────────────────────────────────────

def load_config(config_path: str = "config/config.yaml") -> dict:
    """Load YAML config and resolve env variable placeholders."""
    with open(config_path) as f:
        raw = f.read()

    # Replace ${ENV_VAR} placeholders with actual env values
    import re
    def replace_env(match):
        var = match.group(1)
        val = os.getenv(var, "")
        if not val:
            log.warning(f"Environment variable ${{{var}}} not set")
        return val

    resolved = re.sub(r"\$\{(\w+)\}", replace_env, raw)
    config = yaml.safe_load(resolved)
    log.info(f"Config loaded from {config_path}")
    return config


# ── Data Fetcher ──────────────────────────────────────────────────────────────

def fetch_data(config: dict, source_override: str = None, file_override: str = None):
    """
    Fetch data based on config source type.
    source_override: 'csv' | 'gsheet' | 'sql' (overrides config)
    """
    source = source_override or config["data_source"]["type"]
    log.info(f"Data source: {source}")

    if source == "csv":
        filepath = file_override or config["data_source"]["csv"]["filepath"]
        return load_csv(filepath)

    elif source == "gsheet":
        cfg = config["data_source"]["gsheet"]
        return fetch_google_sheet(
            sheet_id=cfg["sheet_id"],
            sheet_range=cfg["range"],
            creds_path=cfg["credentials_path"],
        )

    elif source == "sql":
        cfg = config["data_source"]["sql"]
        return fetch_from_sql(
            query=cfg["query"],
            connection_string=cfg["connection_string"],
        )

    else:
        raise ValueError(f"Unknown data source type: {source}. Use 'csv', 'gsheet', or 'sql'")


# ── Core Pipeline ─────────────────────────────────────────────────────────────

def run_pipeline(config: dict, source_override: str = None, file_override: str = None):
    """
    Full pipeline:
    1. Fetch data
    2. Compute summary stats
    3. Detect anomalies
    4. Generate LLM report
    5. Deliver via Slack / Email / Local file
    """
    log.info("=" * 55)
    log.info(f"🚀 Starting Ops Report Pipeline — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    log.info("=" * 55)

    # ── 1. Fetch data
    try:
        df = fetch_data(config, source_override, file_override)
    except Exception as e:
        log.error(f"Data fetch failed: {e}")
        raise

    # ── 2. Compute summary
    report_cfg = config["report"]
    numeric_cols = report_cfg.get("numeric_columns", [])
    date_col = report_cfg.get("date_column")

    if not numeric_cols:
        # Auto-detect numeric columns
        numeric_cols = df.select_dtypes(include="number").columns.tolist()
        log.info(f"Auto-detected numeric columns: {numeric_cols}")

    summary = compute_summary(df, numeric_cols, date_col)
    log.info(f"Summary computed: {len(summary['metrics'])} metrics over {summary['report_period']}")

    # ── 3. Detect anomalies
    z_threshold = report_cfg.get("anomaly_z_threshold", 2.0)
    anomalies = detect_anomalies(df, numeric_cols, z_threshold)
    log.info(f"Anomaly detection complete: {len(anomalies)} anomalies found")

    # ── 4. Generate LLM report
    groq_api_key = os.getenv("GROQ_API_KEY") or config.get("groq", {}).get("api_key", "")
    if not groq_api_key:
        raise ValueError("GROQ_API_KEY not set. Add it to .env or config.yaml")

    report_text = generate_report_with_llm(
        summary=summary,
        anomalies=anomalies,
        report_type=report_cfg.get("type", "Daily"),
        team_context=report_cfg.get("team_context", "Operations team"),
        groq_api_key=groq_api_key,
    )

    # ── 5. Print to console always
    print("\n" + "=" * 55)
    print("📋 GENERATED REPORT")
    print("=" * 55)
    print(report_text)
    print("=" * 55 + "\n")

    # ── 6. Save locally always
    saved_path = save_report_locally(report_text, output_dir=report_cfg.get("output_dir", "reports"))

    # ── 7. Slack delivery
    delivery = config.get("delivery", {})
    slack_cfg = delivery.get("slack", {})
    if slack_cfg.get("enabled", False):
        webhook = os.getenv("SLACK_WEBHOOK_URL") or slack_cfg.get("webhook_url", "")
        if webhook:
            send_to_slack(report_text, webhook, report_cfg.get("type", "Daily"))
        else:
            log.warning("Slack enabled but SLACK_WEBHOOK_URL not set — skipping")

    # ── 8. Email delivery
    email_cfg = delivery.get("email", {})
    if email_cfg.get("enabled", False):
        send_email(
            report_text=report_text,
            subject=f"{report_cfg.get('type', 'Daily')} Ops Report — {config.get('team_name', 'Ops')}",
            recipients=email_cfg.get("recipients", []),
            smtp_host=os.getenv("SMTP_HOST") or email_cfg.get("smtp_host", "smtp.gmail.com"),
            smtp_port=int(os.getenv("SMTP_PORT") or email_cfg.get("smtp_port", 465)),
            smtp_user=os.getenv("SMTP_USER") or email_cfg.get("smtp_user", ""),
            smtp_password=os.getenv("SMTP_PASSWORD") or email_cfg.get("smtp_password", ""),
        )

    log.info(f"✅ Pipeline complete. Report saved to: {saved_path}")
    return report_text


# ── Scheduler ─────────────────────────────────────────────────────────────────

def start_scheduler(config: dict):
    """Schedule reports based on config frequency."""
    schedule_cfg = config.get("schedule", {})
    frequency = schedule_cfg.get("frequency", "daily")
    run_time = schedule_cfg.get("time", "08:00")

    def job():
        log.info(f"⏰ Scheduled job triggered at {datetime.now()}")
        try:
            run_pipeline(config)
        except Exception as e:
            log.error(f"Scheduled pipeline failed: {e}")

    if frequency == "daily":
        schedule.every().day.at(run_time).do(job)
        log.info(f"📅 Scheduler set: Daily at {run_time}")

    elif frequency == "weekly":
        day = schedule_cfg.get("day", "monday").lower()
        getattr(schedule.every(), day).at(run_time).do(job)
        log.info(f"📅 Scheduler set: Every {day.capitalize()} at {run_time}")

    elif frequency == "hourly":
        schedule.every().hour.do(job)
        log.info("📅 Scheduler set: Every hour")

    else:
        raise ValueError(f"Unknown frequency: {frequency}. Use 'daily', 'weekly', or 'hourly'")

    log.info("⏳ Scheduler running. Press Ctrl+C to stop.")
    while True:
        schedule.run_pending()
        time.sleep(30)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Ops Report Generator — AI-powered automated reporting"
    )
    parser.add_argument(
        "--run-now",
        action="store_true",
        help="Run the report pipeline immediately"
    )
    parser.add_argument(
        "--schedule",
        action="store_true",
        help="Start the scheduler for recurring reports"
    )
    parser.add_argument(
        "--config",
        default="config/config.yaml",
        help="Path to config file (default: config/config.yaml)"
    )
    parser.add_argument(
        "--source",
        choices=["csv", "gsheet", "sql"],
        help="Override data source type"
    )
    parser.add_argument(
        "--file",
        help="Path to CSV file (if source=csv)"
    )

    args = parser.parse_args()

    if not args.run_now and not args.schedule:
        parser.print_help()
        print("\n💡 Quick start: python main.py --run-now --source csv --file data/sample.csv")
        return

    config = load_config(args.config)

    if args.run_now:
        run_pipeline(config, source_override=args.source, file_override=args.file)

    elif args.schedule:
        start_scheduler(config)


if __name__ == "__main__":
    main()
