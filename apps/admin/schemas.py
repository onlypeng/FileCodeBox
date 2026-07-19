import datetime
from typing import Optional, Union

from pydantic import BaseModel


class IDData(BaseModel):
    id: int


class IDsData(BaseModel):
    ids: list[int]


class LoginData(BaseModel):
    password: str


class UpdateFileData(BaseModel):
    expired_at: Optional[str] = None
    expired_count: Optional[int] = None


class BatchUpdateFileData(BaseModel):
    ids: list[int]
    expired_at: Optional[str] = None
    expired_count: Optional[int] = None
    clearExpiredAt: Optional[bool] = None
    clear_expired_at: Optional[bool] = None


class FilePolicyActionData(BaseModel):
    id: int
    action: str
    downloadLimit: Optional[int] = None
    download_limit: Optional[int] = None


class BatchFilePolicyActionData(BaseModel):
    ids: list[int]
    action: str
    downloadLimit: Optional[int] = None
    download_limit: Optional[int] = None


class FileMetadataData(BaseModel):
    id: int
    note: Optional[str] = None
    tags: Optional[list[str]] = None


class FileViewPresetData(BaseModel):
    id: Optional[str] = None
    name: str
    filters: dict
    params: Optional[dict] = None


class FileViewPresetDeleteData(BaseModel):
    id: str


class ShareItem(BaseModel):
    code: str
    link: str


class DeleteItem(BaseModel):
    id: int


class UpdateConfigRequest(BaseModel):
    file_storage: Optional[str] = None
    storage_path: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None
    notify_title: Optional[str] = None
    notify_content: Optional[str] = None
    page_explain: Optional[str] = None
    keywords: Optional[str] = None
    s3_access_key_id: Optional[str] = None
    s3_secret_access_key: Optional[str] = None
    s3_bucket_name: Optional[str] = None
    s3_endpoint_url: Optional[str] = None
    s3_region_name: Optional[str] = None
    s3_signature_version: Optional[str] = None
    s3_hostname: Optional[str] = None
    s3_proxy: Optional[int] = None
    max_save_seconds: Optional[int] = None
    aws_session_token: Optional[str] = None
    onedrive_domain: Optional[str] = None
    onedrive_client_id: Optional[str] = None
    onedrive_username: Optional[str] = None
    onedrive_password: Optional[str] = None
    onedrive_root_path: Optional[str] = None
    onedrive_proxy: Optional[int] = None
    webdav_root_path: Optional[str] = None
    webdav_proxy: Optional[int] = None
    admin_token: Optional[str] = None
    openUpload: Optional[int] = None
    uploadSize: Optional[int] = None
    expireStyle: Optional[list] = None
    uploadMinute: Optional[int] = None
    enableChunk: Optional[int] = None
    webdav_url: Optional[str] = None
    webdav_password: Optional[str] = None
    webdav_username: Optional[str] = None
    opacity: Optional[float] = None
    background: Optional[str] = None
    uploadCount: Optional[int] = None
    themesSelect: Optional[str] = None
    errorMinute: Optional[int] = None
    errorCount: Optional[int] = None
    serverWorkers: Optional[int] = None
    serverHost: Optional[str] = None
    serverPort: Optional[int] = None
    showAdminAddr: Optional[int] = None
    robotsText: Optional[str] = None
    maxMultiFileCount: Optional[int] = None
    maxCollectionFiles: Optional[int] = None
    maxSendFiles: Optional[int] = None
    collectionDefaultMaxFiles: Optional[int] = None
    collectionDefaultExpireDays: Optional[int] = None
    trustedProxies: Optional[str] = None
