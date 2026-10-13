#!/usr/bin/env python3
"""Read embedded ComfyUI metadata without modifying the source media."""

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys


class ExtractionError(Exception):
    pass


def decode(value):
    """Handle ordinary JSON, double-encoded JSON and EXIF ASCII markers."""
    for _ in range(4):
        if not isinstance(value, str):
            break
        cleaned = value.strip().strip("\x00")
        if cleaned.startswith("ASCII\x00\x00\x00"):
            cleaned = cleaned[8:]
        try:
            value = json.loads(cleaned)
        except (ValueError, RecursionError):
            return cleaned
    return value


def collect_metadata(metadata, source, found, warnings):
    """Recognize named tags and JSON comment wrappers, never arbitrary text."""
    def accept(kind, raw, location):
        value = decode(raw)
        if value is None or value == "":
            return
        if kind == "workflow" and not isinstance(value, dict):
            warnings.append(f"Invalid workflow JSON at {location}.")
            return
        if kind == "prompt" and not isinstance(value, (dict, str)):
            warnings.append(f"Invalid prompt metadata at {location}.")
            return
        if kind == "prompt" and isinstance(value, str) and value.startswith(("{", "[")):
            warnings.append(f"Invalid prompt JSON at {location}.")
            return
        if kind in found:
            if found[kind][0] != value:
                warnings.append(f"Conflicting {kind} at {location}; keeping {found[kind][1]}.")
        else:
            found[kind] = (value, location)

    def visit(obj, location, depth=0):
        if depth > 12:
            return
        obj = decode(obj)
        if isinstance(obj, list):
            for index, entry in enumerate(obj):
                visit(entry, f"{location}[{index}]", depth + 1)
        elif isinstance(obj, dict):
            for key, value in obj.items():
                name = key.rsplit(":", 1)[-1].lower()
                tag_location = f"{location}.{key}"
                if name in ("prompt", "workflow"):
                    accept(name, value, tag_location)
                elif name in ("comment", "usercomment", "description", "imagedescription",
                              "parameters", "metadata", "extra_pnginfo", "exif", "tags",
                              "format", "streams"):
                    visit(value, tag_location, depth + 1)
                elif name in ("make", "model") and isinstance(value, str):
                    # ComfyUI's WebP saver uses these EXIF tags with named prefixes.
                    visit(value, tag_location, depth + 1)
        elif isinstance(obj, str):
            for kind in ("prompt", "workflow"):
                if obj.lower().startswith(kind + ":"):
                    accept(kind, obj[len(kind) + 1:], location)
                    break

    visit(metadata, source)


def prompt_texts(prompt, workflow):
    """List stored text candidates with provenance; do not guess graph execution."""
    result = []
    if isinstance(prompt, str):
        return [{"source": "prompt", "text": prompt}]
    if isinstance(prompt, dict):
        for node_id, node in prompt.items():
            if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
                continue
            for field, value in node["inputs"].items():
                name = field.lower()
                if isinstance(value, str) and value.strip() and (
                    name in ("text", "text_g", "text_l", "positive", "negative")
                    or "prompt" in name
                ):
                    result.append({"source": "prompt", "node_id": str(node_id),
                                   "node_type": node.get("class_type", ""),
                                   "field": field, "text": value})
    # UI widget positions are node-specific. Only recognize known text encoders.
    if not result and isinstance(workflow, dict):
        nodes = workflow.get("nodes", [])
        if not isinstance(nodes, list):
            return result
        for node in nodes:
            if not isinstance(node, dict):
                continue
            if node.get("type") == "CLIPTextEncode":
                widgets = node.get("widgets_values", [])
                if isinstance(widgets, list) and widgets and isinstance(widgets[0], str):
                    result.append({"source": "workflow", "node_id": str(node.get("id", "")),
                                   "node_type": node["type"], "field": "text",
                                   "text": widgets[0]})
    return result


