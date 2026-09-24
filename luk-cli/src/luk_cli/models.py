"""Pydantic models: the JSON contract (spec §5.4, `schema_version` 2) and stored-session shapes (§4.1).

Contract rules: every key is always present (null = unknown), so each contract model forbids
unknown keys and its serialization-mode JSON Schema marks every property required — `luk schema`
must use `model_json_schema(mode="serialization")`. Cookie values are `SecretStr` and leave memory
only through `StorageState.to_playwright()`.

Text from Luk is untrusted third-party data (§7.1): every contract string loses its C0 controls (tab
and newline kept), DEL and C1 controls on validation (`plain_text`), so an ESC/CSI/OSC sequence in an
offer title can never drive the user's terminal, a CSV or a JSON reader (CWE-150).
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Final, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, SecretStr, SerializeAsAny, field_validator, model_validator

from luk_cli.errors import ErrorCode, LukError

SCHEMA_VERSION: Final = 2  # 2: ListMeta gained `offset` and `next_offset` (the §5.1 cursor)

JobType = Literal["full_time", "part_time", "contractor", "intern", "per_diem", "other"]
Modality = Literal["on_site", "remote", "hybrid"]
PostedWithin = Literal["24h", "3d", "1w", "1m", "3m", "6m"]
CountryCode = Literal["CL", "CO", "MX", "PE", "BR"]
Currency = Literal["CLP", "COP", "PEN", "MXN", "BRL"]
AreaContext = Literal["jobs", "companies"]
JobStatus = Literal["open", "expired", "closed"]
Kind = Literal[
    "job_search", "job", "jobs", "suggestion_list", "area_list", "similar_roles", "related_roles",
    "company_list", "company", "job_list", "application_list", "cv_list", "whoami",
    "session_status", "account_status",
]


_CONTROL_RE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def plain_text(text: str) -> str:
    """`text` without terminal control characters: C0 except tab and newline, DEL, and C1 (e.g. ESC,
    BEL, CR, the 8-bit CSI \\x9b). The printable text around a sequence stays."""
    return _CONTROL_RE.sub("", text)


class ContractModel(BaseModel):
    """Base of every JSON-contract model; its strings never carry control characters (`plain_text`)."""

    model_config = ConfigDict(extra="forbid", json_schema_serialization_defaults_required=True)

    @field_validator("*", mode="before")
    @classmethod
    def _plain_text(cls, value: Any) -> Any:
        if isinstance(value, str):
            return plain_text(value)
        if isinstance(value, list):
            return [plain_text(item) if isinstance(item, str) else item for item in value]
        return value


class Salary(ContractModel):
    raw: str | None = None
    currency: str | None = None
    min: int | None = None
    max: int | None = None
    period: str | None = None


class JobCard(ContractModel):
    slug: str
    url: str
    title: str
    company: str | None = None
    location: str | None = None
    salary: Salary | None = None
    employment_type: JobType | None = None
    modality: Modality | None = None
    labels: list[str] = Field(default_factory=list)
    posted_ago: str | None = None
    posted_at_approx: date | None = None


class Address(ContractModel):
    locality: str | None = None
    region: str | None = None
    country: str | None = None


class JobPosting(JobCard):
    offer_id: int | None = None
    canonical_slug: str | None = None
    company_slug: str | None = None
    company_url: str | None = None
    address: Address | None = None
    work_hours: str | None = None
    vacancies: int | None = None
    date_posted: date | None = None
    valid_through: date | None = None
    status: JobStatus
    direct_apply: bool | None = None
    description_text: str
    requirements_text: str | None = None
    text_truncated: bool = False


class ListMeta(ContractModel):
    """A list read from `page` after skipping `offset` results. (`next_page`, `next_offset`) is the position
    right after the last result: calling again there continues without a skip or a repeat. Both are null
    exactly when `has_more` is false; a fully read page continues at (page + 1, 0)."""

    total: int | None = None
    page: int
    offset: int = 0
    last_fetched_page: int
    per_page: int | None = None
    last_page: int | None = None
    has_more: bool
    next_page: int | None = None
    next_offset: int | None = None


class JobList(ListMeta):
    results: list[JobCard] = Field(default_factory=list)
    details: list[JobPosting] | None = None
    not_found: list[str] = Field(default_factory=list)


class SearchFilters(ContractModel):
    job_types: list[JobType] = Field(default_factory=list)
    posted_within: PostedWithin | None = None
    min_salary: int | None = None
    max_salary: int | None = None
    currency: Currency | None = None


class SearchQuery(ContractModel):
    roles: list[str] = Field(default_factory=list)
    location_ids: list[int] = Field(default_factory=list)
    countries: list[CountryCode] = Field(default_factory=list)
    worldwide: bool = False
    filters: SearchFilters = Field(default_factory=SearchFilters)


class AreaRef(ContractModel):
    id: int
    display_path: str
    area_type_label: str | None = None


class JobSearch(JobList):
    query: SearchQuery
    effective_location: AreaRef | None = None
    location_alternatives: list[AreaRef] = Field(default_factory=list)
    related_roles: list[str] = Field(default_factory=list)


class Jobs(ContractModel):
    results: list[JobPosting] = Field(default_factory=list)
    not_found: list[str] = Field(default_factory=list)


class CompanyCard(ContractModel):
    slug: str
    url: str
    name: str
    location: str | None = None
    sector: str | None = None
    size: str | None = None
    active_offers: int | None = None
    offer_locations: list[str] = Field(default_factory=list)


class CompanyList(ListMeta):
    results: list[CompanyCard] = Field(default_factory=list)


class Company(ContractModel):
    slug: str
    url: str
    name: str
    location: str | None = None
    active_offers: int | None = None
    jobs: list[JobCard] = Field(default_factory=list)
    page: int
    has_more: bool
    next_page: int | None = None


class Area(ContractModel):
    id: int
    name: str
    display_path: str
    area_type: str | None = None
    area_type_label: str | None = None
    offer_count: int | None = None
    depth: int | None = None
    visitor_country_match: bool | None = None


class AreaList(ContractModel):
    query: str
    context: AreaContext
    results: list[Area] = Field(default_factory=list)


class SimilarRoles(ContractModel):
    role: str
    resolved_name: str | None = None
    items: list[str] = Field(default_factory=list)
    next_page: int | None = None


class Suggestion(ContractModel):
    query: str
    popularity: int | None = None


class SuggestionList(ContractModel):
    query: str
    results: list[Suggestion] = Field(default_factory=list)


class RelatedRoles(ContractModel):
    role: str
    resolved_name: str | None = None
    similar: list[str] = Field(default_factory=list)
    suggestions: list[Suggestion] = Field(default_factory=list)


class Application(ContractModel):
    """Provisional until the first capture (§8.1): raw text only, nothing guessed."""

    slug: str | None = None
    url: str | None = None
    title: str
    company: str | None = None
    applied_at_text: str | None = None
    status_text: str | None = None


class ApplicationList(ListMeta):
    results: list[Application] = Field(default_factory=list)


class Cv(ContractModel):
    """Provisional until the first capture (§8.1): metadata only, never the file."""

    name: str
    updated_at_text: str | None = None


class CvList(ContractModel):
    results: list[Cv] = Field(default_factory=list)


class WhoAmI(ContractModel):
    logged_in: bool
    name: str | None = None
    email: str | None = None


class SessionMeta(ContractModel):
    """meta.json (§4.1); tolerates keys written by newer versions."""

    model_config = ConfigDict(extra="ignore", json_schema_serialization_defaults_required=True)

    name: str | None = None
    email: str | None = None
    logged_in_at: datetime
    last_auth_ok_at: datetime | None = None
    last_validated_at: datetime | None = None
    browser: str
    luk_cli_version: str


class CookieInfo(ContractModel):
    """A stored cookie without its value; `expires` is 'session' or an ISO-8601 UTC timestamp."""

    name: str
    domain: str
    expires: str


class SessionStatus(ContractModel):
    path: str
    exists: bool
    age_seconds: int | None = None
    cookies: list[CookieInfo] = Field(default_factory=list)
    meta: SessionMeta | None = None
    expiry: Literal["server-side, unknown"] = "server-side, unknown"
    last_auth_ok_at: datetime | None = None
    last_validated_at: datetime | None = None
    valid: bool | None = None


class AccountStatus(ContractModel):
    """MCP `account_status` (§7.2): local files only, never cookie values."""

    session_present: bool
    valid: bool | None = None
    name: str | None = None
    email: str | None = None
    logged_in_at: datetime | None = None
    last_auth_ok_at: datetime | None = None
    private_tools_enabled: bool
    login_instructions: str


KIND_MODELS: dict[Kind, type[ContractModel]] = {
    "job_search": JobSearch,
    "job": JobPosting,
    "jobs": Jobs,
    "suggestion_list": SuggestionList,
    "area_list": AreaList,
    "similar_roles": SimilarRoles,
    "related_roles": RelatedRoles,
    "company_list": CompanyList,
    "company": Company,
    "job_list": JobList,
    "application_list": ApplicationList,
    "cv_list": CvList,
    "whoami": WhoAmI,
    "session_status": SessionStatus,
    "account_status": AccountStatus,
}

DataT = TypeVar("DataT", bound=ContractModel)


class Envelope(ContractModel, Generic[DataT]):
    """`{"schema_version": 2, "kind", "data", "warnings"}`; `data` must be exactly KIND_MODELS[kind]."""

    schema_version: Literal[2] = SCHEMA_VERSION
    kind: Kind
    data: SerializeAsAny[DataT]
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _data_matches_kind(self) -> Envelope[DataT]:
        expected = KIND_MODELS[self.kind]
        if type(self.data) is not expected:
            raise ValueError(f"kind {self.kind!r} needs {expected.__name__}, got {type(self.data).__name__}")
        return self


class ErrorInfo(ContractModel):
    code: ErrorCode
    exit_code: int
    message: str
    hint: str | None = None


class ErrorEnvelope(ContractModel):
    """`{"schema_version": 2, "kind": "error", "error": {code, exit_code, message, hint}}`."""

    schema_version: Literal[2] = SCHEMA_VERSION
    kind: Literal["error"] = "error"
    error: ErrorInfo

    @classmethod
    def from_error(cls, err: LukError) -> ErrorEnvelope:
        return cls(error=ErrorInfo(code=err.code, exit_code=err.exit_code, message=err.message, hint=err.hint))


class _Stored(BaseModel):
    """Playwright storage_state shapes; unknown keys are dropped on load, so never written back."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class StoredCookie(_Stored):
    name: str
    value: SecretStr
    domain: str
    path: str = "/"
    expires: float = -1  # -1 = browser-session cookie
    http_only: bool = Field(default=False, alias="httpOnly")
    secure: bool = False
    same_site: str = Field(default="Lax", alias="sameSite")

    @property
    def key(self) -> tuple[str, str, str]:
        """Identity for write-back merges: (name, domain, path)."""
        return (self.name, self.domain, self.path)


class LocalStorageItem(_Stored):
    name: str
    value: SecretStr


class StoredOrigin(_Stored):
    origin: str
    local_storage: list[LocalStorageItem] = Field(default_factory=list, alias="localStorage")


class StorageState(_Stored):
    cookies: list[StoredCookie] = Field(default_factory=list)
    origins: list[StoredOrigin] = Field(default_factory=list)

    def to_playwright(self) -> dict[str, Any]:
        """REVEALS secret values — only for writing session.json and seeding Playwright. Never log it."""
        return {
            "cookies": [
                {
                    "name": c.name,
                    "value": c.value.get_secret_value(),
                    "domain": c.domain,
                    "path": c.path,
                    "expires": c.expires,
                    "httpOnly": c.http_only,
                    "secure": c.secure,
                    "sameSite": c.same_site,
                }
                for c in self.cookies
            ],
            "origins": [
                {
                    "origin": o.origin,
                    "localStorage": [{"name": i.name, "value": i.value.get_secret_value()} for i in o.local_storage],
                }
                for o in self.origins
            ],
        }
