// ==================== 分块上传优化 ====================

const CHUNK_UPLOAD_DEBUG = false;

function chunkDebugLog(...args) {
    if (CHUNK_UPLOAD_DEBUG) {
        console.log(...args);
    }
}

/**
 * 分块上传大文件
 * @param {File} file - 要上传的文件
 * @param {string} url - 上传URL
 * @param {Object} options - 配置选项
 * @returns {Promise} 上传结果
 */
async function uploadFileInChunks(file, url, options = {}) {
    const {
        chunkSize = 32 * 1024 * 1024, // 32MB per chunk (优化：更大块减少开销)
        maxRetries = 3,
        onProgress = null,
        onChunkProgress = null,
        concurrent = 8, // 并发上传数 (增加并发)
        resume = false, // 是否支持断点续传
        uploadId: providedUploadId = '',
        extraFormData = null,
        headers = null,
        checkExisting = false
    } = options;

    const fileSize = file.size;
    const totalChunks = Math.ceil(fileSize / chunkSize);
    const uploadId = providedUploadId || (window.crypto && window.crypto.randomUUID
        ? window.crypto.randomUUID()
        : 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
            const r = Math.random() * 16 | 0;
            const v = c === 'x' ? r : (r & 0x3 | 0x8);
            return v.toString(16);
        }));

    const formatBytes = window.formatBytes;
    chunkDebugLog(`[ChunkUpload] Starting: ${file.name} (${formatBytes(fileSize)})`);
    chunkDebugLog(`[ChunkUpload] Chunk size: ${formatBytes(chunkSize)}, Total chunks: ${totalChunks}`);
    chunkDebugLog(`[ChunkUpload] Concurrent uploads: ${concurrent}`);

    // 已上传的块
    let uploadedChunks = new Set();
    let failedChunks = [];
    let completionResult = null;

    function appendExtraFields(formData) {
        if (!extraFormData) return;
        if (extraFormData instanceof FormData) {
            for (const [key, value] of extraFormData.entries()) {
                formData.append(key, value);
            }
            return;
        }
        Object.entries(extraFormData).forEach(([key, value]) => {
            if (Array.isArray(value)) {
                value.forEach(item => formData.append(key, item));
            } else if (value !== undefined && value !== null) {
                formData.append(key, value);
            }
        });
    }

    function applyHeaders(xhr) {
        if (!headers) return;
        Object.entries(headers).forEach(([key, value]) => {
            if (value !== undefined && value !== null) {
                xhr.setRequestHeader(key, value);
            }
        });
    }

    async function checkUploadedChunks() {
        const formData = new FormData();
        formData.append('check_chunks', '1');
        formData.append('total_chunks', totalChunks);
        formData.append('upload_id', uploadId);
        formData.append('file_name', file.name);
        formData.append('file_size', fileSize);
        appendExtraFields(formData);

        return new Promise((resolve) => {
            const xhr = new XMLHttpRequest();
            xhr.addEventListener('load', () => {
                if (xhr.status !== 200) {
                    resolve([]);
                    return;
                }
                try {
                    const result = JSON.parse(xhr.responseText);
                    resolve(Array.isArray(result.uploaded_chunks) ? result.uploaded_chunks : []);
                } catch (_e) {
                    resolve([]);
                }
            });
            xhr.addEventListener('error', () => resolve([]));
            xhr.open('POST', url);
            applyHeaders(xhr);
            xhr.send(formData);
        });
    }

    // 上传单个块
    async function uploadChunk(chunkIndex, retry = 0) {
        if (uploadedChunks.has(chunkIndex)) {
            return {success: true, skipped: true, chunk_index: chunkIndex};
        }
        const start = chunkIndex * chunkSize;
        const end = Math.min(start + chunkSize, fileSize);
        const chunk = file.slice(start, end);

        const formData = new FormData();
        formData.append('file', chunk, file.name);
        formData.append('chunk_index', chunkIndex);
        formData.append('total_chunks', totalChunks);
        formData.append('upload_id', uploadId);
        formData.append('file_name', file.name);
        formData.append('file_size', fileSize);
        formData.append('resume', resume ? '1' : '0');
        appendExtraFields(formData);

        try {
            const xhr = new XMLHttpRequest();

            return new Promise((resolve, reject) => {
                // 上传进度
                if (onChunkProgress) {
                    xhr.upload.addEventListener('progress', (e) => {
                        if (e.lengthComputable) {
                            const chunkProgress = (e.loaded / e.total) * 100;
                            onChunkProgress(chunkIndex, chunkProgress, e.loaded, e.total);
                        }
                    });
                }

                // 上传完成
                xhr.addEventListener('load', () => {
                    if (xhr.status === 200) {
                        try {
                            const result = JSON.parse(xhr.responseText);
                            if (result.success) {
                                if (result.upload_complete || result.message || (result.data && result.data.uploaded)) {
                                    completionResult = result;
                                }
                                uploadedChunks.add(chunkIndex);
                                if (onProgress) {
                                    const progress = (uploadedChunks.size / totalChunks) * 100;
                                    onProgress(progress, uploadedChunks.size, totalChunks);
                                }
                                resolve(result);
                            } else {
                                reject(new Error(result.error || 'Upload failed'));
                            }
                        } catch (e) {
                            reject(new Error('Invalid response'));
                        }
                    } else {
                        reject(new Error(`HTTP ${xhr.status}`));
                    }
                });

                // 上传错误
                xhr.addEventListener('error', () => {
                    reject(new Error('Network error'));
                });

                // 上传中止
                xhr.addEventListener('abort', () => {
                    reject(new Error('Upload aborted'));
                });

                // 发送请求
                xhr.open('POST', url);
                applyHeaders(xhr);
                xhr.send(formData);
            });
        } catch (error) {
            console.warn(`[ChunkUpload] Chunk ${chunkIndex} failed (attempt ${retry + 1}):`, error);

            if (retry < maxRetries) {
                await new Promise(resolve => setTimeout(resolve, 1000 * (retry + 1)));
                return uploadChunk(chunkIndex, retry + 1);
            } else {
                failedChunks.push(chunkIndex);
                throw error;
            }
        }
    }

    // 并发上传所有块
    try {
        if (resume && checkExisting) {
            const existing = await checkUploadedChunks();
            uploadedChunks = new Set(existing.map(Number).filter(idx => idx >= 0 && idx < totalChunks));
            if (uploadedChunks.size === totalChunks && totalChunks > 0) {
                uploadedChunks.delete(totalChunks - 1);
            }
            if (uploadedChunks.size && onProgress) {
                onProgress((uploadedChunks.size / totalChunks) * 100, uploadedChunks.size, totalChunks);
            }
            chunkDebugLog(`[ChunkUpload] Resume found: ${uploadedChunks.size}/${totalChunks}`);
        }
        const chunks = Array.from({length: totalChunks}, (_, i) => i);

        chunkDebugLog(`[ChunkUpload] Chunks to upload: ${chunks.length}/${totalChunks}`);

        // 分批并发上传
        for (let i = 0; i < chunks.length; i += concurrent) {
            const batch = chunks.slice(i, i + concurrent);
            await Promise.all(batch.map(index => uploadChunk(index)));
        }

        chunkDebugLog(`[ChunkUpload] Completed: ${uploadedChunks.size}/${totalChunks} chunks`);

        if (completionResult) {
            return completionResult;
        }

        return {
            success: true,
            upload_id: uploadId,
            chunks_uploaded: uploadedChunks.size,
            total_chunks: totalChunks,
            file_name: file.name,
            file_size: fileSize
        };
    } catch (error) {
        console.error('[ChunkUpload] Failed:', error);
        throw {
            error: error.message,
            upload_id: uploadId,
            uploaded_chunks: Array.from(uploadedChunks),
            failed_chunks: failedChunks
        };
    }
}

