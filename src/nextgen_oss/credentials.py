"""
STS 临时凭证签发抽象层

各云厂商都提供 STS (Security Token Service) 来签发临时凭证，
但 API 各不相同。这里统一为 STSProvider 接口。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional


@dataclass
class TemporaryCredentials:
    """统一的临时凭证结构"""
    access_key: str
    secret_key: str
    session_token: str
    expiration: float  # Unix timestamp
    region: str = ""
    bucket: str = ""
    raw_response: Optional[Dict[str, Any]] = None

    @property
    def is_expired(self) -> bool:
        import time
        return time.time() >= self.expiration

    def remaining_seconds(self) -> float:
        import time
        return max(0, self.expiration - time.time())


class STSProvider(ABC):
    """STS 临时凭证签发器基类"""

    @abstractmethod
    def get_credential(
        self,
        actions: List[str],
        resources: List[str],
        duration_seconds: int = 1800,
        **kwargs,
    ) -> TemporaryCredentials:
        """
        签发临时凭证

        :param actions: 允许的操作列表（各厂商格式不同，由子类适配）
        :param resources: 允许访问的资源路径
        :param duration_seconds: 有效期（秒）
        :return: 统一的临时凭证
        """
        ...


class CosSTSProvider(STSProvider):
    """
    腾讯云 COS STS 签发

    依赖: pip install qcloud-python-sts (可选依赖 nextgen-oss[cos-sts])
    """

    def __init__(self, secret_id: str, secret_key: str, region: str, bucket: str):
        self._secret_id = secret_id
        self._secret_key = secret_key
        self._region = region
        self._bucket = bucket

    def get_credential(
        self,
        actions: List[str],
        resources: List[str],
        duration_seconds: int = 1800,
        **kwargs,
    ) -> TemporaryCredentials:
        try:
            from sts.sts import Sts, Scope
        except ImportError:
            raise ImportError(
                "qcloud-python-sts is required for COS STS. "
                "Install with: pip install nextgen-oss[cos-sts]"
            )

        scopes = []
        for action in actions:
            for res in resources:
                scopes.append(Scope(action, self._bucket, self._region, res))

        config = {
            "sts_scheme": "https",
            "sts_url": "sts.tencentcloudapi.com/",
            "duration_seconds": duration_seconds,
            "secret_id": self._secret_id,
            "secret_key": self._secret_key,
            "region": self._region,
            "policy": Sts.get_policy(scopes),
        }

        sts = Sts(config)
        response = sts.get_credential()

        cred = response.get("credentials", {})
        return TemporaryCredentials(
            access_key=cred.get("tmpSecretId", ""),
            secret_key=cred.get("tmpSecretKey", ""),
            session_token=cred.get("sessionToken", ""),
            expiration=float(response.get("expiredTime", 0)),
            region=self._region,
            bucket=self._bucket,
            raw_response=dict(response),
        )


class AwsSTSProvider(STSProvider):
    """
    AWS STS 签发

    使用 boto3 内置的 STS 客户端，无需额外依赖。
    """

    def __init__(
        self,
        access_key: str,
        secret_key: str,
        region: str,
        role_arn: str,
    ):
        self._access_key = access_key
        self._secret_key = secret_key
        self._region = region
        self._role_arn = role_arn

    def get_credential(
        self,
        actions: List[str],
        resources: List[str],
        duration_seconds: int = 1800,
        **kwargs,
    ) -> TemporaryCredentials:
        import json
        import boto3

        session_name = kwargs.get("session_name", "nextgen-oss-session")

        # 构建 AWS IAM Policy
        policy = json.dumps({
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Action": actions,
                "Resource": resources,
            }],
        })

        sts_client = boto3.client(
            "sts",
            aws_access_key_id=self._access_key,
            aws_secret_access_key=self._secret_key,
            region_name=self._region,
        )

        response = sts_client.assume_role(
            RoleArn=self._role_arn,
            RoleSessionName=session_name,
            DurationSeconds=duration_seconds,
            Policy=policy,
        )

        cred = response["Credentials"]
        # AWS 返回的 Expiration 是 datetime 对象
        exp = cred["Expiration"]
        if isinstance(exp, datetime):
            exp_ts = exp.timestamp()
        else:
            exp_ts = float(exp)

        return TemporaryCredentials(
            access_key=cred["AccessKeyId"],
            secret_key=cred["SecretAccessKey"],
            session_token=cred["SessionToken"],
            expiration=exp_ts,
            region=self._region,
            raw_response=response,
        )


class OssSTSProvider(STSProvider):
    """
    阿里云 OSS STS 签发

    依赖: pip install nextgen-oss[oss-sts]
    """

    def __init__(
        self,
        access_key: str,
        secret_key: str,
        region: str,
        role_arn: str,
        endpoint: str = "sts.aliyuncs.com",
    ):
        self._access_key = access_key
        self._secret_key = secret_key
        self._region = region
        self._role_arn = role_arn
        self._endpoint = endpoint

    def get_credential(
        self,
        actions: List[str],
        resources: List[str],
        duration_seconds: int = 1800,
        **kwargs,
    ) -> TemporaryCredentials:
        try:
            from alibabacloud_sts20150401.client import Client as StsClient
            from alibabacloud_sts20150401.models import AssumeRoleRequest
            from alibabacloud_tea_openapi.models import Config as OpenApiConfig
        except ImportError:
            raise ImportError(
                "alibabacloud-sts20150401 is required for OSS STS. "
                "Install with: pip install nextgen-oss[oss-sts]"
            )

        import json

        session_name = kwargs.get("session_name", "nextgen-oss-session")

        policy = json.dumps({
            "Version": "1",
            "Statement": [{
                "Effect": "Allow",
                "Action": actions,
                "Resource": resources,
            }],
        })

        config = OpenApiConfig(
            access_key_id=self._access_key,
            access_key_secret=self._secret_key,
            endpoint=self._endpoint,
        )
        sts_client = StsClient(config)

        request = AssumeRoleRequest(
            role_arn=self._role_arn,
            role_session_name=session_name,
            duration_seconds=duration_seconds,
            policy=policy,
        )
        response = sts_client.assume_role(request)
        cred = response.body.credentials

        return TemporaryCredentials(
            access_key=cred.access_key_id,
            secret_key=cred.access_key_secret,
            session_token=cred.security_token,
            expiration=float(
                datetime.fromisoformat(
                    cred.expiration.replace("Z", "+00:00")
                ).timestamp()
            ),
            region=self._region,
            raw_response=response.body.to_map(),
        )


class CachedSTSProvider:
    """
    带缓存和自动刷新的 STS 凭证管理器

    包装任意 STSProvider，自动缓存凭证并在过期前提前刷新。
    支持将凭证注入环境变量（用于 CosNodes 等子进程读取）。

    用法:
        provider = CosSTSProvider(secret_id, secret_key, region, bucket)
        cached = CachedSTSProvider(provider, refresh_before=300)

        # 获取凭证（自动缓存，过期前 300 秒自动刷新）
        cred = cached.get_credential(actions, resources, duration_seconds=3600)

        # 注入环境变量（兼容旧版 CosNodes）
        cached.inject_env(cred, prefix="cos")
    """

    def __init__(
        self,
        provider: STSProvider,
        refresh_before: int = 300,
    ):
        """
        :param provider: 底层 STS 签发器
        :param refresh_before: 提前刷新秒数（凭证剩余时间 < 此值时自动刷新）
        """
        self._provider = provider
        self._refresh_before = refresh_before
        self._cached: Optional[TemporaryCredentials] = None
        self._cache_key: Optional[str] = None
        import threading
        self._lock = threading.Lock()

    def _make_cache_key(self, actions: List[str], resources: List[str], duration_seconds: int) -> str:
        """生成缓存 key（相同参数复用同一凭证）"""
        import hashlib
        raw = f"{sorted(actions)}:{sorted(resources)}:{duration_seconds}"
        return hashlib.md5(raw.encode()).hexdigest()

    def get_credential(
        self,
        actions: List[str],
        resources: List[str],
        duration_seconds: int = 1800,
        **kwargs,
    ) -> TemporaryCredentials:
        """
        获取凭证（带缓存）

        如果缓存中有相同参数且未过期（含提前刷新窗口）的凭证，直接返回；
        否则重新签发并缓存。
        """
        cache_key = self._make_cache_key(actions, resources, duration_seconds)

        with self._lock:
            if (
                self._cached is not None
                and self._cache_key == cache_key
                and self._cached.remaining_seconds() > self._refresh_before
            ):
                return self._cached

            # 签发新凭证
            cred = self._provider.get_credential(
                actions, resources, duration_seconds, **kwargs
            )
            self._cached = cred
            self._cache_key = cache_key
            return cred

    @property
    def cached_credential(self) -> Optional[TemporaryCredentials]:
        """获取当前缓存的凭证（可能为 None 或已过期）"""
        return self._cached

    def invalidate(self):
        """强制清除缓存"""
        with self._lock:
            self._cached = None
            self._cache_key = None

    @staticmethod
    def inject_env(
        cred: TemporaryCredentials,
        prefix: str = "cos",
        extra: Optional[Dict[str, str]] = None,
    ):
        """
        将凭证注入环境变量

        旧版格式 (prefix="cos")：
            cos_secretId, cos_secretKey, cos_sessionToken, cos_region, cos_bucket

        标准格式 (prefix="NEXTGEN_OSS")：
            NEXTGEN_OSS_ACCESS_KEY, NEXTGEN_OSS_SECRET_KEY, NEXTGEN_OSS_SESSION_TOKEN,
            NEXTGEN_OSS_REGION, NEXTGEN_OSS_BUCKET

        :param cred: 临时凭证
        :param prefix: 环境变量前缀
        :param extra: 额外注入的环境变量
        """
        import os

        if prefix.lower() == "cos":
            # 旧版兼容格式
            os.environ["cos_secretId"] = cred.access_key
            os.environ["cos_secretKey"] = cred.secret_key
            os.environ["cos_sessionToken"] = cred.session_token
            os.environ["cos_region"] = cred.region
            if cred.bucket:
                os.environ["cos_bucket"] = cred.bucket
        else:
            os.environ[f"{prefix}_ACCESS_KEY"] = cred.access_key
            os.environ[f"{prefix}_SECRET_KEY"] = cred.secret_key
            os.environ[f"{prefix}_SESSION_TOKEN"] = cred.session_token
            os.environ[f"{prefix}_REGION"] = cred.region
            if cred.bucket:
                os.environ[f"{prefix}_BUCKET"] = cred.bucket

        if extra:
            for k, v in extra.items():
                os.environ[k] = v
