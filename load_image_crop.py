"""Load Image & Crop: LoadImage plus an interactive crop rectangle.

The frontend (web/load_image_crop.js) shows the picked image on the node
and lets the user drag/resize a crop area. The selection is stored in the
hidden "crop" string widget as JSON with normalized coordinates
{"x":0..1,"y":0..1,"w":0..1,"h":0..1}. Empty string means no crop.
"""

import hashlib
import io
import json
import os

import numpy as np
import torch
from PIL import Image, ImageOps, ImageSequence

import folder_paths
import node_helpers

try:
    from aiohttp import web
    from server import PromptServer
except Exception:
    web = None
    PromptServer = None


ASPECT_RATIOS = [
    "Freeform",
    "1:1 (Square)",
    "2:3 (Portrait Photo)",
    "3:2 (Photo)",
    "3:4 (Portrait Standard)",
    "4:3 (Standard)",
    "9:16 (Portrait Widescreen)",
    "16:9 (Widescreen)",
    "21:9 (Ultrawide)",
]
DEFAULT_ASPECT_RATIO = "Freeform"


def _annotated_image_path(filename, file_type="input", subfolder=""):
    parts = filename.replace("\\", "/").split("/") + subfolder.replace("\\", "/").split("/")
    if os.path.isabs(filename) or os.path.isabs(subfolder) or ".." in parts:
        raise ValueError("Invalid image path")
    rel = os.path.join(subfolder, filename) if subfolder else filename
    if file_type and file_type != "input":
        rel = "{} [{}]".format(rel, file_type)
    return folder_paths.get_annotated_filepath(rel)


def _webp_preview_response(path):
    with node_helpers.pillow(Image.open, path) as img:
        img.seek(0)
        frame = node_helpers.pillow(ImageOps.exif_transpose, img).convert("RGBA")
        data = io.BytesIO()
        frame.save(data, format="PNG")
    return web.Response(body=data.getvalue(), content_type="image/png")


if PromptServer is not None and web is not None:
    @PromptServer.instance.routes.get("/obvpm/load_image_crop/preview")
    async def webp_preview(request):
        try:
            filename = request.query.get("filename", "")
            file_type = request.query.get("type", "input")
            subfolder = request.query.get("subfolder", "")
            path = _annotated_image_path(filename, file_type, subfolder)
            if not path.lower().endswith(".webp"):
                raise web.HTTPBadRequest(reason="Only WebP previews are handled here")
            return _webp_preview_response(path)
        except web.HTTPException:
            raise
        except Exception as e:
            raise web.HTTPBadRequest(reason=str(e))


def _parse_crop(crop, width, height):
    """Return (x0, y0, x1, y1) pixel box, or None for full image."""
    if not crop:
        return None
    try:
        data = json.loads(crop)
        x = float(data["x"])
        y = float(data["y"])
        w = float(data["w"])
        h = float(data["h"])
    except (ValueError, KeyError, TypeError):
        return None
    x0 = max(0, min(width - 1, round(x * width)))
    y0 = max(0, min(height - 1, round(y * height)))
    x1 = max(x0 + 1, min(width, round((x + w) * width)))
    y1 = max(y0 + 1, min(height, round((y + h) * height)))
    if x0 == 0 and y0 == 0 and x1 == width and y1 == height:
        return None
    return (x0, y0, x1, y1)