/**
 * 使用分块上传替换原有的上传函数
 */
async function uploadFileWithProgress(file, url, options = {}) {
    const {
        onProgress = null,
        onStart = null,
        onComplete = null,
        onError = null,
        useChunkUpload = true, // 是否使用分块上传
        chunkSize = 8 * 1024 * 1024 // 8MB
    } = options;

    // 小文件（< 100MB）使用普通上传
    if (!useChunkUpload || file.size < 100 * 1024 * 1024) {
        chunkDebugLog('[Upload] Using regular upload for small file');
        return uploadFileRegular(file, url, { onProgress, onStart, onComplete, onError });
    }

    // 大文件使用分块上传
    chunkDebugLog('[Upload] Using chunked upload for large file');

    if (onStart) onStart();

    try {
        const result = await uploadFileInChunks(file, url, {
            chunkSize,
            onProgress,
            concurrent: 8
        });

        if (onComplete) onComplete(result);
        return result;
    } catch (error) {
        if (onError) onError(error);
        throw error;
    }
}

/**
 * 普通上传（用于小文件）
 */
async function uploadFileRegular(file, url, options = {}) {
    const { onProgress = null, onStart = null, onComplete = null, onError = null } = options;

    if (onStart) onStart();

    return new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        const formData = new FormData();
        formData.append('file', file);

        xhr.upload.addEventListener('progress', (e) => {
            if (e.lengthComputable && onProgress) {
                const progress = (e.loaded / e.total) * 100;
                onProgress(progress, e.loaded, e.total);
            }
        });

        xhr.addEventListener('load', () => {
            if (xhr.status === 200) {
                try {
                    const result = JSON.parse(xhr.responseText);
                    if (onComplete) onComplete(result);
                    resolve(result);
                } catch (e) {
                    reject(new Error('Invalid response'));
                }
            } else {
                reject(new Error(`HTTP ${xhr.status}`));
            }
        });

        xhr.addEventListener('error', () => {
            const err = new Error('Network error');
            if (onError) onError(err);
            reject(err);
        });

        xhr.open('POST', url);
        xhr.send(formData);
    });
}

// 导出到全局
window.uploadFileInChunks = uploadFileInChunks;
window.uploadFileWithProgress = uploadFileWithProgress;
