import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from mcp_server_tapd.attachment_download import AttachmentDownloadError, download_ticket_files


class AttachmentDownloadTest(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.rows = [
            {"Attachment": {"id": str(item), "workspace_id": 21, "entry_id": "211",
                            "type": "story", "filename": name}}
            for item, name in [(1, "用例.xlsx"), (2, "场景.xlsx")]
        ]
        self.client.list_ticket_attachments.return_value = {"status": 1, "data": self.rows}
        self.client.get_ticket_attachment_download.return_value = {
            "status": 1, "data": {"Attachment": {"download_url": "https://file.tapd.cn/attachments/test?salt=fixture"}}
        }

    @patch("mcp_server_tapd.attachment_download.requests.get")
    def test_downloads_each_original_with_no_credentials_or_signed_link_in_result(self, get):
        response = get.return_value.__enter__.return_value
        response.status_code = 200
        response.headers = {"Content-Type": "application/zip"}
        response.iter_content.side_effect = [[b"PK first"], [b"PK second"]]
        with tempfile.TemporaryDirectory(dir="tmp") as temp:
            files = download_ticket_files(self.client, 21, "211", "story", temp)["attachments"]
            self.assertEqual([Path(item["local_path"]).read_bytes() for item in files], [b"PK first", b"PK second"])
            self.assertEqual([item["filename"] for item in files], ["用例.xlsx", "场景.xlsx"])
            self.assertTrue(all(item["status"] == "downloaded" for item in files))
            self.assertNotIn("salt", str(files))
            self.assertNotIn("headers", get.call_args.kwargs)
            self.assertFalse(get.call_args.kwargs["allow_redirects"])

    @patch("mcp_server_tapd.attachment_download.requests.get")
    def test_rejects_unrelated_attachment_and_untrusted_download_host(self, get):
        with tempfile.TemporaryDirectory(dir="tmp") as temp:
            self.rows[0]["Attachment"]["entry_id"] = "999"
            with self.assertRaises(AttachmentDownloadError):
                download_ticket_files(self.client, 21, "211", "story", temp, attachment_id="1")
            self.client.get_ticket_attachment_download.assert_not_called()
            self.rows[0]["Attachment"]["entry_id"] = "211"
            self.client.get_ticket_attachment_download.return_value["data"]["Attachment"]["download_url"] = "https://file.tapd.cn.example.invalid/private"
            files = download_ticket_files(self.client, 21, "211", "story", temp)["attachments"]
            self.assertTrue(all(item["status"] == "failed" for item in files))
            self.assertEqual(list(Path(temp).iterdir()), [])
            get.assert_not_called()
