"""
report_engine.py
Core engine: pulls data → detects anomalies → generates LLM narrative → delivers report
"""

import os
import json
import logging
import smtplib
import re
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

import pandas as pd
import requests
from groq import Groq
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(filename)s:%(lineno)d — %(message)s"
)
log = logging.getLogger(__name__)


# ── Google Sheets ─────────────────────────────────────────────────────────────

def fetch_google_sheet(sheet_id: str, sheet_range: str, creds_path: str) -> pd.DataFrame:
    """Pull data from a Google Sheet and return as DataFrame."""
    log.info(f"Fetching Google Sheet: {sheet_id} range={sheet_range}")
    creds = Credentials.from_service_account_file(
        creds_path,
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
    )
    service = build("sheets", "v4", credentials=creds)
    result = service.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=sheet_range
    ).execute()
    rows = result.get("values", [])
    if not rows:
        raise ValueError(f"No data found in sheet {sheet_id} range {sheet_range}")
    df = pd.DataFrame(rows[1:], columns=rows[0])
    # Convert numeric columns
    for col in df.columns:
        try:
            df[col] = pd.to_numeric(df[col].str.replace(",", ""), errors="ignore")
        except AttributeError:
            pass
    log.info(f"Fetched {len(df)} rows, {len(df.columns)} columns from Google Sheets")
    return df


# ── CSV Loader ────────────────────────────────────────────────────────────────

def load_csv(filepath: str) -> pd.DataFrame:
    """Load a CSV file as DataFrame."""
    log.info(f"Loading CSV: {filepath}")
    df = pd.read_csv(filepath)
    log.info(f"Loaded {len(df)} rows from {filepath}")
    return df


# ── SQL Loader ────────────────────────────────────────────────────────────────

def fetch_from_sql(query: str, connection_string: str) -> pd.DataFrame:
    """Pull data via SQL. Supports PostgreSQL and MySQL via SQLAlchemy."""
    try:
        from sqlalchemy import create_engine, text
        log.info("Connecting to SQL database…")
        engine = create_engine(connection_string)
        with engine.connect() as conn:
            df = pd.read_sql(text(query), conn)
        log.info(f"SQL query returned {len(df)} rows")
        return df
    except ImportError:
        raise ImportError("Install sqlalchemy: pip install sqlalchemy psycopg2-binary")


# ── Anomaly Detection ─────────────────────────────────────────────────────────

def detect_anomalies(df: pd.DataFrame, numeric_cols: list[str], z_threshold: float = 2.0) -> list[dict]:
    """
    Flag values that deviate more than z_threshold standard deviations from
    the column mean. Returns list of anomaly dicts for LLM context.
    """
    anomalies = []
    for col in numeric_cols:
        if col not in df.columns:
            continue
        series = pd.to_numeric(df[col], errors="coerce").dropna()
        if len(series) < 3:
            continue
        mean = series.mean()
        std = series.std()
        if std == 0:
            continue
        latest_val = series.iloc[-1]
        z_score = (latest_val - mean) / std
        if abs(z_score) >= z_threshold:
            direction = "spike" if z_score > 0 else "drop"
            pct_change = ((latest_val - mean) / mean) * 100
            anomalies.append({
                "metric": col,
                "latest_value": round(float(latest_val), 2),
                "mean": round(float(mean), 2),
                "z_score": round(float(z_score), 2),
                "direction": direction,
                "pct_from_mean": round(float(pct_change), 1),
            })
            log.info(f"Anomaly detected — {col}: {direction} ({pct_change:+.1f}% from mean)")
    return anomalies


# ── Compute Summary Stats ─────────────────────────────────────────────────────

def compute_summary(df: pd.DataFrame, numeric_cols: list[str], date_col: Optional[str] = None) -> dict:
    """Compute key statistics for the LLM prompt."""
    summary = {
        "total_rows": len(df),
        "columns": list(df.columns),
        "report_period": "N/A",
        "metrics": {}
    }

    # Date range
    if date_col and date_col in df.columns:
        try:
            dates = pd.to_datetime(df[date_col], errors="coerce").dropna()
            if not dates.empty:
                summary["report_period"] = f"{dates.min().date()} to {dates.max().date()}"
        except Exception:
            pass

    # Per-metric stats
    for col in numeric_cols:
        if col not in df.columns:
            continue
        series = pd.to_numeric(df[col], errors="coerce").dropna()
        if series.empty:
            continue
        summary["metrics"][col] = {
            "latest": round(float(series.iloc[-1]), 2),
            "previous": round(float(series.iloc[-2]), 2) if len(series) > 1 else None,
            "mean": round(float(series.mean()), 2),
            "max": round(float(series.max()), 2),
            "min": round(float(series.min()), 2),
            "total": round(float(series.sum()), 2),
            "wow_change_pct": (
                round(((series.iloc[-1] - series.iloc[-2]) / series.iloc[-2]) * 100, 1)
                if len(series) > 1 and series.iloc[-2] != 0 else None
            ),
        }
    return summary


# ── Groq LLM Report Generation ────────────────────────────────────────────────

