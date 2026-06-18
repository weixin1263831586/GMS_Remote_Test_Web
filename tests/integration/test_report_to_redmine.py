import unittest


class _Client:
    def __init__(self):
        self.closed = False
        self.uploaded = []
        self.replies = []

    async def upload_file(self, content, filename, content_type):
        self.uploaded.append((content, filename, content_type))
        return f"token-{filename}"

    async def reply_issue(self, issue_id, notes, files):
        self.replies.append((issue_id, notes, files))
        return {"issue_id": issue_id, "updated": True}

    async def close(self):
        self.closed = True


class ReportToRedmineWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_publish_uploads_files_replies_and_closes_client(self):
        from workflows.report_to_redmine import ReportToRedmineWorkflow

        client = _Client()
        workflow = ReportToRedmineWorkflow(lambda: client)

        result = await workflow.publish(
            issue_id="123",
            notes="report ready",
            files=[
                {
                    "filename": "result.zip",
                    "content": b"zip",
                    "content_type": "application/zip",
                }
            ],
        )

        self.assertTrue(result["updated"])
        self.assertEqual(client.uploaded[0][1], "result.zip")
        self.assertEqual(
            client.replies[0][2],
            [
                {
                    "token": "token-result.zip",
                    "filename": "result.zip",
                    "content_type": "application/zip",
                }
            ],
        )
        self.assertTrue(client.closed)


if __name__ == "__main__":
    unittest.main()
