/**
 * 统一对象存储客户端（JS 版）
 *
 * 对齐 Python 版 NextGenStorage/client.py 的设计思路：
 * 基于 @aws-sdk/client-s3 的 S3 兼容协议，通过 endpoint 切换不同云厂商。
 * 腾讯云 COS / 阿里云 OSS / AWS S3 / MinIO 等均可使用同一套接口。
 */

import { S3Client, GetObjectCommand, HeadObjectCommand, PutObjectCommand, ListObjectsV2Command } from '@aws-sdk/client-s3';
import { getSignedUrl } from '@aws-sdk/s3-request-presigner';
import { getProvider, detectProvider } from './providers';

export default class StorageClient {
  /**
   * @param {Object} options
   * @param {string} [options.provider='oss'] - 厂商标识 ("cos", "oss", "s3", "minio")
   * @param {string} options.region - 地域
   * @param {string} options.accessKey - AK
   * @param {string} options.secretKey - SK
   * @param {string} [options.sessionToken] - 临时凭证 token
   * @param {string} [options.endpoint] - 自定义 endpoint（优先级高于 provider 预设）
   * @param {string} [options.bucket] - 默认 bucket
   * @param {boolean} [options.forcePathStyle=false] - 是否使用 path-style
   */
  constructor({
    provider = 'oss',
    region = '',
    accessKey = '',
    secretKey = '',
    sessionToken = null,
    endpoint = null,
    bucket = '',
    forcePathStyle = false,
  } = {}) {
    this._providerKey = provider;
    this._region = region;
    this._bucket = bucket;

    // 解析厂商配置
    this._providerConfig = getProvider(provider);

    // 确定 endpoint
    const effectiveEndpoint = endpoint
      || (this._providerConfig.endpointTemplate
        ? this._providerConfig.endpointTemplate.replace('{region}', region)
        : undefined);

    // 确定 addressing style
    const usePathStyle = forcePathStyle || this._providerConfig.addressingStyle === 'path';

    // 构建 S3 客户端
    const clientConfig = {
      region: region || 'us-east-1',
      credentials: {
        accessKeyId: accessKey,
        secretAccessKey: secretKey,
        ...(sessionToken ? { sessionToken } : {}),
      },
      forcePathStyle: usePathStyle,
      // 禁用 flexible checksums（阿里云 OSS 不兼容，会导致预签名 URL 带上 x-amz-checksum-mode=ENABLED 参数）
      requestChecksumCalculation: 'WHEN_REQUIRED',
      responseChecksumValidation: 'WHEN_REQUIRED',
    };

    if (effectiveEndpoint) {
      clientConfig.endpoint = effectiveEndpoint;
    }

    this._client = new S3Client(clientConfig);
    this._endpoint = effectiveEndpoint;
  }

  /** 获取底层 S3Client 实例 */
  get client() {
    return this._client;
  }

  /** 获取默认 bucket */
  get bucket() {
    return this._bucket;
  }

  /** 获取 region */
  get region() {
    return this._region;
  }

  /** 获取 provider key */
  get provider() {
    return this._providerKey;
  }

  /**
   * 从后端凭证 JSON 创建客户端（工厂方法）
   *
   * 后端 /CosServer/get_credential 返回的格式：
   * {
   *   credentials: { AccessKeyId, AccessKeySecret, SecurityToken },
   *   region: "cn-shenzhen",
   *   bucket: "my-bucket",
   *   endpoint: "https://oss-cn-shenzhen.aliyuncs.com",
   *   expiration_time: 1234567890
   * }
   *
   * @param {Object} credentialData - 后端返回的凭证对象
   * @param {string} [providerHint] - 厂商提示，不传则从 endpoint 自动推断
   * @returns {StorageClient}
   */
  static fromCredentials(credentialData, providerHint = null) {
    const { credentials, region, bucket, endpoint } = credentialData;

    // 自动推断 provider
    const provider = providerHint || detectProvider(endpoint) || 'oss';
    const providerConfig = getProvider(provider);
    const mapping = providerConfig.credentialMapping;

    return new StorageClient({
      provider,
      region,
      accessKey: credentials[mapping.accessKey] || '',
      secretKey: credentials[mapping.secretKey] || '',
      sessionToken: credentials[mapping.sessionToken] || null,
      endpoint,
      bucket,
    });
  }

