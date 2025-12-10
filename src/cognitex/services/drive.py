"""Google Drive API integration for document sync."""

import io
from typing import Any, Generator

import structlog
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

from cognitex.services.google_auth import get_google_credentials

logger = structlog.get_logger()

# Folders to fully index (text extraction + embeddings)
PRIORITY_FOLDERS = ["dundee", "myWayDigitalHealth", "glucose.ai", "birmingham"]

# MIME types we can extract text from
EXPORTABLE_MIME_TYPES = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}

READABLE_MIME_TYPES = [
    "text/plain",
    "text/markdown",
    "text/csv",
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
]


class DriveService:
    """Service for interacting with Google Drive API."""

    def __init__(self):
        credentials = get_google_credentials()
        self.service = build("drive", "v3", credentials=credentials)

    def list_files(
        self,
        page_size: int = 100,
        query: str | None = None,
        fields: str = "files(id, name, mimeType, modifiedTime, owners, shared, parents, size, webViewLink)",
        order_by: str = "modifiedTime desc",
    ) -> Generator[dict, None, None]:
        """
        List files from Drive with pagination.

        Args:
            page_size: Number of files per page
            query: Optional Drive query string (e.g., "name contains 'report'")
            fields: Fields to return for each file
            order_by: Sort order

        Yields:
            File metadata dictionaries
        """
        page_token = None

        while True:
            results = self.service.files().list(
                pageSize=page_size,
                q=query,
                fields=f"nextPageToken, {fields}",
                orderBy=order_by,
                pageToken=page_token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute()

            for file in results.get("files", []):
                yield file

            page_token = results.get("nextPageToken")
            if not page_token:
                break

    def list_all_files(self, page_size: int = 100) -> Generator[dict, None, None]:
        """List all files (excluding trashed)."""
        yield from self.list_files(
            page_size=page_size,
            query="trashed = false",
        )

    def get_file_metadata(self, file_id: str) -> dict:
        """Get detailed metadata for a specific file."""
        return self.service.files().get(
            fileId=file_id,
            fields="id, name, mimeType, modifiedTime, createdTime, owners, shared, "
                   "sharedWithMeTime, sharingUser, parents, size, webViewLink, "
                   "permissions(emailAddress, role, type)",
            supportsAllDrives=True,
        ).execute()

    def get_folder_id_by_name(self, folder_name: str) -> str | None:
        """
        Find a folder by name (case-insensitive search).

        Returns the folder ID or None if not found.
        """
        query = f"name = '{folder_name}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        results = self.service.files().list(
            q=query,
            fields="files(id, name)",
            pageSize=10,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()

        files = results.get("files", [])
        if files:
            return files[0]["id"]
        return None

    def list_files_in_folder(
        self,
        folder_id: str,
        recursive: bool = True,
    ) -> Generator[dict, None, None]:
        """
        List all files in a folder, optionally recursively.

        Args:
            folder_id: The folder ID to search
            recursive: If True, include files in subfolders

        Yields:
            File metadata dictionaries with path information
        """
        def _list_recursive(fid: str, path: str = ""):
            # List files in this folder
            query = f"'{fid}' in parents and trashed = false"

            for file in self.list_files(query=query):
                file["_path"] = f"{path}/{file['name']}" if path else file["name"]

                if file["mimeType"] == "application/vnd.google-apps.folder":
                    if recursive:
                        yield from _list_recursive(file["id"], file["_path"])
                else:
                    yield file

        yield from _list_recursive(folder_id)

    def get_file_content(self, file_id: str, mime_type: str) -> str | None:
        """
        Get the text content of a file.

        Args:
            file_id: The file ID
            mime_type: The file's MIME type

        Returns:
            Text content or None if unable to extract
        """
        try:
            # Google Docs/Sheets/Slides - export as text
            if mime_type in EXPORTABLE_MIME_TYPES:
                export_mime = EXPORTABLE_MIME_TYPES[mime_type]
                request = self.service.files().export_media(
                    fileId=file_id,
                    mimeType=export_mime,
                )
                content = request.execute()
                return content.decode("utf-8") if isinstance(content, bytes) else content

            # Regular files - download directly
            if mime_type in READABLE_MIME_TYPES or mime_type.startswith("text/"):
                request = self.service.files().get_media(fileId=file_id)
                buffer = io.BytesIO()
                downloader = MediaIoBaseDownload(buffer, request)

                done = False
                while not done:
                    _, done = downloader.next_chunk()

                content = buffer.getvalue()

                # Handle PDF - would need PyPDF2 or similar
                if mime_type == "application/pdf":
                    return self._extract_pdf_text(content)

                # Handle DOCX - would need python-docx
                if mime_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
                    return self._extract_docx_text(content)

                # Plain text types
                try:
                    return content.decode("utf-8")
                except UnicodeDecodeError:
                    return content.decode("latin-1", errors="ignore")

            return None

        except Exception as e:
            logger.warning("Failed to get file content", file_id=file_id, error=str(e))
            return None

    def _extract_pdf_text(self, content: bytes) -> str | None:
        """Extract text from PDF bytes."""
        try:
            import pypdf
            reader = pypdf.PdfReader(io.BytesIO(content))
            text_parts = []
            for page in reader.pages:
                text = page.extract_text()
                if text:
                    text_parts.append(text)
            return "\n\n".join(text_parts) if text_parts else None
        except ImportError:
            logger.warning("pypdf not installed, cannot extract PDF text")
            return None
        except Exception as e:
            logger.warning("Failed to extract PDF text", error=str(e))
            return None

    def _extract_docx_text(self, content: bytes) -> str | None:
        """Extract text from DOCX bytes."""
        try:
            from docx import Document
            doc = Document(io.BytesIO(content))
            return "\n\n".join([p.text for p in doc.paragraphs if p.text])
        except ImportError:
            logger.warning("python-docx not installed, cannot extract DOCX text")
            return None
        except Exception as e:
            logger.warning("Failed to extract DOCX text", error=str(e))
            return None

    def get_sharing_info(self, file_id: str) -> list[dict]:
        """
        Get detailed sharing information for a file.

        Returns list of sharing entries with email, role, type.
        """
        try:
            permissions = self.service.permissions().list(
                fileId=file_id,
                fields="permissions(emailAddress, role, type, displayName)",
                supportsAllDrives=True,
            ).execute()
            return permissions.get("permissions", [])
        except Exception as e:
            logger.warning("Failed to get sharing info", file_id=file_id, error=str(e))
            return []

    def find_priority_folders(self) -> dict[str, str | None]:
        """
        Find folder IDs for the priority folders.

        Returns dict mapping folder name to folder ID (or None if not found).
        """
        result = {}
        for folder_name in PRIORITY_FOLDERS:
            folder_id = self.get_folder_id_by_name(folder_name)
            result[folder_name] = folder_id
            if folder_id:
                logger.info("Found priority folder", name=folder_name, id=folder_id)
            else:
                logger.warning("Priority folder not found", name=folder_name)
        return result


# Singleton instance
_drive_service: DriveService | None = None


def get_drive_service() -> DriveService:
    """Get or create the Drive service singleton."""
    global _drive_service
    if _drive_service is None:
        _drive_service = DriveService()
    return _drive_service
