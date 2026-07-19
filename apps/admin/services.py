import hashlib
import os
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Optional

from core.response import APIResponse
from core.storage import FileStorageInterface, storages
from core.settings import (
    ADMIN_SESSION_EXPIRE_MAX,
    ADMIN_SESSION_EXPIRE_MIN,
    settings,
)
from core.config import refresh_settings
from core.security import INTERNAL_CONFIG_KEYS, generate_jwt_secret
from apps.base.models import FileCodes, KeyValue, ActivityLog, FileItem, CollectionFile
from fastapi import HTTPException
from core.settings import data_root
from core.utils import get_now, hash_password, is_password_hashed


class ActivityLogService:
    @classmethod
    async def log(cls, action: str, target_type: str = None, target_id: int = None, user_ip: str = "", user_agent: str = None, detail: dict = None):
        await ActivityLog.create(
            action=action,
            target_type=target_type,
            target_id=target_id,
            user_ip=user_ip,
            user_agent=user_agent,
            detail=detail,
        )

    @classmethod
    async def get_logs(cls, page: int = 1, size: int = 20, action: str = None, target_type: str = None, start_time=None, end_time=None, keyword: str = None):
        offset = (page - 1) * size
        query = ActivityLog.all().order_by("-created_at")

        if action:
            query = query.filter(action=action)
        if target_type:
            query = query.filter(target_type=target_type)
        if start_time:
            query = query.filter(created_at__gte=start_time)
        if end_time:
            query = query.filter(created_at__lte=end_time)
        if keyword:
            query = query.filter(
                (ActivityLog.user_ip.contains(keyword)) |
                (ActivityLog.action.contains(keyword))
            )

        total = await query.count()
        logs = await query.limit(size).offset(offset)

        result = []
        for log in logs:
            result.append({
                "id": log.id,
                "action": log.action,
                "target_type": log.target_type,
                "target_id": log.target_id,
                "user_ip": log.user_ip,
                "user_agent": log.user_agent,
                "detail": log.detail,
                "created_at": log.created_at.isoformat() if log.created_at else None,
            })

        return result, total


