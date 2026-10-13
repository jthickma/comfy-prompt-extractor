import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zlib

from comfy_extract import (ExtractionError, collect_metadata, extract, main,
                           prompt_texts, save_outputs)


PROMPT = {"6": {"class_type": "CLIPTextEncode", "inputs": {"text": "A fox 🦊 in snow"}},
          "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "blurry"}},
          "8": {"class_type": "KSampler", "inputs": {"positive": ["6", 0]}}}
WORKFLOW = {"nodes": [{"id": 6, "type": "CLIPTextEncode",
                       "widgets_values": ["A fox 🦊 in snow"]}], "links": []}


def png(path, tags):
    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff))
    data = b"\x89PNG\r\n\x1a\n"
    data += chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    for key, value in tags.items():
        data += chunk(b"iTXt", key.encode() + b"\0\0\0\0\0" + json.dumps(value).encode())
    data += chunk(b"IDAT", zlib.compress(b"\0\xff\0\0")) + chunk(b"IEND", b"")
    path.write_bytes(data)


class ParsingTests(unittest.TestCase):
    def parse(self, data):
        found, warnings = {}, []
        collect_metadata(data, "fixture", found, warnings)
        return {key: entry[0] for key, entry in found.items()}, warnings

    def test_direct_grouped_tags(self):
        found, warnings = self.parse({"PNG:Prompt": json.dumps(PROMPT),
                                     "PNG:Workflow": json.dumps(WORKFLOW)})
        self.assertEqual(found, {"prompt": PROMPT, "workflow": WORKFLOW})
        self.assertEqual(warnings, [])

    def test_video_comment_and_stream_tags(self):
        found, _ = self.parse({"format": {"tags": {"comment": json.dumps({"prompt": PROMPT})}},
                               "streams": [{"tags": {"WORKFLOW": json.dumps(WORKFLOW)}}]})
        self.assertEqual(found, {"prompt": PROMPT, "workflow": WORKFLOW})

    def test_webp_prefixes_and_double_encoding(self):
        found, _ = self.parse({"IFD0:Make": "prompt:" + json.dumps(PROMPT),
                               "IFD0:Model": "workflow:" + json.dumps(WORKFLOW)})
        self.assertEqual(found["workflow"], WORKFLOW)
        nested, _ = self.parse({"Comment": json.dumps(json.dumps({"prompt": PROMPT}))})
        self.assertEqual(nested["prompt"], PROMPT)

    def test_invalid_json_is_not_workflow(self):
        found, warnings = self.parse({"Workflow": "{broken", "Prompt": "{broken"})
        self.assertEqual(found, {})
        self.assertEqual(len(warnings), 2)

    def test_conflicts_preserve_first(self):
        found, warnings = self.parse([{"Prompt": PROMPT}, {"Prompt": "different"}])
        self.assertEqual(found["prompt"], PROMPT)
        self.assertIn("Conflicting", warnings[0])

    def test_unrelated_comment_is_not_prompt(self):
        found, _ = self.parse({"Comment": "Created with ComfyUI", "Title": "a fox"})
        self.assertEqual(found, {})

    def test_text_candidates_have_provenance(self):
        texts = prompt_texts(PROMPT, WORKFLOW)
        self.assertEqual([item["text"] for item in texts], ["A fox 🦊 in snow", "blurry"])
        self.assertEqual(texts[0]["node_id"], "6")
        self.assertEqual(prompt_texts(None, WORKFLOW)[0]["source"], "workflow")

    def test_unusual_workflow_is_preserved_without_text_guessing(self):
        for nodes in (None, {}, "invalid", [None, {"type": "CustomPrompt", "widgets_values": ["text"]}]):
            self.assertEqual(prompt_texts(None, {"nodes": nodes}), [])

    def test_no_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "empty.png"
            path.touch()
            with patch("comfy_extract.shutil.which", return_value=None):
                with self.assertRaisesRegex(ExtractionError, "Install ExifTool"):
                    extract(path)

    def test_missing_file(self):
        with self.assertRaisesRegex(ExtractionError, "not a file"):
            extract("/nonexistent/comfy-extractor-fixture.png")

    def test_timeout_diagnostic(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "empty.png"
            path.touch()
            with patch("comfy_extract.shutil.which", return_value="reader"), patch(
                "comfy_extract.subprocess.run", side_effect=subprocess.TimeoutExpired("reader", 1)
            ):
                with self.assertRaisesRegex(ExtractionError, "timed out"):
                    extract(path, 1)

    def test_ffprobe_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "empty.mp4"
            path.touch()
            with patch("comfy_extract.shutil.which", side_effect=lambda tool: tool), patch(
                "comfy_extract.read_tool", side_effect=[[], {"format": {"tags": {
                    "comment": json.dumps({"prompt": PROMPT, "workflow": WORKFLOW})}}}]
            ) as reader:
                self.assertEqual(extract(path)["workflow"], WORKFLOW)
                self.assertEqual(reader.call_count, 2)


@unittest.skipUnless(shutil.which("exiftool"), "ExifTool required")
class MediaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def test_png_roundtrip_and_exports(self):
        path = self.directory / "file with spaces.png"
        png(path, {"prompt": PROMPT, "workflow": WORKFLOW})
        original = hashlib.sha256(path.read_bytes()).hexdigest()
        result = extract(path)
        self.assertEqual(result["prompt"], PROMPT)
        self.assertEqual(result["workflow"], WORKFLOW)
        self.assertEqual(result["warnings"], [])
        outputs = save_outputs(result, self.directory / "exports")
        self.assertEqual(len(outputs), 4)
        self.assertEqual(json.loads(outputs[2].read_text()), WORKFLOW)
        self.assertIn("🦊", outputs[3].read_text())
        with self.assertRaisesRegex(ExtractionError, "Output exists"):
            save_outputs(result, self.directory / "exports")
        save_outputs(result, self.directory / "exports", force=True)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), original)

    def test_partial_and_missing_metadata(self):
        path = self.directory / "partial.png"
        png(path, {"workflow": WORKFLOW})
        result = extract(path)
        self.assertIsNone(result["prompt"])
        self.assertIn("No embedded prompt found.", result["warnings"])
        self.assertEqual(len(save_outputs(result, self.directory / "exports")), 3)
        png(path, {})
        with self.assertRaisesRegex(ExtractionError, "No embedded"):
            extract(path)

    def test_cli_json(self):
        path = self.directory / "cli.png"
        png(path, {"prompt": PROMPT, "workflow": WORKFLOW})
        process = subprocess.run(["python3", "comfy_extract.py", str(path)],
                                 capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout)["workflow"], WORKFLOW)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
    def test_video_roundtrips(self):
        for extension in ("mp4", "mov", "mkv", "webm"):
            with self.subTest(extension=extension):
                path = self.directory / f"video.{extension}"
                codec = "libvpx-vp9" if extension == "webm" else "libx264"
                args = ["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                        "color=c=red:s=16x16:d=0.1", "-c:v", codec,
                        "-metadata", "comment=" + json.dumps({"prompt": PROMPT, "workflow": WORKFLOW}),
                        str(path)]
                subprocess.run(args, check=True, capture_output=True)
                result = extract(path)
                self.assertEqual(result["prompt"], PROMPT)
                self.assertEqual(result["workflow"], WORKFLOW)
                # Prove ffprobe alone can read the video comments.
                real_which = shutil.which
                with patch("comfy_extract.shutil.which",
                           side_effect=lambda tool: real_which(tool) if tool == "ffprobe" else None):
                    self.assertEqual(extract(path)["workflow"], WORKFLOW)

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg required")
    def test_webp_exif_prefixes(self):
        path = self.directory / "image.webp"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                        "color=c=red:s=16x16", "-frames:v", "1", str(path)],
                       check=True, capture_output=True)
        subprocess.run(["exiftool", "-overwrite_original",
                        "-Make=prompt:" + json.dumps(PROMPT),
                        "-Model=workflow:" + json.dumps(WORKFLOW), str(path)],
                       check=True, capture_output=True)
        self.assertEqual(extract(path)["workflow"], WORKFLOW)

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg required")
    def test_jpeg_user_comment(self):
        path = self.directory / "image.jpg"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                        "color=c=red:s=16x16", "-frames:v", "1", str(path)],
                       check=True, capture_output=True)
        subprocess.run(["exiftool", "-overwrite_original", "-UserComment=" + json.dumps(
            {"prompt": PROMPT, "workflow": WORKFLOW}), str(path)], check=True, capture_output=True)
        self.assertEqual(extract(path)["prompt"], PROMPT)


if __name__ == "__main__":
    unittest.main()