def generate_report_with_llm(
    summary: dict,
    anomalies: list[dict],
    report_type: str,
    team_context: str,
    groq_api_key: str,
) -> str:
    """
    Call Groq LLM to write a human-readable ops report narrative.
    Returns the full report as a formatted string.
    """
    client = Groq(api_key=groq_api_key)

    anomaly_text = (
        json.dumps(anomalies, indent=2) if anomalies
        else "No significant anomalies detected."
    )

    system_prompt = """You are a sharp operations analyst at a fast-growing tech company (similar to ShareChat).
You write concise, data-driven operations reports that are direct and actionable.
Your reports are read by ops leads and senior managers who have no time for fluff.
Format: use clear sections with emoji headers. Be specific with numbers. 
Flag anomalies clearly. End with 3 prioritized action items.
Never use placeholder text. Write as if this is a real report going to leadership."""

    user_prompt = f"""Write a {report_type} operations report for the following data.

TEAM CONTEXT: {team_context}

DATA SUMMARY:
{json.dumps(summary, indent=2)}

ANOMALIES DETECTED:
{anomaly_text}

TODAY'S DATE: {datetime.now().strftime("%B %d, %Y")}

Write the report with these sections:
1. 📊 Executive Summary (2-3 sentences, key headline numbers)
2. 📈 Metric Breakdown (cover each metric with WoW change and trend)
3. 🚨 Anomalies & Alerts (only if anomalies exist, explain business impact)
4. 💡 Insights (patterns you notice, what's driving the numbers)
5. ✅ Action Items (exactly 3, prioritized, each assigned to a team/person type)

Keep the entire report under 500 words. Be precise with every number from the data."""

    log.info("Calling Groq LLM for report generation…")
    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=1200,
        temperature=0.2,
    )
    report_text = response.choices[0].message.content.strip()
    log.info("LLM report generated successfully")
    return report_text


# ── Slack Delivery ────────────────────────────────────────────────────────────

def send_to_slack(report_text: str, webhook_url: str, report_type: str) -> bool:
    """Post the report to a Slack channel via Incoming Webhook."""
    today = datetime.now().strftime("%b %d, %Y")
    payload = {
        "blocks": [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"📋 {report_type} Ops Report — {today}",
                }
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": report_text[:2900]  # Slack block limit
                }
            },
            {"type": "divider"},
            {
                "type": "context",
                "elements": [{
                    "type": "mrkdwn",
                    "text": f"_Generated by Ops Report Bot · {datetime.now().strftime('%H:%M IST')} · Powered by Groq LLaMA 3.3_"
                }]
            }
        ]
    }
    log.info("Sending report to Slack…")
    resp = requests.post(webhook_url, json=payload, timeout=10)
    if resp.status_code == 200 and resp.text == "ok":
        log.info("✅ Slack delivery successful")
        return True
    else:
        log.error(f"Slack delivery failed: {resp.status_code} — {resp.text}")
        return False


# ── Email Delivery ────────────────────────────────────────────────────────────

def send_email(
    report_text: str,
    subject: str,
    recipients: list[str],
    smtp_host: str,
    smtp_port: int,
    smtp_user: str,
    smtp_password: str,
) -> bool:
    """Send the report as a formatted HTML email."""
    today = datetime.now().strftime("%B %d, %Y")

    # Convert markdown-style text to simple HTML
    html_body = f"""
    <html><body style="font-family:Inter,Arial,sans-serif;max-width:700px;margin:auto;padding:24px;color:#1a1a2e;">
    <div style="background:#5b8fff;padding:20px 24px;border-radius:8px 8px 0 0;">
        <h2 style="color:#fff;margin:0;">📋 {subject}</h2>
        <p style="color:#e0e8ff;margin:4px 0 0;">{today}</p>
    </div>
    <div style="background:#f8faff;border:1px solid #e0e8ff;border-top:none;border-radius:0 0 8px 8px;padding:24px;">
        <pre style="white-space:pre-wrap;font-family:Inter,Arial,sans-serif;font-size:14px;line-height:1.7;color:#1a1a2e;">{report_text}</pre>
    </div>
    <p style="text-align:center;color:#999;font-size:12px;margin-top:16px;">
        Generated by Ops Report Bot · Powered by Groq LLaMA 3.3 70B
    </p>
    </body></html>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"📋 {subject} — {today}"
    msg["From"] = smtp_user
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(report_text, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    log.info(f"Sending email to {recipients}…")
    try:
        with smtplib.SMTP_SSL(smtp_host, smtp_port) as server:
            server.login(smtp_user, smtp_password)
            server.sendmail(smtp_user, recipients, msg.as_string())
        log.info("✅ Email delivery successful")
        return True
    except Exception as e:
        log.error(f"Email delivery failed: {e}")
        return False


# ── Save Report Locally ───────────────────────────────────────────────────────

def save_report_locally(report_text: str, output_dir: str = "reports") -> str:
    """Save the report as a .txt file with timestamp."""
    os.makedirs(output_dir, exist_ok=True)
    filename = f"ops_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    filepath = os.path.join(output_dir, filename)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(f"OPS REPORT — {datetime.now().strftime('%B %d, %Y %H:%M')}\n")
        f.write("=" * 60 + "\n\n")
        f.write(report_text)
    log.info(f"Report saved locally: {filepath}")
    return filepath