class FileService:
    FILE_METADATA_KEY_PREFIX = "admin_file_metadata:"
    FILE_VIEW_PRESETS_KEY = "admin_file_view_presets"
    ADMIN_ACTIVITY_KEY = "admin_activity_events"
    MAX_METADATA_NOTE_LENGTH = 2000
    MAX_METADATA_TAGS = 12
    MAX_METADATA_TAG_LENGTH = 24
    MAX_VIEW_PRESETS = 24
    MAX_VIEW_PRESET_NAME_LENGTH = 32
    MAX_VIEW_PRESET_KEYWORD_LENGTH = 80
    MAX_ADMIN_ACTIVITIES = 80
    MAX_ADMIN_ACTIVITY_TEXT_LENGTH = 120

    POLICY_ACTIONS = {
        "extend_24h",
        "extend_7d",
        "make_permanent",
        "reset_download_limit",
    }

    SORT_FIELDS = {
        "created_at",
        "createdat",
        "expired_at",
        "expiredat",
        "name",
        "size",
        "used_count",
        "usedcount",
        "code",
    }
    VIEW_PRESET_STATUS_VALUES = {"all", "active", "expired"}
    VIEW_PRESET_TYPE_VALUES = {"all", "file", "text", "chunked"}
    VIEW_PRESET_HEALTH_VALUES = {
        "all",
        "attention",
        "danger",
        "warning",
        "healthy",
        "expired",
        "expiring_soon",
        "storage_issue",
        "never_retrieved",
        "permanent",
    }
    VIEW_PRESET_SORT_FIELDS = {
        "created_at",
        "expired_at",
        "name",
        "size",
        "used_count",
        "code",
    }

    def __init__(self):
        self._file_storage: Optional[FileStorageInterface] = None

    @property
    def file_storage(self) -> FileStorageInterface:
        if self._file_storage is None:
            self._file_storage = storages[settings.file_storage]()
        return self._file_storage

    def _file_metadata_key(self, file_id: int) -> str:
        return f"{self.FILE_METADATA_KEY_PREFIX}{file_id}"

    async def _delete_file_code(self, file_code: FileCodes):
        if file_code.text is None:
            await self.file_storage.delete_file(file_code)
        await KeyValue.filter(key=self._file_metadata_key(file_code.id)).delete()
        if file_code.is_multi_file:
            await FileItem.filter(file_code=file_code).delete()
        await file_code.delete()

    async def delete_file(self, file_id: int, user_ip: str = ""):
        file_code = await FileCodes.get(id=file_id)
        target_name = f"{file_code.prefix}{file_code.suffix}" if file_code.prefix or file_code.suffix else "文本分享"
        await self._delete_file_code(file_code)
        await ActivityLogService.log(
            action="file.delete",
            target_type="file",
            target_id=file_id,
            user_ip=user_ip,
            detail={"code": file_code.code, "name": target_name},
        )

    async def delete_files(self, file_ids: list[int], user_ip: str = ""):
        unique_ids = list(dict.fromkeys(file_ids))
        deleted = []
        failed = []
        missing = []

        for file_id in unique_ids:
            file_code = await FileCodes.filter(id=file_id).first()
            if not file_code:
                missing.append(file_id)
                continue

            try:
                await self._delete_file_code(file_code)
                deleted.append(file_id)
            except Exception as exc:
                failed.append({"id": file_id, "reason": str(exc)})

        if deleted:
            await ActivityLogService.log(
                action="files.batch_delete",
                target_type="file",
                user_ip=user_ip,
                detail={
                    "count": len(deleted),
                    "requestedCount": len(file_ids),
                    "uniqueCount": len(unique_ids),
                    "deleted": deleted,
                    "missing": missing,
                    "failedCount": len(failed),
                },
            )

        return {
            "requestedCount": len(file_ids),
            "requested_count": len(file_ids),
            "uniqueCount": len(unique_ids),
            "unique_count": len(unique_ids),
            "deletedCount": len(deleted),
            "deleted_count": len(deleted),
            "missingCount": len(missing),
            "missing_count": len(missing),
            "failedCount": len(failed),
            "failed_count": len(failed),
            "deleted": deleted,
            "missing": missing,
            "failed": failed,
        }

    async def update_files(self, file_ids: list[int], update_data: dict[str, Any], user_ip: str = ""):
        unique_ids = list(dict.fromkeys(file_ids))
        updated = []
        failed = []
        missing = []

        for file_id in unique_ids:
            file_code = await FileCodes.filter(id=file_id).first()
            if not file_code:
                missing.append(file_id)
                continue

            try:
                await file_code.update_from_dict(update_data).save()
                updated.append(file_id)
            except Exception as exc:
                failed.append({"id": file_id, "reason": str(exc)})

        if updated:
            await ActivityLogService.log(
                action="files.batch_update",
                target_type="file",
                user_ip=user_ip,
                detail={
                    "count": len(updated),
                    "fields": sorted(update_data.keys()),
                    "requestedCount": len(file_ids),
                    "uniqueCount": len(unique_ids),
                    "updated": updated,
                    "missing": missing,
                    "failedCount": len(failed),
                },
            )

        return {
            "requestedCount": len(file_ids),
            "requested_count": len(file_ids),
            "uniqueCount": len(unique_ids),
            "unique_count": len(unique_ids),
            "updatedCount": len(updated),
            "updated_count": len(updated),
            "missingCount": len(missing),
            "missing_count": len(missing),
            "failedCount": len(failed),
            "failed_count": len(failed),
            "updated": updated,
            "missing": missing,
            "failed": failed,
        }

    def _build_policy_action_update(
        self,
        file_code: FileCodes,
        action: str,
        now: datetime,
        download_limit: Optional[int] = None,
    ) -> dict[str, Any]:
        update_data: dict[str, Any] = {}

        if action == "extend_24h":
            # 从原过期时间延长；若原过期时间已过或为空，则从当前时间起算
            base = now
            original_expired_at = file_code.expired_at
            if original_expired_at is not None:
                expired_naive = original_expired_at.replace(tzinfo=None) if hasattr(original_expired_at, 'tzinfo') and original_expired_at.tzinfo else original_expired_at
                if expired_naive > now:
                    base = expired_naive
            update_data["expired_at"] = base + timedelta(hours=24)
            update_data["expired_count"] = -1
        elif action == "extend_7d":
            # 从原过期时间延长；若原过期时间已过或为空，则从当前时间起算
            base = now
            original_expired_at = file_code.expired_at
            if original_expired_at is not None:
                expired_naive = original_expired_at.replace(tzinfo=None) if hasattr(original_expired_at, 'tzinfo') and original_expired_at.tzinfo else original_expired_at
                if expired_naive > now:
                    base = expired_naive
            update_data["expired_at"] = base + timedelta(days=7)
            update_data["expired_count"] = -1
        elif action == "make_permanent":
            update_data["expired_at"] = None
            update_data["expired_count"] = -1
        elif action == "reset_download_limit":
            update_data["expired_count"] = -1
            if download_limit is not None:
                update_data["expired_count"] = download_limit

        return update_data

    async def apply_file_policy_action(
        self,
        file_id: int,
        action: str,
        download_limit: Optional[int] = None,
        user_ip: str = "",
    ) -> dict[str, Any]:
        file_code = await FileCodes.filter(id=file_id).first()
        if not file_code:
            raise HTTPException(status_code=404, detail="文件不存在")

        action = action.strip().lower()
        if action not in self.POLICY_ACTIONS:
            raise HTTPException(status_code=400, detail="无效的策略操作")

        now = await get_now()
        update_data = self._build_policy_action_update(
            file_code=file_code,
            action=action,
            now=now,
            download_limit=download_limit,
        )

        await file_code.update_from_dict(update_data).save()
        await ActivityLogService.log(
            action="file.policy_action",
            target_type="file",
            target_id=file_id,
            user_ip=user_ip,
            detail={
                "code": file_code.code,
                "name": f"{file_code.prefix}{file_code.suffix}" if file_code.prefix or file_code.suffix else "文本分享",
                "policyAction": action,
            },
        )
        return await self.get_file_detail(file_id)

    async def apply_files_policy_action(
        self,
        file_ids: list[int],
        action: str,
        download_limit: Optional[int] = None,
        user_ip: str = "",
    ) -> dict[str, Any]:
        action = action.strip().lower()
        if action not in self.POLICY_ACTIONS:
            raise HTTPException(status_code=400, detail="无效的策略操作")

        unique_ids = list(dict.fromkeys(file_ids))
        updated = []
        failed = []
        missing = []

        now = await get_now()

        for file_id in unique_ids:
            file_code = await FileCodes.filter(id=file_id).first()
            if not file_code:
                missing.append(file_id)
                continue

            try:
                update_data = self._build_policy_action_update(
                    file_code=file_code,
                    action=action,
                    now=now,
                    download_limit=download_limit,
                )
                await file_code.update_from_dict(update_data).save()
                updated.append(file_id)
            except Exception as exc:
                failed.append({"id": file_id, "reason": str(exc)})

        if updated:
            await ActivityLogService.log(
                action="files.batch_policy_action",
                target_type="file",
                user_ip=user_ip,
                detail={
                    "count": len(updated),
                    "policyAction": action,
                    "requestedCount": len(file_ids),
                    "uniqueCount": len(unique_ids),
                    "updated": updated,
                    "missing": missing,
                    "failedCount": len(failed),
                },
            )

        return {
            "requestedCount": len(file_ids),
            "requested_count": len(file_ids),
            "uniqueCount": len(unique_ids),
            "unique_count": len(unique_ids),
            "updatedCount": len(updated),
            "updated_count": len(updated),
            "missingCount": len(missing),
            "missing_count": len(missing),
            "failedCount": len(failed),
            "failed_count": len(failed),
            "updated": updated,
            "missing": missing,
            "failed": failed,
        }

    async def get_file_metadata(self, file_id: int) -> dict[str, Any]:
        record = await KeyValue.filter(key=self._file_metadata_key(file_id)).first()
        if record and record.value:
            return record.value
        return {"note": "", "tags": []}

    def _normalize_metadata_note(self, note: Optional[str]) -> str:
        if note is None:
            return ""
        return str(note).strip()[: self.MAX_METADATA_NOTE_LENGTH]

    def _normalize_metadata_tags(self, tags: Optional[list[str]]) -> list[str]:
        if tags is None:
            return []
        result: list[str] = []
        seen: set[str] = set()
        for tag in tags:
            normalized = str(tag).strip()[: self.MAX_METADATA_TAG_LENGTH]
            if normalized and normalized not in seen:
                seen.add(normalized)
                result.append(normalized)
                if len(result) >= self.MAX_METADATA_TAGS:
                    break
        return result

    async def update_file_metadata(
        self,
        file_id: int,
        note: Optional[str],
        tags: Optional[list[str]],
        update_note: bool,
        update_tags: bool,
        user_ip: str = "",
    ) -> dict[str, Any]:
        file_code = await FileCodes.filter(id=file_id).first()
        if not file_code:
            raise HTTPException(status_code=404, detail="文件不存在")

        current_metadata = await self.get_file_metadata(file_id)
        next_metadata = dict(current_metadata)
        if update_note:
            next_metadata["note"] = self._normalize_metadata_note(note)
        if update_tags:
            next_metadata["tags"] = self._normalize_metadata_tags(tags)

        now = await get_now()
        updated_at = now.isoformat()
        next_metadata["updatedAt"] = updated_at
        next_metadata["updated_at"] = updated_at
        await KeyValue.update_or_create(
            key=self._file_metadata_key(file_id),
            defaults={"value": next_metadata},
        )
        await ActivityLogService.log(
            action="file.metadata_update",
            target_type="file",
            target_id=file_id,
            user_ip=user_ip,
            detail={
                "code": file_code.code,
                "name": f"{file_code.prefix}{file_code.suffix}" if file_code.prefix or file_code.suffix else "文本分享",
                "updateNote": update_note,
                "updateTags": update_tags,
                "tagCount": len(next_metadata["tags"]),
            },
        )
        return await self.get_file_detail(file_id)

    async def _get_file_view_presets(self) -> list[dict]:
        record = await KeyValue.filter(key=self.FILE_VIEW_PRESETS_KEY).first()
        if record and record.value:
            return record.value
        return []

    async def list_file_view_presets(self) -> dict[str, Any]:
        presets = await self._get_file_view_presets()
        return {
            "presets": presets,
            "items": presets,
            "total": len(presets),
        }

    def _normalize_file_view_preset_name(self, name: str) -> str:
        return str(name).strip()[: self.MAX_VIEW_PRESET_NAME_LENGTH]

    def _normalize_file_view_preset_filters(self, filters: dict[str, Any]) -> dict[str, Any]:
        return {
            "keyword": str(filters.get("keyword", "")).strip()[: self.MAX_VIEW_PRESET_KEYWORD_LENGTH],
            "status": str(filters.get("status", "all")).strip().lower(),
            "type": str(filters.get("type", "all")).strip().lower(),
            "health": str(filters.get("health", "all")).strip().lower(),
            "sortBy": str(filters.get("sortBy", "created_at")).strip().lower(),
            "sortOrder": str(filters.get("sortOrder", "desc")).strip().lower(),
            "size": max(1, min(100, int(filters.get("size", 10) or 10))),
        }

    async def save_file_view_preset(
        self,
        preset_id: Optional[str],
        name: str,
        filters: dict[str, Any],
        user_ip: str = "",
    ) -> dict[str, Any]:
        presets = await self._get_file_view_presets()
        normalized_name = self._normalize_file_view_preset_name(name)
        normalized_filters = self._normalize_file_view_preset_filters(filters)
        now = await get_now()
        updated_at = now.isoformat()

        if not normalized_name:
            raise HTTPException(status_code=400, detail="预设名称不能为空")

        if len(presets) >= self.MAX_VIEW_PRESETS:
            raise HTTPException(status_code=400, detail="预设数量已达上限")

        if preset_id:
            for i, preset in enumerate(presets):
                if preset.get("id") == preset_id:
                    presets[i] = {
                        "id": preset_id,
                        "name": normalized_name,
                        "filters": normalized_filters,
                        "params": normalized_filters,
                        "updatedAt": updated_at,
                        "updated_at": updated_at,
                    }
                    break
        else:
            new_id = str(uuid.uuid4())[:8]
            presets.append({
                "id": new_id,
                "name": normalized_name,
                "filters": normalized_filters,
                "params": normalized_filters,
                "createdAt": updated_at,
                "created_at": updated_at,
                "updatedAt": updated_at,
                "updated_at": updated_at,
            })
            preset_id = new_id

        await KeyValue.update_or_create(
            key=self.FILE_VIEW_PRESETS_KEY,
            defaults={"value": presets},
        )
        await ActivityLogService.log(
            action="file.view_preset.save",
            target_type="view_preset",
            target_id=None,
            user_ip=user_ip,
            detail={"presetId": preset_id, "name": normalized_name},
        )
        return {"id": preset_id, "name": normalized_name, "filters": normalized_filters}

    async def delete_file_view_preset(self, preset_id: str, user_ip: str = "") -> dict[str, Any]:
        presets = await self._get_file_view_presets()
        presets = [p for p in presets if p.get("id") != preset_id]
        await KeyValue.update_or_create(
            key=self.FILE_VIEW_PRESETS_KEY,
            defaults={"value": presets},
        )
        await ActivityLogService.log(
            action="file.view_preset.delete",
            target_type="view_preset",
            user_ip=user_ip,
            detail={"presetId": preset_id},
        )
        return {"ok": True}

    async def _build_file_list_item(self, file_code: FileCodes, now: datetime) -> dict:
        is_expired = await file_code.is_expired()
        is_text = file_code.text is not None
        is_chunked = file_code.is_chunked
        is_multi_file = bool(file_code.is_multi_file)
        file_count = 0
        if is_multi_file:
            file_count = await FileItem.filter(file_code_id=file_code.id).count()

        # 即将过期判定（24小时内过期且当前未过期）
        expiring_soon = False
        is_permanent = file_code.expired_at is None and (
            file_code.expired_count is None or file_code.expired_count < 0
        )
        if not is_expired and not is_permanent and file_code.expired_at:
            expired_at = file_code.expired_at.replace(tzinfo=None) if file_code.expired_at.tzinfo else file_code.expired_at
            remaining_seconds = (expired_at - now).total_seconds()
            if 0 <= remaining_seconds <= 24 * 3600:
                expiring_soon = True

        return {
            "id": file_code.id,
            "code": file_code.code,
            "prefix": file_code.prefix,
            "suffix": file_code.suffix,
            "name": f"{file_code.prefix}{file_code.suffix}" if file_code.prefix or file_code.suffix else None,
            "size": file_code.size,
            "text": file_code.text,
            "is_text": is_text,
            "isText": is_text,
            "is_chunked": is_chunked,
            "isChunked": is_chunked,
            "is_multi_file": is_multi_file,
            "isMultiFile": is_multi_file,
            "file_count": file_count,
            "fileCount": file_count,
            "expired_at": file_code.expired_at.isoformat() if file_code.expired_at else None,
            "expired_count": file_code.expired_count,
            "used_count": file_code.used_count,
            "usedCount": file_code.used_count,
            "created_at": file_code.created_at.isoformat() if file_code.created_at else None,
            "status": "expired" if is_expired else ("expiring_soon" if expiring_soon else "active"),
            "is_expired": is_expired,
            "isExpired": is_expired,
            "is_permanent": is_permanent,
            "isPermanent": is_permanent,
            "expiring_soon": expiring_soon,
            "expiringSoon": expiring_soon,
        }

    async def _build_file_detail(self, file_code: FileCodes, now: datetime) -> dict:
        item = await self._build_file_list_item(file_code, now)
        is_expired = item["isExpired"]
        is_text = item["isText"]
        is_permanent = item["isPermanent"]

        remaining_downloads = None
        if file_code.expired_count is not None and file_code.expired_count >= 0:
            remaining_downloads = file_code.expired_count

        display_name = f"{file_code.prefix}{file_code.suffix}" if file_code.prefix or file_code.suffix else None
        detail: dict[str, Any] = {
            **item,
            "filename": display_name,
            "displayName": display_name,
            "display_name": display_name,
            "isPermanent": is_permanent,
            "is_permanent": is_permanent,
            "hasDownloadLimit": file_code.expired_count is not None and file_code.expired_count >= 0,
            "has_download_limit": file_code.expired_count is not None and file_code.expired_count >= 0,
            "hasExpirationTime": file_code.expired_at is not None,
            "has_expiration_time": file_code.expired_at is not None,
            "textLength": len(file_code.text) if file_code.text else 0,
            "text_length": len(file_code.text) if file_code.text else 0,
            "canPreviewText": is_text and file_code.text is not None,
            "can_preview_text": is_text and file_code.text is not None,
            "canDownload": not is_text and not is_expired,
            "can_download": not is_text and not is_expired,
            "storageBackend": settings.file_storage,
            "storage_backend": settings.file_storage,
            "remainingDownloads": remaining_downloads,
            "remaining_downloads": remaining_downloads,
        }

        if not is_text:
            # 使用模型自带的方法获取存储路径
            try:
                file_path = await file_code.get_file_path()
            except Exception:
                file_path = None
            detail.update({
                "filePath": file_path,
                "file_path": file_path,
                "uuidFileName": file_code.uuid_file_name,
                "uuid_file_name": file_code.uuid_file_name,
            })

        metadata = await self.get_file_metadata(file_code.id)
        detail["metadata"] = metadata

        return detail

    async def build_file_health_summary(self, all_codes: list[FileCodes], now: datetime) -> dict:
        expired_count = 0
        expiring_soon_count = 0
        never_retrieved_count = 0
        permanent_count = 0
        healthy_count = 0
        attention_count = 0
        danger_count = 0
        warning_count = 0

        for file_code in all_codes:
            is_expired = await file_code.is_expired()
            if is_expired:
                expired_count += 1
                danger_count += 1
                continue

            is_permanent = file_code.expired_at is None and (file_code.expired_count is None or file_code.expired_count < 0)
            if is_permanent:
                permanent_count += 1
                healthy_count += 1
                continue

            if file_code.expired_at:
                expired_at = file_code.expired_at.replace(tzinfo=None) if file_code.expired_at.tzinfo else file_code.expired_at
                remaining_seconds = (expired_at - now).total_seconds()
                if remaining_seconds < 0:
                    expired_count += 1
                    danger_count += 1
                elif remaining_seconds < 24 * 3600:
                    expiring_soon_count += 1
                    warning_count += 1
                else:
                    healthy_count += 1
            else:
                healthy_count += 1

            if file_code.used_count == 0:
                never_retrieved_count += 1

        return {
            "expiredCount": expired_count,
            "expiringSoonCount": expiring_soon_count,
            "neverRetrievedCount": never_retrieved_count,
            "permanentCount": permanent_count,
            "healthyCount": healthy_count,
            "healthAttentionCount": attention_count,
            "healthDangerCount": danger_count,
            "healthWarningCount": warning_count,
        }

    async def list_files(self, page: int, size: int, keyword: str = "", status: str = "", file_type: str = "", health: str = "", sort_by: str = "created_at", sort_order: str = "desc", user_ip: str = ""):
        from tortoise.expressions import Q
        offset = (page - 1) * size
        page = max(page, 1)
        size = min(max(size, 1), 100)

        collection_file_ids = await CollectionFile.filter(
            file_code_id__isnull=False
        ).values_list("file_code_id", flat=True)
        exclude_ids = [fid for fid in collection_file_ids if fid is not None]

        query = FileCodes.all()

        if keyword:
            query = query.filter(Q(prefix__icontains=keyword) | Q(suffix__icontains=keyword))

        if exclude_ids:
            query = query.exclude(id__in=exclude_ids)

        now = await get_now()

        if status:
            if status == "active":
                query = query.filter(Q(expired_at__isnull=True) | Q(expired_at__gte=now)).exclude(expired_count=0)
            elif status == "expired":
                query = query.filter(Q(expired_at__isnull=False, expired_at__lt=now) | Q(expired_count=0))

        if file_type:
            if file_type == "text":
                query = query.filter(text__isnull=False)
            elif file_type == "file":
                query = query.filter(text__isnull=True)
            elif file_type == "chunked":
                query = query.filter(is_chunked=True)

        if health:
            if health == "expired":
                query = query.filter(Q(expired_at__isnull=False, expired_at__lt=now) | Q(expired_count=0))
            elif health == "expiring_soon":
                query = query.filter(expired_at__isnull=False, expired_at__gte=now, expired_at__lte=now + timedelta(hours=24))
            elif health == "never_retrieved":
                query = query.filter(used_count=0)
            elif health == "permanent":
                query = query.filter(expired_at__isnull=True, expired_count__lt=0)

        sort_field = sort_by
        if sort_field not in self.SORT_FIELDS:
            sort_field = "created_at"

        if sort_order.lower() == "asc":
            query = query.order_by(sort_field)
        else:
            query = query.order_by(f"-{sort_field}")

        total = await query.count()
        files = await query.limit(size).offset(offset)

        # 统计汇总时同样排除 CollectionFile 关联的 FileCodes 记录，避免重复计数
        if exclude_ids:
            all_codes = await FileCodes.exclude(id__in=exclude_ids)
        else:
            all_codes = await FileCodes.all()
        summary = await self.build_file_health_summary(all_codes, now)
        summary["totalFiles"] = len(all_codes)
        summary["storageUsed"] = sum([code.size for code in all_codes])
        summary["usedCount"] = sum([code.used_count for code in all_codes])

        result = []
        for f in files:
            result.append(await self._build_file_list_item(f, now))

        return result, total, summary

    async def get_file_detail(self, file_id: int):
        file_code = await FileCodes.filter(id=file_id).first()
        if not file_code:
            raise HTTPException(status_code=404, detail="文件不存在")

        now = await get_now()
        return await self._build_file_detail(file_code, now)

    async def list_admin_activities(self, limit: int = 20, action: str = None, target_type: str = None, keyword: str = None) -> dict[str, Any]:
        query = ActivityLog.all().order_by("-created_at")

        if action:
            query = query.filter(action=action)
        if target_type:
            query = query.filter(target_type=target_type)

        activities = await query.limit(min(limit, 100))

        result = []
        for activity in activities:
            result.append({
                "id": activity.id,
                "action": activity.action,
                "target_type": activity.target_type,
                "target_id": activity.target_id,
                "user_ip": activity.user_ip,
                "user_agent": activity.user_agent,
                "detail": activity.detail,
                "created_at": activity.created_at.isoformat() if activity.created_at else None,
            })

        return {"activities": result, "total": len(result)}


