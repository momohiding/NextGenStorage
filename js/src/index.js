/**
 * nextgen-storage (JS)
 *
 * 统一对象存储抽象层，对齐 Python 版 nextgen-oss。
 * 支持腾讯云 COS / 阿里云 OSS / AWS S3 / MinIO。
 *
 * 用法:
 *   import { StorageClient, detectProvider } from 'nextgen-storage';
 *
 *   const client = StorageClient.fromCredentials(credentialData);
 *   const blob = await client.getObjectAsBlob('path/to/file.png');
 */

export { default as StorageClient } from './StorageClient';
export {
  getProvider,
  registerProvider,
  detectProvider,
  PROVIDER_REGISTRY,
} from './providers';
