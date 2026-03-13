/**
 * 云厂商预设配置（对齐 Python 版 NextGenStorage/providers.py）
 *
 * 每家对象存储都兼容 S3 协议，差异只在 endpoint 格式和 STS 凭证字段名。
 * 通过 ProviderConfig 统一描述，StorageClient 根据 provider 名称自动选取。
 */

/**
 * @typedef {Object} ProviderConfig
 * @property {string} name - 厂商显示名
 * @property {string} endpointTemplate - endpoint 模板，{region} 会被替换
 * @property {string} addressingStyle - 'virtual' | 'path'
 * @property {{accessKey: string, secretKey: string, sessionToken: string}} credentialMapping
 *   STS 凭证字段映射：统一内部名 → 各厂商 STS 返回字段名
 * @property {string} signatureVersion - 签名版本
 */

const PROVIDER_REGISTRY = {
  cos: {
    name: 'Tencent Cloud COS',
    endpointTemplate: 'https://cos.{region}.myqcloud.com',
    addressingStyle: 'virtual',
    credentialMapping: {
      accessKey: 'tmpSecretId',
      secretKey: 'tmpSecretKey',
      sessionToken: 'sessionToken',
    },
    signatureVersion: 's3v4',
  },
  'cos-internal': {
    name: 'Tencent Cloud COS (Internal)',
    endpointTemplate: 'https://cos-internal.{region}.tencentcos.cn',
    addressingStyle: 'virtual',
    credentialMapping: {
      accessKey: 'tmpSecretId',
      secretKey: 'tmpSecretKey',
      sessionToken: 'sessionToken',
    },
    signatureVersion: 's3v4',
  },
  oss: {
    name: 'Alibaba Cloud OSS',
    endpointTemplate: 'https://oss-{region}.aliyuncs.com',
    addressingStyle: 'virtual',
    credentialMapping: {
      accessKey: 'AccessKeyId',
      secretKey: 'AccessKeySecret',
      sessionToken: 'SecurityToken',
    },
    signatureVersion: 's3v4',
  },
  s3: {
    name: 'AWS S3',
    endpointTemplate: '',
    addressingStyle: 'virtual',
    credentialMapping: {
      accessKey: 'AccessKeyId',
      secretKey: 'SecretAccessKey',
      sessionToken: 'SessionToken',
    },
    signatureVersion: 's3v4',
  },
  minio: {
    name: 'MinIO',
    endpointTemplate: 'http://{region}',
    addressingStyle: 'path',
    credentialMapping: {
      accessKey: 'accessKey',
      secretKey: 'secretKey',
      sessionToken: 'sessionToken',
    },
    signatureVersion: 's3v4',
  },
};

/**
 * 获取厂商配置
 * @param {string} key
 * @returns {ProviderConfig}
 */
export function getProvider(key) {
  const config = PROVIDER_REGISTRY[key];
  if (!config) {
    const available = Object.keys(PROVIDER_REGISTRY).join(', ');
    throw new Error(`Unknown provider '${key}', available: ${available}`);
  }
  return config;
}

/**
 * 注册自定义厂商配置
 * @param {string} key
 * @param {ProviderConfig} config
 */
export function registerProvider(key, config) {
  PROVIDER_REGISTRY[key] = config;
}

/**
 * 根据 endpoint URL 自动推断 provider
 * @param {string} endpoint
 * @returns {string|null}
 */
export function detectProvider(endpoint) {
  if (!endpoint) return null;
  if (endpoint.includes('aliyuncs.com')) return 'oss';
  if (endpoint.includes('myqcloud.com')) return 'cos';
  if (endpoint.includes('tencentcos.cn')) return 'cos-internal';
  if (endpoint.includes('amazonaws.com')) return 's3';
  return null;
}

export { PROVIDER_REGISTRY };
export default PROVIDER_REGISTRY;