class ConfigService:
    def get_config(self):
        from core.utils import SENSITIVE_KEYS
        config = dict(settings.items())
        for key in SENSITIVE_KEYS:
            if key in config and config[key]:
                config[key] = "******"
        return config

    async def update_config(self, data: dict, user_ip: str = ""):
        from core.utils import hash_password, is_password_hashed, encrypt_value, SENSITIVE_KEYS
        from core.settings import CONFIG_NUMERIC_LIMITS, CONFIG_BOOLEAN_KEYS
        admin_token = data.get("admin_token")
        if admin_token is None or admin_token == "":
            raise HTTPException(status_code=400, detail="管理员密码不能为空")

        if not is_password_hashed(admin_token):
            data["admin_token"] = hash_password(admin_token)

        # 过滤掉空字符串键，避免覆盖默认值
        keys_to_drop = []
        for key, value in list(data.items()):
            if key not in settings.default_config:
                keys_to_drop.append(key)
                continue
            # 空字符串不更新（保留原值）
            if value == "" and key not in ("storage_path", "s3_hostname", "background",
                                            "webdav_url", "webdav_username", "webdav_password",
                                            "keywords", "robotsText", "notify_title", "notify_content",
                                            "name", "description", "admin_token", "page_explain",
                                            "trustedProxies", "s3_endpoint_url", "s3_region_name",
                                            "s3_access_key_id", "s3_secret_access_key", "s3_bucket_name",
                                            "themesSelect"):
                keys_to_drop.append(key)
                continue
            if key in SENSITIVE_KEYS and isinstance(value, str) and value and not value.startswith("enc:"):
                data[key] = encrypt_value(value)
            # 布尔型配置项：只允许 0 或 1
            if key in CONFIG_BOOLEAN_KEYS:
                try:
                    bool_val = int(value)
                except (TypeError, ValueError):
                    raise HTTPException(status_code=400, detail=f"配置项 {key} 必须是 0 或 1")
                if bool_val not in (0, 1):
                    raise HTTPException(status_code=400, detail=f"配置项 {key} 必须是 0 或 1")
                data[key] = bool_val
                continue
            # 数值型配置项：类型转换 + 范围验证
            if key in CONFIG_NUMERIC_LIMITS:
                min_val, max_val, desc = CONFIG_NUMERIC_LIMITS[key]
                try:
                    if key == "opacity":
                        num_val = float(value)
                    else:
                        num_val = int(value)
                except (TypeError, ValueError):
                    raise HTTPException(status_code=400, detail=f"配置项 {key} 必须是数字")
                if num_val < min_val or num_val > max_val:
                    raise HTTPException(status_code=400, detail=desc)
                data[key] = num_val

        for k in keys_to_drop:
            data.pop(k, None)

        await KeyValue.filter(key="settings").update(value=data)
        for k, v in data.items():
            settings.__setattr__(k, v)
        refresh_settings()

        await ActivityLogService.log(
            action="config.update",
            target_type="config",
            user_ip=user_ip,
            detail={"updated_keys": list(data.keys())},
        )


