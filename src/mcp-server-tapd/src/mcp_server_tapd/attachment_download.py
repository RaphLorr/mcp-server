"""Read the original TAPD attachment bytes into OpenClaw's media directory."""

import os
import re
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import requests

PAGE_SIZE = 20
MAX_BYTES = 150 * 1024 * 1024
FILE_HOSTS = {"file.tapd.cn", "file.tapd.oa.com", "file.tapd.woa.com"}


class AttachmentDownloadError(ValueError):
    """A safe error message that contains no credentials or signed URLs."""


def _download_url(reply):
    data = reply.get("data") or {}
    if reply.get("status") != 1:
        raise AttachmentDownloadError("TAPD 未签发附件下载链接")
    link = (data.get("Attachment") or {}).get("download_url")
    if not isinstance(link, str):
        raise AttachmentDownloadError("TAPD 未返回附件下载链接")
    url = urlsplit(link)
    if (url.scheme not in {"https", "http"} or url.hostname not in FILE_HOSTS
            or url.username is not None or url.password is not None or url.port is not None):
        raise AttachmentDownloadError("TAPD 返回的附件地址不受支持")
    return urlunsplit(url._replace(scheme="https"))


def _save_original(url, filename, directory, byte_limit):
    size = 0
    deadline = time.monotonic() + 120
    with requests.get(url, stream=True, allow_redirects=False, timeout=(10, 30)) as response:
        if response.status_code != 200:
            raise AttachmentDownloadError(f"附件文件服务器返回 HTTP {response.status_code}")
        if response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() == "text/html":
            if Path(filename).suffix.lower() not in {".html", ".htm"}:
                raise AttachmentDownloadError("附件文件服务器返回了网页")
        length = response.headers.get("Content-Length")
        if length and length.isdecimal() and int(length) > byte_limit:
            raise AttachmentDownloadError("附件超过本次下载剩余大小上限")
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".tapd-", delete=False) as temp:
            source = Path(temp.name)
            try:
                for chunk in response.iter_content(64 * 1024):
                    size += len(chunk)
                    if size > byte_limit:
                        raise AttachmentDownloadError("附件超过本次下载剩余大小上限")
                    if time.monotonic() > deadline:
                        raise AttachmentDownloadError("附件下载超时")
                    temp.write(chunk)
                temp.close()
                destination = directory / f"{uuid4()}--{filename}"
                os.replace(source, destination)
            finally:
                source.unlink(missing_ok=True)
    return str(destination), size


def download_ticket_files(client, workspace_id, entry_id, entity_type, directory, attachment_id=None, page=1):
    if (workspace_id < 1 or not re.fullmatch(r"[0-9]+", entry_id)
            or entity_type not in {"story", "bug"} or page < 1
            or (attachment_id is not None and not re.fullmatch(r"[0-9]+", attachment_id))):
        raise AttachmentDownloadError("请填写项目 ID、工作项 ID 和 story 或 bug 类型；附件 ID 和页码须有效")
    filters = {"workspace_id": workspace_id, "entry_id": entry_id, "type": entity_type,
               "page": page, "limit": PAGE_SIZE}
    if attachment_id is not None:
        filters["id"] = attachment_id
    listing = client.list_ticket_attachments(filters)
    rows = listing.get("data")
    if listing.get("status") != 1 or not isinstance(rows, list):
        raise AttachmentDownloadError("TAPD 未返回附件清单，请核对项目及工作项访问权限")
    if len(rows) > PAGE_SIZE:
        raise AttachmentDownloadError("TAPD 返回的附件数量超过单页上限")

    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    downloaded = []
    total = 0
    for row in rows:
        attachment = row.get("Attachment", {})
        current_id = str(attachment.get("id", ""))
        filename = attachment.get("filename")
        if (str(attachment.get("entry_id")) != entry_id
                or str(attachment.get("workspace_id")) != str(workspace_id)
                or attachment.get("type") != entity_type
                or not re.fullmatch(r"[0-9]+", current_id)
                or (attachment_id is not None and current_id != attachment_id)):
            raise AttachmentDownloadError("TAPD 返回了其他工作项的附件")
        if (not isinstance(filename, str) or not filename.strip() or filename in {".", ".."}
                or re.search(r"[/\\\x00-\x1f\x7f]", filename) or len(filename.encode()) > 200):
            downloaded.append({"attachment_id": current_id, "status": "failed", "error": "附件文件名无效"})
            continue
        result = {"attachment_id": current_id, "filename": filename}
        try:
            url = _download_url(client.get_ticket_attachment_download(workspace_id, current_id))
            path, size = _save_original(url, filename, directory, MAX_BYTES - total)
            total += size
            result.update(status="downloaded", local_path=path, size_bytes=size)
        except AttachmentDownloadError as error:
            result.update(status="failed", error=str(error))
        except (requests.RequestException, OSError, ValueError):
            result.update(status="failed", error="附件下载或落盘失败，请重试")
        downloaded.append(result)
    return {"attachments": downloaded, "next_page": page + 1 if len(rows) == PAGE_SIZE else None}