def _parse_max_megapixels(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return 0.0
    return value if np.isfinite(value) else 0.0


class LoadImageCrop:
    CATEGORY = "obvpm/image"
    FUNCTION = "load"
    RETURN_TYPES = ("IMAGE", "MASK")
    RETURN_NAMES = ("image", "mask")
    DESCRIPTION = (
        "Loads an image and crops it to the area selected interactively on "
        "the node's preview. Drag to draw the crop area, drag inside it to "
        "move, drag its corners to resize, click to clear. With no crop "
        "drawn the full image is output. If max_megapixels is greater than "
        "0, the output is scaled down to fit within it (aspect preserved)."
    )

    OUTPUT_TOOLTIPS = (
        "The loaded image, cropped to the selection (if any) and scaled down to max_megapixels (if set).",
        "Mask from the image's alpha channel, cropped and scaled the same way.",
    )

    @classmethod
    def INPUT_TYPES(cls):
        input_dir = folder_paths.get_input_directory()
        files = [f for f in os.listdir(input_dir) if os.path.isfile(os.path.join(input_dir, f))]
        files = folder_paths.filter_files_content_types(files, ["image"])
        return {
            "required": {
                "image": (sorted(files), {
                    "image_upload": True,
                    "tooltip": "The image file to load. Upload, drag & drop, or pick an existing input file.",
                }),
                "crop": ("STRING", {
                    "default": "",
                    "tooltip": "Managed by the crop editor on the node — no need to edit by hand.",
                }),
                "aspect_ratio": (ASPECT_RATIOS, {
                    "default": DEFAULT_ASPECT_RATIO,
                    "tooltip": "Locks the crop selection to the selected aspect ratio.",
                }),
                "max_megapixels": ("FLOAT", {
                    "default": 0.0, "min": 0.0, "max": 128.0, "step": 0.01,
                    "tooltip": "If the selected image area is larger than this many megapixels, then it is downscaled to it for output. Set to 0 to disable downscaling.",
                }),
            }
        }

    def load(self, image, crop="", aspect_ratio=DEFAULT_ASPECT_RATIO, max_megapixels=0.0):
        max_megapixels = _parse_max_megapixels(max_megapixels)
        image_path = folder_paths.get_annotated_filepath(image)
        img = node_helpers.pillow(Image.open, image_path)

        output_images = []
        output_masks = []
        w, h = None, None

        if (img.format or "").upper() == "WEBP":
            img.seek(0)
            frames = [img]
        else:
            frames = ImageSequence.Iterator(img)

        for i in frames:
            i = node_helpers.pillow(ImageOps.exif_transpose, i)

            frame = i.convert("RGB")
            if len(output_images) == 0:
                w, h = frame.size
            if frame.size != (w, h):
                continue

            frame = np.array(frame).astype(np.float32) / 255.0
            frame = torch.from_numpy(frame)[None,]
            if "A" in i.getbands():
                mask = np.array(i.getchannel("A")).astype(np.float32) / 255.0
                mask = 1.0 - torch.from_numpy(mask)
            else:
                mask = torch.zeros((h, w), dtype=torch.float32)
            output_images.append(frame)
            output_masks.append(mask.unsqueeze(0))

        images = torch.cat(output_images, dim=0)
        masks = torch.cat(output_masks, dim=0)

        box = _parse_crop(crop, images.shape[2], images.shape[1])
        if box is not None:
            x0, y0, x1, y1 = box
            images = images[:, y0:y1, x0:x1, :]
            masks = masks[:, y0:y1, x0:x1]

        if max_megapixels > 0:
            import comfy.utils

            height, width = images.shape[1], images.shape[2]
            target = max_megapixels * 1024 * 1024
            current = width * height
            if current > target:
                scale = (target / current) ** 0.5
                new_width = max(1, round(width * scale))
                new_height = max(1, round(height * scale))
                images = comfy.utils.common_upscale(
                    images.movedim(-1, 1), new_width, new_height, "lanczos", "disabled"
                ).movedim(1, -1)
                masks = comfy.utils.common_upscale(
                    masks.unsqueeze(1), new_width, new_height, "bilinear", "disabled"
                ).squeeze(1)

        return (images, masks)

    @classmethod
    def IS_CHANGED(cls, image, crop="", aspect_ratio=DEFAULT_ASPECT_RATIO, max_megapixels=0.0):
        image_path = folder_paths.get_annotated_filepath(image)
        m = hashlib.sha256()
        with open(image_path, "rb") as f:
            m.update(f.read())
        return m.digest().hex()

    @classmethod
    def VALIDATE_INPUTS(cls, image, crop="", aspect_ratio=DEFAULT_ASPECT_RATIO, max_megapixels=0.0):
        if not folder_paths.exists_annotated_filepath(image):
            return "Invalid image file: {}".format(image)
        return True