class CollectionService:
    def __init__(self):
        self.file_storage: FileStorageInterface = storages[settings.file_storage]()

    COLLECTION_SORT_FIELDS = {
        "created_at",
        "expired_at",
        "delivery_expired_at",
        "retrieve_expired_at",
        "title",
        "max_files",
    }

    async def list_collections(
        self,
        page: int = 1,
        size: int = 10,
        keyword: str = None,
        status: str = "",
        sort_by: str = "created_at",
        sort_order: str = "desc",
    ):
        from apps.base.models import CollectionBox, CollectionFile
        from tortoise.expressions import Q

        page = max(page, 1)
        size = min(max(size, 1), 100)
        sort_by = sort_by if sort_by in self.COLLECTION_SORT_FIELDS else "created_at"
        sort_order = "desc" if sort_order.lower() == "desc" else "asc"
        order_field = f"{'-' if sort_order == 'desc' else ''}{sort_by}"

        now = datetime.now()
        query = CollectionBox.all()
        if keyword:
            query = query.filter(
                Q(title__icontains=keyword) |
                Q(collection_code__icontains=keyword) |
                Q(delivery_code__icontains=keyword) |
                Q(retrieve_code__icontains=keyword)
            )

        # 状态筛选需要分两次查询（active / expired），因过期判断依赖 expired_at
        status_norm = (status or "").strip().lower()
        if status_norm == "active":
            query = query.filter(Q(expired_at__isnull=True) | Q(expired_at__gt=now))
        elif status_norm == "expired":
            query = query.filter(expired_at__lt=now)

        total = await query.count()
        offset = (page - 1) * size
        boxes = await query.order_by(order_field).offset(offset).limit(size).values(
            "id", "collection_code", "delivery_code", "retrieve_code",
            "title", "description", "max_files", "created_at", "expired_at",
            "delivery_expired_at", "retrieve_expired_at", "creator_ip", "creator_name"
        )

        total_files_in_page = 0
        result = []
        for box in boxes:
            file_count = await CollectionFile.filter(collection_id=box["id"]).count()
            total_files_in_page += file_count
            expired_at = box.get("expired_at")
            if expired_at and hasattr(expired_at, 'tzinfo') and expired_at.tzinfo:
                expired_at = expired_at.replace(tzinfo=None)
            is_expired = expired_at is not None and expired_at < now

            delivery_expired_at = box.get("delivery_expired_at")
            if delivery_expired_at and hasattr(delivery_expired_at, 'tzinfo') and delivery_expired_at.tzinfo:
                delivery_expired_at = delivery_expired_at.replace(tzinfo=None)
            retrieve_expired_at = box.get("retrieve_expired_at")
            if retrieve_expired_at and hasattr(retrieve_expired_at, 'tzinfo') and retrieve_expired_at.tzinfo:
                retrieve_expired_at = retrieve_expired_at.replace(tzinfo=None)

            # 即将过期判定（24小时内）
            expiring_soon = False
            if expired_at is not None and not is_expired:
                expiring_soon = (expired_at - now).total_seconds() <= 86400

            result.append({
                **box,
                "file_count": file_count,
                "is_expired": is_expired,
                "expiring_soon": expiring_soon,
                "delivery_is_expired": delivery_expired_at is not None and delivery_expired_at < now,
                "retrieve_is_expired": retrieve_expired_at is not None and retrieve_expired_at < now,
            })

        # 统计概览（不受 status 筛选影响）
        all_total = await CollectionBox.all().count()
        expired_total = await CollectionBox.filter(expired_at__lt=now).count()
        active_total = all_total - expired_total
        total_files = await CollectionFile.all().count()
        permanent_total = await CollectionBox.filter(expired_at__isnull=True).count()
        expiring_soon_total = 0
        # 即将过期：未过期且24小时内过期
        soon_threshold = now + timedelta(hours=24)
        soon_boxes = await CollectionBox.filter(
            expired_at__isnull=False,
            expired_at__gt=now,
            expired_at__lt=soon_threshold,
        ).count()
        expiring_soon_total = soon_boxes

        return {
            "data": result,
            "total": total,
            "page": page,
            "size": size,
            "summary": {
                "totalCollections": all_total,
                "activeCollections": active_total,
                "expiredCollections": expired_total,
                "expiringSoonCollections": expiring_soon_total,
                "permanentCollections": permanent_total,
                "totalFiles": total_files,
                "filteredFiles": total_files_in_page,
            }
        }

    async def get_collection_files(self, collection_id: int):
        from apps.base.models import CollectionBox, CollectionFile
        box = await CollectionBox.filter(id=collection_id).first()
        if not box:
            raise HTTPException(status_code=404, detail="收件箱不存在")

        files = await CollectionFile.filter(collection=box).order_by("-created_at").values(
            "id", "uploader_name", "uploader_ip", "status", "created_at", "file_code_id"
        )
        result = []
        for f in files:
            file_info: dict = {
                "id": f["id"],
                "file_name": "",
                "file_size": 0,
                "uploader_name": f["uploader_name"] or "",
                "uploader_ip": f["uploader_ip"] or "",
                "status": f["status"] or "completed",
                "created_at": f["created_at"],
                "file_code_id": f["file_code_id"],
            }
            if f["file_code_id"]:
                fc = await FileCodes.filter(id=f["file_code_id"]).first()
                if fc:
                    file_info["file_name"] = f"{fc.prefix}{fc.suffix}" if fc.prefix or fc.suffix else ""
                    file_info["file_size"] = fc.size or 0
                    file_info["code"] = fc.code
                    # 推断文件类型：文本优先，然后分片，再普通文件
                    if fc.text is not None:
                        file_info["file_type"] = "text"
                    elif fc.is_chunked:
                        file_info["file_type"] = "chunked"
                    else:
                        file_info["file_type"] = "file"
                    file_info["is_text"] = fc.text is not None
                    file_info["is_chunked"] = bool(fc.is_chunked)
            result.append(file_info)
        return result

    async def delete_collection(self, collection_id: int, user_ip: str = ""):
        from apps.base.models import CollectionBox
        box = await CollectionBox.filter(id=collection_id).first()
        if not box:
            raise HTTPException(status_code=404, detail="收件箱不存在")

        collection_code = box.collection_code
        title = box.title

        collection_files = await CollectionFile.filter(collection=box).all()
        for cf in collection_files:
            if cf.file_code_id:
                file_code = await FileCodes.filter(id=cf.file_code_id).first()
                if file_code:
                    try:
                        await self.file_storage.delete_file(file_code)
                    except Exception:
                        pass
                    await file_code.delete()

        await box.delete()

        await ActivityLogService.log(
            action="collection.delete",
            target_type="collection",
            target_id=collection_id,
            user_ip=user_ip,
            detail={"collection_code": collection_code, "title": title},
        )

    async def batch_delete_collections(self, collection_ids: list, user_ip: str = ""):
        deleted_count = 0
        for collection_id in collection_ids:
            try:
                await self.delete_collection(collection_id, user_ip)
                deleted_count += 1
            except Exception:
                pass
        return deleted_count


