"""请求体的 Pydantic 模型与校验规则。"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator, model_validator


def _norm_decimal(v):
    # JSON 数字经 float 解析会引入二进制误差，统一经 str 转为精确 Decimal
    if isinstance(v, float):
        return Decimal(str(v))
    return v


class TierIn(BaseModel):
    """阶梯：threshold 为该档起始累计销量（册，从 0 起），rate 为费率（小数，如 0.08 表示 8%）。"""

    threshold: int = Field(ge=0)
    rate: Decimal = Field(ge=0)

    @field_validator("rate", mode="before")
    @classmethod
    def _rate(cls, v):
        return _norm_decimal(v)


class AuthorIn(BaseModel):
    author_id: str = Field(min_length=1, max_length=128)
    ratio: Decimal = Field(ge=0, le=100, description="分成比例（百分数，全体合计须为 100）")

    @field_validator("ratio", mode="before")
    @classmethod
    def _ratio(cls, v):
        return _norm_decimal(v)


class WorkIn(BaseModel):
    work_id: str = Field(min_length=1, max_length=128)
    title: str | None = None
    tiers: list[TierIn] = Field(min_length=1)
    authors: list[AuthorIn] = Field(min_length=1)

    @model_validator(mode="after")
    def _check(self):
        thresholds = [t.threshold for t in self.tiers]
        if thresholds[0] != 0:
            raise ValueError("首个阶梯门槛必须为 0")
        if any(b <= a for a, b in zip(thresholds, thresholds[1:])):
            raise ValueError("阶梯销量门槛必须严格递增")
        ids = [a.author_id for a in self.authors]
        if len(set(ids)) != len(ids):
            raise ValueError("作者编号重复")
        if sum(a.ratio for a in self.authors) != Decimal(100):
            raise ValueError("作者分成比例合计必须为 100")
        return self


class SplitChangeIn(BaseModel):
    """作者分成变更：为作品提交一个新分成版本（版本按提交顺序生效）。

    - change_id 全局唯一：同号同内容重试返回原版本，同号异内容拒绝（409）；
    - 作者编号不得重复，比例非负且合计必须为 100。
    """

    change_id: str = Field(min_length=1, max_length=128)
    authors: list[AuthorIn] = Field(min_length=1)

    @model_validator(mode="after")
    def _check(self):
        ids = [a.author_id for a in self.authors]
        if len(set(ids)) != len(ids):
            raise ValueError("作者编号重复")
        if sum(a.ratio for a in self.authors) != Decimal(100):
            raise ValueError("作者分成比例合计必须为 100")
        return self


class SaleIn(BaseModel):
    sale_id: str = Field(min_length=1, max_length=128)
    work_id: str = Field(min_length=1, max_length=128)
    business_time: datetime
    copies: int = Field(gt=0)
    net_price: Decimal = Field(ge=0, description="每册净价（元）")

    @field_validator("net_price", mode="before")
    @classmethod
    def _price(cls, v):
        return _norm_decimal(v)


class RefundIn(BaseModel):
    refund_id: str = Field(min_length=1, max_length=128)
    sale_id: str = Field(min_length=1, max_length=128)
    copies: int = Field(gt=0)


class PriceCorrectionIn(BaseModel):
    """销售净价更正：凭全局唯一更正编号、原销售编号与非负新每册净价提交。

    - 仅对各档未退册数，按原销售阶梯费率与入账时分成版本计算新旧净价的每册版税差额；
    - 同更正编号同内容重试返回原结果，异内容拒绝（409，更正编号全局唯一）；
    - 销售不存在 404、已全退 400、新价等于当前更正价 409。
    """

    correction_id: str = Field(min_length=1, max_length=128)
    sale_id: str = Field(min_length=1, max_length=128)
    new_net_price: Decimal = Field(ge=0, description="更正后每册净价（元，非负）")

    @field_validator("new_net_price", mode="before")
    @classmethod
    def _price(cls, v):
        return _norm_decimal(v)


class CloseIn(BaseModel):
    """关账请求。request_id 可选：携带时作为幂等键，同键重试返回原报表。"""

    request_id: str | None = Field(default=None, min_length=1, max_length=128)


class PaymentIn(BaseModel):
    """作者付款结算请求：按当前最新关账批次结算可付额。

    - close_id 必须是当前最新关账批次，否则拒绝（409）；
    - amount_cents 为支付金额（分），必须为正且不超过可付额。
    """

    author_id: str = Field(min_length=1, max_length=128)
    close_id: int = Field(gt=0)
    payment_id: str = Field(min_length=1, max_length=128)
    amount_cents: int = Field(gt=0)