  /**
   * 获取文件内容
   * @param {string} key - 对象键
   * @param {string} [bucketName] - bucket（不传则用默认）
   * @returns {Promise<Response>} - fetch Response 对象
   */
  async getObject(key, bucketName = null) {
    const command = new GetObjectCommand({
      Bucket: bucketName || this._bucket,
      Key: key,
    });
    const response = await this._client.send(command);
    return response;
  }

  /**
   * 获取文件为 Blob
   * @param {string} key
   * @param {string} [bucketName]
   * @returns {Promise<Blob>}
   */
  async getObjectAsBlob(key, bucketName = null) {
    const response = await this.getObject(key, bucketName);
    // SDK v3 返回的 Body 是 ReadableStream (浏览器) 或 Readable (Node)
    if (response.Body instanceof ReadableStream) {
      const reader = response.Body.getReader();
      const chunks = [];
      let done = false;
      while (!done) {
        const result = await reader.read();
        done = result.done;
        if (result.value) chunks.push(result.value);
      }
      return new Blob(chunks, {
        type: response.ContentType || 'application/octet-stream'
      });
    }
    // 如果已经是 Blob（某些 polyfill 环境）
    if (response.Body instanceof Blob) {
      return response.Body;
    }
    // 兜底：转 arrayBuffer
    const arrayBuffer = await response.Body.transformToByteArray();
    return new Blob([arrayBuffer], {
      type: response.ContentType || 'application/octet-stream'
    });
  }

  /**
   * 获取文件为指定类型
   * @param {string} key
   * @param {'blob'|'text'|'arraybuffer'} dataType
   * @param {string} [bucketName]
   * @returns {Promise<Blob|string|ArrayBuffer>}
   */
  async getObjectWithDataType(key, dataType = 'blob', bucketName = null) {
    const blob = await this.getObjectAsBlob(key, bucketName);
    switch (dataType) {
      case 'text':
        return blob.text();
      case 'arraybuffer':
        return blob.arrayBuffer();
      case 'blob':
      default:
        return blob;
    }
  }

  /**
   * 检查文件是否存在
   * @param {string} key
   * @param {string} [bucketName]
   * @returns {Promise<boolean>}
   */
  async headObject(key, bucketName = null) {
    try {
      const command = new HeadObjectCommand({
        Bucket: bucketName || this._bucket,
        Key: key,
      });
      await this._client.send(command);
      return true;
    } catch (err) {
      if (err.name === 'NotFound' || err.$metadata?.httpStatusCode === 404 || err.$metadata?.httpStatusCode === 403) {
        return false;
      }
      console.error(`headObject error for key=${key}:`, err);
      return false;
    }
  }

  /**
   * 生成预签名 URL
   * @param {string} key
   * @param {number} [expiresIn=900] - 有效期（秒）
   * @param {Object} [queryParams={}] - 额外查询参数（如 response-content-disposition）
   * @param {string} [bucketName]
   * @returns {Promise<string>}
   */
  async generatePresignedUrl(key, expiresIn = 900, queryParams = {}, bucketName = null) {
    const commandInput = {
      Bucket: bucketName || this._bucket,
      Key: key,
    };
    // 处理 response-content-disposition 等
    if (queryParams['response-content-disposition']) {
      commandInput.ResponseContentDisposition = queryParams['response-content-disposition'];
    }
    if (queryParams['response-content-type']) {
      commandInput.ResponseContentType = queryParams['response-content-type'];
    }

    const command = new GetObjectCommand(commandInput);
    const url = await getSignedUrl(this._client, command, { expiresIn });
    return url;
  }

  /**
   * 上传文件
   * @param {string} key
   * @param {Blob|File|ArrayBuffer|string} body
   * @param {Object} [options={}]
   * @param {string} [options.contentType]
   * @param {string} [options.bucket]
   * @returns {Promise<Object>}
   */
  async putObject(key, body, { contentType, bucket } = {}) {
    const command = new PutObjectCommand({
      Bucket: bucket || this._bucket,
      Key: key,
      Body: body,
      ...(contentType ? { ContentType: contentType } : {}),
    });
    return this._client.send(command);
  }

  // ======================== Node.js 文件操作（仅 Node.js 环境可用） ========================

