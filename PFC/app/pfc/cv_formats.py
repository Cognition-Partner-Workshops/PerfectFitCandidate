"""Shared CV format metadata."""

CV_MIME_TYPES = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".rtf": "application/rtf",
    ".doc": "application/msword",
    ".ppt": "application/vnd.ms-powerpoint",
}
ALLOWED_CV_EXTENSIONS = frozenset(CV_MIME_TYPES)
DEFAULT_CV_EXTENSIONS = [".docx", ".pdf"]
