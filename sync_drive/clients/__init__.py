"""Wind clients – implementations for various cloud storage services."""

from .apple_photos import ApplePhotosClient
from .gdrive import GDriveClient
from .gphotos import GooglePhotosClient
from .icloud import ICloudClient, LocalICloudDriveClient
from .onedrive import OneDriveClient
from .takeout import TakeoutClient

__all__ = [
    "GDriveClient",
    "OneDriveClient",
    "ICloudClient",
    "LocalICloudDriveClient",
    "GooglePhotosClient",
    "ApplePhotosClient",
    "TakeoutClient",
]
