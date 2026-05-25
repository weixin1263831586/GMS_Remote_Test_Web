// ==================== 分块上传优化 ====================

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
        resume = false // 是否支持断点续传
    } = options;

    const fileSize = file.size;
    const totalChunks = Math.ceil(fileSize / chunkSize);
    const uploadId = window.crypto && window.crypto.randomUUID
        ? window.crypto.randomUUID()
        : 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
            const r = Math.random() * 16 | 0;
            const v = c === 'x' ? r : (r & 0x3 | 0x8);
            return v.toString(16);
        });

    const formatBytes = getFormatBytes();
    console.log(`[ChunkUpload] Starting: ${file.name} (${formatBytes(fileSize)})`);
    console.log(`[ChunkUpload] Chunk size: ${formatBytes(chunkSize)}, Total chunks: ${totalChunks}`);
    console.log(`[ChunkUpload] Concurrent uploads: ${concurrent}`);

    // 已上传的块
    let uploadedChunks = new Set();
    let failedChunks = [];
    let completionResult = null;

    // 如果支持断点续传，检查已上传的块（可选，会稍微减慢初始速度）
    if (resume && false) { // 暂时禁用断点检查以提高速度
        try {
            const checkResponse = await fetch(`${url}?check_chunks=1&upload_id=${uploadId}`, {
                method: 'POST'
            });
            if (checkResponse.ok) {
                const uploadedData = await checkResponse.json();
                if (uploadedData.uploaded_chunks) {
                    uploadedChunks = new Set(uploadedData.uploaded_chunks);
                    console.log(`[ChunkUpload] Resuming: ${uploadedChunks.size} chunks already uploaded`);
                }
            }
        } catch (e) {
            console.log('[ChunkUpload] Could not check resume status, starting fresh');
        }
    }

    // 上传单个块
    async function uploadChunk(chunkIndex, retry = 0) {
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
                                if (result.data && result.data.uploaded) {
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
        const chunks = Array.from({length: totalChunks}, (_, i) => i);

        // 过滤已上传的块（断点续传）
        const chunksToUpload = resume ?
            chunks.filter(i => !uploadedChunks.has(i)) :
            chunks;

        console.log(`[ChunkUpload] Chunks to upload: ${chunksToUpload.length}/${totalChunks}`);

        // 分批并发上传
        for (let i = 0; i < chunksToUpload.length; i += concurrent) {
            const batch = chunksToUpload.slice(i, i + concurrent);
            await Promise.all(batch.map(index => uploadChunk(index)));
        }

        console.log(`[ChunkUpload] Completed: ${uploadedChunks.size}/${totalChunks} chunks`);

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
 * 本地格式化字节大小函数 (fallback)
 */
function formatBytesLocal(bytes, decimals = 2) {
    if (bytes === 0) return '0 Bytes';
    if (!bytes && bytes !== 0) return bytes + ' bytes';

    const k = 1024;
    const dm = decimals < 0 ? 0 : decimals;
    const sizes = ['Bytes', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(dm)) + ' ' + sizes[i];
}

/**
 * 获取格式化字节函数 (优先使用全局，否则使用本地)
 */
function getFormatBytes() {
    return window.formatBytes || formatBytesLocal;
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
        console.log('[Upload] Using regular upload for small file');
        return uploadFileRegular(file, url, { onProgress, onStart, onComplete, onError });
    }

    // 大文件使用分块上传
    console.log('[Upload] Using chunked upload for large file');

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
            if (onError) onError(new Error('Network error'));
            reject(new Error('Network error'));
        });

        xhr.open('POST', url);
        xhr.send(formData);
    });
}

// 导出到全局
window.uploadFileInChunks = uploadFileInChunks;
window.uploadFileWithProgress = uploadFileWithProgress;
