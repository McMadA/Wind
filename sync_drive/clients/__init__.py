"""Wind clients – implementations for various cloud storage services."""

from .gdrive import GDriveClient
from .onedrive import OneDriveClient
from .icloud import ICloudClient
from .gphotos import GooglePhotosClient

__all__ = ["GDriveClient", "OneDriveClient", "ICloudClient", "GooglePhotosClient"]