def read_tool(command, timeout):
    try:
        process = subprocess.run(command, capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise ExtractionError(f"{Path(command[0]).name} timed out after {timeout:g}s") from exc
    except OSError as exc:
        raise ExtractionError(f"Cannot run {command[0]}: {exc}") from exc
    if process.returncode:
        detail = process.stderr.strip() or "could not read this file"
        raise ExtractionError(f"{Path(command[0]).name}: {detail}")
    try:
        return json.loads(process.stdout)
    except (ValueError, RecursionError) as exc:
        raise ExtractionError(f"{Path(command[0]).name} returned invalid JSON") from exc


def extract(path, timeout=30):
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise ExtractionError(f"Input is not a file: {path}")
    found, warnings, errors = {}, [], []
    readers = [
        ("exiftool", ["-j", "-G1", "-a", "-s", str(path)]),
        ("ffprobe", ["-v", "error", "-show_entries", "format_tags:stream_tags",
                     "-of", "json", str(path)]),
    ]
    available = 0
    for tool, arguments in readers:
        executable = shutil.which(tool)
        if not executable:
            continue
        available += 1
        try:
            metadata = read_tool([executable, *arguments], timeout)
            collect_metadata(metadata, tool, found, warnings)
        except ExtractionError as exc:
            errors.append(str(exc))
        if "prompt" in found and "workflow" in found:
            break
    if not available:
        raise ExtractionError("Install ExifTool and/or ffprobe (FFmpeg) to read media metadata.")
    if not found:
        detail = "No embedded ComfyUI prompt or workflow found. Metadata may be absent or stripped."
        if errors:
            detail += "\n" + "\n".join(errors)
        if warnings:
            detail += "\n" + "\n".join(warnings)
        raise ExtractionError(detail)
    warnings.extend(errors)
    for kind in ("prompt", "workflow"):
        if kind not in found:
            warnings.append(f"No embedded {kind} found.")
    prompt = found.get("prompt", (None,))[0]
    workflow = found.get("workflow", (None,))[0]
    return {"input_file": str(path), "prompt": prompt, "workflow": workflow,
            "prompt_texts": prompt_texts(prompt, workflow),
            "metadata_sources": {key: value[1] for key, value in found.items()},
            "warnings": warnings}


def json_text(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def save_outputs(result, output_dir, force=False):
    directory = Path(output_dir).expanduser().resolve()
    # Keep the media extension so image.png and image.mp4 cannot collide.
    base = directory / Path(result["input_file"]).name
    outputs = {Path(str(base) + ".comfy.json"): json_text(result)}
    for kind in ("prompt", "workflow"):
        if result[kind] is not None:
            outputs[Path(str(base) + f".{kind}.json")] = json_text(result[kind])
    if result["prompt_texts"]:
        text = "\n\n".join(
            f"[{item.get('node_id', 'prompt')} / {item.get('field', 'text')}]\n{item['text']}"
            for item in result["prompt_texts"]
        ) + "\n"
        outputs[Path(str(base) + ".prompts.txt")] = text
    source = Path(result["input_file"])
    for target in outputs:
        if target.resolve() == source or (target.exists() and target.samefile(source)):
            raise ExtractionError(f"Refusing to overwrite source media: {target}")
        if target.exists() and not force:
            raise ExtractionError(f"Output exists: {target}. Use --force to replace exports.")
    directory.mkdir(parents=True, exist_ok=True)
    for target, content in outputs.items():
        with target.open("w" if force else "x", encoding="utf-8") as handle:
            handle.write(content)
    return list(outputs)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", help="ComfyUI-generated image or video")
    parser.add_argument("-o", "--output-dir", help="Also save JSON and readable text exports here")
    parser.add_argument("--force", action="store_true", help="Replace existing exports")
    parser.add_argument("--timeout", type=float, default=30, help="Seconds per reader (default: 30)")
    args = parser.parse_args(argv)
    if not 0 < args.timeout < float("inf"):
        parser.error("--timeout must be a positive finite number")
    try:
        result = extract(args.file, args.timeout)
        if args.output_dir:
            for path in save_outputs(result, args.output_dir, args.force):
                print(f"Saved: {path}", file=sys.stderr)
        print(json_text(result), end="")
        for warning in result["warnings"]:
            print(f"Warning: {warning}", file=sys.stderr)
        return 0
    except (ExtractionError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
