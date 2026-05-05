"""
Check billing & quota status via gcloud CLI.
No search calls made — purely management API.

Run:
    python scripts/check_billing.py > billing_output.txt 2>&1
"""
import subprocess
import sys
import os
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from dotenv import load_dotenv
load_dotenv(REPO / ".env")

PROJECT = os.getenv("GCP_PROJECT_ID", "madison-rag-60")

def run(cmd, label):
    print("=" * 70)
    print(f" {label}")
    print("=" * 70)
    print(f"  $ {cmd}")
    print()
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=30
        )
        if result.stdout:
            print(result.stdout)
        if result.stderr:
            print("STDERR:")
            print(result.stderr)
        if result.returncode != 0:
            print(f"  Exit code: {result.returncode}")
    except FileNotFoundError:
        print("  ERROR: gcloud not installed or not in PATH")
        print("  Install from: https://cloud.google.com/sdk/docs/install")
    except Exception as e:
        print(f"  ERROR: {type(e).__name__}: {e}")
    print()


print(f"Checking project: {PROJECT}")
print()

# 1. Is gcloud authenticated?
run("gcloud auth list", "1) Active gcloud accounts")

# 2. What project is set?
run("gcloud config get-value project", "2) Current default project")

# 3. Is billing enabled on the project?
run(f"gcloud billing projects describe {PROJECT}",
    "3) Billing status for the project")

# 4. Which billing account is linked?
run(f"gcloud billing projects describe {PROJECT} --format=\"value(billingAccountName)\"",
    "4) Linked billing account")

# 5. Is Discovery Engine API enabled?
run(f"gcloud services list --enabled --project={PROJECT} --filter=\"NAME:discoveryengine.googleapis.com\"",
    "5) Discovery Engine API enabled?")

# 6. Recent billing account info (if accessible)
run("gcloud billing accounts list",
    "6) Billing accounts visible to your gcloud login")
