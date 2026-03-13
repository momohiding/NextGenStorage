/**
 * 统一对象存储客户端（JS 版）
 *
 * 对齐 Python 版 NextGenStorage/client.py 的设计思路：
 * 基于 @aws-sdk/client-s3 的 S3 兼容协议，通过 endpoint 切换不同云厂商。
 * 腾讯云 COS / 阿里云 OSS / AWS S3 / MinIO 等均可使用同一套接口。
 */

import { S3Client, GetObjectCommand, HeadObjectCommand, PutObjectCommand } from '@aws-sdk/client-s3';
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
