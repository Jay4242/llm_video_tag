# LLM Video Tag Plugin

This Stash plugin suggests tags for video scenes using a vision-capable LLM (OpenAI-compatible API) combined with ffmpeg scene detection.

Features
- Adds a UI dropdown action on the scene page: "Tag scene (LLM)".
- Detects scene changes via ffmpeg's `select` filter and extracts keyframes at scene boundaries.
- Supplements scene-change frames with evenly-spaced frames to reach a configurable target count.
- Sends extracted frames (base64-encoded PNGs, kept entirely in memory) to your configured LLM endpoint.
- Displays suggested tags in a selection modal with live progress and the LLM's reasoning output.
- Optionally provides the LLM with existing tag names, descriptions, and custom tag images for better matching.
- Applies selected tags to the scene via GraphQL mutations (creates new tags as needed).
- Registers as a task via `window.registerTask` for task-compatible UIs.

Notes
- Requires **ffmpeg** and **ffprobe** on the system PATH.
- The Python backend uses only the standard library — no pip packages are required.
- The plugin writes intermediate results to a `results/` directory (served as plugin assets for the UI).
  - Streaming progress files are deleted once the final result is written.
  - Completed result files are automatically cleaned up on the next plugin run if older than 1 hour.
- Frame extraction is entirely in-memory; no frame files are written to disk.

Configuration (Settings)
- **llmBaseUrl** (env: `LLM_BASE_URL`; default `http://localhost:11434/v1`) — OpenAI-compatible API base URL.
- **llmModel** (env: `LLM_MODEL`; default `gemma3:4b-it-q8_0`) — Model name for tagging.
- **llmTemp** (env: `LLM_TEMP`; default `0.7`) — Sampling temperature.
- **llmMaxTokens** (env: `LLM_MAX_TOKENS`; default `-1` = backend default) — Maximum tokens to request.
- **llmTimeout** (env: `LLM_TIMEOUT`; default `3600`) — Timeout in seconds for LLM requests.
- **sceneThreshold** (env: `LLM_SCENE_THRESHOLD`; default `0.3`) — ffmpeg scene detection sensitivity (0.0–1.0). Higher values detect fewer scene changes.
- **maxFrames** (env: `LLM_MAX_FRAMES`; default `20`) — Maximum number of frames to extract and send to the LLM.
- **frameWidth** (env: `LLM_FRAME_WIDTH`; default `640`) — Resize extracted frames to this width in pixels (`-1` to keep original size).
- **includeTagDescriptions** (env: `LLM_INCLUDE_TAG_DESCRIPTIONS`; default `true`) — Send tag descriptions alongside tag names in the LLM prompt.
- **includeTagImages** (env: `LLM_INCLUDE_TAG_IMAGES`; default `false`) — Send each tag's custom image to help the LLM visually match tags. Only tags with manually-set images are included; generated default placeholders are skipped.
- **zzdebugTracing** (BOOLEAN; default `false`) — Enables extra debug logging.

Configuration (Environment Variables)
- `LLM_API_KEY`: API key for authenticated LLM endpoints.
- `LLM_TAG_PROMPT`: Custom system prompt override for the tagging assistant.

Installation
1. Ensure **ffmpeg** and **ffprobe** are installed and available on the system PATH.
2. Place this folder in your Stash plugins directory as `llm_video_tag`.
3. Reload plugins in the Stash UI.

Usage
- Open a scene page and use the operations menu (three dots) to run "Tag scene (LLM)", or use the registered task if your UI supports it.
- A modal will open showing LLM progress and reasoning, then suggested tags with checkboxes. Select the tags you want and click "Apply Tags".
