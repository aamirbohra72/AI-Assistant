import base64
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from jsonschema import ValidationError
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from app.services import resume_parser


def pdf_parts(text: str | None = None) -> list[dict]:
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    if text:
        font = DictionaryObject({
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        })
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font}),
        })
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("ascii"))
        page[NameObject("/Contents")] = stream
    buffer = io.BytesIO()
    writer.write(buffer)
    return [{"inlineData": {"mimeType": "application/pdf", "data": base64.b64encode(buffer.getvalue()).decode()}}]


class ResumeParserTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.settings = SimpleNamespace(
            RESUME_PROVIDER="groq", GROQ_API_KEY="test-only-key",
            GROQ_RESUME_MODEL="llama-3.3-70b-versatile",
            GEMINI_FLASH_MODEL="primary", GEMINI_RESUME_FALLBACK_MODEL="fallback",
        )
        self.result = {
            "full_name": "Test Candidate", "skills": ["Python"],
            "experience": [], "education": [], "claims_to_validate": [],
        }
        self.settings_patch = patch.object(resume_parser, "get_settings", return_value=self.settings)
        self.settings_patch.start()
        self.addCleanup(self.settings_patch.stop)

    def response(self, data: dict, finish_reason: str = "stop") -> httpx.Response:
        return httpx.Response(200, json={"choices": [{
            "finish_reason": finish_reason, "message": {"content": json.dumps(data)},
        }]})

    def test_pdf_text_extraction(self) -> None:
        self.assertIn("Test Candidate Python", resume_parser._resume_text(pdf_parts("Test Candidate Python")))

    def test_scanned_pdf_rejected(self) -> None:
        with self.assertRaisesRegex(resume_parser.UnsupportedResumeError, "OCR"):
            resume_parser._resume_text(pdf_parts())

    def test_docx_text_parts_preserved(self) -> None:
        self.assertEqual(resume_parser._resume_text([{"text": "RESUME TEXT:\nCandidate"}]), "RESUME TEXT:\nCandidate")

    async def test_groq_request_and_result(self) -> None:
        with patch.object(resume_parser, "request_with_retry", new_callable=AsyncMock) as request, \
                patch.object(resume_parser.gemini, "generate_json", new_callable=AsyncMock) as gemini:
            request.return_value = self.response(self.result)
            result = await resume_parser.parse_resume(pdf_parts("Test Candidate Python"))
            self.assertEqual(result, self.result)
            gemini.assert_not_awaited()
            args, kwargs = request.await_args
            self.assertEqual(args, ("POST", "https://api.groq.com/openai/v1/chat/completions"))
            self.assertEqual(kwargs["headers"]["Authorization"], "Bearer test-only-key")
            self.assertEqual(kwargs["json"]["response_format"], {"type": "json_object"})
            self.assertIn("Test Candidate Python", kwargs["json"]["messages"][1]["content"])

    async def test_invalid_schema_rejected(self) -> None:
        with patch.object(resume_parser, "request_with_retry", new_callable=AsyncMock) as request:
            request.return_value = self.response({"skills": []})
            with self.assertRaises(ValidationError):
                await resume_parser.parse_resume([{"text": "Candidate"}])

    async def test_truncated_result_rejected(self) -> None:
        with patch.object(resume_parser, "request_with_retry", new_callable=AsyncMock) as request:
            request.return_value = self.response(self.result, "length")
            with self.assertRaisesRegex(ValueError, "did not finish"):
                await resume_parser.parse_resume([{"text": "Candidate"}])

    async def test_missing_key_fails_without_request(self) -> None:
        self.settings.GROQ_API_KEY = ""
        with patch.object(resume_parser, "request_with_retry", new_callable=AsyncMock) as request:
            with self.assertRaisesRegex(ValueError, "GROQ_API_KEY"):
                await resume_parser.parse_resume([{"text": "Candidate"}])
            request.assert_not_awaited()

    async def test_gemini_default_unchanged(self) -> None:
        self.settings.RESUME_PROVIDER = "gemini"
        with patch.object(resume_parser.gemini, "generate_json", new_callable=AsyncMock) as generate:
            generate.return_value = self.result
            self.assertEqual(await resume_parser.parse_resume([{"text": "Candidate"}]), self.result)
            self.assertEqual(generate.await_args.kwargs["model"], "primary")

    async def test_gemini_503_still_uses_fallback(self) -> None:
        self.settings.RESUME_PROVIDER = "gemini"
        response = httpx.Response(503, request=httpx.Request("POST", "https://example.test"))
        error = httpx.HTTPStatusError("Unavailable", request=response.request, response=response)
        with patch.object(resume_parser.gemini, "generate_json", new_callable=AsyncMock) as generate:
            generate.side_effect = [error, self.result]
            self.assertEqual(await resume_parser.parse_resume([{"text": "Candidate"}]), self.result)
            self.assertEqual(generate.await_args.kwargs["model"], "fallback")


if __name__ == "__main__":
    unittest.main()