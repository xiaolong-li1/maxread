from __future__ import annotations

import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from email import policy
from email.parser import BytesParser
from pathlib import Path


ALLOWED_HOSTS = {"mail.163.com", "fs.mail.163.com", "u.163.com"}
URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.I)
FILENAME_RE = re.compile(r"(?:file_name|filename)=\"([^\"]+\.pdf)\"", re.I)


def download_external_pdfs(raw_path: Path, target_dir: Path, max_bytes: int = 50 * 1024 * 1024) -> list[Path]:
    """Download attachment links embedded in HTML mail parts.

    Outlook/163 large-attachment messages often contain no MIME attachment;
    the HTML part contains a short-lived 163 download URL instead. Only known
    163 attachment hosts are allowed, and the downloaded bytes must look like a
    PDF before they are handed to the rest of the pipeline.
    """
    try:
        message = BytesParser(policy=policy.default).parsebytes(raw_path.read_bytes())
    except (OSError, ValueError):
        return []
    html_parts: list[str] = []
    for part in message.walk():
        if part.get_content_type() != "text/html":
            continue
        payload = part.get_payload(decode=True) or b""
        charset = part.get_content_charset() or "utf-8"
        html_parts.append(payload.decode(charset, errors="replace"))
    if not html_parts:
        return []
    raw_html = html.unescape("\n".join(html_parts))
    urls: list[str] = []
    for value in URL_RE.findall(raw_html):
        parsed = urllib.parse.urlparse(value)
        if parsed.scheme == "https" and parsed.hostname in ALLOWED_HOSTS and (
            "large-attachment-download" in parsed.path or "fs/preview" in parsed.path
        ):
            clean = value.rstrip(".,);]")
            if clean not in urls:
                urls.append(clean)
    if not urls:
        return []
    names = [urllib.parse.unquote(name) for name in FILENAME_RE.findall(raw_html)]
    target_dir.mkdir(parents=True, exist_ok=True)
    outputs: list[Path] = []
    for index, url in enumerate(urls, start=1):
        filename = next((name for name in names if name.lower().endswith(".pdf")), f"external-attachment-{index}.pdf")
        filename = re.sub(r"[^\w.()\-\u4e00-\u9fff]+", "_", Path(filename).name, flags=re.UNICODE)[:180]
        target = target_dir / filename
        if target.exists() and target.stat().st_size > 0:
            outputs.append(target)
            continue
        download_url, remote_name = _resolve_download_url(url)
        if download_url:
            url = download_url
        if remote_name:
            filename = remote_name
        request = urllib.request.Request(url, headers={"User-Agent": "zip-lab-recruiting/0.1"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                content_type = (response.headers.get("Content-Type") or "").lower()
                data = response.read(max_bytes + 1)
        except (OSError, urllib.error.URLError):
            continue
        if len(data) > max_bytes or not data.startswith(b"%PDF"):
            continue
        target.write_bytes(data)
        outputs.append(target)
    return outputs


def _resolve_download_url(url: str) -> tuple[str, str]:
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    key = query.get("file", [""])[0]
    if not key or parsed.hostname not in {"mail.163.com", "u.163.com"} or "large-attachment-download" not in parsed.path:
        return url, ""
    access_token = query.get("encryptToken", [""])[0]
    try:
        info_url = "https://mail.163.com/filehub/bg/link/info/get?" + urllib.parse.urlencode({"key": key, "accessToken": access_token})
        with urllib.request.urlopen(info_url, timeout=30) as response:
            info = json.loads(response.read().decode("utf-8", errors="replace"))
        link_info = info.get("data", {}).get("linkInfo", {})
        remote_name = str(link_info.get("fileInfo", {}).get("filename") or "")
        prepare = urllib.request.Request(
            "https://mail.163.com/filehub/bg/dl/prepare",
            data=json.dumps({"linkKey": key, "accessToken": access_token}).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "zip-lab-recruiting/0.1"},
            method="POST",
        )
        with urllib.request.urlopen(prepare, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
        return str(payload.get("data", {}).get("downloadUrl") or url), remote_name
    except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError):
        return url, ""
