# ComfyUI prompt extractor

A small local command-line tool. Give it a generated image or video; get the
embedded ComfyUI **prompt JSON**, **workflow JSON**, and readable prompt text.
Source media is read only. No Python packages or online services are needed.

## Requirements

Python 3.9+ and ExifTool and/or ffprobe. Install both for the broadest coverage:

```sh
sudo apt install python3 libimage-exiftool-perl ffmpeg
```

## Use

From this folder:

```sh
python3 comfy_extract.py '/path/to/image.png'
python3 comfy_extract.py '/path/to/video.mp4' --output-dir './extracted'
```

The result is printed as JSON. `--output-dir` also saves:

- `video.mp4.comfy.json`: full result, including sources and warnings.
- `video.mp4.prompt.json`: original embedded API prompt (or a JSON string if plain text).
- `video.mp4.workflow.json`: original embedded workflow, ready to load into ComfyUI.
- `video.mp4.prompts.txt`: readable text candidates, labeled by node and input.

Exports are created only for available metadata. Existing files are protected;
use `--force` to replace exports. Filenames retain the media extension to avoid
collisions. JSON goes to stdout; status and errors go to stderr, so redirection works:

```sh
python3 comfy_extract.py '/path/to/image.png' > result.json
```

Exit status is `0` if at least one of prompt/workflow is recovered and `1` on
failure. Partial extraction includes warnings and `null` for missing fields.
`--timeout 60` sets the maximum wait per metadata reader.

## What it reads

ExifTool reads image/video metadata first. If either prompt or workflow is still
missing, ffprobe checks container and stream tags. Supported storage patterns:

- Named `Prompt` and `Workflow` tags (including PNG text chunks).
- JSON wrappers in `Comment`, `UserComment`, description and metadata fields.
- WebP EXIF `Make` / `Model` values prefixed with `prompt:` / `workflow:`.
- Video format and stream tags, including JSON comments used by video savers.
- JSON stored as an escaped JSON string.

PNG, JPEG, WebP, GIF, MP4, MOV, WebM and MKV can be inspected when the installed
readers support them. Recovery depends on what the saver actually embedded.
ComfyUI's [SaveImage implementation](https://github.com/Comfy-Org/ComfyUI/blob/master/nodes.py)
stores the API prompt and workflow metadata, and
[VideoHelperSuite](https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite/blob/main/videohelpersuite/nodes.py)
supports embedded video metadata.

The API prompt is a graph, not necessarily a single sentence. `prompt_texts`
lists literal text/positive/negative/prompt inputs and uses known `CLIPTextEncode`
workflow widgets as a fallback. It does not label every text as the final executed
positive or negative prompt, resolve arbitrary custom nodes, or reconstruct
runtime-generated text that was never saved. The full graphs are preserved.
Missing or stripped metadata cannot be reconstructed from the pixels.

## Verify

```sh
python3 -m unittest discover -s tests -v
```

Tests exercise parsing and real local metadata extraction using small generated
image/video fixtures. Integration cases skip when the required tools are absent.
