"""Redmine issue reply API."""

import logging

from fastapi import APIRouter, Depends, Request, UploadFile

from features.auth import (
    CurrentUser,
    require_human_principal_when_auth_required,
)
from features.redmine.api import get_redmine_config_for_request
from features.redmine.client import RedmineClient
from foundation.error_model import ApiError, record_internal_error
from foundation.responses import error_response, success_response


logger = logging.getLogger(__name__)

router = APIRouter()

# 回复附件与证据链路同级的单附件上限，并限制总上传量（内容全量驻留内存）。
_MAX_REPLY_ATTACHMENT_BYTES = 256 * 1024 * 1024
_MAX_REPLY_ATTACHMENT_TOTAL_BYTES = 256 * 1024 * 1024


@router.post("/api/redmine/reply")
async def redmine_reply(
    request: Request,
    _user: CurrentUser | None = Depends(
        require_human_principal_when_auth_required
    ),
):
    """向 Redmine 工单发送文本回复和可选附件。"""
    try:
        content_type = (request.headers.get("content-type") or "").lower()
        files: list[UploadFile] = []
        if content_type.startswith("multipart/form-data"):
            form = await request.form()
            issue_id = str(form.get("issue_id") or "").strip()
            reply_text = str(form.get("reply_text") or "").strip()
            files = [item for item in form.getlist("files") if isinstance(item, UploadFile)]
        else:
            body = await request.json()
            issue_id = str(body.get("issue_id") or "").strip()
            reply_text = str(body.get("reply_text") or "").strip()

        # issue_id 会拼进 Redmine 路径：只接受正整数，拒绝路径注入形态。
        try:
            issue_number = int(issue_id)
        except (TypeError, ValueError):
            issue_number = 0
        if issue_number <= 0:
            return error_response('issue_id 必须是正整数', status_code=400)
        issue_id = str(issue_number)

        if not reply_text:
            return error_response('缺少 reply_text 参数', status_code=400)

        logger.info(f"[Redmine Reply] 准备发送回复到 Issue #{issue_id}，附件数: {len(files)}")

        # Replies are externally visible writes.  They must use the current
        # resource owner's credentials, never a legacy process-global account.
        manager = get_redmine_config_for_request(request)
        stored_creds = manager.load_redmine_credentials() or {}
        api_key = manager.load_redmine_api_key()
        if not api_key and not (
            stored_creds.get("username") and stored_creds.get("password")
        ):
            return error_response('未配置 Redmine 凭证', status_code=401)

        try:
            redmine_config = manager.get_redmine_config()
            base_url = redmine_config['base_url']
        except ValueError as e:
            return error_response(str(e), status_code=404)

        attachment_files = []
        total_bytes = 0
        for f in files:
            content = await f.read()
            if not content:
                continue
            # 文件名来自不可信输入（可含换行/敏感内容），不写日志；
            # 超限附件整体拒绝，避免半批上传后才发现超限。
            if len(content) > _MAX_REPLY_ATTACHMENT_BYTES:
                return error_response(
                    f'附件超过大小上限 {_MAX_REPLY_ATTACHMENT_BYTES} 字节', status_code=400
                )
            total_bytes += len(content)
            if total_bytes > _MAX_REPLY_ATTACHMENT_TOTAL_BYTES:
                return error_response(
                    f'附件总量超过上限 {_MAX_REPLY_ATTACHMENT_TOTAL_BYTES} 字节', status_code=400
                )
            file_content_type = f.content_type or 'application/octet-stream'
            filename = f.filename or 'attachment'
            logger.info(f"[Redmine Reply] 上传附件: {len(content)} bytes")
            attachment_files.append({'content': content, 'filename': filename, 'content_type': file_content_type})

        client = RedmineClient(
            base_url,
            stored_creds.get('username', ''),
            stored_creds.get('password', ''),
            api_key=api_key,
        )
        try:
            result = await client.reply_issue(issue_id, reply_text, attachment_files)
        except Exception:
            # 远端 Redmine 的网络/5xx/认证失败是上游故障，不是程序错误：
            # 映射 502 让调用方知道可以重试，而不是误报 500。
            message = record_internal_error(
                logger, "回复发送", "[Redmine Reply] remote reply failed"
            )
            return ApiError.upstream_failure(
                f'Redmine 回复发送失败: {message}', service='redmine'
            ).to_response()
        finally:
            await client.close()
        attachment_info = f"，携带 {result.get('attachments', 0)} 个附件" if result.get('attachments') else ''
        logger.info(f"[Redmine Reply] 回复已成功发送到 Issue #{issue_id}{attachment_info}")
        return success_response(result, message=f'回复已发送到 Redmine Issue #{issue_id}{attachment_info}')

    except Exception:
        message = record_internal_error(
            logger, "回复发送", "[Redmine Reply] send reply failed"
        )
        return error_response(message, status_code=500)
