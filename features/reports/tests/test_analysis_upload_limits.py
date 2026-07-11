import asyncio
import io
import unittest
from unittest.mock import patch

from starlette.datastructures import UploadFile

from features.reports import analysis_api
from features.reports import uploads as report_uploads
from features.reports.api_helpers import AnalysisMode


class ReportUploadLimitTests(unittest.TestCase):
    def test_single_upload_over_limit_returns_payload_too_large(self):
        upload = UploadFile(filename='report.zip', file=io.BytesIO(b'1234'))

        with patch.object(report_uploads, 'MAX_REPORT_UPLOAD_BYTES', 3):
            response = asyncio.run(
                analysis_api.analyze_reports(
                    mode=AnalysisMode.UPLOAD,
                    file=upload,
                )
            )

        self.assertEqual(response.status_code, 413)

    def test_multiple_uploads_share_one_total_budget(self):
        uploads = [
            UploadFile(filename='a.txt', file=io.BytesIO(b'12')),
            UploadFile(filename='b.txt', file=io.BytesIO(b'34')),
        ]

        with patch.object(report_uploads, 'MAX_REPORT_UPLOAD_BYTES', 3):
            response = asyncio.run(
                analysis_api.analyze_reports(
                    mode=AnalysisMode.UPLOAD,
                    file=None,
                    files=uploads,
                    files_array=None,
                )
            )

        self.assertEqual(response.status_code, 413)


if __name__ == '__main__':
    unittest.main()
