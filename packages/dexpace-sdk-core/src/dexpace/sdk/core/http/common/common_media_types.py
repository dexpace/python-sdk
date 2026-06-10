# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Pre-constructed `MediaType` constants for the most common content types.

Exposed as module-level constants. Reusing these avoids re-parsing the same
media-type string on hot paths.
"""

from __future__ import annotations

from typing import Final

from .media_type import MediaType

TEXT_PLAIN: Final = MediaType.of("text", "plain")
TEXT_HTML: Final = MediaType.of("text", "html")
TEXT_CSS: Final = MediaType.of("text", "css")
TEXT_JAVASCRIPT: Final = MediaType.of("text", "javascript")
TEXT_CSV: Final = MediaType.of("text", "csv")

APPLICATION_JSON: Final = MediaType.of("application", "json")
APPLICATION_XML: Final = MediaType.of("application", "xml")
APPLICATION_FORM_URLENCODED: Final = MediaType.of("application", "x-www-form-urlencoded")
APPLICATION_OCTET_STREAM: Final = MediaType.of("application", "octet-stream")
APPLICATION_PDF: Final = MediaType.of("application", "pdf")
APPLICATION_ZIP: Final = MediaType.of("application", "zip")
APPLICATION_VND_API_JSON: Final = MediaType.of("application", "vnd.api+json")
APPLICATION_HAL_JSON: Final = MediaType.of("application", "hal+json")
APPLICATION_PROBLEM_JSON: Final = MediaType.of("application", "problem+json")

IMAGE_JPEG: Final = MediaType.of("image", "jpeg")
IMAGE_PNG: Final = MediaType.of("image", "png")
IMAGE_GIF: Final = MediaType.of("image", "gif")
IMAGE_SVG_XML: Final = MediaType.of("image", "svg+xml")

AUDIO_MPEG: Final = MediaType.of("audio", "mpeg")
VIDEO_MP4: Final = MediaType.of("video", "mp4")

MULTIPART_FORM_DATA: Final = MediaType.of("multipart", "form-data")
MULTIPART_BYTERANGES: Final = MediaType.of("multipart", "byteranges")

__all__ = [
    "APPLICATION_FORM_URLENCODED",
    "APPLICATION_HAL_JSON",
    "APPLICATION_JSON",
    "APPLICATION_OCTET_STREAM",
    "APPLICATION_PDF",
    "APPLICATION_PROBLEM_JSON",
    "APPLICATION_VND_API_JSON",
    "APPLICATION_XML",
    "APPLICATION_ZIP",
    "AUDIO_MPEG",
    "IMAGE_GIF",
    "IMAGE_JPEG",
    "IMAGE_PNG",
    "IMAGE_SVG_XML",
    "MULTIPART_BYTERANGES",
    "MULTIPART_FORM_DATA",
    "TEXT_CSS",
    "TEXT_CSV",
    "TEXT_HTML",
    "TEXT_JAVASCRIPT",
    "TEXT_PLAIN",
    "VIDEO_MP4",
]
