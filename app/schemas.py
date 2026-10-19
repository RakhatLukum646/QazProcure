from datetime import date
from typing import Literal
from pydantic import BaseModel, Field, ConfigDict, field_validator, model_validator


class AnalysisRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    intent: Literal['search','compare','summary','price_anomaly','fair_price','quantity_anomaly'] = 'search'
    language: Literal['ru','kk'] = 'ru'
    bins: list[str] = Field(default_factory=list, max_length=100)
    years: list[int] = Field(default_factory=lambda: [2024,2025,2026], max_length=10)
    enstru: str | None = Field(default=None, max_length=80)
    kato: str | None = Field(default=None, pattern=r'^\d{9}$')
    text: str | None = Field(default=None, max_length=200)
    lot_id: int | None = Field(default=None, gt=0)
    lot_number: str | None = Field(default=None, max_length=80)
    threshold_pct: float = Field(default=30, ge=0, le=10000)
    direction: Literal['above','below','both'] = 'both'
    top_k: int = Field(default=10, ge=1, le=50)
    group_by: Literal['year','customer_bin','enstru'] = 'year'
    date_from: date | None = None
    date_to: date | None = None

    @field_validator('bins')
    @classmethod
    def valid_bins(cls, values):
        if any(len(v) != 12 or not v.isdigit() for v in values):
            raise ValueError('BIN must contain exactly 12 digits')
        return list(dict.fromkeys(values))

    @field_validator('years')
    @classmethod
    def valid_years(cls, values):
        if not values or any(v < 2000 or v > 2100 for v in values):
            raise ValueError('Provide years from 2000 to 2100')
        return sorted(set(values))

    @model_validator(mode='after')
    def ordered_dates(self):
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError('date_from must not exceed date_to')
        return self


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    previous: AnalysisRequest | None = None
    filters: AnalysisRequest | None = None
