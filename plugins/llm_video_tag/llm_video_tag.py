#!/usr/bin/env python3
"""
LLM Video Tag Plugin

Uses a vision-capable LLM to suggest tags for a video scene. Scene-change frames
are extracted via ffmpeg's scene detection, combined with evenly-spaced frames to
hit a target count, and sent to the LLM as a series of frame images. All frame
data is kept in memory — nothing is written to disk.

Self-contained; does not require the CommunityScrapers repo at runtime.
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
import subprocess
import sys
import traceback
import urllib.error
import urllib.request
from typing import Any, Optional, List, Dict

# Stash helper classes
try:
    from StashPluginHelper import StashPluginHelper, taskQueue  # type: ignore
except Exception:
    from stash_helper_fallback import StashPluginHelper, taskQueue  # type: ignore

# ----------------------------
# Configuration and utilities
# ----------------------------

DEFAULT_BASE_URL = "http://localhost:11434/v1"
DEFAULT_MODEL = "gemma3:4b-it-q8_0"
DEFAULT_TEMP = 0.7
DEFAULT_MAX_TOKENS = -1
DEFAULT_TIMEOUT = 3600.0
DEFAULT_SCENE_THRESHOLD = 0.3
DEFAULT_MAX_FRAMES = 20
DEFAULT_FRAME_WIDTH = 640

PROMPT_DEFAULT = (
    "You are a tagging assistant. You will be shown a series of frames extracted from a video "
    "at key scenes and evenly-spaced intervals. Look carefully at ALL frames and return ONLY a "
    "JSON array of short, general-purpose tags that describe the video content AS A WHOLE. "
    "Include as many tags as are applicable; there is no strict upper limit. "
    "Use lowercase ASCII letters/digits; multiword tags may contain spaces. "
    "Do NOT use dashes; use spaces between words. "
    "Do NOT guess or infer hidden attributes. Include a tag only if it is clearly visible in "
    "at least one of the frames. "
    "No private data, no people-identification or names, no hashes, no numbering, no explanations, "
    "no code fences, no extra text."
)

settings = {
    "llmModel": DEFAULT_MODEL,
    "llmTemp": DEFAULT_TEMP,
    "llmMaxTokens": DEFAULT_MAX_TOKENS,
    "llmTimeout": DEFAULT_TIMEOUT,
    "sceneThreshold": DEFAULT_SCENE_THRESHOLD,
    "maxFrames": DEFAULT_MAX_FRAMES,
    "frameWidth": DEFAULT_FRAME_WIDTH,
    "zzdebugTracing": False,
}

try:
    from llm_video_tag_settings import config  # type: ignore
except Exception:
    config = {}

stash = StashPluginHelper(settings=settings, config=config, maxbytes=10 * 1024 * 1024)

PNG_MAGIC = b'\x89PNG\r\n\x1a\n'


def _fetch_plugin_setting(name: str) -> Optional[str]:
    try:
        query = """
            query($ids: [ID!]) {
                configuration {
                    plugins(include: $ids)
                }
            }
        """
        variables = {"ids": ["llm_video_tag", "LLMVideoTag"]}
        resp = stash._graphql(query, variables)  # type: ignore[attr-defined]
        if not isinstance(resp, dict):
            return None
        plugins_map = (((resp.get("data") or {}).get("configuration") or {}).get("plugins")) or {}
        if not isinstance(plugins_map, dict):
            return None
        for pid in variables["ids"]:
            settings_map = plugins_map.get(pid)
            if isinstance(settings_map, dict):
                v = settings_map.get(name)
                if v is not None:
                    return str(v)
        return None
    except Exception:
        return None


def _fetch_llm_base_url_from_settings() -> Optional[str]:
    return _fetch_plugin_setting("llmBaseUrl")


def _resolve_base_url() -> str:
    try:
        arg_url = ((stash.JSON_INPUT or {}).get("args") or {}).get("llmBaseUrl") if isinstance(stash.JSON_INPUT, dict) else None
        if isinstance(arg_url, str) and arg_url.strip():
            return arg_url.strip().rstrip("/")
    except Exception:
        pass

    try:
        ui_url = stash.Setting("llmBaseUrl", None)
        if isinstance(ui_url, str) and ui_url.strip():
            return ui_url.strip().rstrip("/")
    except Exception:
        pass

    raw_url = None
    try:
        if isinstance(stash.JSON_INPUT, dict):
            settings_src = stash.JSON_INPUT.get("settings") or {}
            if isinstance(settings_src, dict):
                raw_url = settings_src.get("llmBaseUrl")
            elif isinstance(settings_src, list):
                for item in settings_src:
                    if isinstance(item, dict) and item.get("key") == "llmBaseUrl":
                        raw_url = item.get("value")
                        break
            if not raw_url:
                alt_src = stash.JSON_INPUT.get("pluginSettings") or {}
                if isinstance(alt_src, dict):
                    raw_url = alt_src.get("llmBaseUrl")
                elif isinstance(alt_src, list):
                    for item in alt_src:
                        if isinstance(item, dict) and item.get("key") == "llmBaseUrl":
                            raw_url = item.get("value")
                            break
        if isinstance(raw_url, str) and raw_url.strip():
            return raw_url.strip().rstrip("/")
    except Exception:
        pass

    try:
        fetched = _fetch_llm_base_url_from_settings()
        if isinstance(fetched, str) and fetched.strip():
            return fetched.strip().rstrip("/")
    except Exception:
        pass

    env_url = os.getenv("LLM_BASE_URL")
    if isinstance(env_url, str) and env_url.strip():
        return env_url.strip().rstrip("/")

    return DEFAULT_BASE_URL.rstrip("/")


def _env_or_setting(name: str, env: str, default: Any) -> Any:
    v = stash.Setting(name, None)
    if v is None:
        v = os.getenv(env, None)
    if v is None or (isinstance(v, str) and not v.strip()):
        return default
    return v


BASE_URL: str = _resolve_base_url()
MODEL: str = str(_env_or_setting("llmModel", "LLM_MODEL", DEFAULT_MODEL))
TEMP: float = float(_env_or_setting("llmTemp", "LLM_TEMP", DEFAULT_TEMP))
MAX_TOKENS: int = int(_env_or_setting("llmMaxTokens", "LLM_MAX_TOKENS", DEFAULT_MAX_TOKENS))
TIMEOUT: float = float(_env_or_setting("llmTimeout", "LLM_TIMEOUT", DEFAULT_TIMEOUT))
API_KEY: str = os.getenv("LLM_API_KEY", "none")
PROMPT: str = os.getenv("LLM_TAG_PROMPT", PROMPT_DEFAULT)
SCENE_THRESHOLD: float = float(_env_or_setting("sceneThreshold", "LLM_SCENE_THRESHOLD", DEFAULT_SCENE_THRESHOLD))
MAX_FRAMES: int = int(_env_or_setting("maxFrames", "LLM_MAX_FRAMES", DEFAULT_MAX_FRAMES))
FRAME_WIDTH: int = int(_env_or_setting("frameWidth", "LLM_FRAME_WIDTH", DEFAULT_FRAME_WIDTH))
INCLUDE_TAG_DESCRIPTIONS: bool = _env_or_setting("includeTagDescriptions", "LLM_INCLUDE_TAG_DESCRIPTIONS", True)
if isinstance(INCLUDE_TAG_DESCRIPTIONS, str):
    INCLUDE_TAG_DESCRIPTIONS = INCLUDE_TAG_DESCRIPTIONS.strip().lower() in ("true", "1", "yes", "on")

INCLUDE_TAG_IMAGES: bool = _env_or_setting("includeTagImages", "LLM_INCLUDE_TAG_IMAGES", False)
if isinstance(INCLUDE_TAG_IMAGES, str):
    INCLUDE_TAG_IMAGES = INCLUDE_TAG_IMAGES.strip().lower() in ("true", "1", "yes", "on")

MAX_TAG_IMAGES: int = int(_env_or_setting("maxTagImages", "LLM_MAX_TAG_IMAGES", 50))


def _check_ffmpeg() -> None:
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
    except FileNotFoundError:
        raise RuntimeError("ffmpeg not found on PATH. Please install ffmpeg to use this plugin.")
    except subprocess.CalledProcessError:
        raise RuntimeError("ffmpeg is installed but returned an error. Check your ffmpeg installation.")
    try:
        subprocess.run(["ffprobe", "-version"], capture_output=True, check=True)
    except FileNotFoundError:
        raise RuntimeError("ffprobe not found on PATH. It is part of the ffmpeg package.")


# ----------------------------
# HTTP helpers
# ----------------------------

def _http_post_json(url: str, json_body: Dict[str, Any], headers: Optional[Dict[str, str]] = None, timeout: float = TIMEOUT) -> Dict[str, Any]:
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    data = json.dumps(json_body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            try:
                return json.loads(raw.decode("utf-8", errors="ignore"))
            except Exception as e:
                raise RuntimeError(f"Non-JSON response from {url}: {raw[:500]!r} ({e})") from e
    except urllib.error.HTTPError as e:
        detail = getattr(e, "read", lambda: b"")()
        raise RuntimeError(f"HTTP {e.code} {e.reason} from {url}: {detail[:500].decode('utf-8', errors='ignore')}") from e
    except Exception as e:
        raise RuntimeError(f"HTTP POST failed for {url}: {e}") from e


# ----------------------------
# ffmpeg frame extraction
# ----------------------------

def _get_video_info(video_path: str) -> tuple[float, float]:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=duration,r_frame_rate",
         "-of", "json", video_path],
        capture_output=True, text=True
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {proc.stderr.strip()}")
    info = json.loads(proc.stdout)
    streams = info.get("streams", [])
    if not streams:
        raise RuntimeError("No video stream found in file")
    stream = streams[0]
    duration = float(stream.get("duration", 0))
    if duration <= 0:
        raise RuntimeError(f"Video duration is zero or unknown: {duration}")
    fr_str = stream.get("r_frame_rate", "24/1")
    num, den = fr_str.split("/")
    fps = float(num) / float(den) if float(den) != 0 else 24.0
    return duration, fps


def _extract_frames(video_path: str, max_frames: int, scene_threshold: float, frame_width: int) -> List[str]:
    _check_ffmpeg()

    duration, fps = _get_video_info(video_path)
    stash.Log(f"[LLMVideoTag] Video duration={duration:.1f}s fps={fps:.1f}")

    max_frames = max(1, min(max_frames, 50))
    scene_threshold = max(0.01, min(1.0, scene_threshold))
    stash.Log(f"[LLMVideoTag] Extracting up to {max_frames} frames, threshold={scene_threshold}")

    step = max(1, int(duration * fps / max_frames))

    scale_filter = ""
    if frame_width > 0:
        scale_filter = f",scale={frame_width}:-1"

    cmd = [
        "ffmpeg", "-i", video_path,
        "-vf", f"select='gt(scene\\,{scene_threshold})+not(mod(n\\,{step}))'{scale_filter}",
        "-vsync", "vfr", "-f", "image2pipe", "-vcodec", "png", "pipe:1",
        "-loglevel", "error",
        "-y",
    ]
    stash.Trace(f"[LLMVideoTag] ffmpeg: {' '.join(cmd)}")

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        raw_data = proc.stdout.read()
        _, stderr_data = proc.communicate(timeout=300)
        if proc.returncode != 0 and proc.returncode is not None:
            err = stderr_data.decode("utf-8", errors="ignore").strip()[:500]
            stash.Warn(f"[LLMVideoTag] ffmpeg exited with code {proc.returncode}: {err}")
    except FileNotFoundError:
        raise RuntimeError("ffmpeg not found on PATH")
    except subprocess.TimeoutExpired:
        proc.kill()
        raw_data = proc.stdout.read() if proc.stdout else b""
        stash.Warn("[LLMVideoTag] ffmpeg timed out after 300s")

    if not raw_data:
        fallback = _extract_frames_fallback(video_path, max_frames, frame_width)
        if fallback:
            return fallback
        raise RuntimeError("ffmpeg produced no frame data")

    frames_parts = raw_data.split(PNG_MAGIC)
    frames = [PNG_MAGIC + part for part in frames_parts if part]

    stash.Log(f"[LLMVideoTag] Got {len(frames)} raw frames from ffmpeg")

    if not frames:
        fallback = _extract_frames_fallback(video_path, max_frames, frame_width)
        if fallback:
            return fallback
        raise RuntimeError("No frames extracted from video")

    if len(frames) > max_frames:
        indices = [int(i * len(frames) / max_frames) for i in range(max_frames)]
        frames = [frames[i] for i in indices]
        stash.Log(f"[LLMVideoTag] Downsampled to {len(frames)} frames")

    b64_frames = []
    for i, png_data in enumerate(frames):
        b64 = base64.b64encode(png_data).decode("utf-8")
        b64_frames.append(b64)
        stash.Trace(f"[LLMVideoTag] Frame {i + 1}: {len(png_data)} bytes raw, {len(b64)} bytes b64")

    return b64_frames


def _extract_frames_fallback(video_path: str, max_frames: int, frame_width: int) -> List[str]:
    stash.Log("[LLMVideoTag] Using fallback: evenly-spaced frame extraction via fps filter")

    duration, _ = _get_video_info(video_path)
    target_fps = max_frames / duration if duration > 0 else 1.0

    scale_filter = ""
    if frame_width > 0:
        scale_filter = f",scale={frame_width}:-1"

    cmd = [
        "ffmpeg", "-i", video_path,
        "-vf", f"fps={target_fps}{scale_filter}",
        "-f", "image2pipe", "-vcodec", "png", "pipe:1",
        "-loglevel", "error",
        "-y",
    ]

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        raw_data = proc.stdout.read()
        _, stderr_data = proc.communicate(timeout=300)
    except subprocess.TimeoutExpired:
        proc.kill()
        raw_data = proc.stdout.read() if proc.stdout else b""
        return []

    frames_parts = raw_data.split(PNG_MAGIC)
    frames = [PNG_MAGIC + part for part in frames_parts if part]

    stash.Log(f"[LLMVideoTag] Fallback produced {len(frames)} frames")

    if len(frames) > max_frames:
        indices = [int(i * len(frames) / max_frames) for i in range(max_frames)]
        frames = [frames[i] for i in indices]

    return [base64.b64encode(f).decode("utf-8") for f in frames]


# ----------------------------
# LLM integration
# ----------------------------

def _message_content_to_str(msg: Any) -> str:
    if isinstance(msg, str):
        return msg
    if isinstance(msg, list):
        parts: list[str] = []
        for part in msg:
            if isinstance(part, str):
                parts.append(part)
                continue
            if isinstance(part, dict):
                txt = part.get("text") or part.get("content")
                if txt:
                    parts.append(str(txt))
        if parts:
            return "\n".join(parts)
    if msg is None:
        return ""
    try:
        return json.dumps(msg)
    except Exception:
        return str(msg)


def _server_base_url() -> str:
    sc = stash.JSON_INPUT.get("server_connection") or stash.JSON_INPUT.get("serverConnection") or {}
    if isinstance(sc, dict):
        scheme = sc.get("Scheme") or sc.get("scheme") or "http"
        host = sc.get("endpoint") or sc.get("Endpoint") or sc.get("host")
        if host:
            return f"{scheme}://{host}"
    return "http://localhost:9999"


def _fetch_tag_image_base64(image_path: str, tag_name: str) -> Optional[tuple[str, str]]:
    if not image_path:
        return None
    if "default=true" in image_path:
        return None

    base_url = _server_base_url()
    full_url = image_path if image_path.startswith("http") else base_url + image_path

    try:
        req = urllib.request.Request(full_url, method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read()
            content_type = resp.headers.get("Content-Type") or "image/png"
            b64 = base64.b64encode(body).decode("utf-8")
            stash.Log(f"[LLMVideoTag] Fetched tag image for '{tag_name}' ({len(body)} bytes, {content_type})")
            return b64, content_type
    except Exception as e:
        stash.Error(f"[LLMVideoTag] Failed to fetch image for tag '{tag_name}': {e}")
        return None


def _build_messages(b64_frames: List[str], existing_tags: Optional[list[dict[str, str]]] = None) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [{"role": "system", "content": PROMPT}]

    use_images = INCLUDE_TAG_IMAGES or bool(_env_or_setting("includeTagImages", "LLM_INCLUDE_TAG_IMAGES", False))
    if isinstance(use_images, str):
        use_images = use_images.strip().lower() in ("true", "1", "yes", "on")
    if not use_images:
        gql_val = _fetch_plugin_setting("includeTagImages")
        if gql_val is not None:
            use_images = gql_val.strip().lower() in ("true", "1", "yes", "on")
            stash.Log(f"[LLMVideoTag] includeTagImages from GraphQL: {gql_val!r} -> use_images={use_images}")

    _raw_settings = stash.JSON_INPUT.get("settings") if isinstance(stash.JSON_INPUT, dict) else {}
    _raw_psettings = stash.JSON_INPUT.get("pluginSettings") if isinstance(stash.JSON_INPUT, dict) else {}
    if isinstance(_raw_settings, dict):
        stash.Log(f"[LLMVideoTag] JSON_INPUT.settings keys: {sorted(_raw_settings.keys())}")
    elif isinstance(_raw_settings, list):
        stash.Log(f"[LLMVideoTag] JSON_INPUT.settings is list of {len(_raw_settings)} items")
    else:
        stash.Log(f"[LLMVideoTag] JSON_INPUT.settings type: {type(_raw_settings).__name__}")
    if isinstance(_raw_psettings, dict):
        stash.Log(f"[LLMVideoTag] JSON_INPUT.pluginSettings keys: {sorted(_raw_psettings.keys())}")
    elif isinstance(_raw_psettings, list):
        stash.Log(f"[LLMVideoTag] JSON_INPUT.pluginSettings is list of {len(_raw_psettings)} items")

    if existing_tags:
        if use_images:
            tagged = [t for t in existing_tags if t.get("image_path") and "default=true" not in str(t.get("image_path", ""))]
            stash.Log(f"[LLMVideoTag] _build_messages: includeTagImages=True, {len(existing_tags)} tags total, {len(tagged)} with custom images")
        else:
            stash.Log(f"[LLMVideoTag] _build_messages: includeTagImages=False ({INCLUDE_TAG_IMAGES=!r}), skipping tag images")

        intro, payload = _format_tags_for_prompt(existing_tags, INCLUDE_TAG_DESCRIPTIONS)
        tag_text = json.dumps(payload, ensure_ascii=False)
        content: list[dict[str, Any]] = [{"type": "text", "text": f"EXISTING TAGS (provided as input context, these are NOT your output):\n\n{intro}\n\n{tag_text}"}]

        if use_images:
            content.append({"type": "text", "text": "Note: the images below show the tag's representative image. This does NOT guarantee that the tag's content appears in the video — use your own judgment based on the video frames."})
            tag_images_added = 0
            for tag in existing_tags:
                if MAX_TAG_IMAGES > 0 and tag_images_added >= MAX_TAG_IMAGES:
                    break
                image_path = tag.get("image_path") or tag.get("imagePath")
                name = tag.get("name", "")
                try:
                    result = _fetch_tag_image_base64(image_path, name)
                except Exception as e:
                    stash.Error(f"[LLMVideoTag] Exception fetching tag image for '{name}': {e}")
                    continue
                if result:
                    b64, mime = result
                    content.append({"type": "text", "text": f"Image for tag '{name}':"})
                    content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
                    tag_images_added += 1
            if tag_images_added:
                stash.Log(f"[LLMVideoTag] Added {tag_images_added} tag image(s) to prompt")
            else:
                stash.Log(f"[LLMVideoTag] No custom tag images found to include")

        messages.append({
            "role": "user",
            "content": content,
        })

    messages.append({
        "role": "user",
        "content": [{"type": "text", "text": "The following are the frames from the video to analyze:"}],
    })

    for i, b64 in enumerate(b64_frames):
        messages.append({
            "role": "user",
            "content": [
                {"type": "text", "text": f"Frame {i + 1}:"},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
            ],
        })

    messages.append({
        "role": "user",
        "content": [{"type": "text", "text": "Based on the frames above, return a JSON array of tags describing the video content as a whole."}],
    })

    return messages


def _log_prompt(messages: list[dict[str, Any]]) -> None:
    try:
        parts = [f"[LLMVideoTag] Prompt (text only):"]
        image_count = 0
        for m in messages:
            content = m.get("content")
            if isinstance(content, str):
                parts.append(f"  {m.get('role')}: {content}")
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        parts.append(f"  {m.get('role')}: {part.get('text','')}")
                    elif isinstance(part, dict) and part.get("type") == "image_url":
                        image_count += 1
        parts.append(f"  (images: {image_count})")
        stash.Error("\n".join(parts))  # single atomic write to avoid interleaving
    except Exception:
        pass


def _call_llm_b64_frames_nonstreaming(b64_frames: List[str], existing_tags: Optional[list[dict[str, str]]] = None) -> str:
    url = f"{BASE_URL}/chat/completions"
    headers = {"Authorization": f"Bearer {API_KEY}"} if API_KEY and API_KEY != "none" else {}
    messages = _build_messages(b64_frames, existing_tags)
    _log_prompt(messages)

    payload = {"model": MODEL, "messages": messages, "temperature": TEMP, "max_tokens": MAX_TOKENS}
    data = _http_post_json(url, payload, headers=headers, timeout=TIMEOUT)
    try:
        msg = (data["choices"][0]["message"]) or {}
        content = _message_content_to_str(msg.get("content"))
        if not content:
            content = _message_content_to_str(msg)
        return content
    except Exception:
        raise RuntimeError(f"Unexpected LLM response: {data!r}")


def _write_stream_progress(scene_id, request_id, reasoning, output, done, error=None):
    if not scene_id or not request_id:
        return
    results_dir = os.path.join(_plugin_dir(), "results")
    os.makedirs(results_dir, exist_ok=True)
    safe_request_id = re.sub(r"[^A-Za-z0-9_-]", "_", str(request_id).strip())
    payload = {
        "scene_id": int(scene_id),
        "done": bool(done),
        "reasoning": str(reasoning) if reasoning else "",
        "output": str(output) if output else "",
        "error": str(error) if error else None,
    }
    tmp_path = os.path.join(results_dir, f"{scene_id}_{safe_request_id}_stream.json.tmp")
    final_path = os.path.join(results_dir, f"{scene_id}_{safe_request_id}_stream.json")
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    os.replace(tmp_path, final_path)


def _call_llm_b64_frames_streaming(b64_frames, existing_tags, request_id, scene_id):
    url = f"{BASE_URL}/chat/completions"
    h = {"Content-Type": "application/json"}
    if API_KEY and API_KEY != "none":
        h["Authorization"] = f"Bearer {API_KEY}"

    messages = _build_messages(b64_frames, existing_tags)
    _log_prompt(messages)

    payload = {"model": MODEL, "messages": messages, "temperature": TEMP, "max_tokens": MAX_TOKENS, "stream": True}

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=h, method="POST")

    reasoning_parts: list[str] = []
    output_parts: list[str] = []
    partial_line = ""

    _write_stream_progress(scene_id, request_id, "", "", done=False)

    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            while True:
                byte = resp.read(1)
                if not byte:
                    break
                partial_line += byte.decode("utf-8", errors="replace")

                if not partial_line.endswith("\n"):
                    continue

                line = partial_line.strip()
                partial_line = ""
                if not line:
                    continue
                if not line.startswith("data: "):
                    continue
                data_str = line[len("data: "):]
                if data_str == "[DONE]":
                    break
                try:
                    event = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                choices = event.get("choices")
                if not choices or not isinstance(choices, list):
                    continue
                delta = (choices[0] or {}).get("delta", {})
                if not isinstance(delta, dict):
                    continue

                rc = delta.get("reasoning_content")
                c = delta.get("content")

                if isinstance(rc, str) and rc:
                    reasoning_parts.append(rc)
                if isinstance(c, str) and c:
                    output_parts.append(c)

                _write_stream_progress(
                    scene_id, request_id,
                    "".join(reasoning_parts),
                    "".join(output_parts),
                    done=False,
                )
    except Exception as e:
        _write_stream_progress(
            scene_id, request_id,
            "".join(reasoning_parts),
            "".join(output_parts),
            done=True, error=str(e),
        )
        raise

    reasoning_text = "".join(reasoning_parts)
    output_text = "".join(output_parts)

    if not output_text and not reasoning_text:
        raise RuntimeError("No content received from LLM stream")

    _write_stream_progress(scene_id, request_id, reasoning_text, output_text, done=True)

    return reasoning_text, output_text


def _call_llm_b64_frames(b64_frames: List[str], existing_tags: Optional[list[dict[str, str]]] = None, request_id: Optional[str] = None, scene_id: Optional[int] = None) -> tuple[str, str]:
    if request_id and scene_id:
        try:
            reasoning, content = _call_llm_b64_frames_streaming(b64_frames, existing_tags, request_id, scene_id)
            return reasoning, content
        except Exception as e:
            stash.Error(f"[LLMVideoTag] Streaming failed ({e}), falling back to non-streaming")

    content = _call_llm_b64_frames_nonstreaming(b64_frames, existing_tags)
    reasoning = _extract_reasoning_from_content(content)
    return reasoning, content


def _extract_reasoning_from_content(content: str) -> str:
    think_match = re.search(r"<think>(.*?)</think>", content, flags=re.DOTALL | re.IGNORECASE)
    if think_match:
        return think_match.group(1).strip()
    return ""


def _strip_think_blocks(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)


def _parse_tags(text: str) -> List[str]:
    text = text.strip()
    tags: List[str] = []
    start = text.find("["); end = text.rfind("]")
    if start != -1 and end != -1 and end > start:
        maybe_json = text[start : end + 1]
        try:
            arr = json.loads(maybe_json)
            if isinstance(arr, list):
                tags = [str(x) for x in arr]
        except Exception:
            pass
    if not tags:
        sep = "," if "," in text else "\n"
        tags = [t.strip() for t in text.split(sep)]

    cleaned: List[str] = []
    for t in tags:
        t = t.strip().strip("#").strip().lower()
        t = "".join(ch for ch in t if (ch.isalnum() or ch in "- _"))
        if 1 <= len(t) <= 50:
            cleaned.append(t)
    seen = set()
    uniq: List[str] = []
    for t in cleaned:
        if t and t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


def _existing_tags() -> list[dict[str, str]]:
    try:
        query = """
            query($filter: FindFilterType) {
              findTags(filter: $filter) {
                tags { name aliases description ignore_auto_tag image_path }
              }
            }
        """
        variables = {"filter": {"per_page": -1}}
        resp = stash._graphql(query, variables)  # type: ignore[attr-defined]
        entries: list[dict[str, str]] = []
        sample_real: list[str] = []
        sample_default: list[str] = []
        if isinstance(resp, dict):
            tags = (((resp.get("data") or {}).get("findTags") or {}).get("tags")) or []
            for t in tags or []:
                if t.get("ignore_auto_tag"):
                    continue
                name = t.get("name")
                description = t.get("description") or ""
                image_path = t.get("image_path")
                if name:
                    entries.append({"name": str(name), "description": str(description), "image_path": image_path})
                    if image_path and isinstance(image_path, str):
                        if "default=true" in image_path:
                            if len(sample_default) < 3:
                                sample_default.append(f"{name}: {image_path[:100]}...")
                        else:
                            if len(sample_real) < 3:
                                sample_real.append(f"{name}: {image_path[:100]}...")
                for alias in t.get("aliases") or []:
                    entries.append({"name": str(alias), "description": str(description), "image_path": image_path})
        seen = set()
        uniq: list[dict[str, str]] = []
        real_count = 0
        for entry in entries:
            name = entry["name"]
            if name not in seen:
                seen.add(name)
                uniq.append(entry)
                ip = entry.get("image_path")
                if ip and isinstance(ip, str) and "default=true" not in ip:
                    real_count += 1
        stash.Log(f"[LLMVideoTag] Tags fetched: {len(uniq)} total, {real_count} with custom images")
        if sample_real:
            stash.Log(f"[LLMVideoTag] Sample custom image tags: {sample_real}")
        if sample_default:
            stash.Log(f"[LLMVideoTag] Sample default image tags: {sample_default}")
        return uniq
    except Exception as e:
        stash.Error(f"[LLMVideoTag] Failed to fetch existing tags: {e}")
        return []


def _format_tags_for_prompt(tags: list[dict[str, str]], include_descriptions: bool) -> tuple[str, str]:
    if include_descriptions:
        intro = (
            "The following input is a JSON array of existing tags already in the database, each with a name "
            "and description. Use the descriptions to understand what each tag means. Choose from these tags "
            "where applicable, but you may suggest additional tags that are not in this list. "
            "Do not guess or infer hidden attributes."
        )
        payload = [{"name": e["name"], "description": e.get("description", "")} for e in tags]
    else:
        intro = (
            "The following input is a JSON array of existing tags already in the database. "
            "Choose from these where applicable, but you may suggest additional tags that are not in this list. "
            "Do not guess or infer hidden attributes."
        )
        payload = [entry["name"] for entry in tags]
    return intro, payload


def _fetch_scene_path(scene_id: int) -> Optional[str]:
    try:
        query = """
            query($id: ID!) {
              findScene(id: $id) {
                files { path }
              }
            }
        """
        resp = stash._graphql(query, {"id": str(scene_id)})  # type: ignore[attr-defined]
        scene = (resp or {}).get("data", {}).get("findScene") or {}
        files = scene.get("files") or []
        if isinstance(files, list) and files:
            return (files[0] or {}).get("path")
        return None
    except Exception as e:
        stash.Error(f"[LLMVideoTag] GraphQL path lookup failed for scene {scene_id}: {e}")
        return None


def tags_from_scene(scene_id: int, request_id: Optional[str] = None) -> tuple[Optional[List[str]], str, str]:
    video_path = _fetch_scene_path(scene_id)
    if not video_path:
        stash.Error(f"[LLMVideoTag] No video file path found for scene id={scene_id}")
        return None, "", ""

    if not os.path.isfile(video_path):
        stash.Error(f"[LLMVideoTag] Video file does not exist: {video_path}")
        return None, "", ""

    try:
        existing = _existing_tags()
    except Exception:
        existing = []

    try:
        b64_frames = _extract_frames(video_path, MAX_FRAMES, SCENE_THRESHOLD, FRAME_WIDTH)
        stash.Log(f"[LLMVideoTag] Extracted {len(b64_frames)} frames for scene {scene_id}")

        reasoning, content = _call_llm_b64_frames(b64_frames, existing_tags=existing, request_id=request_id, scene_id=scene_id)
        stash.Error(f"[LLMVideoTag] LLM raw output: {content}")
        cleaned = _strip_think_blocks(content)
        tags = _parse_tags(cleaned)
        return tags, reasoning, content
    except Exception as e:
        tb = traceback.format_exc()
        stash.Error(f"[LLMVideoTag] Tagging failed for scene {scene_id}: {e}\n{tb}")
        return [], "", ""


def _plugin_dir() -> str:
    sc = stash.JSON_INPUT.get("server_connection") or stash.JSON_INPUT.get("serverConnection") or {}
    if isinstance(sc, dict):
        for key in ("plugin_dir", "PluginDir", "pluginDir"):
            val = sc.get(key)
            if isinstance(val, str) and val:
                return val
    return os.path.dirname(os.path.abspath(__file__))


def _write_result(scene_id: int, tags: List[str], error: Optional[str] = None, request_id: Optional[str] = None, reasoning: str = "", output: str = "") -> None:
    results_dir = os.path.join(_plugin_dir(), "results")
    os.makedirs(results_dir, exist_ok=True)
    safe_request_id = None
    if isinstance(request_id, str) and request_id.strip():
        safe_request_id = re.sub(r"[^A-Za-z0-9_-]", "_", request_id.strip())
    payload = {
        "scene_id": scene_id,
        "tags": tags,
        "error": error,
        "request_id": safe_request_id,
        "reasoning": reasoning,
        "output": output,
    }
    suffix = f"_{safe_request_id}" if safe_request_id else ""
    tmp_path = os.path.join(results_dir, f"{scene_id}{suffix}.json.tmp")
    final_path = os.path.join(results_dir, f"{scene_id}{suffix}.json")
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    os.replace(tmp_path, final_path)
    if safe_request_id:
        stream_path = os.path.join(results_dir, f"{scene_id}_{safe_request_id}_stream.json")
        try:
            os.remove(stream_path)
        except OSError:
            pass


def _cleanup_old_results(max_age_seconds: int = 3600) -> None:
    results_dir = os.path.join(_plugin_dir(), "results")
    if not os.path.isdir(results_dir):
        return
    now = time.time()
    count = 0
    for fname in os.listdir(results_dir):
        if not fname.endswith(".json"):
            continue
        fpath = os.path.join(results_dir, fname)
        try:
            if now - os.path.getmtime(fpath) > max_age_seconds:
                os.remove(fpath)
                count += 1
        except OSError:
            pass
    if count:
        stash.Log(f"[LLMVideoTag] Cleaned up {count} old result file(s)")


def tag_scene_task() -> None:
    try:
        args = stash.JSON_INPUT.get("args", {}) if stash.JSON_INPUT else {}
        scene_id = args.get("scene_id")
        request_id = args.get("request_id")
        if scene_id is None:
            stash.Error("[LLMVideoTag] No scene_id supplied to tag_scene_task")
            return
        scene_id = int(scene_id)
        tags, reasoning, output = tags_from_scene(scene_id, request_id=request_id)
        error = None
        if tags is None:
            error = "No video file path found."
            tags = []
        _write_result(scene_id, tags, error=error, request_id=request_id, reasoning=reasoning, output=output)
    except Exception as e:
        tb = traceback.format_exc()
        stash.Error(f"[LLMVideoTag] Exception in tag_scene_task: {e}\nTraceBack={tb}")
        try:
            args = stash.JSON_INPUT.get("args", {}) if stash.JSON_INPUT else {}
            scene_id = args.get("scene_id")
            if scene_id is not None:
                request_id = args.get("request_id")
                _write_result(int(scene_id), [], error=str(e), request_id=request_id, reasoning="", output="")
        except Exception:
            pass


# -------------
# Entry point
# -------------
try:
    _cleanup_old_results()
    if stash.Setting("zzdebugTracing", False):
        stash.Error(f"[LLMVideoTag] Using BASE_URL={BASE_URL!r} model={MODEL!r} temp={TEMP} max_tokens={MAX_TOKENS} timeout={TIMEOUT}")
        stash.Error(f"[LLMVideoTag] maxFrames={MAX_FRAMES} sceneThreshold={SCENE_THRESHOLD} frameWidth={FRAME_WIDTH}")
        stash.Error(f"[LLMVideoTag] includeTagDescriptions={INCLUDE_TAG_DESCRIPTIONS} includeTagImages={INCLUDE_TAG_IMAGES}")
    if INCLUDE_TAG_IMAGES:
        stash.Log(f"[LLMVideoTag] includeTagImages is ENABLED — tag images will be sent to the LLM")
    if stash.PLUGIN_TASK_NAME == "tag_scene_task":
        stash.Error(f"PLUGIN_TASK_NAME={stash.PLUGIN_TASK_NAME}")
        tag_scene_task()
    elif stash.JSON_INPUT and (stash.JSON_INPUT.get("args", {}).get("mode") == "tag_scene_task"):
        stash.Error("Dispatch via args.mode=tag_scene_task")
        tag_scene_task()
    else:
        stash.Error(f"[LLMVideoTag] No task specified (PLUGIN_TASK_NAME={stash.PLUGIN_TASK_NAME}). Nothing to do.")
except Exception as e:
    tb = traceback.format_exc()
    stash.Error(f"[LLMVideoTag] Exception while running plugin: {e}\nTraceBack={tb}")

try:
    print("null")
except Exception:
    pass
