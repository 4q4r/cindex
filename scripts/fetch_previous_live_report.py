from __future__ import annotations

import argparse
import io
import json
import os
import zipfile
from pathlib import Path
from urllib.request import Request, urlopen


def _request_json(url: str, token: str) -> dict:
    req = Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urlopen(req) as response:
        return json.loads(response.read().decode("utf-8"))


def _download(url: str, token: str) -> bytes:
    req = Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
    )
    with urlopen(req) as response:
        return response.read()


def _find_artifact(
    artifacts: list[dict], artifact_name: str, current_run_id: str,
) -> dict | None:
    """Find the most recent non-expired artifact matching the name."""
    for artifact in artifacts:
        if artifact.get("name") != artifact_name:
            continue
        if artifact.get("expired", True):
            continue
        workflow_run = artifact.get("workflow_run", {})
        if current_run_id and str(workflow_run.get("id")) == str(current_run_id):
            continue
        return artifact
    return None


def _extract_json_from_zip(archive_bytes: bytes) -> str | None:
    """Extract the first .json file content from a zip archive."""
    zf = zipfile.ZipFile(io.BytesIO(archive_bytes))
    for name in zf.namelist():
        if name.endswith(".json"):
            return zf.read(name).decode("utf-8")
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="tmp/previous_live_smoke_report.json")
    parser.add_argument("--artifact-name", default="live-smoke-report")
    args = parser.parse_args()

    token = os.getenv("GITHUB_TOKEN", "")
    repo = os.getenv("GITHUB_REPOSITORY", "")
    current_run_id = os.getenv("GITHUB_RUN_ID", "")
    if not token or not repo:
        return 0

    artifacts_api = (
        f"https://api.github.com/repos/{repo}/actions/artifacts?per_page=100"
    )
    payload = _request_json(artifacts_api, token)
    artifacts = payload.get("artifacts", [])
    selected = _find_artifact(artifacts, args.artifact_name, current_run_id)

    if selected is None:
        return 0

    archive_url = selected.get("archive_download_url", "")
    if not archive_url:
        return 0
    archive_bytes = _download(archive_url, token)
    content = _extract_json_from_zip(archive_bytes)
    if content is None:
        return 0

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(content, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
