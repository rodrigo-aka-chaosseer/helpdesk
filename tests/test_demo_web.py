import io
import tempfile
import unittest
from unittest.mock import Mock
from pathlib import Path
from unittest.mock import patch

from demo.app import create_app


def response(status_code, body, headers=None):
    result = Mock()
    result.status_code = status_code
    result.json.return_value = body
    result.headers = headers or {}
    result.content = b"demo attachment"
    return result


class DemoWebTests(unittest.TestCase):
    def test_api_url_is_loaded_from_localstack_state_file(self):
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "test-state.env"
            state_file.write_text(
                "STACK_NAME=helpdesk-local\n"
                "API_URL=http://localhost:4566/restapis/example/Prod/_user_request_\n",
                encoding="utf-8",
            )
            app = create_app(state_file=state_file)

        self.assertEqual(
            app.config["API_BASE"],
            "http://localhost:4566/restapis/example/Prod/_user_request_",
        )

    def test_list_tickets_forwards_only_supported_filters(self):
        app = create_app("http://localhost:4566/restapis/example/Prod/_user_request_")
        client = app.test_client()
        upstream = response(200, {"items": [], "nextToken": None})
        with patch("demo.app.requests.request", return_value=upstream) as request_mock:
            result = client.get(
                "/api/chamados?status=ABERTO&limit=5&ignored=drop"
            )

        self.assertEqual(result.status_code, 200)
        request_mock.assert_called_once_with(
            "GET",
            "http://localhost:4566/restapis/example/Prod/_user_request_/chamados",
            timeout=20,
            params={"status": "ABERTO", "limit": "5"},
        )

    def test_attachment_upload_uses_api_presign_upload_and_confirmation(self):
        app = create_app(
            "http://localhost:4566/restapis/example/Prod/_user_request_",
            "http://localhost:4566",
        )
        client = app.test_client()
        api_responses = [
            response(
                201,
                {
                    "anexoId": "attachment-id",
                    "uploadUrl": "http://localstack:4566/bucket/",
                    "fields": {"key": "anexos/key", "tagging": "<Tagging />"},
                },
            ),
            response(200, {"anexoId": "attachment-id", "status": "CONFIRMADO"}),
        ]
        with (
            patch("demo.app.requests.request", side_effect=api_responses) as api_mock,
            patch(
                "demo.app.requests.post", return_value=response(204, {})
            ) as s3_mock,
        ):
            result = client.post(
                "/api/chamados/ticket-id/anexos",
                data={"arquivo": (io.BytesIO(b"hello"), "hello.txt", "text/plain")},
                content_type="multipart/form-data",
            )

        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.get_json()["status"], "CONFIRMADO")
        self.assertEqual(
            s3_mock.call_args.args[0], "http://localhost:4566/bucket/"
        )
        self.assertNotIn("Host", s3_mock.call_args.kwargs["headers"])
        self.assertEqual(api_mock.call_count, 2)


if __name__ == "__main__":
    unittest.main()