class LocalFileService:
    def __init__(self):
        self.file_storage: FileStorageInterface = storages[settings.file_storage]()

    async def list_local_files(self):
        if settings.file_storage != "local":
            raise HTTPException(status_code=400, detail="仅本地存储支持此功能")

        files_dir = os.path.join(data_root, "files")
        if not os.path.exists(files_dir):
            return []

        result = []
        for filename in os.listdir(files_dir):
            filepath = os.path.join(files_dir, filename)
            if os.path.isfile(filepath):
                result.append({
                    "name": filename,
                    "path": filepath,
                    "size": os.path.getsize(filepath),
                    "mtime": datetime.fromtimestamp(os.path.getmtime(filepath)).isoformat(),
                })

        return result

    async def delete_local_file(self, filename: str, user_ip: str = ""):
        if settings.file_storage != "local":
            raise HTTPException(status_code=400, detail="仅本地存储支持此功能")

        files_dir = os.path.join(data_root, "files")
        filepath = os.path.join(files_dir, filename)

        if not os.path.exists(filepath):
            raise HTTPException(status_code=404, detail="文件不存在")

        os.remove(filepath)

        await ActivityLogService.log(
            action="local.delete",
            target_type="local_file",
            user_ip=user_ip,
            detail={"filename": filename},
        )

    async def share_local_file(self, filename: str, expire_style: str = "day", expire_value: int = 1, user_ip: str = ""):
        if settings.file_storage != "local":
            raise HTTPException(status_code=400, detail="仅本地存储支持此功能")

        files_dir = os.path.join(data_root, "files")
        filepath = os.path.join(files_dir, filename)

        if not os.path.exists(filepath):
            raise HTTPException(status_code=404, detail="文件不存在")

        from apps.base.utils import get_expire_info
        from apps.base.models import FileCodes

        size = os.path.getsize(filepath)
        # get_expire_info 返回 (expired_at, expired_count, used_count, code)
        expire_at, expire_count, _used_count, code = await get_expire_info(expire_value, expire_style)

        file_code = await FileCodes.create(
            code=code,
            prefix=filename,
            suffix="",
            size=size,
            expire_style=expire_style,
            expire_value=expire_value,
            expired_at=expire_at,
            expired_count=expire_count,
        )

        await ActivityLogService.log(
            action="local.share",
            target_type="local_file",
            user_ip=user_ip,
            detail={"filename": filename, "code": code},
        )

        return {"code": code, "link": f"/{code}"}