  /**
   * 下载文件到本地路径（Node.js 环境）
   * @param {string} key - 对象键
   * @param {string} localPath - 本地保存路径
   * @param {string} [bucketName] - bucket
   * @returns {Promise<string>} 本地文件绝对路径
   */
  async downloadToFile(key, localPath, bucketName = null) {
    const fs = await import('fs');
    const path = await import('path');
    const { pipeline } = await import('stream/promises');

    // 确保目录存在
    const dir = path.default.dirname(path.default.resolve(localPath));
    fs.default.mkdirSync(dir, { recursive: true });

    const response = await this.getObject(key, bucketName);

    // SDK v3 在 Node.js 中返回的 Body 是 Readable stream
    const writeStream = fs.default.createWriteStream(localPath);
    await pipeline(response.Body, writeStream);

    const absPath = path.default.resolve(localPath);
    const stat = fs.default.statSync(absPath);
    return absPath;
  }

  /**
   * 从本地文件上传（Node.js 环境）
   *
   * 注意 OSS 兼容性（参考 memory:m0bvh35l）：
   * - 使用 putObject + Buffer（而非 stream），避免 chunked encoding
   * - 显式传 ContentLength
   *
   * @param {string} key - 对象键
   * @param {string} localPath - 本地文件路径
   * @param {Object} [options={}]
   * @param {string} [options.contentType] - MIME 类型
   * @param {string} [options.bucket] - bucket
   * @returns {Promise<Object>} 上传结果
   */
  async uploadFromFile(key, localPath, { contentType, bucket } = {}) {
    const fs = await import('fs');

    // 读取整个文件为 Buffer（OSS 兼容：避免 stream 导致 chunked encoding）
    const data = fs.default.readFileSync(localPath);
    const fileSize = data.length;

    const command = new PutObjectCommand({
      Bucket: bucket || this._bucket,
      Key: key,
      Body: data,
      ContentLength: fileSize,
      ...(contentType ? { ContentType: contentType } : {}),
    });
    return this._client.send(command);
  }

  /**
   * 列出前缀下所有对象
   * @param {string} prefix - 前缀
   * @param {string} [bucketName] - bucket
   * @param {number} [maxKeys=1000] - 每次请求最大数量
   * @returns {Promise<string[]>} 对象键列表
   */
  async listObjects(prefix, bucketName = null, maxKeys = 1000) {
    const allKeys = [];
    let continuationToken = undefined;

    while (true) {
      const command = new ListObjectsV2Command({
        Bucket: bucketName || this._bucket,
        Prefix: prefix,
        MaxKeys: maxKeys,
        ...(continuationToken ? { ContinuationToken: continuationToken } : {}),
      });

      const response = await this._client.send(command);
      const contents = response.Contents || [];

      for (const item of contents) {
        // 跳过目录标记（以 / 结尾的空对象）
        if (!item.Key.endsWith('/')) {
          allKeys.push(item.Key);
        }
      }

      if (response.IsTruncated) {
        continuationToken = response.NextContinuationToken;
      } else {
        break;
      }
    }

    return allKeys;
  }

  /**
   * 批量下载目录到本地（Node.js 环境）
   * @param {string} prefix - OSS 前缀
   * @param {string} localDir - 本地目录
   * @param {Object} [options={}]
   * @param {string} [options.bucket] - bucket
   * @param {boolean} [options.flat=false] - 是否扁平化（所有文件放同一目录）
   * @returns {Promise<{downloaded: string[], failed: string[]}>}
   */
  async downloadDirectory(prefix, localDir, { bucket, flat = false } = {}) {
    const path = await import('path');
    const keys = await this.listObjects(prefix, bucket);
    const downloaded = [];
    const failed = [];

    for (const key of keys) {
      try {
        const relativePath = flat
          ? path.default.basename(key)
          : (key.startsWith(prefix) ? key.substring(prefix.length) : key);
        const localPath = path.default.join(localDir, relativePath);
        await this.downloadToFile(key, localPath, bucket);
        downloaded.push(localPath);
      } catch (err) {
        console.error(`下载失败 ${key}: ${err.message}`);
        failed.push(key);
      }
    }

    return { downloaded, failed };
  }

  /**
   * 销毁客户端
   */
  destroy() {
    if (this._client) {
      this._client.destroy();
      this._client = null;
    }
  }
}
