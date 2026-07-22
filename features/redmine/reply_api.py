"""Redmine issue reply API."""

import logging

from fastapi import APIRouter, Request, UploadFile

from features.redmine.client import RedmineClient
from foundation.config import config_manager
from foundation.responses import error_response, success_response


logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/api/redmine/reply")
async def redmine_reply(request: Request):
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

        if not issue_id:
            return error_response('缺少 issue_id 参数', status_code=400)

        if not reply_text:
            return error_response('缺少 reply_text 参数', status_code=400)

        logger.info(f"[Redmine Reply] 准备发送回复到 Issue #{issue_id}，附件数: {len(files)}")

        stored_creds = config_manager.load_redmine_credentials()
        if not stored_creds:
            return error_response('未配置 Redmine 凭证', status_code=401)

        try:
            redmine_config = config_manager.get_redmine_config()
            base_url = redmine_config['base_url']
        except ValueError as e:
            return error_response(str(e), status_code=404)

        attachment_files = []
        for f in files:
            content = await f.read()
            if not content:
                continue
            file_content_type = f.content_type or 'application/octet-stream'
            filename = f.filename or 'attachment'
            logger.info(f"[Redmine Reply] 上传附件: {filename} ({len(content)} bytes)")
            attachment_files.append({'content': content, 'filename': filename, 'content_type': file_content_type})

        client = RedmineClient(base_url, stored_creds.get('username'), stored_creds.get('password'))
        result = await client.reply_issue(issue_id, reply_text, attachment_files)
        attachment_info = f"，携带 {result.get('attachments', 0)} 个附件" if result.get('attachments') else ''
        logger.info(f"[Redmine Reply] 回复已成功发送到 Issue #{issue_id}{attachment_info}")
        return success_response(result, message=f'回复已发送到 Redmine Issue #{issue_id}{attachment_info}')

    except Exception as e:
        logger.error(f"[Redmine Reply] 发送回复失败：{e}")
        return error_response(
            f'发送失败：{e!s}',
            status_code=500,
        )
